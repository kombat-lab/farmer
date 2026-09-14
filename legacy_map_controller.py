from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from automation_policy import DelayRange, LegacyMapPolicy, TargetPolicy
from blessing import BLESSING_BUTTON, BLESSING_STATUS_MARKER
from discovery import DiscoveryEventKind, DiscoveryObservation, DiscoveryStatus
from fog_input import semantic_fog_text
from game_input import ActionOutcome, InboundEvent
from message_snapshot import MessageSnapshot
from models import (
    ActionType,
    BotState,
    ButtonPosition,
    MapInfo,
    MessageKind,
    Position,
    RuntimeContext,
)
from navigator import SnakeNavigator
from parser import classify_message, normalize, parse_map
from targeting import analyze_map_targets

logger = logging.getLogger("fog_farmer")

MAP_COMMAND = "Карта"
ATTACK_BUTTON = "⚔️ Напасть"
BACK_TO_MAP_BUTTON = "↩️ К карте"
MAX_FAILED_MOVE_ATTEMPTS = len(SnakeNavigator.ALL_MOVE_BUTTONS)
_COMMITTED_ACTION_OUTCOMES = frozenset(
    {
        ActionOutcome.SENT,
        ActionOutcome.DELIVERY_UNKNOWN,
        ActionOutcome.DUPLICATE,
    }
)


def _action_was_committed(outcome: object) -> bool:
    if not isinstance(outcome, ActionOutcome):
        raise ValueError("Map action runtime must return ActionOutcome")
    return outcome in _COMMITTED_ACTION_OUTCOMES


class LegacyMapRuntime(Protocol):
    """Explicit host state and effects needed by the disposable map adapter."""

    @property
    def context(self) -> RuntimeContext: ...

    @property
    def state(self) -> BotState: ...

    @state.setter
    def state(self, value: BotState) -> None: ...

    @property
    def pause_requested(self) -> bool: ...

    @property
    def moves_in_cycle(self) -> int: ...

    @moves_in_cycle.setter
    def moves_in_cycle(self, value: int) -> None: ...

    @property
    def cycle_move_target(self) -> int: ...

    @property
    def recovery_refresh_requested(self) -> bool: ...

    @recovery_refresh_requested.setter
    def recovery_refresh_requested(self, value: bool) -> None: ...

    def legacy_map_policy(self) -> LegacyMapPolicy: ...
    def target_policy(self) -> TargetPolicy: ...
    def log(self, text: str) -> None: ...
    def mark_progress(self, reason: str) -> None: ...
    def is_current(self, event: InboundEvent) -> bool: ...
    def battle_health_is_low(self) -> bool: ...
    def wait_for_battle_health(self) -> None: ...
    def activity_break_is_due(self) -> bool: ...
    def recovery_elapsed(self) -> float: ...
    def recovery_minimum_wait(self) -> float: ...
    def recovery_minimum_hp(self) -> int: ...

    async def navigation_model_is_current(self, version: int) -> bool: ...
    async def set_navigation_model_version(self, version: int) -> None: ...
    async def clear_map_obstacles(self) -> int: ...
    async def get_map_obstacles(self, location_name: str) -> set[Position]: ...
    async def forget_map_obstacles(self, location_name: str, positions: set[Position]) -> int: ...
    async def remember_map_obstacle(self, location_name: str, position: Position) -> bool: ...
    async def record_map_event(self, event_type: str, message: str) -> None: ...
    async def stop(self, reason: str) -> None: ...
    async def enter_paused(self) -> None: ...
    async def complete_cycle(self) -> None: ...
    async def start_activity_break(self) -> None: ...
    async def pause_after_movement(self) -> None: ...
    async def try_refresh_blessing_from_map(
        self, event: InboundEvent, policy: LegacyMapPolicy
    ) -> bool: ...
    async def handle_blessing_menu(
        self, event: InboundEvent, policy: LegacyMapPolicy
    ) -> bool: ...
    def confirm_blessing_from_text(self, text: str, policy: LegacyMapPolicy) -> None: ...
    async def request_current_state(
        self, *, force: bool = False, recovery_reason: str | None = None
    ) -> bool: ...
    async def send_game_message(self, text: str, action_label: str) -> ActionOutcome: ...
    async def finish_health_recovery(self, current_hp: int, max_hp: int | None) -> None: ...

    async def click_button(
        self,
        event: InboundEvent,
        *,
        action_type: ActionType,
        description: str,
        delay_range: DelayRange,
        exact: str | None = None,
        position: ButtonPosition | None = None,
    ) -> ActionOutcome: ...


