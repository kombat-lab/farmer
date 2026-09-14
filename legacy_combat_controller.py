from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Hashable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from automation_policy import LegacyCombatPolicy, TargetPolicy
from battle_records import BattleOutcome, RecordBattleResult, SourceEventId
from combat import (
    CombatAction,
    CombatActionKind,
    CombatEventKind,
    CombatObservation,
    CombatStatus,
)
from combat_round import CombatRoundState, same_combatant_name
from combat_rules import CombatRuleset, CombatTurnInput, LegacyCombatRuleset
from combat_strategy import (
    CombatDecisionTrace,
    CombatMemory,
    RecentCombatKnowledge,
    SkillTarget,
    resolve_decision_trace,
)
from event_cache import BoundedKeyCache
from fog_input import semantic_fog_text
from game_input import ActionOutcome, InboundEvent
from human_delays import parse_remaining_seconds
from json_types import JsonValue
from legacy_battle_rewards import reward_bundle_from_reward
from message_snapshot import MessageSnapshot
from models import MessageKind
from parser import classify_message, extract_combat_target, normalize
from rewards import BattleReward, parse_battle_reward
from targeting import select_combat_target
from telegram_buttons import find_button


class LegacyCombatContext(Protocol):
    """Only shared health and encounter identity; no discovery or map state."""

    current_hp: int | None
    max_hp: int | None
    active_target: str | None
    battle_target: str | None
    combat_enemies: list[str]

    def add_combat_enemy(self, name: str) -> None: ...
    def remove_combat_enemy(self, name: str) -> None: ...
    def clear_combat(self) -> None: ...


class LegacyCombatRuntime(Protocol):
    @property
    def context(self) -> LegacyCombatContext: ...

    @property
    def running(self) -> bool: ...

    @property
    def session_id(self) -> int | None: ...

    def legacy_combat_policy(self) -> LegacyCombatPolicy: ...
    def target_policy(self) -> TargetPolicy: ...
    def now(self) -> datetime: ...
    def battle_position(self) -> tuple[int, int] | None: ...
    def source_event_id(self, snapshot: MessageSnapshot) -> SourceEventId: ...
    def is_current(self, event: InboundEvent) -> bool: ...
    def action_cooldown_remaining(self) -> float: ...
    def enter_combat(self) -> None: ...
    def interrupt_discovery(self) -> None: ...
    def log(self, text: str) -> None: ...
    def mark_progress(self, reason: str) -> None: ...
    def record_session_victory(self, battle_id: int, reward: BattleReward) -> bool: ...
    def record_session_defeat(self, battle_id: int) -> bool: ...
    def report_session(self) -> None: ...

    async def load_combat_knowledge(self, *, namespace: str) -> Mapping[int, object]: ...
    async def save_combat_knowledge(
        self, profile_max_hp: int, payload: Mapping[str, JsonValue], *, namespace: str
    ) -> None: ...
    async def add_treatment_enemy_target(self, target_name: str) -> bool: ...
    async def remove_treatment_enemy_target(self, target_name: str) -> bool: ...
    async def record_combat_event(self, event_type: str, message: str) -> None: ...
    async def record_battle(
        self, outcome: BattleOutcome, decisions: tuple[CombatDecisionTrace, ...]
    ) -> RecordBattleResult: ...
    async def begin_death_recovery(self, target_name: str) -> None: ...
    async def recover_latest_state(self, reason: str) -> bool: ...
    async def execute_combat_action(
        self, observation: CombatObservation, action: CombatAction
    ) -> ActionOutcome:
        """Apply timing/cooldown and verify snapshot freshness again before RPC."""
        ...


@dataclass(frozen=True, slots=True)
class LegacyCombatObservation:
    kind: CombatEventKind
    event: InboundEvent
    observed_at: datetime
    round_state: CombatRoundState | None
    legacy_kind: MessageKind
    authoritative: bool

    @property
    def snapshot(self) -> MessageSnapshot:
        return self.event.snapshot

    def __post_init__(self) -> None:
        if type(self.authoritative) is not bool:
            raise ValueError("Признак авторитетности должен быть bool")
        if not isinstance(self.kind, CombatEventKind) or not isinstance(self.event, InboundEvent):
            raise ValueError("Нужны CombatEventKind и immutable InboundEvent")
        if not isinstance(self.legacy_kind, MessageKind):
            raise ValueError("Нужен распознанный legacy MessageKind")
        if self.round_state is not None and not isinstance(self.round_state, CombatRoundState):
            raise ValueError("Нужен CombatRoundState")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("Время боевого наблюдения должно содержать часовой пояс")


