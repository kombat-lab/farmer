from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from combat_round import CombatRoundState
from combat_strategy import CombatDecision, CombatMemory, SkillTarget
from parser import normalize
from skills import HEALING_MANA_RESERVE, SkillButton, available_skills, parse_current_mana

SHADOW_HORIZON = 6
SHADOW_MAX_HORIZON = 24
SHADOW_BEAM_WIDTH = 64
SHADOW_PLAN_VERSION = 4
BASIC_ATTACK_MANA_RESTORE = 2
RENEWAL_DURATION = 3
COMBAT_PLANNER_MODES = ("shadow", "guarded", "active")
DEFAULT_SKILL_COOLDOWNS = {
    "атака аколита": 0,
    "святое свечение": 1,
    "лечение": 3,
    "обновление": 3,
}


@dataclass(frozen=True)
class CombatPolicySummary:
    key: str
    total_actions: int
    offensive_actions: int
    self_heals: int
    renewals: int


@dataclass(frozen=True)
class BattleLearningSummary:
    profile_max_hp: int
    model_version: int
    rounds: int
    total_actions: int
    offensive_actions: int
    self_heals: int
    renewals: int
    minimum_hp: int | None
    minimum_hp_percent: float | None
    last_decision_hp: int | None
    minimum_mana: int | None
    last_decision_mana: int | None
    effective_self_healing: int
    lost_healing_potential: int
    dangerous_turns: int
    shadow_decisions: int
    shadow_confident: int
    shadow_agreements: int
    policy_key: str


def resolved_decision(trace: dict[str, Any]) -> dict[str, Any]:
    raw_decision = trace.get("decision")
    decision = (
        {str(key): value for key, value in raw_decision.items()}
        if isinstance(raw_decision, dict)
        else {}
    )
    outcome = trace.get("outcome")
    if isinstance(outcome, dict) and outcome.get("target") in {"self", "enemy"}:
        decision["target"] = outcome["target"]
    return decision


def combat_policy_summary(decisions: list[dict[str, Any]]) -> CombatPolicySummary:
    """Groups different button sequences by their meaningful battle style."""
    total = len(decisions)
    offensive = sum(
        str(decision.get("target", "")).casefold() == "enemy"
        for decision in decisions
    )
    self_heals = sum(
        str(decision.get("skill_name", "")).casefold() == "лечение"
        and str(decision.get("target", "")).casefold() == "self"
        for decision in decisions
    )
    renewals = sum(
        str(decision.get("skill_name", "")).casefold() == "обновление"
        and str(decision.get("target", "")).casefold() == "self"
        for decision in decisions
    )

    aggression_ratio = offensive / total if total else 0.0
    aggression = (
        "aggressive"
        if aggression_ratio >= 0.80
        else "balanced"
        if aggression_ratio >= 0.65
        else "defensive"
    )

    def usage_bucket(count: int) -> str:
        ratio = count / total if total else 0.0
        if count == 0:
            return "none"
        if ratio <= 0.10:
            return "light"
        if ratio <= 0.25:
            return "moderate"
        return "heavy"

    offense_counts: dict[str, int] = {}
    for decision in decisions:
        if str(decision.get("target", "")).casefold() != "enemy":
            continue
        name = str(decision.get("skill_name") or "неизвестно").casefold()
        offense_counts[name] = offense_counts.get(name, 0) + 1
    if not offense_counts:
        preference = "none"
    else:
        preference, preference_count = max(
            offense_counts.items(),
            key=lambda item: (item[1], item[0]),
        )
        if len(offense_counts) > 1 and preference_count * 2 <= offensive:
            preference = "mixed"

    key = ":".join(
        (
            aggression,
            f"heal-{usage_bucket(self_heals)}",
            f"renew-{usage_bucket(renewals)}",
            f"offense-{preference}",
        )
    )
    return CombatPolicySummary(key, total, offensive, self_heals, renewals)


