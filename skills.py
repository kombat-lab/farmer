from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from parser import normalize
from telegram_buttons import get_button_texts

CD_RE = re.compile(r"\(\s*CD\s*:\s*(\d+)\s*\)", re.IGNORECASE)
MANA_COST_RE = re.compile(
    r"(?:\[\s*)?(?:🔷\s*)?Мана\s*[: ]\s*(\d+)(?:\s*\])?",
    re.IGNORECASE,
)
CURRENT_MANA_RE = re.compile(
    r"(?:🔷\s*)?Мана\s*:\s*(\d+)(?:\s*/\s*(\d+))?",
    re.IGNORECASE,
)

# Запасные стоимости применяются только когда игра не указала стоимость
# прямо на кнопке навыка.
DEFAULT_MANA_COSTS: dict[str, int] = {
    "лечение": 4,
    "святое свечение": 3,
    "атака аколита": 0,
}


@dataclass(frozen=True)
class SkillButton:
    raw_text: str
    name: str
    cooldown: int
    mana_cost: int

    @property
    def available(self) -> bool:
        return self.cooldown == 0

    def can_cast(self, current_mana: Optional[int]) -> bool:
        if not self.available:
            return False
        if self.mana_cost <= 0:
            return True
        # Если количество маны не удалось распознать, платный навык не
        # нажимаем: это безопаснее, чем потерять ход.
        return current_mana is not None and current_mana >= self.mana_cost


def parse_current_mana(text: str) -> Optional[int]:
    match = CURRENT_MANA_RE.search(text or "")
    return int(match.group(1)) if match else None


def parse_skill_button(text: str) -> SkillButton:
    cooldown_match = CD_RE.search(text)
    cooldown = int(cooldown_match.group(1)) if cooldown_match else 0

    cleaned = CD_RE.sub("", text).strip()
    normalized = normalize(cleaned)
    known = ("лечение", "святое свечение", "атака аколита")
    name = next((value for value in known if value in normalized), cleaned)

    mana_match = MANA_COST_RE.search(cleaned)
    mana_cost = (
        int(mana_match.group(1))
        if mana_match
        else DEFAULT_MANA_COSTS.get(normalize(name), 0)
    )

    return SkillButton(
        raw_text=text,
        name=name,
        cooldown=cooldown,
        mana_cost=mana_cost,
    )


def available_skills(message) -> dict[str, SkillButton]:
    current_mana = parse_current_mana(getattr(message, "raw_text", "") or "")
    result: dict[str, SkillButton] = {}

    for text in get_button_texts(message):
        skill = parse_skill_button(text)
        normalized_name = normalize(skill.name)
        if normalized_name not in DEFAULT_MANA_COSTS:
            continue
        if skill.can_cast(current_mana):
            result[normalized_name] = skill

    return result


def available_skill_names(message) -> set[str]:
    return set(available_skills(message))


def should_use_healing(
    *,
    current_hp: Optional[int],
    max_hp: Optional[int],
    heal_amount: int,
) -> bool:
    """Возвращает True, когда лечение не будет потрачено впустую."""
    if current_hp is None or max_hp is None or heal_amount <= 0:
        return False

    missing_hp = max_hp - current_hp
    return missing_hp >= heal_amount


def choose_skill(
    message,
    *,
    current_hp: Optional[int],
    max_hp: Optional[int],
    heal_amount: int,
) -> Optional[str]:
    available = available_skill_names(message)

    if (
        "лечение" in available
        and should_use_healing(
            current_hp=current_hp,
            max_hp=max_hp,
            heal_amount=heal_amount,
        )
    ):
        return "лечение"

    if "святое свечение" in available:
        return "святое свечение"

    if "атака аколита" in available:
        return "атака аколита"

    return None