@dataclass(frozen=True, slots=True)
class LegacyMapObservation:
    event: InboundEvent
    kind: DiscoveryEventKind
    policy: LegacyMapPolicy
    targets: TargetPolicy
    map_info: MapInfo | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, InboundEvent):
            raise ValueError("Legacy map observation requires an InboundEvent")
        if not isinstance(self.kind, DiscoveryEventKind):
            raise ValueError("Legacy map observation requires DiscoveryEventKind")
        if not isinstance(self.policy, LegacyMapPolicy):
            raise ValueError("Legacy map observation requires LegacyMapPolicy")
        if not isinstance(self.targets, TargetPolicy):
            raise ValueError("Legacy map observation requires TargetPolicy")
        if (self.kind is DiscoveryEventKind.STATE) != (self.map_info is not None):
            raise ValueError("Only map state observations may contain MapInfo")
        if self.map_info is not None and not isinstance(self.map_info, MapInfo):
            raise ValueError("Legacy map state requires MapInfo")

    @property
    def snapshot(self) -> MessageSnapshot:
        return self.event.snapshot


@dataclass(frozen=True, slots=True)
class LegacyMapStateRevision:
    message_id: int
    semantic_text: str
    edit_date: datetime | None

    @property
    def fact_key(self) -> tuple[int, str]:
        return self.message_id, self.semantic_text

    @property
    def timestamp(self) -> float:
        return self.edit_date.timestamp() if self.edit_date is not None else 0.0


