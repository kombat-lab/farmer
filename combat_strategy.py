from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from combat_round import CombatRoundState, parse_combat_round, parse_starting_health
from parser import normalize
from skills import HEALING_MANA_RESERVE, SkillButton, available_skills, parse_current_mana

KNOWN_SKILLS = {
    "лечение",
    "обновление",
    "святое свечение",
    "атака аколита",
}

PERIODIC_EFFECT_MARKERS = (
    "яд",
    "горение",
    "кровотечение",
    "раскаленное ядро",
)


def is_periodic_effect(name: str | None) -> bool:
    normalized = normalize(name or "")
    normalized = normalized.removesuffix("[добивание]").strip()
    return any(marker in normalized for marker in PERIODIC_EFFECT_MARKERS)


class SkillTarget(Enum):
    SELF = "self"
    ENEMY = "enemy"


@dataclass(frozen=True)
class CombatDecision:
    skill_name: str
    target: SkillTarget
    reason: str
    urgent: bool = False


@dataclass(frozen=True)
class SkillAvailability:
    name: str
    mana_cost: int
    cooldown: int
    castable: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mana_cost": self.mana_cost,
            "cooldown": self.cooldown,
            "castable": self.castable,
        }


@dataclass(frozen=True)
class DamageEstimate:
    skill_name: str
    minimum: int | None
    maximum: int | None
    average: float | None
    samples: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "average": self.average,
            "samples": self.samples,
        }


@dataclass(frozen=True)
class CombatDecisionTrace:
    created_at: str
    telegram_message_id: int
    target_name: str
    round_number: int | None
    player_current_hp: int | None
    player_max_hp: int | None
    enemy_current_hp: int | None
    enemy_max_hp: int | None
    current_mana: int | None
    maximum_mana: int | None
    renewal_turns: int
    renewal_tick: int | None
    periodic_damage: int
    periodic_damage_turns: int
    incoming_minimum: int | None
    incoming_maximum: int | None
    incoming_average: float | None
    incoming_samples: int
    critical_incoming_minimum: int | None
    critical_incoming_maximum: int | None
    critical_incoming_average: float | None
    critical_incoming_samples: int
    critical_incoming_rate: float
    expected_next_hit: int | None
    worst_next_hit: int | None
    sustainable_damage_per_turn: int | None
    skills: tuple[SkillAvailability, ...]
    outgoing_damage: tuple[DamageEstimate, ...]
    decision: CombatDecision
    actual_target: SkillTarget | None = None
    actual_effect: str | None = None
    actual_amount: int | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "model_version": 3,
            "created_at": self.created_at,
            "telegram_message_id": self.telegram_message_id,
            "target_name": self.target_name,
            "round_number": self.round_number,
            "player": {
                "current_hp": self.player_current_hp,
                "max_hp": self.player_max_hp,
            },
            "enemy": {
                "current_hp": self.enemy_current_hp,
                "max_hp": self.enemy_max_hp,
            },
            "mana": {"current": self.current_mana, "maximum": self.maximum_mana},
            "renewal": {"turns": self.renewal_turns, "tick": self.renewal_tick},
            "periodic_damage": {
                "amount": self.periodic_damage,
                "turns": self.periodic_damage_turns,
            },
            "incoming_damage": {
                "minimum": self.incoming_minimum,
                "maximum": self.incoming_maximum,
                "average": self.incoming_average,
                "samples": self.incoming_samples,
                "expected_next_hit": self.expected_next_hit,
                "worst_next_hit": self.worst_next_hit,
                "critical": {
                    "minimum": self.critical_incoming_minimum,
                    "maximum": self.critical_incoming_maximum,
                    "average": self.critical_incoming_average,
                    "samples": self.critical_incoming_samples,
                    "rate": self.critical_incoming_rate,
                },
            },
            "sustainable_damage_per_turn": self.sustainable_damage_per_turn,
            "skills": [skill.as_payload() for skill in self.skills],
            "outgoing_damage": [estimate.as_payload() for estimate in self.outgoing_damage],
            "decision": {
                "skill_name": self.decision.skill_name,
                "target": self.decision.target.value,
                "reason": self.decision.reason,
                "urgent": self.decision.urgent,
            },
            "outcome": {
                "target": self.actual_target.value if self.actual_target else None,
                "effect": self.actual_effect,
                "amount": self.actual_amount,
            },
        }

    def format_log(self) -> str:
        def value(number: int | float | None) -> str:
            if number is None:
                return "?"
            if isinstance(number, float):
                return f"{number:.1f}"
            return str(number)

        skills = ", ".join(
            f"{skill.name}[мана {skill.mana_cost}; CD {skill.cooldown}; "
            f"{'доступен' if skill.castable else 'недоступен'}]"
            for skill in self.skills
        )
        incoming = (
            f"наблюдения={self.incoming_samples}, "
            f"min/сред/max={value(self.incoming_minimum)}/"
            f"{value(self.incoming_average)}/{value(self.incoming_maximum)}, "
            f"прогноз={value(self.expected_next_hit)}/{value(self.worst_next_hit)}"
        )
        critical = (
            f"криты={self.critical_incoming_samples} "
            f"({self.critical_incoming_rate:.0%}), "
            f"min/сред/max={value(self.critical_incoming_minimum)}/"
            f"{value(self.critical_incoming_average)}/"
            f"{value(self.critical_incoming_maximum)}"
        )
        return (
            "[COMBAT_PLAN]\n"
            f"  Моб: {self.target_name}; раунд: {value(self.round_number)}\n"
            f"  Состояние: HP {value(self.player_current_hp)}/{value(self.player_max_hp)}; "
            f"враг {value(self.enemy_current_hp)}/{value(self.enemy_max_hp)}; "
            f"мана {value(self.current_mana)}/{value(self.maximum_mana)}\n"
            f"  Эффекты: Обновление {self.renewal_turns} ход. × "
            f"{value(self.renewal_tick)} HP; периодический урон "
            f"{self.periodic_damage} × {self.periodic_damage_turns} ход.\n"
            f"  Входящий урон: {incoming}; {critical}\n"
            f"  Устойчивый темп урона: "
            f"{value(self.sustainable_damage_per_turn)} HP/ход\n"
            f"  Навыки: {skills or 'не распознаны'}\n"
            f"  Решение: {self.decision.skill_name} → {self.decision.target.value}; "
            f"причина: {self.decision.reason}"
        )