def battle_learning_summary(
    traces: list[dict[str, Any]],
) -> BattleLearningSummary:
    decisions = [resolved_decision(trace) for trace in traces]
    policy = combat_policy_summary(decisions)
    hp_values: list[int] = []
    hp_percentages: list[float] = []
    mana_values: list[int] = []
    round_numbers: list[int] = []
    profile_max_hp = 0
    model_version = 0
    effective_self_healing = 0
    lost_healing_potential = 0
    dangerous_turns = 0
    shadow_decisions = 0
    shadow_confident = 0
    shadow_agreements = 0

    for trace in traces:
        raw_version = trace.get("model_version")
        if isinstance(raw_version, int):
            model_version = max(model_version, raw_version)
        round_number = trace.get("round_number")
        if isinstance(round_number, int):
            round_numbers.append(round_number)

        player = trace.get("player")
        current_hp: int | None = None
        max_hp: int | None = None
        if isinstance(player, dict):
            raw_hp = player.get("current_hp")
            raw_max_hp = player.get("max_hp")
            current_hp = raw_hp if isinstance(raw_hp, int) else None
            max_hp = raw_max_hp if isinstance(raw_max_hp, int) else None
            if current_hp is not None:
                hp_values.append(current_hp)
            if max_hp is not None and max_hp > 0:
                profile_max_hp = max_hp
                if current_hp is not None:
                    hp_percentages.append(current_hp * 100.0 / max_hp)

        mana = trace.get("mana")
        if isinstance(mana, dict) and isinstance(mana.get("current"), int):
            mana_values.append(int(mana["current"]))

        worst_hit = None
        incoming = trace.get("incoming_damage")
        if isinstance(incoming, dict) and isinstance(
            incoming.get("worst_next_hit"), int
        ):
            worst_hit = int(incoming["worst_next_hit"])
        if current_hp is not None and max_hp and worst_hit is not None:
            margin = max(10, (max_hp * 3 + 99) // 100)
            dangerous_turns += int(current_hp <= worst_hit + margin)

        outcome = trace.get("outcome")
        decision = resolved_decision(trace)
        if (
            isinstance(outcome, dict)
            and outcome.get("target") == "self"
            and outcome.get("effect") == "healing"
            and str(decision.get("skill_name", "")).casefold() == "лечение"
            and isinstance(outcome.get("amount"), int)
        ):
            amount = int(outcome["amount"])
            effective_self_healing += amount
            estimate = trace.get("direct_heal_estimate")
            if isinstance(estimate, int) and estimate > amount:
                lost_healing_potential += estimate - amount

        shadow = trace.get("shadow_plan")
        if isinstance(shadow, dict):
            shadow_decisions += 1
            confident = bool(shadow.get("confident"))
            shadow_confident += int(confident)
            shadow_agreements += int(confident and bool(shadow.get("agrees")))

    return BattleLearningSummary(
        profile_max_hp=profile_max_hp,
        model_version=model_version,
        rounds=max(round_numbers, default=len(traces)),
        total_actions=policy.total_actions,
        offensive_actions=policy.offensive_actions,
        self_heals=policy.self_heals,
        renewals=policy.renewals,
        minimum_hp=min(hp_values) if hp_values else None,
        minimum_hp_percent=min(hp_percentages) if hp_percentages else None,
        last_decision_hp=hp_values[-1] if hp_values else None,
        minimum_mana=min(mana_values) if mana_values else None,
        last_decision_mana=mana_values[-1] if mana_values else None,
        effective_self_healing=effective_self_healing,
        lost_healing_potential=lost_healing_potential,
        dangerous_turns=dangerous_turns,
        shadow_decisions=shadow_decisions,
        shadow_confident=shadow_confident,
        shadow_agreements=shadow_agreements,
        policy_key=policy.key,
    )


@dataclass(frozen=True)
class ActionProjection:
    skill_name: str
    target: SkillTarget
    mana_cost: int
    score: float
    projected_player_hp: int | None
    projected_enemy_hp: int | None
    expected_enemy_hits: int | None
    projected_turns: int
    projected_mana: int | None
    survival_margin: int | None
    sequence: tuple[str, ...]
    unsafe: bool
    mana_dominated: bool
    effect_samples: int
    unknown_actions: int
    reason: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "target": self.target.value,
            "mana_cost": self.mana_cost,
            "score": round(self.score, 2),
            "projected_player_hp": self.projected_player_hp,
            "projected_enemy_hp": self.projected_enemy_hp,
            "expected_enemy_hits": self.expected_enemy_hits,
            "projected_turns": self.projected_turns,
            "projected_mana": self.projected_mana,
            "survival_margin": self.survival_margin,
            "sequence": list(self.sequence),
            "unsafe": self.unsafe,
            "mana_dominated": self.mana_dominated,
            "effect_samples": self.effect_samples,
            "unknown_actions": self.unknown_actions,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ShadowCombatPlan:
    horizon: int
    recommendation: CombatDecision
    baseline: CombatDecision
    executed: CombatDecision
    confident: bool
    candidates: tuple[ActionProjection, ...]
    control_mode: str = "shadow"

    @property
    def agrees(self) -> bool:
        return (
            normalize(self.recommendation.skill_name)
            == normalize(self.baseline.skill_name)
            and self.recommendation.target is self.baseline.target
        )

    @property
    def has_safe_candidate(self) -> bool:
        return any(not candidate.unsafe for candidate in self.candidates)

    @property
    def controls_action(self) -> bool:
        return (
            normalize(self.executed.skill_name)
            == normalize(self.recommendation.skill_name)
            and self.executed.target is self.recommendation.target
            and not self.agrees
        )

    def with_execution(
        self,
        decision: CombatDecision,
        *,
        mode: str,
    ) -> ShadowCombatPlan:
        return replace(self, executed=decision, control_mode=mode)

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": SHADOW_PLAN_VERSION,
            "horizon": self.horizon,
            "confident": self.confident,
            "has_safe_candidate": self.has_safe_candidate,
            "agrees": self.agrees,
            "control_mode": self.control_mode,
            "controls_action": self.controls_action,
            "recommendation": {
                "skill_name": self.recommendation.skill_name,
                "target": self.recommendation.target.value,
                "reason": self.recommendation.reason,
            },
            "baseline": {
                "skill_name": self.baseline.skill_name,
                "target": self.baseline.target.value,
                "reason": self.baseline.reason,
            },
            "executed": {
                "skill_name": self.executed.skill_name,
                "target": self.executed.target.value,
            },
            "candidates": [candidate.as_payload() for candidate in self.candidates],
        }

    def format_log(self) -> str:
        comparison = "совпадает" if self.agrees else "отличается"
        if not self.has_safe_candidate:
            confidence = "безопасного плана на горизонте нет"
        else:
            confidence = "достаточно данных" if self.confident else "данные ещё копятся"
        candidates = "; ".join(
            f"{candidate.skill_name}→{candidate.target.value}="
            f"{candidate.score:.1f}"
            f"{' опасно' if candidate.unsafe else ''}"
            f"{' лишняя мана' if candidate.mana_dominated else ''}"
            f" [HP≈{candidate.projected_player_hp}, "
            f"враг≈{candidate.projected_enemy_hp}, "
            f"запас≈{candidate.survival_margin}]"
            for candidate in self.candidates
        )
        control = "управляет" if self.controls_action else "наблюдает"
        return (
            "[COMBAT_SHADOW] "
            f"модель=v{SHADOW_PLAN_VERSION}; горизонт={self.horizon}; "
            f"режим={self.control_mode}, {control}; совет="
            f"{self.recommendation.skill_name}→{self.recommendation.target.value}; "
            f"с прежней логикой {comparison}; {confidence}; "
            f"оценки: {candidates}"
        )


