from __future__ import annotations

from collections.abc import Iterable
from weakref import WeakSet

from models import MovePlan, Position, RouteDirection


_active_location_name: str | None = None
_pending_blocked_report: tuple[Position, Position | None] | None = None
_instances: WeakSet["SnakeNavigator"] = WeakSet()


def activate_location(location_name: str | None) -> None:
    """Переключает все навигаторы на геометрию карты из сообщения."""
    global _active_location_name
    if not location_name or location_name == _active_location_name:
        return
    _active_location_name = location_name
    for navigator in tuple(_instances):
        navigator.use_location(location_name)


def report_blocked_transition(current_position: Position) -> None:
    """
    Сообщает навигатору, что последний запланированный переход отклонён.
    Вызывается парсером до обработки карты в Farmer.
    """
    global _pending_blocked_report
    _pending_blocked_report = (current_position, None)
    for navigator in tuple(_instances):
        navigator.reject_last_plan(current_position)


class SnakeNavigator:
    """
    Навигатор по прямоугольной карте с препятствиями.

    Для обычных локаций сохраняется прежняя змейка 9x9.
    Для Мёртвого леса строится непрерывный DFS-маршрут, посещающий
    все доступные клетки. Возвраты по уже посещённым клеткам допустимы.
    """

    def __init__(
        self,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
    ) -> None:
        self.default_bounds = (min_x, max_x, min_y, max_y)
        self.direction = RouteDirection.DOWN
        self.route_index = 0
        self.last_plan: MovePlan | None = None
        self.runtime_blocked: set[Position] = set()
        self.location_name: str | None = None
        _instances.add(self)

        if _active_location_name:
            self.use_location(_active_location_name)
        else:
            self._configure(
                min_x=min_x,
                max_x=max_x,
                min_y=min_y,
                max_y=max_y,
                start_position=(max_x, min_y),
                blocked_cells=frozenset(),
                obstacle_mode=False,
            )

    def use_location(self, location_name: str) -> None:
        from locations import get_location

        location = get_location(location_name)
        self.location_name = location_name
        self.runtime_blocked.clear()
        self.direction = RouteDirection.DOWN
        self.last_plan = None
        self._configure(
            min_x=location.min_x,
            max_x=location.max_x,
            min_y=location.min_y,
            max_y=location.max_y,
            start_position=location.start_position,
            blocked_cells=location.blocked_cells,
            obstacle_mode=bool(location.blocked_cells),
        )

    def _configure(
        self,
        *,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
        start_position: Position,
        blocked_cells: frozenset[Position],
        obstacle_mode: bool,
    ) -> None:
        if min_x > max_x or min_y > max_y:
            raise ValueError("Некорректные границы карты.")

        self.min_x = min_x
        self.max_x = max_x
        self.min_y = min_y
        self.max_y = max_y
        self.start = start_position
        self.blocked_cells = blocked_cells
        self.obstacle_mode = obstacle_mode

        self.route = self._build_route()
        self.position_to_indices = self._index_route(self.route)
        self.route_index = self._nearest_index(
            self.start,
            fallback=0,
        )

    def _inside(self, position: Position) -> bool:
        x, y = position
        return (
            self.min_x <= x <= self.max_x
            and self.min_y <= y <= self.max_y
        )

    def _available(self, position: Position) -> bool:
        return (
            self._inside(position)
            and position not in self.blocked_cells
            and position not in self.runtime_blocked
        )

    def _neighbors(self, position: Position) -> tuple[Position, ...]:
        x, y = position
        if (y - self.min_y) % 2 == 0:
            candidates = (
                (x + 1, y),
                (x, y + 1),
                (x - 1, y),
                (x, y - 1),
            )
        else:
            candidates = (
                (x - 1, y),
                (x, y + 1),
                (x + 1, y),
                (x, y - 1),
            )
        return tuple(
            candidate
            for candidate in candidates
            if self._available(candidate)
        )

    def _build_route(self) -> tuple[Position, ...]:
        if not self.obstacle_mode:
            route: list[Position] = []
            for y in range(self.min_y, self.max_y + 1):
                row_number = y - self.min_y
                if row_number % 2 == 0:
                    x_values = range(
                        self.max_x,
                        self.min_x - 1,
                        -1,
                    )
                else:
                    x_values = range(
                        self.min_x,
                        self.max_x + 1,
                    )
                route.extend((x, y) for x in x_values)
            return tuple(route)

        if not self._available(self.start):
            raise ValueError(
                f"Стартовая клетка {self.start} недоступна."
            )

        visited: set[Position] = {self.start}
        route: list[Position] = [self.start]

        def visit(position: Position) -> None:
            for neighbor in self._neighbors(position):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                route.append(neighbor)
                visit(neighbor)
                route.append(position)

        visit(self.start)

        expected = {
            (x, y)
            for y in range(self.min_y, self.max_y + 1)
            for x in range(self.min_x, self.max_x + 1)
            if self._available((x, y))
        }
        missing = expected - visited
        if missing:
            raise ValueError(
                "На карте есть недостижимые клетки: "
                + ", ".join(map(str, sorted(missing)))
            )

        while len(route) > 1 and route[-1] == self.start:
            route.pop()

        return tuple(route)

    @staticmethod
    def _index_route(
        route: tuple[Position, ...],
    ) -> dict[Position, tuple[int, ...]]:
        index: dict[Position, list[int]] = {}
        for route_index, position in enumerate(route):
            index.setdefault(position, []).append(route_index)
        return {
            position: tuple(indices)
            for position, indices in index.items()
        }

    def _nearest_index(
        self,
        position: Position,
        *,
        around: int | None = None,
        fallback: int | None = None,
    ) -> int:
        indices = self.position_to_indices.get(position)
        if not indices:
            if fallback is not None:
                return fallback
            raise ValueError(
                f"Координаты {position} отсутствуют в маршруте."
            )
        reference = self.route_index if around is None else around
        return min(indices, key=lambda index: abs(index - reference))

    def validate_position(self, position: Position) -> None:
        if position not in self.position_to_indices:
            raise ValueError(
                f"Координаты {position} отсутствуют в маршруте."
            )

    def initialize_from_history(
        self,
        positions: Iterable[Position],
    ) -> RouteDirection:
        history = [
            position
            for position in positions
            if position in self.position_to_indices
        ]

        if not history:
            self.direction = RouteDirection.DOWN
            return self.direction

        self.route_index = self._nearest_index(history[-1])

        for previous, current in reversed(
            list(zip(history, history[1:]))
        ):
            previous_indices = self.position_to_indices[previous]
            current_indices = self.position_to_indices[current]

            for previous_index in previous_indices:
                if previous_index + 1 in current_indices:
                    self.route_index = previous_index + 1
                    self.direction = RouteDirection.DOWN
                    return self.direction
                if previous_index - 1 in current_indices:
                    self.route_index = previous_index - 1
                    self.direction = RouteDirection.UP
                    return self.direction

        if self.route_index == len(self.route) - 1:
            self.direction = RouteDirection.UP
        elif self.route_index == 0:
            self.direction = RouteDirection.DOWN

        return self.direction

    @staticmethod
    def _button_between(
        origin: Position,
        destination: Position,
    ) -> str:
        origin_x, origin_y = origin
        destination_x, destination_y = destination
        delta_x = destination_x - origin_x
        delta_y = destination_y - origin_y

        if delta_y == 0:
            if delta_x == 1:
                return "➡️"
            if delta_x == -1:
                return "⬅️"

        if delta_x == 0 and delta_y == 1:
            return "↘️" if origin_x == 0 else "↙️"

        if delta_x == 0 and delta_y == -1:
            return "↖️" if origin_x == 0 else "↗️"

        raise ValueError(
            f"Нельзя определить кнопку перехода "
            f"{origin} → {destination}."
        )

    def plan(self, position: Position) -> MovePlan:
        self.validate_position(position)
        self.route_index = self._nearest_index(position)

        direction_before = self.direction
        direction_after = direction_before

        if direction_before is RouteDirection.DOWN:
            if self.route_index == len(self.route) - 1:
                direction_after = RouteDirection.UP
                destination_index = self.route_index - 1
            else:
                destination_index = self.route_index + 1
        else:
            if self.route_index == 0:
                direction_after = RouteDirection.DOWN
                destination_index = self.route_index + 1
            else:
                destination_index = self.route_index - 1

        destination = self.route[destination_index]
        plan = MovePlan(
            origin=position,
            destination=destination,
            button=self._button_between(position, destination),
            direction_before=direction_before,
            direction_after_success=direction_after,
        )
        self.last_plan = plan
        return plan

    def confirm_success(
        self,
        plan: MovePlan,
        actual_position: Position,
    ) -> None:
        if actual_position != plan.destination:
            raise ValueError(
                f"Ожидалась клетка {plan.destination}, "
                f"но получена {actual_position}."
            )

        origin_index = self._nearest_index(plan.origin)
        expected_index = (
            origin_index + 1
            if plan.direction_before is RouteDirection.DOWN
            else origin_index - 1
        )

        if (
            0 <= expected_index < len(self.route)
            and self.route[expected_index] == actual_position
        ):
            self.route_index = expected_index
        else:
            self.route_index = self._nearest_index(
                actual_position,
                around=origin_index,
            )

        self.direction = plan.direction_after_success
        self.last_plan = None

    def reject_last_plan(self, current_position: Position) -> None:
        plan = self.last_plan
        if plan is None or current_position != plan.origin:
            return

        if (
            self.obstacle_mode
            and self._inside(plan.destination)
            and plan.destination not in self.blocked_cells
        ):
            self.runtime_blocked.add(plan.destination)

        self.last_plan = None
        self._rebuild_after_obstacle(current_position)

    def _rebuild_after_obstacle(
        self,
        current_position: Position,
    ) -> None:
        old_direction = self.direction
        self.route = self._build_route()
        self.position_to_indices = self._index_route(self.route)
        self.route_index = self._nearest_index(current_position)
        self.direction = old_direction

    def recover_from_actual_transition(
        self,
        previous: Position,
        current: Position,
    ) -> bool:
        if (
            previous not in self.position_to_indices
            or current not in self.position_to_indices
        ):
            return False

        previous_indices = self.position_to_indices[previous]
        current_indices = self.position_to_indices[current]

        for previous_index in previous_indices:
            if previous_index + 1 in current_indices:
                self.route_index = previous_index + 1
                self.direction = RouteDirection.DOWN
                self.last_plan = None
                return True
            if previous_index - 1 in current_indices:
                self.route_index = previous_index - 1
                self.direction = RouteDirection.UP
                self.last_plan = None
                return True

        return False

    @property
    def start_position(self) -> Position:
        return self.route[0]

    @property
    def end_position(self) -> Position:
        return self.route[-1]
