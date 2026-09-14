from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from automation_policy import DelayRange, IntegerRange, LegacyMapPolicy
from blessing import NON_COMBAT_SKILLS_BUTTON
from discovery import (
    DiscoveryController,
    DiscoveryEventKind,
    DiscoveryStatus,
)
from farmer import Farmer
from game_input import (
    ActionOutcome,
    AdmissionDecision,
    InboundEvent,
    InputDescriptor,
)
from game_mechanisms import (
    CycleDescriptor,
    MechanismServices,
    MechanismSnapshot,
)
from legacy_map_controller import LegacyMapController, LegacyMapRuntime
from liveness import LivenessPhase
from message_snapshot import ButtonSnapshot, MessageSnapshot
from models import ActionType, BotState
from notifications import Notifier
from parser import parse_map
from settings_service import SettingsService
from storage import Storage
from supervisor import FarmerSupervisor
from tests.map_runtime_harness import MapRuntimeHarness


@dataclass(frozen=True)
class Message:
    raw_text: str
    buttons: tuple[tuple[ButtonSnapshot, ...], ...] = ()
    id: int = 42
    edit_date: datetime | None = None

    async def click(self, row: int, column: int) -> object:
        raise AssertionError("Map tests must use the runtime action port")


def controller(runtime: LegacyMapRuntime) -> LegacyMapController:
    return LegacyMapController(
        runtime, character_name="Игрок", min_x=0, max_x=8, min_y=0, max_y=8
    )


class LegacyMapControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_move_ack_does_not_turn_ui_edit_into_failed_movement(self) -> None:
        for update in ("keyboard", "countdown"):
            with self.subTest(update=update):
                runtime = MapRuntimeHarness()
                legacy = controller(runtime)
                created = datetime(2026, 9, 14, tzinfo=UTC)
                first = Message(
                    "Позиция: (8, 0)\nМонстры на клетке: 0\n⏳ Осталось: 20 сек.",
                    ((ButtonSnapshot("⬅️"),),),
                    edit_date=created,
                )
                observation = legacy.observe_message(runtime.capture(first))
                assert observation is not None
                await legacy.handle_message(observation)
                pending = runtime.context.pending_move
                assert pending is not None
                ack = Message("Шаг начат", id=43)
                progress = legacy.observe_message(runtime.capture(ack))
                assert progress is not None
                await legacy.handle_message(progress)
                edited = replace(first, edit_date=created + timedelta(seconds=1))
                if update == "keyboard":
                    edited = replace(edited, buttons=((ButtonSnapshot("↙️"),),))
                else:
                    edited = replace(edited, raw_text=edited.raw_text.replace("20 сек", "19 сек"))
                repeated = legacy.observe_message(runtime.capture(edited))
                assert repeated is not None
                await legacy.handle_message(repeated)
                self.assertIs(runtime.context.pending_move, pending)
                self.assertEqual(runtime.context.failed_move_attempts, 0)
                self.assertEqual(runtime.context.move_count, 0)
                self.assertEqual(len(runtime.actions), 1)
                self.assertEqual(runtime.state, BotState.MOVING)

                x, y = pending.destination
                arrived = replace(
                    first,
                    raw_text=f"Позиция: ({x}, {y})\nМонстры на клетке: 0",
                    edit_date=created + timedelta(seconds=2),
                )
                arrival = legacy.observe_message(runtime.capture(arrived))
                assert arrival is not None
                await legacy.handle_message(arrival)
                self.assertEqual(runtime.context.current_position, pending.destination)
                self.assertEqual(runtime.context.move_count, 1)
                self.assertEqual(len(runtime.actions), 2)

    async def test_target_menu_roundtrip_allows_leaving_the_same_checked_cell(self) -> None:
        runtime = MapRuntimeHarness(targets=("Бронзовик",))
        legacy = controller(runtime)
        first = Message("Позиция: (8, 0)\nМонстры на клетке: 1 (Бронзовик)")
        observation = legacy.observe_message(runtime.capture(first))
        assert observation is not None
        await legacy.handle_message(observation)
        menu = Message(
            "Выбери цель для нападения",
            ((ButtonSnapshot("Бронзовик [10/10] занят"),),),
            id=43,
        )
        selection = legacy.observe_message(runtime.capture(menu))
        assert selection is not None
        await legacy.handle_message(selection)
        returned = legacy.observe_message(runtime.capture(first))
        assert returned is not None
        await legacy.handle_message(returned)
        self.assertEqual(runtime.context.checked_empty_position, (8, 0))
        self.assertEqual(
            [action[0] for action in runtime.actions],
            [ActionType.OPEN_ATTACK, ActionType.SELECT_TARGET, ActionType.MOVE],
        )
        self.assertIsNotNone(runtime.context.pending_move)

    async def test_older_map_revision_cannot_confirm_or_replace_a_pending_move(self) -> None:
        runtime = MapRuntimeHarness()
        legacy = controller(runtime)
        updated = datetime(2026, 9, 14, tzinfo=UTC)
        first = Message("Позиция: (8, 0)\nМонстры на клетке: 0", edit_date=updated)
        observation = legacy.observe_message(runtime.capture(first))
        assert observation is not None
        await legacy.handle_message(observation)
        pending = runtime.context.pending_move
        assert pending is not None
        x, y = pending.destination
        older = replace(
            first,
            raw_text=f"Позиция: ({x}, {y})\nМонстры на клетке: 0",
            edit_date=updated - timedelta(seconds=1),
        )
        stale = legacy.observe_message(runtime.capture(older))
        assert stale is not None
        await legacy.handle_message(stale)
        self.assertEqual(runtime.context.current_position, (8, 0))
        self.assertIs(runtime.context.pending_move, pending)
        self.assertEqual(runtime.context.move_count, 0)
        self.assertEqual(len(runtime.actions), 1)

    async def test_stale_map_confirms_facts_without_dispatching_an_action(self) -> None:
        runtime = MapRuntimeHarness(latest=False)
        legacy = controller(runtime)
        plan = legacy.navigator.plan((8, 0))
        runtime.context.pending_move = plan
        runtime.context.checked_empty_position = plan.origin
        x, y = plan.destination
        message = Message(f"Позиция: ({x}, {y})\nМонстры на клетке: 0")
        with patch("legacy_map_controller.parse_map", wraps=parse_map) as parse:
            observation = legacy.observe_message(
                runtime.capture(MessageSnapshot.from_message(message))
            )
            assert observation is not None
            self.assertTrue(await legacy.handle_message(observation))
            parse.assert_called_once()
        self.assertEqual(runtime.context.current_position, plan.destination)
        self.assertEqual(runtime.context.move_count, 1)
        self.assertEqual(runtime.moves_in_cycle, 1)
        self.assertIsNone(runtime.context.pending_move)
        self.assertIsNone(runtime.context.checked_empty_position)
        self.assertEqual(runtime.actions, [])

    async def test_ui_only_map_does_not_reject_a_pending_move(self) -> None:
        runtime = MapRuntimeHarness(latest=False)
        legacy = controller(runtime)
        message = Message("Позиция: (8, 0)\nМонстры на клетке: 0")
        observation = legacy.observe_message(runtime.capture(message))
        assert observation is not None
        await legacy.handle_message(observation)
        plan = legacy.navigator.plan((8, 0))
        runtime.context.pending_move = plan
        edited = Message(message.raw_text, ((ButtonSnapshot("new keyboard"),),))
        repeated = legacy.observe_message(runtime.capture(edited))
        assert repeated is not None
        await legacy.handle_message(repeated)
        self.assertIs(runtime.context.pending_move, plan)
        self.assertEqual(runtime.context.failed_move_attempts, 0)
        self.assertEqual(runtime.actions, [])

    async def test_target_selection_preserves_exact_verified_button(self) -> None:
        runtime = MapRuntimeHarness(targets=("Бронзовик",))
        legacy = controller(runtime)
        message = Message(
            "Выбери цель для нападения",
            ((ButtonSnapshot("Золотой бронзовик [10/20]"),),
             (ButtonSnapshot("Бронзовик [20/20]"),)),
        )
        observation = legacy.observe_message(runtime.capture(message))
        assert observation is not None
        self.assertEqual(observation.kind, DiscoveryEventKind.TARGET_SELECTION)
        await legacy.handle_message(observation)
        self.assertEqual(runtime.actions, [(ActionType.SELECT_TARGET, None, (1, 0))])
        self.assertEqual(runtime.context.battle_target, "Бронзовик")
        self.assertEqual(runtime.state, BotState.COMBAT)

    async def test_uncertain_or_duplicate_actions_advance_without_repeating_rpc(self) -> None:
        for outcome in (ActionOutcome.DELIVERY_UNKNOWN, ActionOutcome.DUPLICATE):
            with self.subTest(outcome=outcome, action="move"):
                runtime = MapRuntimeHarness(click_result=outcome)
                legacy = controller(runtime)
                observation = legacy.observe_message(
                    runtime.capture(Message("Позиция: (8, 0)\nМонстры на клетке: 0"))
                )
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertIsNotNone(runtime.context.pending_move)
                self.assertEqual(runtime.state, BotState.MOVING)
                await legacy.handle_message(observation)
                self.assertEqual(len(runtime.actions), 1)

            with self.subTest(outcome=outcome, action="open attack"):
                runtime = MapRuntimeHarness(targets=("Бронзовик",), click_result=outcome)
                legacy = controller(runtime)
                observation = legacy.observe_message(
                    runtime.capture(
                        Message(
                            "Позиция: (8, 0)\n"
                            "Монстры на клетке: 1 (Бронзовик)"
                        )
                    )
                )
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertEqual(runtime.context.active_target, "Бронзовик")
                self.assertEqual(runtime.state, BotState.TARGET_SELECTION)
                self.assertEqual(len(runtime.actions), 1)

            with self.subTest(outcome=outcome, action="select target"):
                runtime = MapRuntimeHarness(targets=("Бронзовик",), click_result=outcome)
                legacy = controller(runtime)
                observation = legacy.observe_message(
                    runtime.capture(
                        Message(
                            "Выбери цель для нападения",
                            ((ButtonSnapshot("Бронзовик [10/10]"),),),
                        )
                    )
                )
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertEqual(runtime.context.battle_target, "Бронзовик")
                self.assertEqual(runtime.state, BotState.COMBAT)
                self.assertEqual(len(runtime.actions), 1)

    async def test_noncommitted_callbacks_do_not_publish_intent_or_start_recovery(self) -> None:
        for outcome in (
            ActionOutcome.DEFERRED,
            ActionOutcome.STALE,
            ActionOutcome.REJECTED,
        ):
            with self.subTest(outcome=outcome, action="move"):
                runtime = MapRuntimeHarness(click_result=outcome)
                legacy = controller(runtime)
                observation = legacy.observe_message(
                    runtime.capture(Message("Позиция: (8, 0)\nМонстры на клетке: 0"))
                )
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertIsNone(runtime.context.pending_move)
                self.assertEqual(runtime.state, BotState.MAP)
                self.assertEqual(runtime.requests, [])

            with self.subTest(outcome=outcome, action="open attack"):
                runtime = MapRuntimeHarness(targets=("Бронзовик",), click_result=outcome)
                legacy = controller(runtime)
                observation = legacy.observe_message(
                    runtime.capture(
                        Message(
                            "Позиция: (8, 0)\n"
                            "Монстры на клетке: 1 (Бронзовик)"
                        )
                    )
                )
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertIsNone(runtime.context.active_target)
                self.assertEqual(runtime.state, BotState.MAP)
                self.assertEqual(runtime.requests, [])

            with self.subTest(outcome=outcome, action="select target"):
                runtime = MapRuntimeHarness(targets=("Бронзовик",), click_result=outcome)
                legacy = controller(runtime)
                observation = legacy.observe_message(
                    runtime.capture(
                        Message(
                            "Выбери цель для нападения",
                            ((ButtonSnapshot("Бронзовик [10/10]"),),),
                        )
                    )
                )
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertIsNone(runtime.context.battle_target)
                self.assertEqual(runtime.state, BotState.TARGET_SELECTION)
                self.assertEqual(runtime.requests, [])

            with self.subTest(outcome=outcome, action="return to map"):
                runtime = MapRuntimeHarness(click_result=outcome)
                runtime.context.current_position = (8, 0)
                legacy = controller(runtime)
                observation = legacy.observe_message(
                    runtime.capture(
                        Message(
                            "Выбери цель для нападения",
                            ((ButtonSnapshot("↩️ К карте"),),),
                        )
                    )
                )
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertEqual(runtime.requests, [])
                self.assertEqual(len(runtime.actions), 1)

    async def test_map_and_target_callbacks_use_their_own_delay_ranges(self) -> None:
        policy = LegacyMapPolicy(
            moves_per_cycle=IntegerRange(1, 10),
            blessing_enabled=False,
            move_delay=DelayRange(1, 2),
            open_attack_delay=DelayRange(3, 4),
            target_selection_delay=DelayRange(5, 6),
        )
        runtime = MapRuntimeHarness(targets=("Бронзовик",), policy=policy)
        legacy = controller(runtime)
        map_observation = legacy.observe_message(
            runtime.capture(
                Message("Позиция: (8, 0)\nМонстры на клетке: 1 (Бронзовик)")
            )
        )
        assert map_observation is not None
        await legacy.handle_message(map_observation)
        target_observation = legacy.observe_message(
            runtime.capture(
                Message(
                    "Выбери цель для нападения",
                    ((ButtonSnapshot("Бронзовик [10/10]"),),),
                )
            )
        )
        assert target_observation is not None
        await legacy.handle_message(target_observation)
        self.assertEqual(
            runtime.action_delays,
            [policy.open_attack_delay, policy.target_selection_delay],
        )

    async def test_unknown_blessing_open_consumes_map_without_movement(self) -> None:
        policy = LegacyMapPolicy(
            moves_per_cycle=IntegerRange(1, 10),
            blessing_enabled=True,
            move_delay=DelayRange(1, 2),
            open_attack_delay=DelayRange(3, 4),
            target_selection_delay=DelayRange(5, 6),
        )
        runtime = MapRuntimeHarness(policy=policy, click_result=ActionOutcome.DELIVERY_UNKNOWN)
        legacy = controller(runtime)
        observation = legacy.observe_message(
            runtime.capture(
                Message(
                    "Позиция: (8, 0)\nМонстры на клетке: 0",
                    ((ButtonSnapshot(NON_COMBAT_SKILLS_BUTTON),),),
                )
            )
        )
        assert observation is not None
        await legacy.handle_message(observation)
        self.assertEqual(
            runtime.actions,
            [(ActionType.OPEN_ATTACK, NON_COMBAT_SKILLS_BUTTON, None)],
        )
        self.assertEqual(runtime.action_delays, [policy.open_attack_delay])
        self.assertTrue(runtime.blessing.refresh_in_progress)
        self.assertIsNone(runtime.context.pending_move)

    async def test_observation_keeps_snapshot_after_source_mutation(self) -> None:
        source = Message("Позиция: (8, 0)\nМонстры на клетке: 0")
        runtime = MapRuntimeHarness()
        legacy = controller(runtime)
        observation = legacy.observe_message(runtime.capture(source))
        assert observation is not None
        object.__setattr__(source, "raw_text", "Выбери цель для нападения")
        await legacy.handle_message(observation)
        self.assertEqual(observation.snapshot.raw_text, "Позиция: (8, 0)\nМонстры на клетке: 0")
        self.assertEqual(runtime.actions[0][0], ActionType.MOVE)

    async def test_invalid_runtime_action_outcome_is_rejected(self) -> None:
        runtime = MapRuntimeHarness()
        runtime.click_result = True  # type: ignore[assignment]
        legacy = controller(runtime)
        observation = legacy.observe_message(
            runtime.capture(Message("Позиция: (8, 0)\nМонстры на клетке: 0"))
        )
        assert observation is not None
        with self.assertRaisesRegex(ValueError, "ActionOutcome"):
            await legacy.handle_message(observation)

    async def test_disappeared_target_marks_cell_and_requests_state_through_host(self) -> None:
        runtime = MapRuntimeHarness()
        runtime.context.current_position = (3, 4)
        runtime.context.active_target = "Бронзовик"
        legacy = controller(runtime)
        message = Message("Монстр не найден на текущей клетке")
        observation = legacy.observe_message(runtime.capture(message))
        assert observation is not None
        await legacy.handle_message(observation)
        self.assertIsNone(runtime.context.active_target)
        self.assertEqual(runtime.context.checked_empty_position, (3, 4))
        self.assertEqual(runtime.requests, [(False, None)])
        self.assertEqual(runtime.events[0][0], "TARGET_GONE")

    async def test_raw_request_delegates_command_and_label_without_guards(self) -> None:
        runtime = MapRuntimeHarness()
        discovery: DiscoveryController = controller(runtime)
        self.assertEqual(await discovery.request_state(), ActionOutcome.SENT)
        self.assertEqual(runtime.raw_messages, [("Карта", "map_message")])
        self.assertEqual(runtime.requests, [])

    async def test_recovery_map_resumes_only_after_confirmed_health(self) -> None:
        for hp in (50, 100):
            with self.subTest(hp=hp):
                runtime = MapRuntimeHarness(
                    state=BotState.RECOVERY, recovery_refresh_requested=True
                )
                legacy = controller(runtime)
                message = Message(
                    f"Позиция: (8, 0)\nМонстры на клетке: 0\nИгрок\n❤️ {hp}/400"
                )
                observation = legacy.observe_message(runtime.capture(message))
                assert observation is not None
                await legacy.handle_message(observation)
                self.assertFalse(runtime.recovery_refresh_requested)
                if hp < 100:
                    self.assertEqual(runtime.state, BotState.RECOVERY)
                    self.assertEqual(runtime.actions, [])
                else:
                    self.assertEqual(runtime.state, BotState.MOVING)
                    self.assertEqual(len(runtime.actions), 1)
                    self.assertEqual(runtime.events[0][0], "RECOVERY_FINISHED")

    async def test_status_exposes_confirmed_moves_and_reset_keeps_their_total(self) -> None:
        runtime = MapRuntimeHarness()
        runtime.context.move_count = 7
        runtime.context.current_position = (8, 0)
        legacy = controller(runtime)
        legacy.navigator.visited_positions.update({(7, 0), (6, 0)})
        legacy.reset_cycle()
        self.assertEqual(legacy.navigator.visited_positions, {(8, 0)})
        self.assertEqual(legacy.status(), DiscoveryStatus(None, 7, "перемещений"))

    async def test_combat_messages_remain_outside_discovery(self) -> None:
        runtime = MapRuntimeHarness()
        legacy = controller(runtime)
        self.assertIsNone(legacy.observe_message(runtime.capture(Message("Выберите навык:"))))
        self.assertIsNone(legacy.observe_message(runtime.capture(Message("Бой завершён. Победа"))))