def _observed_damage(memory: CombatMemory, skill_name: str) -> tuple[int, int]:
    """Returns a conservative damage estimate and its sample count."""
    observed = memory.outgoing_damage.get(normalize(skill_name))
    if observed is None or observed.samples <= 0:
        return 0, 0
    if observed.samples >= 2 and observed.minimum is not None:
        return observed.minimum, observed.samples
    # One observation may be a critical hit. It is still useful for a shadow
    # forecast, but not as a guaranteed finishing value.
    return max(1, math.floor(observed.average(0.0) * 0.75)), observed.samples


def _candidate_targets(
    available: dict[str, SkillButton],
    memory: CombatMemory,
) -> list[tuple[SkillButton, SkillTarget]]:
    result: list[tuple[SkillButton, SkillTarget]] = []
    for name, skill in available.items():
        normalized = normalize(name)
        if normalized == "лечение":
            result.append((skill, SkillTarget.SELF))
            if memory.treatment_can_target_enemy():
                result.append((skill, SkillTarget.ENEMY))
        elif normalized == "обновление":
            if memory.renewal_turns <= 0:
                result.append((skill, SkillTarget.SELF))
        else:
            result.append((skill, SkillTarget.ENEMY))
    return result


@dataclass(frozen=True)
class _SkillModel:
    name: str
    target: SkillTarget
    mana_cost: int
    cooldown: int
    damage: int
    immediate_healing: int
    starts_renewal: bool
    effect_samples: int

    @property
    def label(self) -> str:
        return f"{self.name}→{self.target.value}"