@dataclass
class ObservedRange:
    minimum: int | None = None
    maximum: int | None = None
    samples: int = 0
    total: int = 0

    def add(self, value: int) -> None:
        if value <= 0:
            return
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.samples += 1
        self.total += value

    def average(self, fallback: float) -> float:
        return self.total / self.samples if self.samples else fallback


@dataclass
class RecentCombatKnowledge:
    """Short in-memory history, isolated per monster and current farmer run."""

    sample_limit: int = 12
    incoming: dict[str, list[int]] = field(default_factory=dict)
    critical_incoming: dict[str, list[int]] = field(default_factory=dict)
    outgoing: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    skill_cooldowns: dict[str, int] = field(default_factory=dict)
    direct_healing: list[int] = field(default_factory=list)
    renewal_healing: list[int] = field(default_factory=list)
    treatment_enemy_targets: set[str] = field(default_factory=set)

    @staticmethod
    def _sample_list(value: object, limit: int) -> list[int]:
        if not isinstance(value, list):
            return []
        samples = [int(item) for item in value if isinstance(item, int) and item > 0]
        return samples[-limit:]

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sample_limit": self.sample_limit,
            "incoming": self.incoming,
            "critical_incoming": self.critical_incoming,
            "outgoing": self.outgoing,
            "skill_cooldowns": self.skill_cooldowns,
            "direct_healing": self.direct_healing,
            "renewal_healing": self.renewal_healing,
            "treatment_enemy_targets": sorted(self.treatment_enemy_targets),
        }

    @classmethod
    def from_payload(cls, payload: object) -> RecentCombatKnowledge:
        if not isinstance(payload, dict):
            return cls()
        raw_limit = payload.get("sample_limit", 12)
        sample_limit = (
            max(1, min(100, raw_limit)) if isinstance(raw_limit, int) else 12
        )
        knowledge = cls(sample_limit=sample_limit)

        for field_name in ("incoming", "critical_incoming"):
            raw_mapping = payload.get(field_name)
            if not isinstance(raw_mapping, dict):
                continue
            parsed = {
                normalize(str(target)): cls._sample_list(values, sample_limit)
                for target, values in raw_mapping.items()
            }
            setattr(knowledge, field_name, {key: value for key, value in parsed.items() if value})

        raw_outgoing = payload.get("outgoing")
        if isinstance(raw_outgoing, dict):
            for target, raw_skills in raw_outgoing.items():
                if not isinstance(raw_skills, dict):
                    continue
                skills = {
                    normalize(str(skill)): cls._sample_list(values, sample_limit)
                    for skill, values in raw_skills.items()
                }
                skills = {key: value for key, value in skills.items() if value}
                if skills:
                    knowledge.outgoing[normalize(str(target))] = skills

        raw_cooldowns = payload.get("skill_cooldowns")
        if isinstance(raw_cooldowns, dict):
            knowledge.skill_cooldowns = {
                normalize(str(name)): max(0, int(value))
                for name, value in raw_cooldowns.items()
                if isinstance(value, int)
            }
        knowledge.direct_healing = cls._sample_list(
            payload.get("direct_healing"), sample_limit
        )
        knowledge.renewal_healing = cls._sample_list(
            payload.get("renewal_healing"), sample_limit
        )
        raw_targets = payload.get("treatment_enemy_targets")
        if isinstance(raw_targets, list):
            knowledge.treatment_enemy_targets = {
                normalize(str(target)) for target in raw_targets if str(target).strip()
            }
        return knowledge

    def _append(self, samples: list[int], value: int) -> None:
        if value <= 0:
            return
        samples.append(value)
        del samples[: max(0, len(samples) - self.sample_limit)]

    def add_incoming(
        self,
        target_name: str | None,
        value: int,
        *,
        critical: bool = False,
    ) -> None:
        target = normalize(target_name or "")
        if target:
            collection = self.critical_incoming if critical else self.incoming
            self._append(collection.setdefault(target, []), value)

    def observe_cooldown(self, skill_name: str, cooldown: int) -> None:
        normalized = normalize(skill_name)
        if normalized:
            self.skill_cooldowns[normalized] = max(
                cooldown,
                self.skill_cooldowns.get(normalized, 0),
            )

    def add_outgoing(self, target_name: str | None, skill_name: str, value: int) -> None:
        target = normalize(target_name or "")
        if not target:
            return
        skills = self.outgoing.setdefault(target, {})
        self._append(skills.setdefault(skill_name, []), value)

    def add_direct_healing(self, value: int) -> None:
        self._append(self.direct_healing, value)

    def add_renewal_healing(self, value: int) -> None:
        self._append(self.renewal_healing, value)

    def confirm_treatment_enemy(self, target_name: str | None) -> None:
        target = normalize(target_name or "")
        if target:
            self.treatment_enemy_targets.add(target)

    def revoke_treatment_enemy(self, target_name: str | None) -> None:
        target = normalize(target_name or "")
        self.treatment_enemy_targets.discard(target)
        target_skills = self.outgoing.get(target)
        if target_skills is not None:
            target_skills.pop("лечение", None)

    def treatment_can_target_enemy(self, target_name: str | None) -> bool:
        target = normalize(target_name or "")
        return bool(target and target in self.treatment_enemy_targets)

    def load_into(self, memory: CombatMemory) -> None:
        target = normalize(memory.target_name or "")
        for value in self.incoming.get(target, []):
            memory.incoming_damage.add(value)
        for value in self.critical_incoming.get(target, []):
            memory.critical_incoming_damage.add(value)
        for skill_name, values in self.outgoing.get(target, {}).items():
            observed = memory.outgoing_damage.setdefault(skill_name, ObservedRange())
            for value in values:
                observed.add(value)
        for value in self.direct_healing:
            memory.direct_healing.add(value)
        for value in self.renewal_healing:
            memory.renewal_healing.add(value)
        memory.skill_cooldowns.update(self.skill_cooldowns)