class AlternativeInputPolicy:
    def describe(self, snapshot: MessageSnapshot) -> InputDescriptor:
        fact_key = ("future", snapshot.id, snapshot.raw_text)
        return InputDescriptor(snapshot, fact_key, (fact_key, snapshot.buttons), False)

    def admit(
        self,
        previous: InputDescriptor | None,
        current_prompt: InputDescriptor | None,
        incoming: InputDescriptor,
    ) -> AdmissionDecision:
        return AdmissionDecision.ACCEPT


class AlternativeRuntime:
    def __init__(self, policy: AlternativeInputPolicy) -> None:
        self._policy = policy
        self.services: MechanismServices | None = None
        self.initializations = 0
        self.closes = 0
        self.cycle_numbers: list[int] = []
        self.handled: list[InboundEvent] = []
        self.requests = 0
        self._cycle: CycleDescriptor | None = None

    @property
    def input_policy(self) -> AlternativeInputPolicy:
        return self._policy

    def validate(self) -> None:
        return None

    async def initialize(self) -> None:
        self.initializations += 1

    def start_cycle(self, cycle_number: int) -> CycleDescriptor:
        if self.initializations != 1:
            raise AssertionError("cycle started outside initialized lifetime")
        self.cycle_numbers.append(cycle_number)
        self._cycle = CycleDescriptor(7, 5, 9, "поисков")
        return self._cycle

    async def handle(self, event: InboundEvent) -> bool:
        self.handled.append(event)
        return True

    async def request_state(self) -> ActionOutcome:
        self.requests += 1
        return ActionOutcome.SENT

    def snapshot(self) -> MechanismSnapshot:
        return MechanismSnapshot(
            phase_name="DISCOVERY",
            position=None,
            location_name="future-zone",
            current_hp=None,
            max_hp=None,
            active_target=None,
            total_progress_units=len(self.handled),
            cycle_progress_units=len(self.handled),
            liveness_phase=LivenessPhase.GENERAL,
            liveness_suspended=False,
        )

    def cycle_descriptor(self) -> CycleDescriptor | None:
        return self._cycle

    def diagnostics(self) -> dict[str, str]:
        return {"implementation": "future"}

    def session_elapsed_seconds(self) -> int:
        return 0

    def format_session_report(self, title: str) -> str:
        return title

    async def aclose(self) -> None:
        self.closes += 1