@dataclass(frozen=True)
class _SearchState:
    hp: int
    enemy_hp: int | None
    mana: int | None
    renewal_turns: int
    periodic_turns: int
    cooldowns: tuple[tuple[str, int], ...]
    turns: int = 0
    enemy_hits: int = 0
    minimum_safety_margin: int | None = None
    unsafe_events: int = 0
    unknown_actions: int = 0
    sequence: tuple[str, ...] = ()

    @property
    def won(self) -> bool:
        return self.enemy_hp == 0


def _cooldown_map(state: _SearchState) -> dict[str, int]:
    return dict(state.cooldowns)


def _build_skill_models(
    available: dict[str, SkillButton],
    round_state: CombatRoundState | None,
    memory: CombatMemory,
) -> tuple[dict[tuple[str, SkillTarget], _SkillModel], dict[str, int]]:
    buttons = (
        {normalize(skill.name): skill for skill in round_state.available_skills}
        if round_state is not None and round_state.available_skills
        else dict(available)
    )
    models: dict[tuple[str, SkillTarget], _SkillModel] = {}
    initial_cooldowns: dict[str, int] = {}
    direct_heal = memory.direct_heal() or 0

    for raw_name, button in buttons.items():
        name = normalize(raw_name)
        unavailable = normalize(button.unavailable_reason or "")
        if "магия заблокирована" in unavailable:
            continue
        initial_cooldowns[name] = max(0, button.cooldown)
        cooldown = max(
            button.cooldown,
            memory.skill_cooldowns.get(name, DEFAULT_SKILL_COOLDOWNS.get(name, 0)),
        )
        targets = [SkillTarget.ENEMY]
        if name == "лечение":
            targets = [SkillTarget.SELF]
            if memory.treatment_can_target_enemy():
                targets.append(SkillTarget.ENEMY)
        elif name == "обновление":
            targets = [SkillTarget.SELF]

        for target in targets:
            damage = 0
            healing = 0
            starts_renewal = False
            effect_samples = 0
            if target is SkillTarget.ENEMY:
                damage, effect_samples = _observed_damage(memory, name)
            elif name == "лечение":
                healing = direct_heal
                effect_samples = memory.direct_healing.samples
            elif name == "обновление":
                starts_renewal = True
                effect_samples = memory.renewal_healing.samples

            models[(name, target)] = _SkillModel(
                name=name,
                target=target,
                mana_cost=button.mana_cost,
                cooldown=cooldown,
                damage=damage,
                immediate_healing=healing,
                starts_renewal=starts_renewal,
                effect_samples=effect_samples,
            )

    return models, initial_cooldowns


