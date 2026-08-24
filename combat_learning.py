from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from combat_round import CombatRoundState
from combat_strategy import CombatDecision, CombatMemory, SkillTarget
from parser import normalize
from skills import HEALING_MANA_RESERVE, SkillButton, available_skills

SHADOW_HORIZON = 3
SHADOW_PLAN_VERSION = 3


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
    unsafe: bool
    mana_dominated: bool
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
            "unsafe": self.unsafe,
            "mana_dominated": self.mana_dominated,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ShadowCombatPlan:
    horizon: int
    recommendation: CombatDecision
    executed: CombatDecision
    confident: bool
    candidates: tuple[ActionProjection, ...]

    @property
    def agrees(self) -> bool:
        return (
            normalize(self.recommendation.skill_name)
            == normalize(self.executed.skill_name)
            and self.recommendation.target is self.executed.target
        )

    @property
    def has_safe_candidate(self) -> bool:
        return any(not candidate.unsafe for candidate in self.candidates)

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": SHADOW_PLAN_VERSION,
            "horizon": self.horizon,
            "confident": self.confident,
            "has_safe_candidate": self.has_safe_candidate,
            "agrees": self.agrees,
            "recommendation": {
                "skill_name": self.recommendation.skill_name,
                "target": self.recommendation.target.value,
                "reason": self.recommendation.reason,
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
            for candidate in self.candidates
        )
        return (
            "[COMBAT_SHADOW] "
            f"горизонт={self.horizon}; совет="
            f"{self.recommendation.skill_name}→{self.recommendation.target.value}; "
            f"с фактическим решением {comparison}; {confidence}; "
            f"оценки: {candidates}"
        )


