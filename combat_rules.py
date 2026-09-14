from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import ClassVar, Protocol

from automation_policy import COMBAT_PLANNER_MODES, CombatPlannerMode
from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from combat_learning import (
    SHADOW_PLAN_VERSION,
    ShadowCombatPlan,
    build_shadow_plan,
    select_combat_planner_decision,
)
from combat_round import CombatRoundState, parse_combat_round
from combat_strategy import (
    COMBAT_MODEL_VERSION,
    CombatDecision,
    CombatDecisionTrace,
    CombatMemory,
    SkillTarget,
    build_decision_trace,
    choose_combat_action,
)
from game_message import ReadableGameMessage
from telegram_buttons import get_button_texts


@dataclass(frozen=True, slots=True)
class CombatTurnInput:
    """An observed combat state; planning neither learns nor performs actions.

    The caller retains ownership of memory and must not change it during this
    synchronous operation. Supplying the already observed round keeps event
    processing separate from decision making; otherwise the view is parsed.
    """

    message: ReadableGameMessage
    message_id: int
    created_at: datetime
    memory: CombatMemory
    current_hp: int | None
    max_hp: int | None
    heal_threshold: int
    planner_mode: CombatPlannerMode = "shadow"
    round_state: CombatRoundState | None = None

    def __post_init__(self) -> None:
        if type(self.message_id) is not int or self.message_id <= 0:
            raise ValueError("ID сообщения должен быть положительным целым")
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("Время наблюдения должно содержать часовой пояс")
        if not isinstance(self.memory, CombatMemory):
            raise ValueError("Нужна память текущей версии CombatMemory")
        for value in (self.current_hp, self.max_hp):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("HP должен быть неотрицательным целым или None")
        if type(self.heal_threshold) is not int or self.heal_threshold <= 0:
            raise ValueError("Порог лечения должен быть положительным целым")
        if self.planner_mode not in COMBAT_PLANNER_MODES:
            raise ValueError("Неизвестный режим планировщика")
        if self.round_state is not None and not isinstance(self.round_state, CombatRoundState):
            raise ValueError("Нужен разобранный CombatRoundState")


@dataclass(frozen=True, slots=True)
class CombatTurnPlan:
    """Immutable decision data, independent of the source message and memory."""

    decision: CombatDecision | None
    round_state: CombatRoundState | None
    shadow_plan: ShadowCombatPlan | None
    _trace: CombatDecisionTrace | None = field(repr=False)

    def __post_init__(self) -> None:
        if self.round_state is not None and not isinstance(self.round_state, CombatRoundState):
            raise ValueError("Нужен разобранный CombatRoundState")
        if self.decision is None:
            if self._trace is not None or self.shadow_plan is not None:
                raise ValueError("План без действия не может содержать trace или прогноз")
            return
        if not isinstance(self.decision, CombatDecision):
            raise ValueError("Нужен CombatDecision")
        if (
            not isinstance(self.decision.skill_name, str)
            or not self.decision.skill_name.strip()
            or not isinstance(self.decision.target, SkillTarget)
            or not isinstance(self.decision.reason, str)
            or type(self.decision.urgent) is not bool
        ):
            raise ValueError("Некорректное боевое решение")
        if (
            not isinstance(self._trace, CombatDecisionTrace)
            or self._trace.decision != self.decision
        ):
            raise ValueError("Trace должен соответствовать выбранному действию")
        if self.shadow_plan is not None and (
            not isinstance(self.shadow_plan, ShadowCombatPlan)
            or self.shadow_plan.executed != self.decision
        ):
            raise ValueError("Прогноз должен соответствовать выбранному действию")

    @property
    def trace(self) -> CombatDecisionTrace | None:
        """Return a legacy trace with a fresh, independently mutable payload.

        Legacy traces contain a shadow-plan dictionary for persistence. Keep
        that dictionary out of the stored plan so consumers cannot mutate it.
        """
        if self._trace is None:
            return None
        return replace(
            self._trace,
            shadow_plan=(self.shadow_plan.as_payload() if self.shadow_plan else None),
        )


class CombatRuleset(Protocol):
    @property
    def ruleset_id(self) -> str: ...

    @property
    def rules_version(self) -> int: ...

    @property
    def model_version(self) -> int: ...

    @property
    def planner_version(self) -> int: ...

    @property
    def knowledge_namespace(self) -> str: ...

    def parse_round(self, message: ReadableGameMessage) -> CombatRoundState | None: ...

    def plan_turn(self, turn: CombatTurnInput) -> CombatTurnPlan: ...


@dataclass(frozen=True, slots=True)
class LegacyCombatRuleset:
    """The current acolyte mechanics, delegated to their existing algorithms.

    Changing discovery does not change this ruleset. A change to the meaning
    of learned combat samples needs a new rules version and knowledge namespace.
    Planner revisions are reported separately because they do not alter those
    samples. This class does not load, save, or migrate persisted knowledge.
    """

    knowledge_namespace: ClassVar[str] = LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
    ruleset_id: ClassVar[str] = "legacy-acolyte"
    rules_version: ClassVar[int] = 1
    model_version: ClassVar[int] = COMBAT_MODEL_VERSION
    planner_version: ClassVar[int] = SHADOW_PLAN_VERSION

    def parse_round(self, message: ReadableGameMessage) -> CombatRoundState | None:
        return parse_combat_round(message.raw_text or "", get_button_texts(message))

    def plan_turn(self, turn: CombatTurnInput) -> CombatTurnPlan:
        round_state = turn.round_state
        if round_state is None:
            round_state = self.parse_round(turn.message)
        decision = choose_combat_action(
            turn.message,
            memory=turn.memory,
            current_hp=turn.current_hp,
            max_hp=turn.max_hp,
            heal_threshold=turn.heal_threshold,
            round_state=round_state,
        )
        if decision is None:
            return CombatTurnPlan(None, round_state, None, None)

        shadow_plan = build_shadow_plan(
            turn.message,
            memory=turn.memory,
            current_hp=turn.current_hp,
            max_hp=turn.max_hp,
            executed=decision,
            round_state=round_state,
        )
        if shadow_plan is not None:
            decision = select_combat_planner_decision(shadow_plan, turn.planner_mode)
            shadow_plan = shadow_plan.with_execution(decision, mode=turn.planner_mode)
        trace = build_decision_trace(
            created_at=turn.created_at.isoformat(),
            telegram_message_id=turn.message_id,
            memory=turn.memory,
            round_state=round_state,
            current_hp=turn.current_hp,
            max_hp=turn.max_hp,
            decision=decision,
        )
        return CombatTurnPlan(decision, round_state, shadow_plan, trace)