def _can_use(model: _SkillModel, state: _SearchState) -> bool:
    if _cooldown_map(state).get(model.name, 0) > 0:
        return False
    if model.starts_renewal and state.renewal_turns > 0:
        return False
    return state.mana is None or state.mana >= model.mana_cost


def _apply_skill(
    state: _SearchState,
    model: _SkillModel,
    *,
    max_hp: int,
    max_mana: int | None,
    expected_incoming: int,
    worst_incoming: int,
    renewal_tick: int,
    periodic_damage: int,
    margin: int,
) -> _SearchState:
    hp = state.hp
    renewal_turns = state.renewal_turns
    if renewal_turns > 0 and renewal_tick > 0:
        hp = min(max_hp, hp + renewal_tick)
        renewal_turns -= 1

    mana = state.mana
    if mana is not None:
        mana = max(0, mana - model.mana_cost)

    enemy_hp = state.enemy_hp
    if model.target is SkillTarget.ENEMY and enemy_hp is not None:
        enemy_hp = max(0, enemy_hp - model.damage)
    elif model.immediate_healing > 0:
        hp = min(max_hp, hp + model.immediate_healing)
    elif model.starts_renewal:
        renewal_turns = max(renewal_turns, RENEWAL_DURATION)

    if model.name == "атака аколита" and mana is not None:
        mana = min(max_mana or mana + BASIC_ATTACK_MANA_RESTORE, mana + BASIC_ATTACK_MANA_RESTORE)

    cooldowns = {
        name: max(0, cooldown - 1)
        for name, cooldown in _cooldown_map(state).items()
    }
    cooldowns[model.name] = model.cooldown

    enemy_hits = state.enemy_hits
    periodic_turns = state.periodic_turns
    minimum_safety_margin = state.minimum_safety_margin
    unsafe_events = state.unsafe_events
    if enemy_hp != 0:
        periodic = periodic_damage if periodic_turns > 0 else 0
        current_margin = hp - worst_incoming - periodic - margin
        minimum_safety_margin = (
            current_margin
            if minimum_safety_margin is None
            else min(minimum_safety_margin, current_margin)
        )
        if current_margin <= 0:
            unsafe_events += 1
        hp -= expected_incoming + periodic
        if hp <= 0:
            unsafe_events += 1
            hp = 0
        enemy_hits += 1
        periodic_turns = max(0, periodic_turns - 1)

    return _SearchState(
        hp=hp,
        enemy_hp=enemy_hp,
        mana=mana,
        renewal_turns=renewal_turns,
        periodic_turns=periodic_turns,
        cooldowns=tuple(sorted(cooldowns.items())),
        turns=state.turns + 1,
        enemy_hits=enemy_hits,
        minimum_safety_margin=minimum_safety_margin,
        unsafe_events=unsafe_events,
        unknown_actions=state.unknown_actions + int(model.effect_samples < 2),
        sequence=(*state.sequence, model.label),
    )


def _remaining_hits(state: _SearchState, sustainable_damage: int) -> int | None:
    if state.enemy_hp is None:
        return None
    if state.enemy_hp == 0:
        return 0
    if sustainable_damage <= 0:
        return None
    return max(1, math.ceil(state.enemy_hp / sustainable_damage))


def _survival_margin(
    state: _SearchState,
    *,
    sustainable_damage: int,
    expected_incoming: int,
    renewal_tick: int,
    margin: int,
) -> int | None:
    remaining_hits = _remaining_hits(state, sustainable_damage)
    if remaining_hits is None:
        return None
    healing = min(state.renewal_turns, remaining_hits) * renewal_tick
    return state.hp + healing - remaining_hits * expected_incoming - margin


