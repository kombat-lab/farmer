from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MonsterDefinition:
    """Stable game data used before combat observations are available."""

    name: str


@dataclass(frozen=True, slots=True)
class LocationDefinition:
    """A location and its target priority, from highest to lowest."""

    name: str
    monsters: tuple[MonsterDefinition, ...]
    fallback_width: int = 9
    fallback_height: int = 9
    fallback_start: tuple[int, int] = (0, 4)

    @property
    def monster_names(self) -> tuple[str, ...]:
        return tuple(monster.name for monster in self.monsters)


def _location(
    name: str,
    *monsters: str,
    fallback_size: tuple[int, int] = (9, 9),
    fallback_start: tuple[int, int] = (0, 4),
) -> LocationDefinition:
    return LocationDefinition(
        name=name,
        monsters=tuple(MonsterDefinition(monster) for monster in monsters),
        fallback_width=fallback_size[0],
        fallback_height=fallback_size[1],
        fallback_start=fallback_start,
    )


# This is the stable catalog shipped with the application. Runtime observations
# (obstacles, damage, tactics and user-selected targets) belong in SQLite.
LOCATION_CATALOG: tuple[LocationDefinition, ...] = (
    _location(
        "Поляна",
        "Большая божья коровка-матрона",
        "Бабочка-туманница",
        "Золотой бронзовик",
        "Поганка",
        "Бронзовик",
        "Крапива-жгучка",
        "Кузнечик-прыгун",
        "Клоп-солдатик",
        "Улитка-слизняк",
        "Гусеница-обжора",
        "Жужелица-охотник",
    ),
    _location(
        "Предлес",
        "Осиное гнездо",
        "Белая лиса",
        "Королевский слизень",
        "Слизень-кислотник",
        "Мухомор-споровик",
        "Оса-разведчица",
        "Мышь-полёвка",
        "Стрекоза-лезвие",
        "Паук-охотник",
        "Оса-рабочая",
        "Оса-страж",
        "Живой мох",
        "Жук-щитоносец",
        "Лиса-сорванец",
    ),
    _location(
        "Песчаная Кромка",
        "Лазуритовый скарабей",
        "Богомол-альбинос",
        "Богомол-пескожвал",
        "Сколопендра",
        "Ящерка-песчанка",
        "Колючник-сухоцвет",
    ),
    _location(
        "Мертвый лес",
        "Туманный Жгун",
        "Муха-охотник",
        "Черная мушка",
        "Зеленая муха",
        "Костяной заяц",
        "Летучая мышь",
        "Древесная змея",
        "Кабан",
        "Серый волк",
        "Пенёк",
        fallback_size=(12, 12),
        fallback_start=(0, 0),
    ),
    _location(
        "Выжженное поле",
        "Фонарщик",
        "Пепельник",
        fallback_size=(12, 12),
        fallback_start=(0, 0),
    ),
    _location(
        "Пустынная равнина",
        "Хранитель дюн",
        "Камнешкурый варан",
        "Скорпион",
        "Кактус",
        "Стервятник",
        "Гремучая змея",
        "Пыльник",
    ),
)


LOCATION_NAMES: tuple[str, ...] = tuple(location.name for location in LOCATION_CATALOG)
ALL_MONSTER_NAMES: tuple[str, ...] = tuple(
    monster.name
    for location in LOCATION_CATALOG
    for monster in location.monsters
)

_LOCATIONS_BY_NAME = {location.name: location for location in LOCATION_CATALOG}
_MONSTER_TO_LOCATION = {
    monster.name: location.name
    for location in LOCATION_CATALOG
    for monster in location.monsters
}


def get_location(name: str) -> LocationDefinition | None:
    return _LOCATIONS_BY_NAME.get(name)


def get_monster_names(location_name: str) -> tuple[str, ...]:
    location = get_location(location_name)
    return location.monster_names if location is not None else ()


def get_monster_location(monster_name: str) -> str | None:
    return _MONSTER_TO_LOCATION.get(monster_name)
