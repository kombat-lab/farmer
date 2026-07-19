from __future__ import annotations

from collections.abc import Iterable

from models import MovePlan, Position, RouteDirection


class SnakeNavigator:
    """
    Координатный навигатор по заранее построенному маршруту.

    Маршрут DOWN:
        (max_x, min_y) → ... → (min_x, min_y)
        → следующий ряд в обратном направлении
        → змейкой до нижнего конца.

    Маршрут UP:
        тот же список координат строго в обратном порядке.

    Направление не хранится в файле. Оно восстанавливается:
    1. по двум последним соседним координатам из истории Telegram;
    2. по каждому подтверждённому перемещению;
    3. на конечных точках маршрута автоматически меняется.
    """

    def __init__(
        self,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
    ) -> None:
        if min_x > max_x or min_y > max_y:
            raise ValueError("Некорректные границы карты.")

        self.min_x = min_x
        self.max_x = max_x
        self.min_y = min_y
        self.max_y = max_y

        self.route = self._build_route()
        self.position_to_index = {
            position: index
            for index, position in enumerate(self.route)
        }

        # Если истории недостаточно, новый запуск продолжает вниз.
        self.direction = RouteDirection.DOWN

    def _build_route(self) -> tuple[Position, ...]:
        route: list[Position] = []

        for y in range(self.min_y, self.max_y + 1):
            row_number = y - self.min_y

            if row_number % 2 == 0:
                x_values = range(self.max_x, self.min_x - 1, -1)
            else:
                x_values = range(self.min_x, self.max_x + 1)

            route.extend((x, y) for x in x_values)

        return tuple(route)

    def validate_position(self, position: Position) -> None:
        if position not in self.position_to_index:
            raise ValueError(
                f"Координаты {position} отсутствуют в маршруте."
            )

    def initialize_from_history(
        self,
        positions: Iterable[Position],
    ) -> RouteDirection:
        """
        Восстанавливает направление по истории координат.

        Ожидается хронологический порядок: от старых к новым.
        Берётся последняя пара соседних клеток маршрута.
        """
        history = list(positions)

        for position in history:
            self.validate_position(position)

        for previous, current in reversed(
            list(zip(history, history[1:]))
        ):
            previous_index = self.position_to_index[previous]
            current_index = self.position_to_index[current]

            if current_index == previous_index + 1:
                self.direction = RouteDirection.DOWN
                return self.direction

            if current_index == previous_index - 1:
                self.direction = RouteDirection.UP
                return self.direction

        if history:
            current_index = self.position_to_index[history[-1]]

            if current_index == len(self.route) - 1:
                self.direction = RouteDirection.UP
            elif current_index == 0:
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

        index = self.position_to_index[position]
        direction_before = self.direction
        direction_after = direction_before

        if direction_before is RouteDirection.DOWN:
            if index == len(self.route) - 1:
                direction_after = RouteDirection.UP
                destination = self.route[index - 1]
            else:
                destination = self.route[index + 1]
        else:
            if index == 0:
                direction_after = RouteDirection.DOWN
                destination = self.route[index + 1]
            else:
                destination = self.route[index - 1]

        return MovePlan(
            origin=position,
            destination=destination,
            button=self._button_between(
                position,
                destination,
            ),
            direction_before=direction_before,
            direction_after_success=direction_after,
        )

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

        self.direction = plan.direction_after_success

    def recover_from_actual_transition(
        self,
        previous: Position,
        current: Position,
    ) -> bool:
        """
        Синхронизирует направление после ручного или неожиданного
        перемещения. Возвращает True, если переход является соседним
        шагом маршрута.
        """
        self.validate_position(previous)
        self.validate_position(current)

        previous_index = self.position_to_index[previous]
        current_index = self.position_to_index[current]

        if current_index == previous_index + 1:
            self.direction = RouteDirection.DOWN
            return True

        if current_index == previous_index - 1:
            self.direction = RouteDirection.UP
            return True

        return False

    @property
    def start_position(self) -> Position:
        return self.route[0]

    @property
    def end_position(self) -> Position:
        return self.route[-1]