def _observed_damage(memory: CombatMemory, skill_name: str) -> tuple[int, int]:
    observed = memory.outgoing_damage.get(normalize(skill_name))
    if observed is None or observed.samples <= 0:
        return 0, 0
    average = max(1, round(observed.total / observed.samples))
    return average, observed.samples


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
    """Scores plausible actions locally without controlling the game."""
    if current_hp is None or max_hp is None or max_hp <= 0:
        return None

    available = (
        round_state.castable_skills()
        if round_state is not None and round_state.available_skills
        else available_skills(message)
    )
    if not available:
        return None

    current_mana = round_state.current_mana if round_state is not None else None
    expected_incoming = memory.expected_incoming(after_current_tick=True)
    worst_incoming = memory.predicted_incoming(after_current_tick=True)
    enemy_hp = memory.enemy_current_hp
    sustainable_damage = memory.sustainable_damage_floor()
    direct_heal = memory.direct_heal() or 0
    renewal_tick = memory.renewal_tick() or 0
    margin = max(10, math.ceil(max_hp * 0.03))

    incoming_samples = (
        memory.incoming_damage.samples + memory.critical_incoming_damage.samples
    )
    projections: list[ActionProjection] = []
    all_effects_known = True
    for skill, target in _candidate_targets(available, memory):
        skill_name = normalize(skill.name)
        damage = 0
        effect_samples = 0
        immediate_healing = 0
        new_renewal = 0
        if target is SkillTarget.ENEMY:
            damage, effect_samples = _observed_damage(memory, skill_name)
            all_effects_known = all_effects_known and effect_samples >= 2
        elif skill_name == "лечение":
            immediate_healing = direct_heal
            all_effects_known = (
                all_effects_known and memory.direct_healing.samples >= 2
            )
        elif skill_name == "обновление":
            new_renewal = renewal_tick
            all_effects_known = (
                all_effects_known and memory.renewal_healing.samples >= 2
            )

        lethal = bool(enemy_hp is not None and damage >= enemy_hp > 0)
        mana_after = (
            max(0, current_mana - skill.mana_cost)
            if current_mana is not None
            else None
        )
        if (
            target is SkillTarget.ENEMY
            and skill.mana_cost > 0
            and mana_after is not None
            and mana_after < HEALING_MANA_RESERVE
            and not lethal
        ):
            continue

        remaining_enemy_hp = (
            max(0, enemy_hp - damage) if enemy_hp is not None else None
        )
        if remaining_enemy_hp == 0:
            expected_enemy_hits: int | None = 0
        elif remaining_enemy_hp is not None and sustainable_damage > 0:
            expected_enemy_hits = min(
                horizon,
                max(1, math.ceil(remaining_enemy_hp / sustainable_damage)),
            )
        else:
            expected_enemy_hits = None

        healed_now = min(max_hp, current_hp + immediate_healing)
        existing_renewal_ticks = min(horizon, max(0, memory.renewal_turns))
        future_healing = existing_renewal_ticks * renewal_tick
        # Обновление начинает приносить HP не в ход применения.
        future_healing += max(0, horizon - 1) * new_renewal
        projected_hp = healed_now + future_healing
        if expected_incoming is not None and expected_enemy_hits is not None:
            projected_hp -= expected_incoming * expected_enemy_hits
        projected_hp = max(0, min(max_hp, projected_hp))

        first_hit_hp = healed_now
        if memory.renewal_turns > 0:
            first_hit_hp = min(max_hp, first_hit_hp + renewal_tick)
        immediate_unsafe = bool(
            expected_enemy_hits
            and worst_incoming is not None
            and first_hit_hp <= worst_incoming + margin
        )
        horizon_unsafe = bool(
            expected_enemy_hits
            and expected_incoming is not None
            and projected_hp <= margin
        )
        unsafe = immediate_unsafe or horizon_unsafe

        score = float(projected_hp)
        score += damage * 4.0
        score += float(mana_after or 0) * 2.0
        if remaining_enemy_hp == 0:
            score += 5000.0
        elif expected_enemy_hits is not None and expected_enemy_hits < horizon:
            score += (horizon - expected_enemy_hits) * 120.0
        if unsafe:
            score -= 10000.0

        missing_hp = max_hp - current_hp
        if immediate_healing:
            wasted_potential = max(0, immediate_healing - missing_hp)
            score -= wasted_potential * 6.0
            if missing_hp < max(1, immediate_healing // 2):
                score -= 400.0
        if new_renewal and expected_enemy_hits is not None and expected_enemy_hits <= 2:
            score -= 350.0
        if target is SkillTarget.ENEMY:
            score += 30.0
        if effect_samples == 0 and target is SkillTarget.ENEMY:
            score -= 200.0

        reason = (
            f"через {horizon} хода HP≈{projected_hp}, "
            f"враг≈{remaining_enemy_hp if remaining_enemy_hp is not None else '?'}, "
            f"ответных ударов≈"
            f"{expected_enemy_hits if expected_enemy_hits is not None else '?'}"
        )
        projections.append(
            ActionProjection(
                skill_name=skill_name,
                target=target,
                mana_cost=skill.mana_cost,
                score=score,
                projected_player_hp=projected_hp,
                projected_enemy_hp=remaining_enemy_hp,
                expected_enemy_hits=expected_enemy_hits,
                unsafe=unsafe,
                mana_dominated=False,
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
            and (
                other.projected_player_hp is None
                or candidate.projected_player_hp is None
                or other.projected_player_hp >= candidate.projected_player_hp
            )
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
    confident = incoming_samples >= 4 and all_effects_known and bool(safe_projections)
    best = safe_projections[0] if safe_projections else projections[0]
    recommendation = CombatDecision(
        skill_name=best.skill_name,
        target=best.target,
        reason=f"теневой прогноз: {best.reason}",
        urgent=False,
    )
    if not confident:
        recommendation = executed
    return ShadowCombatPlan(
        horizon=horizon,
        recommendation=recommendation,
        executed=executed,
        confident=confident,
        candidates=tuple(projections),
    )