@dataclass(frozen=True, slots=True)
class LegacyActionClaim:
    epoch: int
    phase: CombatActionKind
    round_number: int | None
    unknown_turn: int


@dataclass(frozen=True, slots=True)
class LegacyBattleArchive:
    start_message_id: int
    end_message_id: int
    source_message_ids: frozenset[int]
    target_name: str
    decisions: tuple[CombatDecisionTrace, ...]
    position: tuple[int, int] | None


class LegacyCombatController:
    """The current combat rules, memory and orchestration behind one replaceable port."""

    def __init__(
        self,
        runtime: LegacyCombatRuntime,
        *,
        character_name: str,
        cache_size: int = 500,
        ruleset: CombatRuleset | None = None,
    ) -> None:
        self.runtime = runtime
        self.character_name = character_name
        self._rules: CombatRuleset = ruleset if ruleset is not None else LegacyCombatRuleset()
        self.memory = CombatMemory()
        self.combat_decisions: list[CombatDecisionTrace] = []
        self.pending_combat_decision: CombatDecisionTrace | None = None
        self.combat_knowledge_profiles: dict[int, RecentCombatKnowledge] = {}
        self.active_combat_profile_max_hp: int | None = None
        self._observed_facts: BoundedKeyCache[Hashable] = BoundedKeyCache(cache_size)
        self._completed_battles: BoundedKeyCache[tuple[int, SourceEventId]] = BoundedKeyCache(
            cache_size
        )
        self._completed_source_events: BoundedKeyCache[SourceEventId] = BoundedKeyCache(
            cache_size
        )
        self._claims: BoundedKeyCache[LegacyActionClaim] = BoundedKeyCache(cache_size)
        self._pending_event: InboundEvent | None = None
        self._battle_epoch = 0
        self._battle_position: tuple[int, int] | None = None
        self._battle_start_id = 0
        self._highest_sequence = 0
        self._active_message_ids: set[int] = set()
        self._closed = False
        self._highest_round: int | None = None
        self._unknown_turn = 0
        self._archives: deque[LegacyBattleArchive] = deque(maxlen=cache_size)
        self._lock = asyncio.Lock()
        self._active = False
        self._initialized = False
        self._targets = runtime.target_policy()
        self._policy = runtime.legacy_combat_policy()
        for target in self._policy.treatment_enemies:
            self.memory.confirm_treatment_enemy(target)

    def status(self) -> CombatStatus:
        return CombatStatus(
            active=self._active,
            target_name=self.memory.target_name or self.runtime.context.active_target,
            enemy_names=tuple(self.runtime.context.combat_enemies),
            pending_action_label=self.memory.pending_skill,
            observations=len(self.memory.round_history),
        )

    def _new_combat_knowledge(self) -> RecentCombatKnowledge:
        knowledge = RecentCombatKnowledge()
        for target in self._policy.treatment_enemies:
            knowledge.confirm_treatment_enemy(target)
        return knowledge

    async def initialize(self) -> None:
        self._targets = self.runtime.target_policy()
        if not self._initialized:
            stored = await self.runtime.load_combat_knowledge(
                namespace=self._rules.knowledge_namespace
            )
            for max_hp, payload in stored.items():
                self.combat_knowledge_profiles[max_hp] = RecentCombatKnowledge.from_payload(payload)
            if stored:
                self.runtime.log(
                    f"Загружена долговременная боевая память: профилей персонажа — {len(stored)}."
                )
            self._initialized = True
        for target in self._policy.treatment_enemies:
            self.memory.confirm_treatment_enemy(target)
            for knowledge in self.combat_knowledge_profiles.values():
                knowledge.confirm_treatment_enemy(target)

    async def persist(self) -> None:
        max_hp = self.active_combat_profile_max_hp
        if max_hp is None:
            return
        payload: dict[str, JsonValue] = deepcopy(self.memory.knowledge.as_payload())
        await self.runtime.save_combat_knowledge(
            max_hp, payload, namespace=self._rules.knowledge_namespace
        )

    def reset(self) -> None:
        self.runtime.context.clear_combat()
        self.memory.reset()
        self.combat_decisions.clear()
        self.pending_combat_decision = None
        self._pending_event = None
        self._active = False
        self._closed = True

    def observe_message(self, event: InboundEvent) -> LegacyCombatObservation | None:
        snapshot = event.snapshot
        targets = self.runtime.target_policy()
        legacy_kind = classify_message(
            snapshot.raw_text, targets.enabled, self.character_name, is_map=False
        )
        kind = {
            MessageKind.COMBAT_STARTED: CombatEventKind.STARTED,
            MessageKind.PLAYER_TURN: CombatEventKind.TURN,
            MessageKind.COMBAT_TARGET_SELECTION: CombatEventKind.TARGET_SELECTION,
            MessageKind.BATTLE_FINISHED: CombatEventKind.FINISHED,
            MessageKind.BATTLE_INVITE: CombatEventKind.INVITATION,
        }.get(legacy_kind)
        if legacy_kind in {
            MessageKind.MOVE_STARTED,
            MessageKind.TARGET_SELECTION,
            MessageKind.TARGET_GONE,
        }:
            return None
        round_state = self._rules.parse_round(snapshot)
        if kind is None:
            if round_state is None:
                return None
            kind = CombatEventKind.UPDATE
        authoritative, _ = self._classify_causality(event, kind, round_state)
        return LegacyCombatObservation(
            kind, event, self.runtime.now(), round_state, legacy_kind, authoritative
        )

    @staticmethod
    def _cursor(event: InboundEvent) -> int:
        """Return the only causal clock after an input has passed admission.

        Telegram message ids and edit timestamps are source provenance. Accepted
        sequence decides local order: a larger value is newer, equality is an
        exact retry, and a smaller value can never mutate the current epoch.
        """

        return event.sequence

    def _archive_for_message(self, message_id: int) -> LegacyBattleArchive | None:
        return next(
            (
                battle
                for battle in reversed(self._archives)
                if message_id in battle.source_message_ids
                or battle.start_message_id <= message_id <= battle.end_message_id
            ),
            None,
        )

    def _classify_causality(
        self, event: InboundEvent, kind: CombatEventKind, parsed: CombatRoundState | None
    ) -> tuple[bool, bool]:
        cursor = self._cursor(event)
        number = parsed.number if parsed is not None else None
        reinforcement = "на помощь врагу присоединился" in normalize(event.snapshot.raw_text)
        explicit_start = kind is CombatEventKind.STARTED and not reinforcement
        newer = cursor > self._highest_sequence
        new_epoch = (self._battle_epoch == 0 and newer) or (
            newer
            and (
                explicit_start
                or (
                    self._closed
                    and kind in {CombatEventKind.TURN, CombatEventKind.TARGET_SELECTION}
                )
            )
        )
        archived = (
            None
            if new_epoch or explicit_start or event.snapshot.id in self._active_message_ids
            else self._archive_for_message(event.snapshot.id)
        )
        authoritative = (
            cursor >= self._highest_sequence
            and archived is None
            and (
                new_epoch
                or number is None
                or self._highest_round is None
                or number >= self._highest_round
            )
        )
        if kind is CombatEventKind.FINISHED and self._closed:
            authoritative = False
        return authoritative, new_epoch

    def _fact_key(self, observation: LegacyCombatObservation) -> Hashable:
        snapshot = observation.snapshot
        if observation.kind is CombatEventKind.STARTED:
            return (
                self._battle_epoch,
                "start",
                snapshot.id,
                normalize(extract_combat_target(snapshot.raw_text) or ""),
                "на помощь врагу присоединился" in normalize(snapshot.raw_text),
            )
        parsed = observation.round_state
        if parsed is not None:
            facts = replace(parsed, available_skills=(), remaining_seconds=None)
            return (
                self._battle_epoch,
                parsed.number if parsed.number is not None else snapshot.id,
                facts,
            )
        return snapshot.id, semantic_fog_text(snapshot.raw_text)

    def _can_act(self, observation: CombatObservation) -> bool:
        return self.runtime.running and self.runtime.is_current(observation.event)

    def _claim_key(
        self, phase: CombatActionKind, observation: LegacyCombatObservation
    ) -> LegacyActionClaim:
        parsed = observation.round_state
        number = parsed.number if parsed is not None else None
        if number is None and self.pending_combat_decision is not None:
            number = self.pending_combat_decision.round_number
        if number is None:
            number = self._highest_round
        return LegacyActionClaim(
            self._battle_epoch, phase, number, self._unknown_turn if number is None else 0
        )

    def _can_confirm(self, observation: LegacyCombatObservation) -> bool:
        source = self._pending_event
        pending = self.pending_combat_decision
        if source is None or pending is None:
            return False
        newer = self._cursor(observation.event) > self._cursor(source)
        parsed = observation.round_state
        return newer and (
            parsed is None
            or parsed.number is None
            or pending.round_number is None
            or parsed.number >= pending.round_number
        )

    def _begin_epoch(self, observation: LegacyCombatObservation) -> None:
        if self._battle_start_id:
            if not self._closed:
                next_message_id = observation.snapshot.id
                known_ids = frozenset(self._active_message_ids)
                end_message_id = (
                    next_message_id - 1
                    if next_message_id > self._battle_start_id
                    else max(known_ids, default=self._battle_start_id)
                )
                self._archives.append(
                    LegacyBattleArchive(
                        self._battle_start_id,
                        end_message_id,
                        known_ids,
                        self.resolved_battle_target(),
                        deepcopy(tuple(self.combat_decisions)),
                        self._battle_position,
                    )
                )
            self.reset()
        self._battle_epoch += 1
        self._battle_position = self.runtime.battle_position()
        self._battle_start_id = observation.snapshot.id
        self._highest_sequence = self._cursor(observation.event)
        self._active_message_ids = {observation.snapshot.id}
        self._closed = False
        self._highest_round = None
        self._unknown_turn = 0

    async def handle_message(self, observation: CombatObservation) -> bool:
        if not isinstance(observation, LegacyCombatObservation):
            return False
        async with self._lock:
            snapshot = observation.snapshot
            source_event_id = self.runtime.source_event_id(snapshot)
            finished_key = (self._battle_epoch, source_event_id)
            if observation.kind is CombatEventKind.FINISHED and (
                finished_key in self._completed_battles
                or source_event_id in self._completed_source_events
            ):
                return True
            self._targets = self.runtime.target_policy()
            self._policy = self.runtime.legacy_combat_policy()
            number = observation.round_state.number if observation.round_state else None
            causal, new_epoch = self._classify_causality(
                observation.event, observation.kind, observation.round_state
            )
            if observation.kind is CombatEventKind.FINISHED:
                await self._record_battle_result(observation, current=causal)
                return True
            if not causal:
                # Old packets cannot replace the encounter or resolve its pending action.
                return True
            if new_epoch:
                self._begin_epoch(observation)
            self._highest_sequence = max(
                self._highest_sequence,
                self._cursor(observation.event),
            )
            self._active_message_ids.add(snapshot.id)
            if number is not None:
                self._highest_round = number
            fact_key = self._fact_key(observation)
            if fact_key not in self._observed_facts:
                await self._observe_combat_message(
                    snapshot.raw_text,
                    observation.legacy_kind,
                    observation.round_state,
                    can_confirm=self._can_confirm(observation),
                    allow_lifecycle=self._can_act(observation),
                )
                self._observed_facts.remember(fact_key)
            elif self._can_act(observation) and observation.round_state is not None:
                self.memory.latest_round = observation.round_state
            if not self._can_act(observation):
                return True
            if observation.kind is CombatEventKind.TURN:
                if self._claim_key(CombatActionKind.ACTION, observation) not in self._claims:
                    await self._handle_turn(observation)
            elif observation.kind is CombatEventKind.TARGET_SELECTION:
                if self._claim_key(CombatActionKind.TARGET, observation) not in self._claims:
                    await self._handle_target_selection(observation)
            elif observation.kind is CombatEventKind.INVITATION:
                self.runtime.log("Приглашение в бой проигнорировано.")
            return True

    def activate_combat_profile(self, max_hp: int | None) -> None:
        if max_hp is None or max_hp <= 0:
            return
        if self.active_combat_profile_max_hp == max_hp:
            return

        knowledge = self.combat_knowledge_profiles.setdefault(
            max_hp,
            self._new_combat_knowledge(),
        )
        self.memory.knowledge = knowledge
        self.active_combat_profile_max_hp = max_hp
        if self.memory.target_name:
            knowledge.load_into(self.memory)
        self.runtime.log(f"Активирован боевой профиль для максимума HP {max_hp}.")

    def canonical_combat_enemy(self, raw_name: str) -> str:
        """Resolve decorated combat text to a configured monster name."""
        normalized = normalize(raw_name)
        candidates = [
            *self._targets.enabled,
            *self.runtime.context.combat_enemies,
        ]
        if self.runtime.context.active_target:
            candidates.append(self.runtime.context.active_target)
        exact = next(
            (candidate for candidate in candidates if normalize(candidate) == normalized), None
        )
        if exact is not None:
            return exact
        matches = [
            candidate
            for candidate in candidates
            if re.search(r"(?<!\w)" + re.escape(normalize(candidate)) + r"(?!\w)", normalized)
        ]
        if matches:
            return max(matches, key=lambda name: len(normalize(name)))
        return raw_name.strip()

    def observed_combat_enemies(
        self,
        round_state: CombatRoundState | None,
        *,
        excluding: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Infer enemies from the already received round without Telegram I/O."""
        if round_state is None:
            return ()

        excluded = {normalize(name) for name in excluding}
        raw_names = [
            combatant.name
            for combatant in round_state.combatants
            if not same_combatant_name(self.character_name, combatant.name)
        ]
        raw_names.extend(
            attack.actor
            for attack in round_state.attacks
            if not same_combatant_name(self.character_name, attack.actor)
        )
        raw_names.extend(round_state.near_death)

        result: list[str] = []
        seen: set[str] = set()
        for raw_name in raw_names:
            enemy = self.canonical_combat_enemy(raw_name)
            normalized = normalize(enemy)
            if not normalized or normalized in excluded or normalized in seen:
                continue
            seen.add(normalized)
            result.append(enemy)
        return tuple(result)

    def switch_combat_enemy(self, enemy: str, *, reason: str) -> bool:
        if normalize(self.memory.target_name or "") == normalize(enemy):
            return False
        previous = self.memory.target_name
        self.runtime.context.active_target = enemy
        self.runtime.context.add_combat_enemy(enemy)
        self.memory.begin(enemy)
        self.pending_combat_decision = None
        self.runtime.log(
            f"Боевая модель переключена: {previous or 'неопределённый моб'} → {enemy}; {reason}."
        )
        return True

    def resolved_battle_target(self) -> str:
        candidates = [
            self.runtime.context.battle_target,
            self.memory.target_name,
            self.runtime.context.active_target,
            *self.runtime.context.combat_enemies,
        ]
        for candidate in candidates:
            if candidate and normalize(candidate) not in {"неизвестная цель", "unknown target"}:
                return candidate
        return "неопределённый моб"

    async def _observe_combat_message(
        self,
        text: str,
        kind: MessageKind,
        round_state: CombatRoundState | None,
        *,
        can_confirm: bool,
        allow_lifecycle: bool,
    ) -> None:
        if kind is MessageKind.COMBAT_STARTED:
            self.activate_combat_profile(self.runtime.context.max_hp)
        elif self.memory.target_name and self.active_combat_profile_max_hp is None:
            self.activate_combat_profile(self.runtime.context.max_hp)

        if kind is MessageKind.COMBAT_STARTED:
            observed_target = extract_combat_target(text)
            normalized_text = normalize(text)
            if "на помощь врагу присоединился" not in normalized_text:
                self.combat_decisions.clear()
                self.pending_combat_decision = None
                self.memory.begin(observed_target, text)
            elif self.memory.target_name is None:
                self.memory.target_name = observed_target

        defeated_enemies = round_state.defeated if round_state is not None else ()
        current_target_was_defeated = any(
            normalize(self.memory.target_name or "") == normalize(defeated)
            for defeated in defeated_enemies
        )
        if not current_target_was_defeated:
            observed_enemies = self.observed_combat_enemies(round_state)
            if observed_enemies and not any(
                normalize(self.memory.target_name or "") == normalize(enemy)
                for enemy in observed_enemies
            ):
                self.switch_combat_enemy(
                    observed_enemies[0],
                    reason="противник распознан в полученном раунде",
                )
        if can_confirm and self.pending_combat_decision is not None and round_state is not None:
            player_skills = [
                skill
                for skill in round_state.skill_uses
                if same_combatant_name(self.character_name, skill.actor)
            ]
            failed_player_skills = [
                skill
                for skill in round_state.failed_skill_uses
                if same_combatant_name(self.character_name, skill.actor)
            ]
            if player_skills:
                confirmed_skill = player_skills[-1].skill
                expected_skill = self.pending_combat_decision.decision.skill_name
                if normalize(expected_skill) == normalize(confirmed_skill):
                    resolved_trace = resolve_decision_trace(
                        self.pending_combat_decision,
                        round_state,
                        self.character_name,
                    )
                    if normalize(expected_skill) == "лечение":
                        planned_target = resolved_trace.decision.target
                        actual_target = resolved_trace.actual_target
                        actual_description = (
                            actual_target.value if actual_target is not None else "unknown"
                        )
                        self.runtime.log(
                            "Результат Лечения: "
                            f"план={planned_target.value}, факт={actual_description}, "
                            f"эффект={resolved_trace.actual_effect or 'не распознан'}, "
                            f"значение={resolved_trace.actual_amount or 0}."
                        )
                        target_name = resolved_trace.target_name
                        if actual_target is SkillTarget.ENEMY:
                            self.memory.confirm_treatment_enemy(target_name)
                            await self.runtime.add_treatment_enemy_target(target_name)
                        elif (
                            planned_target is SkillTarget.ENEMY
                            and actual_target is SkillTarget.SELF
                        ):
                            self.memory.revoke_treatment_enemy(target_name)
                            removed = await self.runtime.remove_treatment_enemy_target(target_name)
                            if removed:
                                await self.runtime.record_combat_event(
                                    "TREATMENT_ENEMY_REVOKED",
                                    f"Лечение недоступно как атака для «{target_name}»",
                                )
                    self.combat_decisions.append(resolved_trace)
                else:
                    self.runtime.log(
                        "Игра подтвердила другой навык: "
                        f"ожидался «{expected_skill}», применён «{confirmed_skill}». "
                        "Решение исключено из статистики."
                    )
                self.pending_combat_decision = None
                self._pending_event = None
                self._unknown_turn += 1
            elif failed_player_skills:
                failed = failed_player_skills[-1]
                self.runtime.log(
                    f"Навык «{failed.skill}» не применён: {failed.reason}. "
                    "Решение исключено из статистики."
                )
                self.pending_combat_decision = None
        self.memory.observe(text, self.character_name, round_state)

        for defeated_enemy in defeated_enemies:
            self.runtime.context.remove_combat_enemy(defeated_enemy)
            self.runtime.log(f"Противник повержен: {defeated_enemy}")
            if normalize(self.memory.target_name or "") == normalize(defeated_enemy):
                next_enemies = self.observed_combat_enemies(
                    round_state,
                    excluding=defeated_enemies,
                )
                if not next_enemies:
                    next_enemies = tuple(self.runtime.context.combat_enemies)
                if next_enemies:
                    self.switch_combat_enemy(
                        next_enemies[0],
                        reason="в этом же бою остался следующий противник",
                    )
                else:
                    self.memory.reset()

        if kind is MessageKind.COMBAT_STARTED and allow_lifecycle:
            combat_target = extract_combat_target(text)
            normalized_text = normalize(text)

            if "на вас напали:" in normalized_text:
                self.runtime.interrupt_discovery()

                if combat_target:
                    self.runtime.context.active_target = combat_target
                    self.runtime.context.add_combat_enemy(combat_target)
                self.runtime.log(
                    "Обнаружено внезапное нападение"
                    + (f": {combat_target}" if combat_target else "")
                )
                self.runtime.mark_progress("внезапное нападение")
            elif "на помощь врагу присоединился" in normalized_text:
                if combat_target:
                    self.runtime.context.add_combat_enemy(combat_target)
                self.runtime.log(
                    "К бою присоединился дополнительный моб"
                    + (f": {combat_target}" if combat_target else "")
                )
                self.runtime.mark_progress("к врагу присоединилось подкрепление")
            else:
                if combat_target:
                    self.runtime.context.active_target = combat_target
                    self.runtime.context.add_combat_enemy(combat_target)
                self.runtime.mark_progress("бой начался")

            self.runtime.enter_combat()
            self._active = True
            return

    async def _handle_turn(self, observation: LegacyCombatObservation) -> None:
        self.runtime.enter_combat()
        self._active = True
        self.runtime.mark_progress("ход игрока")
        if self.pending_combat_decision is not None:
            self.runtime.log(
                "Предыдущее боевое решение не подтверждено сообщением игры; "
                "оно не попадёт в статистику."
            )
            self.pending_combat_decision = None

        snapshot = observation.snapshot
        plan = self._rules.plan_turn(
            CombatTurnInput(
                message=snapshot,
                message_id=snapshot.id,
                created_at=observation.observed_at,
                memory=self.memory,
                current_hp=self.runtime.context.current_hp,
                max_hp=self.runtime.context.max_hp,
                heal_threshold=self._policy.heal_threshold,
                planner_mode=self._policy.planner_mode,
                round_state=self.memory.latest_round,
            )
        )
        round_state = plan.round_state
        current_mana = round_state.current_mana if round_state is not None else None
        self.runtime.log(
            f"Выбор навыка: мана={current_mana if current_mana is not None else 'не распознана'}"
        )
        decision = plan.decision
        if decision is None:
            await self.runtime.recover_latest_state("не найден доступный навык")
            return
        if plan.shadow_plan is not None:
            self.runtime.log(plan.shadow_plan.format_log())
        decision_trace = plan.trace
        assert decision_trace is not None
        skill_name = decision.skill_name
        self.memory.pending_skill = skill_name
        self.memory.pending_target = decision.target
        self.memory.pending_urgent = decision.urgent
        self.runtime.log(decision_trace.format_log())
        position = find_button(snapshot, contains=(skill_name,), exclude=("CD:",))
        outcome = ActionOutcome.REJECTED
        claim = self._claim_key(CombatActionKind.ACTION, observation)
        if position is not None and self._can_act(observation):
            # An exception after an RPC may hide delivery. Reserve before awaiting
            # the runtime and release only when it proves no attempt was committed.
            self._claims.remember(claim)
            self.pending_combat_decision = decision_trace
            self._pending_event = observation.event
            outcome = await self.runtime.execute_combat_action(
                observation,
                CombatAction(
                    kind=CombatActionKind.ACTION,
                    position=position,
                    label=skill_name,
                    urgent=decision.urgent,
                    remaining_seconds=(
                        round_state.remaining_seconds
                        if round_state is not None
                        else parse_remaining_seconds(snapshot.raw_text)
                    ),
                ),
            )
        if outcome in (ActionOutcome.SENT, ActionOutcome.DELIVERY_UNKNOWN, ActionOutcome.DUPLICATE):
            self.pending_combat_decision = decision_trace
            self._pending_event = observation.event
            self._claims.remember(self._claim_key(CombatActionKind.ACTION, observation))
            self.runtime.mark_progress(f"попытка применения навыка {skill_name}")
        else:
            self._claims.discard(claim)
            self.pending_combat_decision = None
            self._pending_event = None
            if outcome is not ActionOutcome.DEFERRED:
                self.memory.pending_skill = None
                self.memory.pending_target = None
                self.memory.pending_urgent = False

    async def _handle_target_selection(self, observation: LegacyCombatObservation) -> None:
        self.runtime.enter_combat()
        self._active = True
        self.runtime.mark_progress("получен список целей навыка")
        if self.runtime.action_cooldown_remaining() > 0:
            self.runtime.log("Выбор боевой цели отложен до завершения Telegram-паузы.")
            return

        snapshot = observation.snapshot
        if normalize(self.memory.pending_skill or "") == "лечение":
            enemy_name, enemy_position = select_combat_target(
                snapshot,
                self._targets.enabled,
                self.runtime.context.active_target,
                preferred_target="enemy",
                character_name=self.character_name,
            )
            if enemy_position is not None:
                confirmed_target = (
                    self.memory.target_name or self.runtime.context.active_target or enemy_name
                )
                self.memory.confirm_treatment_enemy(confirmed_target)
                if confirmed_target and await self.runtime.add_treatment_enemy_target(
                    confirmed_target
                ):
                    self.runtime.log(
                        f"Подтверждено атакующее Лечение для цели «{confirmed_target}»."
                    )
                    await self.runtime.record_combat_event(
                        "TREATMENT_ENEMY_CONFIRMED",
                        f"Лечение может наносить урон цели «{confirmed_target}»",
                    )

        target_name, position = select_combat_target(
            snapshot,
            self._targets.enabled,
            self.runtime.context.active_target,
            preferred_target=(
                "self" if self.memory.pending_target is SkillTarget.SELF else "enemy"
            ),
            character_name=self.character_name,
        )
        if position is None:
            await self.runtime.recover_latest_state("не найдена доступная цель навыка")
            return
        if not self._can_act(observation):
            return
        claim = self._claim_key(CombatActionKind.TARGET, observation)
        self._claims.remember(claim)
        outcome = await self.runtime.execute_combat_action(
            observation,
            CombatAction(
                kind=CombatActionKind.TARGET,
                position=position,
                label=f"боевая цель {target_name}",
                urgent=self.memory.pending_urgent,
                remaining_seconds=parse_remaining_seconds(snapshot.raw_text),
            ),
        )
        if outcome in (ActionOutcome.SENT, ActionOutcome.DELIVERY_UNKNOWN, ActionOutcome.DUPLICATE):
            self._claims.remember(self._claim_key(CombatActionKind.TARGET, observation))
            if outcome is ActionOutcome.SENT:
                self.runtime.mark_progress("цель навыка выбрана")
        else:
            self._claims.discard(claim)
        if outcome is ActionOutcome.REJECTED and self.runtime.running:
            self.memory.pending_skill = None
            self.memory.pending_target = None
            self.memory.pending_urgent = False
            self.pending_combat_decision = None
            self._pending_event = None
            await self.runtime.recover_latest_state("не удалось выбрать цель навыка")

    async def _record_battle_result(
        self, observation: LegacyCombatObservation, *, current: bool
    ) -> None:
        snapshot = observation.snapshot
        text = snapshot.raw_text
        if "Победа" not in text and "Поражение" not in text:
            return
        victory = "Победа" in text
        archive = self._archive_for_message(snapshot.id)
        target_name = (
            self.resolved_battle_target()
            if current
            else archive.target_name
            if archive
            else "неопределённый моб"
        )
        decisions = (
            tuple(self.combat_decisions) if current else archive.decisions if archive else ()
        )
        reward = parse_battle_reward(text) if victory else BattleReward(dust=0, xp=0, items=())
        outcome = BattleOutcome(
            source_event_id=self.runtime.source_event_id(snapshot),
            source_message_id=snapshot.id,
            session_id=self.runtime.session_id,
            target_name=target_name,
            result="VICTORY" if victory else "DEFEAT",
            rewards=reward_bundle_from_reward(reward),
            position=(
                (self._battle_position if self._battle_epoch else self.runtime.battle_position())
                if current
                else archive.position
                if archive
                else None
            ),
            happened_at=observation.observed_at,
        )
        recorded = await self.runtime.record_battle(outcome, deepcopy(decisions))
        added = False
        if recorded.inserted:
            added = (
                self.runtime.record_session_victory(recorded.battle_id, reward)
                if victory
                else self.runtime.record_session_defeat(recorded.battle_id)
            )
        if victory and added:
            self.runtime.report_session()
        if current:
            self._active_message_ids.add(snapshot.id)
            self._highest_sequence = max(
                self._highest_sequence,
                self._cursor(observation.event),
            )
            known_ids = frozenset(self._active_message_ids)
            self._archives.append(
                LegacyBattleArchive(
                    self._battle_start_id or snapshot.id,
                    max(known_ids, default=snapshot.id),
                    known_ids,
                    target_name,
                    deepcopy(decisions),
                    outcome.position,
                )
            )
            self.reset()
            if victory:
                self.runtime.mark_progress("бой завершён победой")
            elif recorded.inserted:
                self.runtime.interrupt_discovery()
                await self.runtime.begin_death_recovery(target_name)
            elif self._can_act(observation):
                await self.runtime.recover_latest_state("повторный исход поражения")
        source_event_id = self.runtime.source_event_id(snapshot)
        self._completed_battles.remember((self._battle_epoch, source_event_id))
        self._completed_source_events.remember(source_event_id)
        if current:
            try:
                await self.persist()
            except Exception as error:
                self.runtime.log(
                    "Не удалось сохранить необязательную боевую память; "
                    f"повтор будет выполнен позже ({type(error).__name__})."
                )