@dataclass
class CombatMemory:
    target_name: str | None = None
    enemy_current_hp: int | None = None
    enemy_max_hp: int | None = None
    renewal_turns: int = 0
    periodic_damage: int = 0
    periodic_damage_turns: int = 0
    incoming_damage: ObservedRange = field(default_factory=ObservedRange)
    critical_incoming_damage: ObservedRange = field(default_factory=ObservedRange)
    outgoing_damage: dict[str, ObservedRange] = field(default_factory=dict)
    skill_cooldowns: dict[str, int] = field(default_factory=dict)
    direct_healing: ObservedRange = field(default_factory=ObservedRange)
    renewal_healing: ObservedRange = field(default_factory=ObservedRange)
    pending_skill: str | None = None
    pending_target: SkillTarget | None = None
    pending_urgent: bool = False
    knowledge: RecentCombatKnowledge = field(default_factory=RecentCombatKnowledge)
    latest_round: CombatRoundState | None = None
    round_history: list[CombatRoundState] = field(default_factory=list)
    last_battle_rounds: tuple[CombatRoundState, ...] = ()

    def reset(self) -> None:
        if self.round_history:
            self.last_battle_rounds = tuple(self.round_history)
        self.target_name = None
        self.enemy_current_hp = None
        self.enemy_max_hp = None
        self.renewal_turns = 0
        self.periodic_damage = 0
        self.periodic_damage_turns = 0
        self.incoming_damage = ObservedRange()
        self.critical_incoming_damage = ObservedRange()
        self.outgoing_damage.clear()
        self.skill_cooldowns.clear()
        self.direct_healing = ObservedRange()
        self.renewal_healing = ObservedRange()
        self.pending_skill = None
        self.pending_target = None
        self.pending_urgent = False
        self.latest_round = None
        self.round_history.clear()

    def begin(self, target_name: str | None, text: str = "") -> None:
        self.reset()
        self.target_name = target_name
        self.knowledge.load_into(self)
        if target_name:
            starting_hp = parse_starting_health(text)
            if starting_hp:
                self.enemy_current_hp, self.enemy_max_hp = starting_hp

    def observe(
        self,
        text: str,
        character_name: str,
        round_state: CombatRoundState | None = None,
    ) -> None:
        parsed = round_state or parse_combat_round(text)
        if parsed is None:
            return

        previous_enemy_hp = self.enemy_current_hp
        self.latest_round = parsed
        self.round_history.append(parsed)
        character = normalize(character_name)
        used_skills = [
            normalize(skill.skill)
            for skill in parsed.skill_uses
            if character in normalize(skill.actor) and normalize(skill.skill) in KNOWN_SKILLS
        ]
        failed_player_skills = [
            normalize(skill.skill)
            for skill in parsed.failed_skill_uses
            if character in normalize(skill.actor)
        ]
        player_skill = (
            used_skills[-1]
            if used_skills
            else None
            if failed_player_skills
            else self.pending_skill
        )

        for skill in parsed.available_skills:
            skill_name = normalize(skill.name)
            self.skill_cooldowns[skill_name] = max(
                skill.cooldown,
                self.skill_cooldowns.get(skill_name, 0),
            )
            self.knowledge.observe_cooldown(skill_name, skill.cooldown)

        for damage_event in parsed.damage:
            recipient = normalize(damage_event.target)
            effect = normalize(damage_event.effect or "")
            if character in recipient:
                if is_periodic_effect(effect):
                    self.periodic_damage = damage_event.amount
                elif damage_event.critical:
                    self.critical_incoming_damage.add(damage_event.amount)
                    self.knowledge.add_incoming(
                        self.target_name,
                        damage_event.amount,
                        critical=True,
                    )
                else:
                    self.incoming_damage.add(damage_event.amount)
                    self.knowledge.add_incoming(self.target_name, damage_event.amount)
            elif (
                player_skill is not None
                and not is_periodic_effect(effect)
                and not damage_event.critical
            ):
                # The game reports only the remaining HP on a finishing hit.
                # Such a value is a censored lower bound, not the real skill
                # damage, and must not reduce the learned damage floor.
                target_is_current_enemy = bool(
                    self.target_name
                    and normalize(self.target_name) in recipient
                )
                finishing_hit_is_capped = bool(
                    target_is_current_enemy
                    and previous_enemy_hp is not None
                    and damage_event.amount >= previous_enemy_hp
                )
                if finishing_hit_is_capped:
                    continue
                self.outgoing_damage.setdefault(player_skill, ObservedRange()).add(
                    damage_event.amount
                )
                self.knowledge.add_outgoing(
                    self.target_name,
                    player_skill,
                    damage_event.amount,
                )

        player = parsed.combatant(character_name)
        for healing_event in parsed.healing:
            if character not in normalize(healing_event.target):
                continue
            effect = normalize(healing_event.effect or "")
            if effect in {"обновление", "renew"}:
                self.renewal_healing.add(healing_event.amount)
                self.knowledge.add_renewal_healing(healing_event.amount)
            elif player_skill == "лечение" and not (
                player is not None and player.current_hp >= player.max_hp
            ):
                # A heal ending exactly at maximum HP may have been capped by
                # the missing health. Its displayed amount is not a reliable
                # measurement of the skill's full power.
                self.direct_healing.add(healing_event.amount)
                self.knowledge.add_direct_healing(healing_event.amount)

        if self.target_name and (target := parsed.combatant(self.target_name)):
            self.enemy_current_hp = target.current_hp
            self.enemy_max_hp = target.max_hp

        effect_turns = (
            {normalize(effect.name): effect.turns for effect in player.effects}
            if player
            else {}
        )
        if "обновление" in effect_turns:
            self.renewal_turns = effect_turns["обновление"]
        elif player is not None:
            self.renewal_turns = 0

        periodic_effects = [
            turns
            for name, turns in effect_turns.items()
            if is_periodic_effect(name)
        ]
        if periodic_effects:
            self.periodic_damage_turns = max(periodic_effects)
        elif player is not None:
            self.periodic_damage_turns = 0
            self.periodic_damage = 0

        if used_skills or failed_player_skills:
            self.pending_skill = None
            self.pending_target = None
            self.pending_urgent = False

    def damage_floor(self, skill_name: str) -> int:
        observed = self.outgoing_damage.get(skill_name)
        if observed is None or observed.samples < 2:
            # Один результат может оказаться критическим ударом и не даёт
            # безопасной гарантии для добивания.
            return 0
        return observed.minimum or 0

    def predicted_incoming(self, *, after_current_tick: bool = False) -> int | None:
        normal = self.incoming_damage
        critical = self.critical_incoming_damage
        if normal.samples <= 0 and critical.samples <= 0:
            return None
        estimates: list[int] = []
        if normal.maximum is not None:
            uncertainty = 1.35 if normal.samples == 1 else 1.25
            if normal.samples >= 4:
                uncertainty = 1.15
            estimates.append(math.ceil(normal.maximum * uncertainty))
        if critical.maximum is not None:
            # После первого подтверждённого крита его величина становится
            # отдельной верхней границей, а не завышает каждый обычный удар.
            uncertainty = 1.25 if critical.samples == 1 else 1.15
            if critical.samples >= 4:
                uncertainty = 1.10
            estimates.append(math.ceil(critical.maximum * uncertainty))
        direct = max(estimates)
        required_turns = 1 if after_current_tick else 0
        periodic = self.periodic_damage if self.periodic_damage_turns > required_turns else 0
        return direct + periodic

    def expected_incoming(self, *, after_current_tick: bool = False) -> int | None:
        normal = self.incoming_damage
        critical = self.critical_incoming_damage
        total_samples = normal.samples + critical.samples
        if total_samples <= 0:
            return None
        uncertainty = 1.20 if total_samples == 1 else 1.15
        if total_samples >= 4:
            uncertainty = 1.10
        average = (normal.total + critical.total) / total_samples
        direct = max(1, math.ceil(average * uncertainty))
        required_turns = 1 if after_current_tick else 0
        periodic = self.periodic_damage if self.periodic_damage_turns > required_turns else 0
        return direct + periodic

    def renewal_tick(self) -> int | None:
        return self.renewal_healing.minimum

    def direct_heal(self) -> int | None:
        return self.direct_healing.minimum

    def confirm_treatment_enemy(self, target_name: str | None = None) -> None:
        self.knowledge.confirm_treatment_enemy(target_name or self.target_name)

    def revoke_treatment_enemy(self, target_name: str | None = None) -> None:
        self.knowledge.revoke_treatment_enemy(target_name or self.target_name)
        self.outgoing_damage.pop("лечение", None)

    def treatment_can_target_enemy(self) -> bool:
        observed = self.outgoing_damage.get("лечение")
        return bool(
            (observed is not None and observed.samples > 0)
            or self.knowledge.treatment_can_target_enemy(self.target_name)
        )

    def critical_incoming_rate(self) -> float:
        total = self.incoming_damage.samples + self.critical_incoming_damage.samples
        return self.critical_incoming_damage.samples / total if total else 0.0

    def sustainable_damage_floor(self) -> int:
        """Conservative damage per turn across a normal cooldown cycle."""
        basic = self.damage_floor("атака аколита")
        candidates = [basic] if basic > 0 else []
        for skill_name in ("святое свечение", "лечение"):
            special = self.damage_floor(skill_name)
            if special <= 0:
                continue
            if basic <= 0:
                candidates.append(special)
                continue
            interval = max(2, self.skill_cooldowns.get(skill_name, 0) + 1)
            candidates.append(basic + (special - basic) // interval)
        return max(candidates, default=0)


def build_decision_trace(
    *,
    created_at: str,
    telegram_message_id: int,
    memory: CombatMemory,
    round_state: CombatRoundState | None,
    current_hp: int | None,
    max_hp: int | None,
    decision: CombatDecision,
) -> CombatDecisionTrace:
    current_mana = round_state.current_mana if round_state is not None else None
    maximum_mana = round_state.max_mana if round_state is not None else None
    skills = tuple(
        SkillAvailability(
            name=skill.name,
            mana_cost=skill.mana_cost,
            cooldown=skill.cooldown,
            castable=skill.can_cast(current_mana),
        )
        for skill in (round_state.available_skills if round_state is not None else ())
    )
    outgoing = tuple(
        DamageEstimate(
            skill_name=skill_name,
            minimum=observed.minimum,
            maximum=observed.maximum,
            average=(observed.average(0.0) if observed.samples else None),
            samples=observed.samples,
        )
        for skill_name, observed in sorted(memory.outgoing_damage.items())
    )
    incoming = memory.incoming_damage
    critical_incoming = memory.critical_incoming_damage
    return CombatDecisionTrace(
        created_at=created_at,
        telegram_message_id=telegram_message_id,
        target_name=memory.target_name or "неопределённый моб",
        round_number=round_state.number if round_state is not None else None,
        player_current_hp=current_hp,
        player_max_hp=max_hp,
        enemy_current_hp=memory.enemy_current_hp,
        enemy_max_hp=memory.enemy_max_hp,
        current_mana=current_mana,
        maximum_mana=maximum_mana,
        renewal_turns=memory.renewal_turns,
        renewal_tick=memory.renewal_tick(),
        periodic_damage=memory.periodic_damage,
        periodic_damage_turns=memory.periodic_damage_turns,
        incoming_minimum=incoming.minimum,
        incoming_maximum=incoming.maximum,
        incoming_average=(incoming.average(0.0) if incoming.samples else None),
        incoming_samples=incoming.samples,
        critical_incoming_minimum=critical_incoming.minimum,
        critical_incoming_maximum=critical_incoming.maximum,
        critical_incoming_average=(
            critical_incoming.average(0.0) if critical_incoming.samples else None
        ),
        critical_incoming_samples=critical_incoming.samples,
        critical_incoming_rate=memory.critical_incoming_rate(),
        expected_next_hit=memory.expected_incoming(after_current_tick=True),
        worst_next_hit=memory.predicted_incoming(after_current_tick=True),
        sustainable_damage_per_turn=memory.sustainable_damage_floor() or None,
        skills=skills,
        outgoing_damage=outgoing,
        decision=decision,
    )


def resolve_decision_trace(
    trace: CombatDecisionTrace,
    round_state: CombatRoundState,
    character_name: str,
) -> CombatDecisionTrace:
    """Adds the observed target and effect without overwriting the plan."""
    character = normalize(character_name)
    skill_name = normalize(trace.decision.skill_name)

    if skill_name == "лечение":
        direct_healing = [
            event.amount
            for event in round_state.healing
            if character in normalize(event.target)
            and normalize(event.effect or "") not in {"обновление", "renew"}
        ]
        if direct_healing:
            return replace(
                trace,
                actual_target=SkillTarget.SELF,
                actual_effect="healing",
                actual_amount=sum(direct_healing),
            )

        enemy_damage = [
            event.amount
            for event in round_state.damage
            if character not in normalize(event.target)
            and not is_periodic_effect(event.effect)
        ]
        if enemy_damage:
            return replace(
                trace,
                actual_target=SkillTarget.ENEMY,
                actual_effect="damage",
                actual_amount=sum(enemy_damage),
            )
        return trace

    if skill_name == "обновление":
        return replace(
            trace,
            actual_target=SkillTarget.SELF,
            actual_effect="effect",
        )

    enemy_damage = [
        event.amount
        for event in round_state.damage
        if character not in normalize(event.target)
        and not is_periodic_effect(event.effect)
    ]
    return replace(
        trace,
        actual_target=trace.decision.target,
        actual_effect="damage" if enemy_damage else "action",
        actual_amount=sum(enemy_damage) if enemy_damage else None,
    )


def _lethal_skill(
    available: dict[str, SkillButton],
    memory: CombatMemory,
) -> str | None:
    if memory.enemy_current_hp is None:
        return None

    candidates: list[tuple[int, int, str]] = []
    priority = {"атака аколита": 0, "святое свечение": 1, "лечение": 2}
    for skill_name in ("атака аколита", "святое свечение", "лечение"):
        skill = available.get(skill_name)
        if skill is None:
            continue
        if memory.damage_floor(skill_name) >= memory.enemy_current_hp:
            candidates.append((skill.mana_cost, priority[skill_name], skill_name))

    return min(candidates)[2] if candidates else None


def _estimated_enemy_turns(
    memory: CombatMemory,
) -> int | None:
    if memory.enemy_current_hp is None:
        return None
    sustainable_damage = memory.sustainable_damage_floor()
    if sustainable_damage <= 0:
        return None
    return max(1, math.ceil(memory.enemy_current_hp / sustainable_damage))


def choose_combat_action(
    message,
    *,
    memory: CombatMemory,
    current_hp: int | None,
    max_hp: int | None,
    heal_threshold: int,
    round_state: CombatRoundState | None = None,
) -> CombatDecision | None:
    available = (
        round_state.castable_skills()
        if round_state is not None and round_state.available_skills
        else available_skills(message)
    )
    if not available:
        return None

    current_mana = (
        round_state.current_mana
        if round_state is not None
        else parse_current_mana(getattr(message, "raw_text", "") or "")
    )
    lethal = _lethal_skill(available, memory)
    if lethal is not None:
        return CombatDecision(
            lethal,
            SkillTarget.ENEMY,
            "добивание до ответного удара",
            urgent=True,
        )

    if current_hp is None or max_hp is None or max_hp <= 0:
        basic = available.get("атака аколита")
        if basic:
            return CombatDecision(basic.name, SkillTarget.ENEMY, "HP не распознано")
        return None

    worst_incoming = memory.predicted_incoming(after_current_tick=True)
    expected_incoming = memory.expected_incoming(after_current_tick=True)
    incoming_known = worst_incoming is not None and expected_incoming is not None
    margin = max(10, math.ceil(max_hp * 0.03))
    if current_hp <= heal_threshold:
        # Порог включает более внимательную оценку риска, но сам по себе
        # больше не приказывает использовать лечение.
        margin += math.ceil(max_hp * 0.02)

    renewal_tick = memory.renewal_tick() or 0
    renewal_credit = renewal_tick if memory.renewal_turns > 0 else 0
    periodic_now = memory.periodic_damage if memory.periodic_damage_turns > 0 else 0
    effective_hp = max(0, min(max_hp, current_hp + renewal_credit) - periodic_now)
    missing_hp = max_hp - effective_hp
    enemy_turns = _estimated_enemy_turns(memory)
    future_renewal = max(0, memory.renewal_turns - 1) * renewal_tick
    survival_turns = (
        max(
            0,
            math.floor(
                (effective_hp + future_renewal - margin) / max(1, expected_incoming)
            ),
        )
        if expected_incoming is not None
        else None
    )
    race_is_safe = (
        survival_turns >= enemy_turns
        if survival_turns is not None and enemy_turns is not None
        else None
    )
    can_survive_worst_hit = (
        effective_hp > worst_incoming + margin if worst_incoming is not None else None
    )
    victory_forecast = (
        f"до победы≈{enemy_turns} ход. при темпе≈"
        f"{memory.sustainable_damage_floor()} HP/ход"
        if enemy_turns is not None
        else "урон по врагу изучается"
    )
    if incoming_known:
        survival_forecast = (
            f"запас≈{survival_turns} ход., входящий урон≈"
            f"{expected_incoming}/{worst_incoming}"
        )
    else:
        survival_forecast = "входящий урон изучается"
    forecast = f"{victory_forecast}, {survival_forecast}"

    treatment = available.get("лечение")
    renewal = available.get("обновление")

    if treatment is not None and can_survive_worst_hit is False:
        return CombatDecision(
            "лечение",
            SkillTarget.SELF,
            f"следующий максимальный удар может быть смертельным; {forecast}",
            urgent=True,
        )

    renewed_survival_turns = (
        max(
            0,
            math.floor(
                (effective_hp + future_renewal + renewal_tick * 3 - margin)
                / max(1, expected_incoming)
            ),
        )
        if expected_incoming is not None and renewal_tick > 0
        else None
    )
    renewal_makes_race_safe = (
        renewal is not None
        and memory.renewal_turns <= 0
        and renewal_tick > 0
        and missing_hp >= renewal_tick * 2
        and enemy_turns is not None
        and enemy_turns >= 3
        and can_survive_worst_hit is True
        and race_is_safe is False
        and renewed_survival_turns is not None
        and renewed_survival_turns >= enemy_turns
    )
    if renewal_makes_race_safe:
        return CombatDecision(
            "обновление",
            SkillTarget.SELF,
            f"периодическое лечение меняет прогноз на безопасный; {forecast}",
        )

    direct_heal = memory.direct_heal() or 0
    healed_hp = min(max_hp, effective_hp + direct_heal)
    healed_survival_turns = (
        max(
            0,
            math.floor(
                (healed_hp + future_renewal - margin) / max(1, expected_incoming)
            ),
        )
        if expected_incoming is not None and direct_heal > 0
        else None
    )
    race_gap = (
        enemy_turns - survival_turns
        if enemy_turns is not None and survival_turns is not None
        else None
    )

    unknown_enemy_emergency_hp = max(margin * 2, math.ceil(max_hp * 0.25))
    if (
        treatment is not None
        and not incoming_known
        and current_hp <= unknown_enemy_emergency_hp
    ):
        return CombatDecision(
            "лечение",
            SkillTarget.SELF,
            f"критический запас HP, а сила нового противника ещё изучается; {forecast}",
            urgent=True,
        )

    if (
        treatment is not None
        and direct_heal <= 0
        and race_is_safe is False
        and survival_turns is not None
        and race_gap is not None
        and race_gap >= 2
        and (current_hp <= heal_threshold or survival_turns <= 2)
    ):
        return CombatDecision(
            "лечение",
            SkillTarget.SELF,
            f"первое безопасное самолечение уточнит его силу; {forecast}",
            urgent=survival_turns <= 1,
        )

    if (
        treatment is not None
        and race_is_safe is False
        and survival_turns is not None
        and (current_hp <= heal_threshold or survival_turns <= 2)
        and race_gap is not None
        and (survival_turns <= 2 or race_gap >= 2)
        and healed_survival_turns is not None
        and healed_survival_turns > survival_turns
        and direct_heal > 0
        and missing_hp >= direct_heal // 2
    ):
        return CombatDecision(
            "лечение",
            SkillTarget.SELF,
            f"мгновенное лечение увеличивает запас ходов; {forecast}",
            urgent=survival_turns <= 1,
        )

    if (
        renewal is not None
        and memory.renewal_turns <= 0
        and race_is_safe is False
        and treatment is None
        and renewal_tick > 0
        and missing_hp >= renewal_tick * 2
        and renewed_survival_turns is not None
        and survival_turns is not None
        and renewed_survival_turns > survival_turns
        and can_survive_worst_hit is True
    ):
        return CombatDecision(
            "обновление",
            SkillTarget.SELF,
            f"увеличивает запас ходов при недоступном мгновенном лечении; {forecast}",
        )

    holy_light = available.get("святое свечение")
    if (
        holy_light is not None
        and current_mana is not None
        and current_mana - holy_light.mana_cost >= HEALING_MANA_RESERVE
    ):
        return CombatDecision(
            "святое свечение",
            SkillTarget.ENEMY,
            f"лучший обычный урон; {forecast}",
        )

    if (
        treatment is not None
        and memory.treatment_can_target_enemy()
        and can_survive_worst_hit is not False
    ):
        return CombatDecision(
            "лечение",
            SkillTarget.ENEMY,
            f"урон сокращает бой выгоднее лечения себя; {forecast}",
        )

    if (
        renewal is not None
        and memory.renewal_turns <= 0
        and (
            race_is_safe is False
            or (race_is_safe is None and current_hp <= heal_threshold)
        )
    ):
        return CombatDecision(
            "обновление",
            SkillTarget.SELF,
            f"единственный доступный способ увеличить запас ходов; {forecast}",
        )

    basic = available.get("атака аколита")
    if basic is not None:
        return CombatDecision(
            "атака аколита",
            SkillTarget.ENEMY,
            f"сохранение маны; {forecast}",
        )

    return None