def _state_score(
    state: _SearchState,
    *,
    starting_enemy_hp: int | None,
    sustainable_damage: int,
    expected_incoming: int,
    renewal_tick: int,
    margin: int,
) -> float:
    mana = state.mana or 0
    if state.won:
        return (
            1_000_000.0
            - state.turns * 4_000.0
            + state.hp * 20.0
            + mana * 50.0
            - state.unsafe_events * 200_000.0
        )
    dealt = (
        max(0, starting_enemy_hp - state.enemy_hp)
        if starting_enemy_hp is not None and state.enemy_hp is not None
        else 0
    )
    survival = _survival_margin(
        state,
        sustainable_damage=sustainable_damage,
        expected_incoming=expected_incoming,
        renewal_tick=renewal_tick,
        margin=margin,
    )
    bounded_survival = max(-5_000, min(5_000, survival or 0))
    return (
        dealt * 12.0
        + state.hp * 5.0
        + mana * 25.0
        + state.renewal_turns * renewal_tick * 2.0
        + bounded_survival * 3.0
        - state.unsafe_events * 200_000.0
        - state.unknown_actions * 150.0
    )


def _state_key(state: _SearchState) -> tuple[object, ...]:
    return (
        state.hp // 5,
        state.enemy_hp,
        state.mana,
        state.renewal_turns,
        state.periodic_turns,
        state.cooldowns,
        state.unsafe_events,
    )


def _best_continuation(
    first: _SkillModel,
    initial: _SearchState,
    models: tuple[_SkillModel, ...],
    *,
    planning_horizon: int,
    max_hp: int,
    max_mana: int | None,
    expected_incoming: int,
    worst_incoming: int,
    renewal_tick: int,
    periodic_damage: int,
    margin: int,
    sustainable_damage: int,
) -> tuple[_SearchState, float]:
    state = _apply_skill(
        initial,
        first,
        max_hp=max_hp,
        max_mana=max_mana,
        expected_incoming=expected_incoming,
        worst_incoming=worst_incoming,
        renewal_tick=renewal_tick,
        periodic_damage=periodic_damage,
        margin=margin,
    )
    states = [state]
    for _ in range(1, planning_horizon):
        expanded: list[_SearchState] = []
        for current in states:
            if current.won or current.hp <= 0:
                expanded.append(current)
                continue
            choices = [model for model in models if _can_use(model, current)]
            if not choices:
                expanded.append(current)
                continue
            expanded.extend(
                _apply_skill(
                    current,
                    model,
                    max_hp=max_hp,
                    max_mana=max_mana,
                    expected_incoming=expected_incoming,
                    worst_incoming=worst_incoming,
                    renewal_tick=renewal_tick,
                    periodic_damage=periodic_damage,
                    margin=margin,
                )
                for model in choices
            )

        best_by_state: dict[tuple[object, ...], _SearchState] = {}
        for candidate in expanded:
            key = _state_key(candidate)
            previous = best_by_state.get(key)
            if previous is None or candidate.unknown_actions < previous.unknown_actions:
                best_by_state[key] = candidate
        states = sorted(
            best_by_state.values(),
            key=lambda candidate: _state_score(
                candidate,
                starting_enemy_hp=initial.enemy_hp,
                sustainable_damage=sustainable_damage,
                expected_incoming=expected_incoming,
                renewal_tick=renewal_tick,
                margin=margin,
            ),
            reverse=True,
        )[:SHADOW_BEAM_WIDTH]
        if states and all(candidate.won or candidate.hp <= 0 for candidate in states):
            break

    best = max(
        states,
        key=lambda candidate: _state_score(
            candidate,
            starting_enemy_hp=initial.enemy_hp,
            sustainable_damage=sustainable_damage,
            expected_incoming=expected_incoming,
            renewal_tick=renewal_tick,
            margin=margin,
        ),
    )
    return best, _state_score(
        best,
        starting_enemy_hp=initial.enemy_hp,
        sustainable_damage=sustainable_damage,
        expected_incoming=expected_incoming,
        renewal_tick=renewal_tick,
        margin=margin,
    )


def _projection_for_decision(
    plan: ShadowCombatPlan,
    decision: CombatDecision,
) -> ActionProjection | None:
    return next(
        (
            candidate
            for candidate in plan.candidates
            if normalize(candidate.skill_name) == normalize(decision.skill_name)
            and candidate.target is decision.target
        ),
        None,
    )


