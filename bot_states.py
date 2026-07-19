from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SettingsInput(StatesGroup):
    cycles_count = State()
    moves_per_cycle = State()
    delay_range = State()
    long_pause_chance = State()
    character_value = State()