class LegacyMapController:
    """Owns the existing map mechanics; it is not a base class for future search."""

    def __init__(
        self,
        runtime: LegacyMapRuntime,
        *,
        character_name: str,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
    ) -> None:
        self.runtime = runtime
        self.character_name = character_name
        self.navigator = SnakeNavigator(min_x, max_x, min_y, max_y)
        self._last_state: LegacyMapStateRevision | None = None

    def status(self) -> DiscoveryStatus:
        return DiscoveryStatus(
            location_name=self.navigator.location_name,
            progress_units=self.runtime.context.move_count,
            progress_label="перемещений",
        )

    async def request_state(self) -> ActionOutcome:
        return await self.runtime.send_game_message(MAP_COMMAND, "map_message")

    async def initialize(self) -> int:
        """Drop only observations made with an incompatible coordinate model."""
        if await self.runtime.navigation_model_is_current(SnakeNavigator.MODEL_VERSION):
            return 0
        deleted = await self.runtime.clear_map_obstacles()
        await self.runtime.set_navigation_model_version(SnakeNavigator.MODEL_VERSION)
        logger.info(
            "Модель навигации обновлена до v%s; "
            "удалено несовместимых препятствий: %s.",
            SnakeNavigator.MODEL_VERSION,
            deleted,
        )
        return deleted

    def reset_cycle(self) -> None:
        self.navigator.reset_coverage(self.runtime.context.current_position)

    def observe_message(self, event: InboundEvent) -> LegacyMapObservation | None:
        if not isinstance(event, InboundEvent):
            raise ValueError("Legacy map discovery requires an InboundEvent")
        snapshot = event.snapshot
        text = snapshot.raw_text
        targets = self.runtime.target_policy()
        policy = self.runtime.legacy_map_policy()
        map_info = parse_map(text, targets.enabled, self.character_name)
        kind = classify_message(
            text,
            targets.enabled,
            self.character_name,
            is_map=map_info is not None,
        )
        event_kind = {
            MessageKind.MAP: DiscoveryEventKind.STATE,
            MessageKind.MOVE_STARTED: DiscoveryEventKind.PROGRESS_CONFIRMED,
            MessageKind.TARGET_SELECTION: DiscoveryEventKind.TARGET_SELECTION,
            MessageKind.TARGET_GONE: DiscoveryEventKind.TARGET_GONE,
        }.get(kind)
        if event_kind is None:
            blessing_button = any(
                BLESSING_BUTTON.casefold() in button.text.casefold()
                for row in snapshot.buttons
                for button in row
            )
            blessing_confirmation = BLESSING_STATUS_MARKER in normalize(text)
            if blessing_button or blessing_confirmation:
                event_kind = DiscoveryEventKind.AUXILIARY
        if event_kind is None:
            return None
        return LegacyMapObservation(event, event_kind, policy, targets, map_info)

    async def handle_message(self, observation: DiscoveryObservation) -> bool:
        if not isinstance(observation, LegacyMapObservation):
            return False
        event = observation.event
        self.runtime.confirm_blessing_from_text(event.snapshot.raw_text, observation.policy)
        if observation.kind is DiscoveryEventKind.STATE:
            assert observation.map_info is not None
            await self.handle_map(event, observation.map_info, observation.policy)
        elif observation.kind is DiscoveryEventKind.PROGRESS_CONFIRMED:
            self.runtime.state = BotState.MOVING
            self.runtime.mark_progress("сервер подтвердил движение")
        elif observation.kind is DiscoveryEventKind.TARGET_SELECTION:
            await self.handle_target_selection(observation)
        elif observation.kind is DiscoveryEventKind.TARGET_GONE:
            await self.handle_target_gone()
        elif observation.kind is DiscoveryEventKind.AUXILIARY:
            await self.runtime.handle_blessing_menu(event, observation.policy)
        return True

    def confirm_pending_move(
        self,
        current_position: tuple[int, int],
        *,
        movement_blocked: bool = False,
    ) -> tuple[int, int] | None:
        previous_obstacles = set(self.navigator.runtime_blocked)
        plan = self.runtime.context.pending_move
        if plan is None:
            if movement_blocked:
                self.navigator.reject_last_plan(
                    current_position,
                    mark_destination_blocked=True,
                )
            learned = self.navigator.runtime_blocked - previous_obstacles
            return next(iter(learned), None)

        if current_position == plan.destination:
            self.navigator.confirm_success(
                plan,
                current_position,
            )
            self.runtime.context.move_count += 1
            self.runtime.moves_in_cycle += 1
            self.runtime.context.failed_move_attempts = 0
            self.runtime.mark_progress("координата изменилась")

            if self.runtime.context.checked_empty_position == plan.origin:
                self.runtime.context.checked_empty_position = None

            self.runtime.log(
                f"Перемещение выполнено: "
                f"{plan.origin} → {current_position} "
                f"через {plan.button}. "
                f"Всего: {self.runtime.context.move_count}"
            )
        elif current_position == plan.origin:
            buttons_exhausted = self.navigator.reject_last_plan(
                current_position,
                mark_destination_blocked=movement_blocked,
            )
            if buttons_exhausted:
                self.runtime.context.failed_move_attempts = MAX_FAILED_MOVE_ATTEMPTS
            else:
                self.runtime.context.failed_move_attempts += 1
            self.runtime.log(
                f"Перемещение через {plan.button} не выполнено. "
                f"Неудач подряд: {self.runtime.context.failed_move_attempts}. "
                "Пробую другую кнопку без запроса истории."
            )
        else:
            recovered = self.navigator.recover_from_actual_transition(
                plan.origin,
                current_position,
            )
            if recovered:
                self.runtime.context.move_count += 1
                self.runtime.moves_in_cycle += 1
                self.runtime.context.failed_move_attempts = 0
                self.runtime.mark_progress("навигатор пересинхронизирован")
                self.runtime.log(
                    "Перемещение подтверждено по фактической позиции: "
                    f"{plan.origin} → {current_position} "
                    f"(ожидалось {plan.destination}). "
                    f"Всего: {self.runtime.context.move_count}"
                )
            else:
                self.runtime.context.failed_move_attempts += 1

        self.runtime.context.pending_move = None
        learned = self.navigator.runtime_blocked - previous_obstacles
        return next(iter(learned), None)


    async def observe_map(self, map_info: MapInfo) -> None:
        geometry_changed = bool(
            map_info.width
            and map_info.height
            and (
                self.navigator.max_x != map_info.width - 1
                or self.navigator.max_y != map_info.height - 1
            )
        )
        if map_info.location_name and (
            map_info.location_name != self.navigator.location_name or geometry_changed
        ):
            learned_obstacles = await self.runtime.get_map_obstacles(map_info.location_name)
            self.navigator.use_location(
                map_info.location_name,
                learned_obstacles,
                current_position=map_info.position,
                width=map_info.width,
                height=map_info.height,
            )
            self.runtime.context.pending_move = None
            self.runtime.context.failed_move_attempts = 0

        self.runtime.context.current_position = map_info.position
        if map_info.current_hp is not None:
            self.runtime.context.current_hp = map_info.current_hp
            self.runtime.context.max_hp = map_info.max_hp

        learned_obstacle = self.confirm_pending_move(
            map_info.position,
            movement_blocked=map_info.movement_blocked,
        )
        route_rebuilt = self.navigator.ensure_position(map_info.position)
        discarded_obstacles = self.navigator.take_recovery_discarded_obstacles()
        if discarded_obstacles and self.navigator.location_name:
            deleted = await self.runtime.forget_map_obstacles(
                self.navigator.location_name,
                discarded_obstacles,
            )
            self.runtime.log(
                "Маршрут не соответствовал фактической позиции. "
                f"Удалено сомнительных препятствий: {deleted}; "
                f"маршрут перестроен от {map_info.position}."
            )
            self.runtime.context.failed_move_attempts = 0
        elif route_rebuilt:
            self.runtime.log(
                f"Маршрут пересинхронизирован по фактической позиции {map_info.position}."
            )
            self.runtime.context.failed_move_attempts = 0
        if learned_obstacle is not None and self.navigator.location_name:
            inserted = await self.runtime.remember_map_obstacle(
                self.navigator.location_name,
                learned_obstacle,
            )
            if inserted:
                self.runtime.log(
                    f"Изучено препятствие: {self.navigator.location_name} "
                    f"{learned_obstacle}. Маршрут перестроен локально."
                )


    async def handle_map(
        self,
        event: InboundEvent,
        map_info: MapInfo,
        policy: LegacyMapPolicy,
    ) -> None:
        snapshot = event.snapshot
        revision = LegacyMapStateRevision(
            snapshot.id,
            semantic_fog_text(snapshot.raw_text),
            snapshot.edit_date,
        )
        previous = self._last_state
        if (
            previous is not None
            and previous.message_id == revision.message_id
            and revision.timestamp < previous.timestamp
        ):
            return
        same_state = previous is not None and previous.fact_key == revision.fact_key
        if not same_state:
            await self.observe_map(map_info)
        self._last_state = revision
        if same_state and self.runtime.context.pending_move is not None:
            # A movement acknowledgement is transient. A following keyboard or
            # countdown edit of its source cell is not a failed transition and
            # must not replace the pending route or dispatch another movement.
            return
        if not self.runtime.is_current(event):
            return

        if self.runtime.state is BotState.RECOVERY:
            await self.handle_recovery_map(event, map_info, policy)
            return

        if self.runtime.pause_requested or self.runtime.state is BotState.PAUSED:
            await self.runtime.enter_paused()
            return

        if self.runtime.state in {BotState.RESTING, BotState.ACTIVITY_BREAK}:
            return

        self.runtime.state = BotState.MAP
        if not same_state:
            self.runtime.mark_progress("карта получена")

        if self.runtime.context.failed_move_attempts >= MAX_FAILED_MOVE_ATTEMPTS:
            await self.runtime.stop(
                "игра не выполнила перемещение после проверки всех доступных кнопок"
            )
            return

        if (
            self.runtime.context.checked_empty_position is not None
            and self.runtime.context.checked_empty_position != map_info.position
        ):
            self.runtime.context.checked_empty_position = None

        self.runtime.log(
            f"Карта: позиция {map_info.position}, "
            f"HP: {self.runtime.context.current_hp}/"
            f"{self.runtime.context.max_hp}, "
            f"монстров заявлено: {map_info.monster_count}, "
            f"показано: {list(map_info.monsters) or 'нет'}"
        )

        if self.runtime.battle_health_is_low():
            self.runtime.wait_for_battle_health()
            return

        if await self.runtime.try_refresh_blessing_from_map(event, policy):
            return

        if (
            map_info.found_target is not None
            and self.runtime.context.checked_empty_position == map_info.position
        ):
            self.runtime.log(
                f"Цель «{map_info.found_target}» на клетке {map_info.position} "
                "уже исчезала или была занята; повторное нападение пропущено."
            )

        if (
            map_info.found_target is not None
            and self.runtime.context.checked_empty_position != map_info.position
        ):
            self.runtime.context.checked_empty_position = None

            outcome = await self.runtime.click_button(
                event,
                exact=ATTACK_BUTTON,
                action_type=ActionType.OPEN_ATTACK,
                description=ATTACK_BUTTON,
                delay_range=policy.open_attack_delay,
            )
            if _action_was_committed(outcome):
                self.runtime.context.active_target = map_info.found_target
                self.runtime.state = BotState.TARGET_SELECTION
                self.runtime.mark_progress("открыт список целей")
            return

        if (
            self.runtime.context.checked_empty_position != map_info.position
            and map_info.has_hidden_monsters
        ):
            self.runtime.context.active_target = None

            outcome = await self.runtime.click_button(
                event,
                exact=ATTACK_BUTTON,
                action_type=ActionType.OPEN_ATTACK,
                description=ATTACK_BUTTON,
                delay_range=policy.open_attack_delay,
            )
            if _action_was_committed(outcome):
                self.runtime.state = BotState.TARGET_SELECTION
                self.runtime.mark_progress("открыт полный список целей")
            return

        if (
            self.runtime.moves_in_cycle >= self.runtime.cycle_move_target
            and self.navigator.cycle_can_finish()
        ):
            await self.runtime.complete_cycle()
            return

        if self.runtime.activity_break_is_due():
            await self.runtime.start_activity_break()
            return

        if map_info.movement_finished:
            await self.runtime.pause_after_movement()

        plan = self.navigator.plan(map_info.position)

        outcome = await self.runtime.click_button(
            event,
            exact=plan.button,
            action_type=ActionType.MOVE,
            description=plan.button,
            delay_range=policy.move_delay,
        )
        if _action_was_committed(outcome):
            self.runtime.context.pending_move = plan
            self.runtime.state = BotState.MOVING
            self.runtime.mark_progress("команда перемещения отправлена")
        else:
            self.navigator.cancel_last_plan(plan)


    async def handle_target_selection(
        self,
        observation: LegacyMapObservation,
    ) -> None:
        event = observation.event
        policy = observation.policy
        self.runtime.state = BotState.TARGET_SELECTION
        self.runtime.mark_progress("список целей получен")

        if self.runtime.battle_health_is_low():
            outcome = await self.runtime.click_button(
                event,
                exact=BACK_TO_MAP_BUTTON,
                action_type=ActionType.SELECT_TARGET,
                description=BACK_TO_MAP_BUTTON,
                delay_range=policy.target_selection_delay,
            )
            if _action_was_committed(outcome):
                self.runtime.wait_for_battle_health()
            return

        if self.runtime.pause_requested:
            outcome = await self.runtime.click_button(
                event,
                exact=BACK_TO_MAP_BUTTON,
                action_type=ActionType.SELECT_TARGET,
                description=BACK_TO_MAP_BUTTON,
                delay_range=policy.target_selection_delay,
            )
            _action_was_committed(outcome)
            return

        analysis = analyze_map_targets(
            event.snapshot,
            observation.targets.enabled,
        )
        found_target = analysis.selected_target
        target_counts = analysis.target_counts

        if found_target is not None and analysis.selected_position is not None:
            outcome = await self.runtime.click_button(
                event,
                position=analysis.selected_position,
                action_type=ActionType.SELECT_TARGET,
                description=f"выбор цели {found_target}",
                delay_range=policy.target_selection_delay,
            )

            if _action_was_committed(outcome):
                self.runtime.context.active_target = found_target
                self.runtime.context.battle_target = found_target
                self.runtime.context.checked_empty_position = None
                self.runtime.state = BotState.COMBAT
                self.runtime.mark_progress("цель выбрана")
            return

        self.runtime.context.active_target = None

        # Если в списке были наши мобы, но все они заняты, клетка уже
        # полностью проверена. То же самое относится к проверке скрытых
        # монстров. После возврата на карту нужно перейти дальше, а не
        # снова открывать тот же список целей.
        all_matching_targets_are_occupied = bool(target_counts) and all(
            found > 0 and occupied >= found for found, occupied in target_counts.values()
        )

        # The immutable target list is complete for this inbound event. Reopening
        # the same prompt cannot reveal more data and can only create a request loop.
        if self.runtime.context.current_position is not None:
            self.runtime.context.checked_empty_position = self.runtime.context.current_position

            if all_matching_targets_are_occupied:
                occupied_summary = ", ".join(
                    f"{target}: {occupied}/{found}"
                    for target, (found, occupied) in target_counts.items()
                )
                self.runtime.log(
                    "Все подходящие цели на клетке заняты. "
                    f"Клетка {self.runtime.context.current_position} "
                    "помечена как проверенная. "
                    f"Занято: {occupied_summary}"
                )

        outcome = await self.runtime.click_button(
            event,
            exact=BACK_TO_MAP_BUTTON,
            action_type=ActionType.SELECT_TARGET,
            description=BACK_TO_MAP_BUTTON,
            delay_range=policy.target_selection_delay,
        )
        if _action_was_committed(outcome):
            self.runtime.mark_progress("возврат к карте")


    async def handle_target_gone(self) -> None:
        """Штатно восстанавливает карту, если выбранный моб уже исчез."""
        disappeared_target = self.runtime.context.active_target or "неизвестная цель"

        self.runtime.context.active_target = None
        self.runtime.context.checked_empty_position = self.runtime.context.current_position
        self.runtime.context.pending_move = None
        self.runtime.context.failed_move_attempts = 0

        self.runtime.state = BotState.MAP
        self.runtime.mark_progress("цель исчезла до начала боя")

        await self.runtime.record_map_event(
            "TARGET_GONE",
            f"Монстр «{disappeared_target}» исчез с текущей клетки",
        )
        self.runtime.log(
            f"Монстр «{disappeared_target}» исчез с клетки. Обновляю карту и продолжаю маршрут."
        )

        await self.runtime.request_current_state()


    async def handle_recovery_map(
        self,
        event: InboundEvent,
        map_info: MapInfo,
        policy: LegacyMapPolicy,
    ) -> None:
        elapsed = self.runtime.recovery_elapsed()
        if elapsed < self.runtime.recovery_minimum_wait():
            return
        current_hp = map_info.current_hp or 0
        self.runtime.log(
            f"Проверка восстановления: "
            f"HP {current_hp}/{map_info.max_hp}, "
            f"прошло {int(elapsed)} сек."
        )
        if current_hp >= self.runtime.recovery_minimum_hp():
            self.runtime.state = BotState.MAP
            await self.runtime.finish_health_recovery(current_hp, map_info.max_hp)
            await self.handle_map(event, map_info, policy)
        else:
            # The map can be newer than the preceding HP notification. Wait
            # for another inbound health update instead of polling Telegram.
            self.runtime.recovery_refresh_requested = False
