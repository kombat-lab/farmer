from __future__ import annotations

from models import MovePlan, Position, RouteDirection


class SnakeNavigator:
    """
    Навигатор по прямоугольной карте с препятствиями.

    Для обычных локаций сохраняется прежняя змейка 9x9.

    Для Мёртвого леса строится непрерывный DFS-маршрут, который посещает
    все доступные клетки. Возвраты по уже посещённым клеткам допустимы.

    Если игра оставляет персонажа на прежней координате, следующий вызов
    plan() автоматически пробует альтернативную кнопку перехода. Это
    позволяет выйти из угла или продолжить движение при неоднозначном
    соответствии диагональных кнопок координатам.
    """

    HORIZONTAL_BUTTONS = ("⬅️", "➡️")
    DIAGONAL_BUTTONS = ("↖️", "↗️", "↙️", "↘️")
    ALL_MOVE_BUTTONS = (
        "⬅️",
        "➡️",
        "↖️",
        "↗️",
        "↙️",
        "↘️",
    )

    def __init__(
        self,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
    ) -> None:
        self.direction = RouteDirection.DOWN
        self.route_index = 0
        self.last_plan: MovePlan | None = None
        self.runtime_blocked: set[Position] = set()
        self.location_name: str | None = None

        # Кнопки, которые уже оставили персонажа на той же координате.
        self.failed_buttons: dict[Position, set[str]] = {}

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
        from locations import get_location_geometry

        geometry = get_location_geometry(location_name)

        self.location_name = location_name
        self.runtime_blocked.clear()
        self.failed_buttons.clear()
        self.direction = RouteDirection.DOWN
        self.last_plan = None

        self._configure(
            min_x=geometry.min_x,
            max_x=geometry.max_x,
            min_y=geometry.min_y,
            max_y=geometry.max_y,
            start_position=geometry.start_position,
            blocked_cells=geometry.blocked_cells,
            obstacle_mode=bool(geometry.blocked_cells),
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
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def _available(self, position: Position) -> bool:
        return (
            self._inside(position)
            and position not in self.blocked_cells
            and position not in self.runtime_blocked
        )

    def _neighbors(
        self,
        position: Position,
    ) -> tuple[Position, ...]:
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

        return tuple(candidate for candidate in candidates if self._available(candidate))

    def _build_route(self) -> tuple[Position, ...]:
        if not self.obstacle_mode:
            linear_route: list[Position] = []

            for y in range(
                self.min_y,
                self.max_y + 1,
            ):
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

                linear_route.extend((x, y) for x in x_values)

            return tuple(linear_route)

        if not self._available(self.start):
            raise ValueError(f"Стартовая клетка {self.start} недоступна.")

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
            for y in range(
                self.min_y,
                self.max_y + 1,
            )
            for x in range(
                self.min_x,
                self.max_x + 1,
            )
            if self._available((x, y))
        }

        missing = expected - visited

        if missing:
            raise ValueError(
                "На карте есть недостижимые клетки: " + ", ".join(map(str, sorted(missing)))
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
            index.setdefault(
                position,
                [],
            ).append(route_index)

        return {position: tuple(indices) for position, indices in index.items()}

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

            raise ValueError(f"Координаты {position} отсутствуют в маршруте.")

        reference = self.route_index if around is None else around

        return min(
            indices,
            key=lambda index: abs(index - reference),
        )

    def validate_position(
        self,
        position: Position,
    ) -> None:
        if position not in self.position_to_indices:
            raise ValueError(f"Координаты {position} отсутствуют в маршруте.")

    @staticmethod
    def _primary_button_between(
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

        raise ValueError(f"Нельзя определить кнопку перехода {origin} → {destination}.")

    @staticmethod
    def _paired_diagonal(button: str) -> str | None:
        pairs = {
            "↖️": "↗️",
            "↗️": "↖️",
            "↙️": "↘️",
            "↘️": "↙️",
        }
        return pairs.get(button)

    def _button_candidates(
        self,
        origin: Position,
        destination: Position,
    ) -> tuple[str, ...]:
        """
        Возвращает кнопки в порядке безопасной проверки.

        Для горизонтального перехода соответствие однозначно.
        Для перехода между строками сначала используется основная
        диагональ, затем противоположная диагональ того же направления.
        После этого допускаются остальные кнопки как аварийный выход.
        """
        primary = self._primary_button_between(
            origin,
            destination,
        )
        candidates: list[str] = [primary]

        paired = self._paired_diagonal(primary)
        if paired is not None:
            candidates.append(paired)

        # На границе сначала пробуем кнопку, которая гарантированно ведёт
        # внутрь прямоугольника по горизонтали.
        origin_x, _ = origin

        if origin_x >= self.max_x:
            candidates.append("⬅️")
        elif origin_x <= self.min_x:
            candidates.append("➡️")
        else:
            candidates.extend(("⬅️", "➡️"))

        candidates.extend(self.DIAGONAL_BUTTONS)

        unique: list[str] = []

        for button in candidates:
            if button not in unique:
                unique.append(button)

        return tuple(unique)

    def _remember_failed_last_plan(
        self,
        current_position: Position,
    ) -> None:
        """
        plan() вызывается повторно на той же координате только тогда,
        когда предыдущая команда не сдвинула персонажа.

        Это позволяет обнаружить обычный отказ перехода даже если игра
        не пишет статус «Туда пройти нельзя».
        """
        plan = self.last_plan

        if plan is None or plan.origin != current_position:
            return

        self.failed_buttons.setdefault(
            current_position,
            set(),
        ).add(plan.button)

        self.last_plan = None

    def _select_button(
        self,
        origin: Position,
        destination: Position,
    ) -> str:
        failed = self.failed_buttons.get(
            origin,
            set(),
        )

        for button in self._button_candidates(
            origin,
            destination,
        ):
            if button not in failed:
                return button

        # Все варианты уже пробовались. Начинаем локальную проверку заново,
        # чтобы навигатор не завис навсегда после изменения состояния карты.
        self.failed_buttons.pop(
            origin,
            None,
        )

        return self._primary_button_between(
            origin,
            destination,
        )

    def plan(
        self,
        position: Position,
    ) -> MovePlan:
        self.validate_position(position)

        # Если предыдущая попытка оставила нас на той же клетке, её кнопка
        # исключается и ниже выбирается следующий вариант.
        self._remember_failed_last_plan(position)

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
            button=self._select_button(
                position,
                destination,
            ),
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
            raise ValueError(f"Ожидалась клетка {plan.destination}, но получена {actual_position}.")

        origin_index = self._nearest_index(plan.origin)

        expected_index = (
            origin_index + 1 if plan.direction_before is RouteDirection.DOWN else origin_index - 1
        )

        if 0 <= expected_index < len(self.route) and self.route[expected_index] == actual_position:
            self.route_index = expected_index
        else:
            self.route_index = self._nearest_index(
                actual_position,
                around=origin_index,
            )

        self.direction = plan.direction_after_success
        self.last_plan = None

        # После успешного выхода с клетки старые неудачные кнопки больше
        # не нужны и не должны влиять на следующий круг.
        self.failed_buttons.pop(
            plan.origin,
            None,
        )

    def reject_last_plan(
        self,
        current_position: Position,
        *,
        mark_destination_blocked: bool = False,
    ) -> bool:
        plan = self.last_plan

        if plan is None or current_position != plan.origin:
            return False

        candidates = self._button_candidates(plan.origin, plan.destination)

        self.failed_buttons.setdefault(
            current_position,
            set(),
        ).add(plan.button)

        if (
            mark_destination_blocked
            and self.obstacle_mode
            and self._inside(plan.destination)
            and plan.destination not in self.blocked_cells
        ):
            self.runtime_blocked.add(plan.destination)
            self._rebuild_after_obstacle(current_position)

        self.last_plan = None
        return all(
            button in self.failed_buttons.get(current_position, set()) for button in candidates
        )

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
        if previous not in self.position_to_indices or current not in self.position_to_indices:
            return False

        previous_indices = self.position_to_indices[previous]
        current_indices = self.position_to_indices[current]

        for previous_index in previous_indices:
            if previous_index + 1 in current_indices:
                self.route_index = previous_index + 1
                self.direction = RouteDirection.DOWN
                self.last_plan = None
                self.failed_buttons.pop(
                    previous,
                    None,
                )
                return True

            if previous_index - 1 in current_indices:
                self.route_index = previous_index - 1
                self.direction = RouteDirection.UP
                self.last_plan = None
                self.failed_buttons.pop(
                    previous,
                    None,
                )
                return True

        # Персонаж всё же переместился, но не на соседний элемент текущего
        # маршрута. Синхронизируемся с фактической доступной координатой.
        self.route_index = self._nearest_index(current)
        self.last_plan = None
        self.failed_buttons.pop(
            previous,
            None,
        )
        return True