def select_combat_planner_decision(
    plan: ShadowCombatPlan,
    mode: str,
) -> CombatDecision:
    """Selects a planner action while keeping every mode fail-safe."""
    normalized_mode = normalize(mode)
    if normalized_mode not in COMBAT_PLANNER_MODES or normalized_mode == "shadow":
        return plan.baseline
    recommendation = _projection_for_decision(plan, plan.recommendation)
    baseline = _projection_for_decision(plan, plan.baseline)
    if not plan.confident or recommendation is None or recommendation.unsafe:
        return plan.baseline
    if normalized_mode == "active":
        return plan.recommendation
    # Guarded mode only replaces a demonstrably weaker baseline. A merely
    # larger HP reserve is not enough: that used to cause needless healing in
    # already safe fights and made them longer.
    if baseline is None or baseline.unsafe:
        return plan.recommendation
    reserve_gain = (recommendation.survival_margin or 0) - (baseline.survival_margin or 0)
    removes_hit = bool(
        recommendation.expected_enemy_hits is not None
        and baseline.expected_enemy_hits is not None
        and recommendation.expected_enemy_hits < baseline.expected_enemy_hits
    )
    economical_attack = bool(
        baseline.mana_dominated
        and recommendation.target is SkillTarget.ENEMY
    )
    if economical_attack and reserve_gain >= -10:
        return plan.recommendation
    if (
        removes_hit
        and recommendation.target is SkillTarget.ENEMY
        and reserve_gain >= -10
    ):
        return plan.recommendation
    return plan.baseline