class LifecycleStrictAlternativeRuntime(AlternativeRuntime):
    """Runtime whose readable state exists only between initialize and aclose."""

    def __init__(self, policy: AlternativeInputPolicy) -> None:
        super().__init__(policy)
        self.closed = False
        self.read_calls = 0

    def _assert_readable(self) -> None:
        if self.initializations != 1 or self.closed:
            raise AssertionError("runtime state read outside initialized lifetime")
        self.read_calls += 1

    def snapshot(self) -> MechanismSnapshot:
        self._assert_readable()
        return super().snapshot()

    def cycle_descriptor(self) -> CycleDescriptor | None:
        self._assert_readable()
        return super().cycle_descriptor()

    def session_elapsed_seconds(self) -> int:
        self._assert_readable()
        return super().session_elapsed_seconds()

    def format_session_report(self, title: str) -> str:
        self._assert_readable()
        return super().format_session_report(title)

    async def aclose(self) -> None:
        self.closes += 1
        self.closed = True


class AlternativeBundle:
    def __init__(self, runtime: AlternativeRuntime) -> None:
        self.runtime = runtime
        self.validations = 0
        self.builds = 0

    def validate(self) -> None:
        self.validations += 1

    def build(self, services: MechanismServices) -> AlternativeRuntime:
        self.builds += 1
        self.runtime.services = services
        return self.runtime


class DiscoveryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_alternative_phase_is_independent_from_legacy_bot_states(self) -> None:
        storage = Storage(Path(":memory:"))
        runtime = AlternativeRuntime(AlternativeInputPolicy())
        farmer = Farmer(
            storage,
            AsyncMock(spec=Notifier),
            SettingsService(storage),
            mechanism_bundle=AlternativeBundle(runtime),
            client=MagicMock(),
        )
        try:
            services = runtime.services
            assert services is not None
            services.set_state_name("DISCOVERY")

            self.assertEqual(services.state_name(), "DISCOVERY")
            self.assertIs(farmer.state, BotState.STARTING)
            await storage.update_state(**farmer._state_snapshot("one-button search"))
            state = await storage.get_state()
            self.assertEqual(state["game_state"], "DISCOVERY")

            for application_state in (
                BotState.PAUSED,
                BotState.RESTING,
                BotState.ACTIVITY_BREAK,
                BotState.STOPPED,
            ):
                farmer.state = application_state
                services.set_state_name("DISCOVERY")
                self.assertIs(farmer.state, application_state)
                self.assertEqual(services.state_name(), application_state.name)
                self.assertEqual(
                    farmer._state_snapshot("application lifecycle")["game_state"],
                    application_state.name,
                )

            with self.assertRaises(ValueError):
                services.set_state_name(" DISCOVERY ")
        finally:
            await farmer.stop("test cleanup")
            await storage.close()

    async def test_lifecycle_strict_runtime_is_never_read_before_init_or_after_close(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "strict-runtime.sqlite3")
            settings = SettingsService(storage)
            await settings.load()
            policy = AlternativeInputPolicy()
            runtime = LifecycleStrictAlternativeRuntime(policy)
            bundle = AlternativeBundle(runtime)
            client = MagicMock()
            client.connect = AsyncMock()
            client.is_user_authorized = AsyncMock(return_value=True)
            client.get_input_entity = AsyncMock(return_value="future-peer")
            client.disconnected = asyncio.get_running_loop().create_future()
            entered = asyncio.Event()
            client.add_event_handler.side_effect = lambda *args: entered.set()
            notifier = AsyncMock(spec=Notifier)
            farmer = Farmer(
                storage,
                notifier,
                settings,
                mechanism_bundle=bundle,
                client=client,
            )
            supervisor = FarmerSupervisor(
                storage,
                notifier,
                settings,
                mechanism_bundle_factory=lambda: bundle,
                client_factory=lambda: client,
            )
            supervisor.farmer = farmer
            runner: asyncio.Task[None] | None = None
            try:
                before = await supervisor.status()
                self.assertIsNone(before["location_name"])
                self.assertEqual(runtime.read_calls, 0)

                with (
                    patch("farmer.API_ID", 1),
                    patch("farmer.API_HASH", "test-hash"),
                    patch("farmer.GAME_BOT", "@future_game"),
                ):
                    runner = asyncio.create_task(farmer.run())
                    await asyncio.wait_for(entered.wait(), timeout=1)
                    active = await supervisor.status()
                    self.assertEqual(active["location_name"], "future-zone")
                    await farmer.stop("strict lifecycle")
                    await asyncio.wait_for(runner, timeout=1)

                self.assertTrue(runtime.closed)
                reads_at_close = runtime.read_calls
                after = await supervisor.status()
                self.assertEqual(after["location_name"], "future-zone")
                self.assertEqual(
                    supervisor._completion_progress(farmer),
                    ("Поисков", 0),
                )
                self.assertEqual(runtime.read_calls, reads_at_close)
                self.assertTrue(farmer.shutdown_complete)
            finally:
                if runner is not None and not runner.done():
                    await farmer.stop("test cleanup")
                    await runner
                elif not farmer.shutdown_complete:
                    await farmer.stop("test cleanup")
                await storage.close()

    async def test_alternative_bundle_runs_without_constructing_legacy_mechanisms(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "discovery-test.sqlite3")
            settings = SettingsService(storage)
            policy = AlternativeInputPolicy()
            runtime = AlternativeRuntime(policy)
            bundle = AlternativeBundle(runtime)
            client = MagicMock()
            client.connect = AsyncMock()
            client.disconnect = AsyncMock()
            client.is_user_authorized = AsyncMock(return_value=True)
            client.get_input_entity = AsyncMock(return_value="future-peer")
            entered = asyncio.Event()

            client.disconnected = asyncio.get_running_loop().create_future()
            client.add_event_handler.side_effect = lambda *args: entered.set()
            notifier = AsyncMock(spec=Notifier)
            poison = AssertionError("legacy mechanism must not be used")
            farmer: Farmer | None = None
            session: asyncio.Task[None] | None = None
            try:
                with (
                    patch("tests.legacy_fog_factory.create_test_client", return_value=client),
                    patch("farmer.API_ID", 1),
                    patch("farmer.API_HASH", "test-hash"),
                    patch("farmer.GAME_BOT", "@future_game"),
                    patch.object(settings, "target_policy", side_effect=poison),
                    patch.object(settings, "legacy_map_policy", side_effect=poison),
                    patch.object(settings, "legacy_combat_policy", side_effect=poison),
                ):
                    farmer = Farmer(
                        storage,
                        notifier,
                        settings,
                        mechanism_bundle=bundle,
                        client=client,
                    )
                    self.assertEqual(runtime.cycle_numbers, [])
                    self.assertEqual(bundle.builds, 1)
                    session = asyncio.create_task(farmer._run_session())
                    await asyncio.wait_for(entered.wait(), timeout=1)
                    self.assertEqual(runtime.cycle_numbers, [1])

                    source = Message("future input")
                    await farmer.enqueue_message(source)
                    object.__setattr__(source, "raw_text", "mutated transport")
                    await asyncio.wait_for(farmer.ingress.join(), timeout=1)
                    await farmer.rest_between_cycles(0)
                    self.assertEqual(runtime.cycle_numbers, [1, 2])
                    client.disconnected.set_result(None)
                    await asyncio.wait_for(session, timeout=1)

                self.assertIs(farmer.input_policy, policy)
                self.assertIs(farmer.mechanisms, runtime)
                self.assertFalse(hasattr(farmer, "battle_notifications"))
                self.assertFalse(
                    any(
                        task.get_name() == "battle-card-notifications"
                        for task in farmer.task_scope.snapshot()
                    )
                )
                assert runtime.services is not None
                self.assertFalse(hasattr(runtime.services, "farmer"))
                self.assertFalse(hasattr(runtime.services, "client"))
                self.assertFalse(hasattr(runtime.services, "storage"))
                self.assertFalse(hasattr(runtime.services, "settings"))
                self.assertEqual(runtime.initializations, 1)
                client.connect.assert_awaited_once()
                self.assertTrue(client.disconnected.done())
                state = await storage.get_state()
                self.assertEqual(state["moves_per_cycle"], 7)

                self.assertEqual(len(runtime.handled), 1)
                event = runtime.handled[0]
                self.assertEqual(event.snapshot.raw_text, "future input")
                self.assertFalse(hasattr(event, "click"))
                self.assertFalse(hasattr(event.snapshot, "click"))
            finally:
                if not client.disconnected.done():
                    client.disconnected.set_result(None)
                if session is not None and not session.done():
                    await session
                if farmer is not None:
                    await farmer.stop("test cleanup")
                    self.assertEqual(runtime.closes, 1)
                await storage.close()

    async def test_farmer_rejects_individual_mechanism_knobs(self) -> None:
        storage = Storage(Path(":memory:"))
        try:
            with self.assertRaises(TypeError):
                Farmer(
                    storage,
                    MagicMock(spec=Notifier),
                    SettingsService(storage),
                    input_policy=AlternativeInputPolicy(),
                )
        finally:
            await storage.close()

    def test_farmer_supervisor_and_contracts_import_with_legacy_blocked(self) -> None:
        project = Path(__file__).resolve().parents[1]
        script = """
import importlib.abc
import sys

class BlockLegacyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('legacy_') or fullname in {
            'farmer_map_runtime', 'farmer_combat_runtime', 'fog_input', 'parser',
            'combat', 'discovery', 'skills', 'models', 'battle_notification_outbox'
        }:
            raise AssertionError('Legacy module imported: ' + fullname)
        return None

sys.meta_path.insert(0, BlockLegacyImports())
sys.path.insert(0, sys.argv[1])
import game_mechanisms
import farmer
import supervisor
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(project)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_legacy_module_does_not_import_runtime_or_combat_rules(self) -> None:
        project = Path(__file__).resolve().parents[1]
        script = """
import importlib.abc
import sys

class BlockRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'telethon', 'farmer', 'combat_rules', 'storage'}:
            raise AssertionError('Legacy adapter imported runtime dependency: ' + fullname)
        return None

sys.meta_path.insert(0, BlockRuntimeImports())
sys.path.insert(0, sys.argv[1])
import legacy_map_controller
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(project)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