def build_shadow_plan(
    message: object,
    *,
    memory: CombatMemory,
    current_hp: int | None,
    max_hp: int | None,
    executed: CombatDecision,
    round_state: CombatRoundState | None = None,
    horizon: int = SHADOW_HORIZON,
) -> ShadowCombatPlan | None:
    """Builds a bounded multi-turn forecast without extra Telegram calls."""
    if current_hp is None or max_hp is None or max_hp <= 0:
        return None

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
    parsed_max_mana = (
        round_state.max_mana
        if round_state is not None
        else None
    )
    max_mana = parsed_max_mana or current_mana
    expected_incoming = memory.expected_incoming(after_current_tick=True)
    worst_incoming = memory.predicted_incoming(after_current_tick=True)
    enemy_hp = memory.enemy_current_hp
    sustainable_damage = memory.sustainable_damage_floor()
    renewal_tick = memory.renewal_tick() or 0
    margin = max(10, math.ceil(max_hp * 0.03))
    if expected_incoming is None:
        expected_incoming = max(1, math.ceil(max_hp * 0.12))
    if worst_incoming is None:
        worst_incoming = max(expected_incoming, math.ceil(max_hp * 0.20))
    incoming_samples = (
        memory.incoming_damage.samples + memory.critical_incoming_damage.samples
    )
    models, initial_cooldowns = _build_skill_models(available, round_state, memory)
    future_models = tuple(models.values())
    if not future_models:
        return None
    estimated_turns = (
        math.ceil(enemy_hp / sustainable_damage)
        if enemy_hp is not None and sustainable_damage > 0
        else horizon
    )
    planning_horizon = max(
        1,
        min(SHADOW_MAX_HORIZON, max(horizon, estimated_turns + 2)),
    )
    initial = _SearchState(
        hp=current_hp,
        enemy_hp=enemy_hp,
        mana=current_mana,
        renewal_turns=max(0, memory.renewal_turns),
        periodic_turns=max(0, memory.periodic_damage_turns - 1),
        cooldowns=tuple(sorted(initial_cooldowns.items())),
    )
    projections: list[ActionProjection] = []
    for skill, target in _candidate_targets(available, memory):
        skill_name = normalize(skill.name)
        model = models.get((skill_name, target))
        if model is None:
            continue
        lethal = bool(enemy_hp is not None and model.damage >= enemy_hp > 0)
        mana_after = max(0, current_mana - skill.mana_cost) if current_mana is not None else None
        if (
            target is SkillTarget.ENEMY
            and skill.mana_cost > 0
            and mana_after is not None
            and mana_after < HEALING_MANA_RESERVE
            and not lethal
        ):
            continue
        best_state, score = _best_continuation(
            model,
            initial,
            future_models,
            planning_horizon=planning_horizon,
            max_hp=max_hp,
            max_mana=max_mana,
            expected_incoming=expected_incoming,
            worst_incoming=worst_incoming,
            renewal_tick=renewal_tick,
            periodic_damage=memory.periodic_damage,
            margin=margin,
            sustainable_damage=sustainable_damage,
        )
        remaining_hits = _remaining_hits(best_state, sustainable_damage)
        expected_enemy_hits = (
            best_state.enemy_hits + remaining_hits
            if remaining_hits is not None
            else None
        )
        survival = _survival_margin(
            best_state,
            sustainable_damage=sustainable_damage,
            expected_incoming=expected_incoming,
            renewal_tick=renewal_tick,
            margin=margin,
        )
        unsafe = best_state.unsafe_events > 0
        sequence_preview = " → ".join(best_state.sequence[:5])
        if len(best_state.sequence) > 5:
            sequence_preview += " → …"
        reason = (
            f"просчитано {best_state.turns} ход.; "
            f"HP≈{best_state.hp}, враг≈"
            f"{best_state.enemy_hp if best_state.enemy_hp is not None else '?'}, "
            f"запас≈{survival if survival is not None else '?'}; "
            f"линия: {sequence_preview}"
        )
        projections.append(
            ActionProjection(
                skill_name=skill_name,
                target=target,
                mana_cost=skill.mana_cost,
                score=score,
                projected_player_hp=best_state.hp,
                projected_enemy_hp=best_state.enemy_hp,
                expected_enemy_hits=expected_enemy_hits,
                projected_turns=best_state.turns,
                projected_mana=best_state.mana,
                survival_margin=survival,
                sequence=best_state.sequence,
                unsafe=unsafe,
                mana_dominated=False,
                effect_samples=model.effect_samples,
                unknown_actions=best_state.unknown_actions,
                reason=reason,
            )
        )

    if not projections:
        return None

    def is_mana_dominated(candidate: ActionProjection) -> bool:
        if (
            candidate.target is not SkillTarget.ENEMY
            or candidate.expected_enemy_hits is None
        ):
            return False
        return any(
            other is not candidate
            and not other.unsafe
            and other.target is SkillTarget.ENEMY
            and other.expected_enemy_hits == candidate.expected_enemy_hits
            and other.mana_cost < candidate.mana_cost
            and (other.survival_margin or 0) >= (candidate.survival_margin or 0)
            for other in projections
        )

    projections = [
        replace(projection, mana_dominated=is_mana_dominated(projection))
        for projection in projections
    ]
    projections.sort(
        key=lambda item: (not item.unsafe, not item.mana_dominated, item.score),
        reverse=True,
    )
    safe_projections = [
        projection
        for projection in projections
        if not projection.unsafe and not projection.mana_dominated
    ]
    if not safe_projections:
        safe_projections = [projection for projection in projections if not projection.unsafe]
    best = safe_projections[0] if safe_projections else projections[0]
    confident = bool(
        safe_projections
        and enemy_hp is not None
        and sustainable_damage > 0
        and current_mana is not None
        and incoming_samples >= 4
        and best.effect_samples >= 2
        and best.unknown_actions == 0
    )
    recommendation = CombatDecision(
        skill_name=best.skill_name,
        target=best.target,
        reason=f"теневой прогноз: {best.reason}",
        urgent=bool(best.survival_margin is not None and best.survival_margin <= margin),
    )
    if not safe_projections:
        recommendation = executed
    return ShadowCombatPlan(
        horizon=planning_horizon,
        recommendation=recommendation,
        baseline=executed,
        executed=executed,
        confident=confident,
        candidates=tuple(projections),
    )
