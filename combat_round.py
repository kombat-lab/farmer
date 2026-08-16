from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from parser import HEART_HP_RE, normalize
from skills import DEFAULT_MANA_COSTS, SkillButton, parse_skill_button

ROUND_RE = re.compile(r"(?:⚔️|🎯)\s*Раунд\s*(\d+)", re.IGNORECASE)
TURN_RE = re.compile(r"^Ход\s+(.+)$", re.IGNORECASE)
USE_SKILL_RE = re.compile(r"^(.+?)\s+использует\s+(.+)$", re.IGNORECASE)
ATTACK_RE = re.compile(r"^(.+?)\s+атакует\s+(.+)$", re.IGNORECASE)
DAMAGE_RE = re.compile(
    r"^(.+?)\s+получает\s+(\d+)\s+урона"
    r"(?:\s*·\s*(.+?)|\s+(.+?))?$",
    re.IGNORECASE,
)
HEAL_RE = re.compile(
    r"^(.+?)\s+восстанавливает\s+(\d+)\s+HP(?:\s*·\s*(.+))?$",
    re.IGNORECASE,
)
EFFECT_RE = re.compile(r"^(?:[^\wа-яё]+)?(.+?)\s*·\s*(\d+)\s+ход", re.IGNORECASE)
EFFECT_APPLIED_RE = re.compile(
    r"Наложено:\s*(.+?)\s+на\s+(\d+)\s+ход",
    re.IGNORECASE,
)
MANA_TRANSITION_RE = re.compile(r"Мана:\s*(\d+)\s*→\s*(\d+)", re.IGNORECASE)
MANA_STATE_RE = re.compile(r"Мана:\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
MANA_SINGLE_RE = re.compile(r"Мана:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
REMAINING_SECONDS_RE = re.compile(r"Осталось:\s*(\d+)\s*сек", re.IGNORECASE)
SELECTED_SKILL_RE = re.compile(r"Выбран навык:\s*([^\n\r]+)", re.IGNORECASE)
TOTAL_DAMAGE_RE = re.compile(r"Урон за раунд:\s*(\d+)", re.IGNORECASE)
DEFEATED_RE = re.compile(r"💀\s*(.+?)\s+повержен(?:а|о|ы)?(?:\s*$|\n)", re.IGNORECASE)
NEAR_DEATH_RE = re.compile(r"💀\s*(.+?)\s+на грани смерти", re.IGNORECASE)
BLOCKED_RE = re.compile(r"^(.+?):\s*🛡️?\s*Заблокировано", re.IGNORECASE)
DODGED_RE = re.compile(
    r"^(?:⚡️|💫)\s*(.+?)\s+"
    r"(?:(?:уш[её]л(?:а|о)?|ушла)\s+из-под удара|увернул(?:ся|ась|ось))",
    re.IGNORECASE,
)
STARTING_HP_RE = re.compile(r"(\d+)❤️\s+из\s+(\d+)❤️", re.IGNORECASE)


class CombatSide(Enum):
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True)
class CombatEffect:
    name: str
    turns: int


@dataclass(frozen=True)
class SkillUse:
    actor: str
    skill: str


@dataclass(frozen=True)
class AttackEvent:
    actor: str
    target: str


@dataclass(frozen=True)
class DamageEvent:
    target: str
    amount: int
    effect: str | None = None
    modifier: str | None = None

    @property
    def critical(self) -> bool:
        return "крит" in normalize(self.modifier or "")


@dataclass(frozen=True)
class HealingEvent:
    target: str
    amount: int
    effect: str | None = None


@dataclass(frozen=True)
class AppliedEffect:
    name: str
    turns: int
    target: str | None = None


@dataclass(frozen=True)
class CombatantState:
    name: str
    current_hp: int
    max_hp: int
    effects: tuple[CombatEffect, ...] = ()


@dataclass(frozen=True)
class CombatRoundState:
    number: int | None
    side: CombatSide | None
    turn_actor: str | None
    mana_before: int | None
    mana_after: int | None
    current_mana: int | None
    max_mana: int | None
    remaining_seconds: int | None
    selected_skill: str | None
    available_skills: tuple[SkillButton, ...]
    skill_uses: tuple[SkillUse, ...]
    attacks: tuple[AttackEvent, ...]
    damage: tuple[DamageEvent, ...]
    healing: tuple[HealingEvent, ...]
    applied_effects: tuple[AppliedEffect, ...]
    combatants: tuple[CombatantState, ...]
    total_damage: int | None
    defeated: tuple[str, ...]
    near_death: tuple[str, ...]
    blocked: tuple[str, ...]
    dodged: tuple[str, ...]

    def combatant(self, name: str) -> CombatantState | None:
        expected = normalize(name)
        for combatant in self.combatants:
            actual = normalize(combatant.name)
            if actual == expected or expected in actual:
                return combatant
        return None

    def castable_skills(self) -> dict[str, SkillButton]:
        return {
            normalize(skill.name): skill
            for skill in self.available_skills
            if skill.can_cast(self.current_mana)
        }


def _match_int(pattern: re.Pattern[str], text: str, group: int = 1) -> int | None:
    match = pattern.search(text)
    return int(match.group(group)) if match else None


def _parse_available_skills(button_texts: Iterable[str]) -> tuple[SkillButton, ...]:
    result: list[SkillButton] = []
    for text in button_texts:
        skill = parse_skill_button(text)
        if normalize(skill.name) in DEFAULT_MANA_COSTS:
            result.append(skill)
    return tuple(result)


def _previous_nonempty(lines: list[str], index: int) -> str | None:
    for candidate in reversed(lines[:index]):
        if candidate:
            return candidate
    return None


def _parse_combatants(lines: list[str]) -> tuple[CombatantState, ...]:
    result: list[CombatantState] = []
    for index, line in enumerate(lines):
        hp_match = HEART_HP_RE.search(line)
        if not hp_match:
            continue
        name = _previous_nonempty(lines, index)
        if not name:
            continue

        effects: list[CombatEffect] = []
        for nearby in lines[index + 1 :]:
            if not nearby:
                break
            effect_match = EFFECT_RE.match(nearby)
            if effect_match:
                effects.append(
                    CombatEffect(
                        name=effect_match.group(1).strip(),
                        turns=int(effect_match.group(2)),
                    )
                )

        result.append(
            CombatantState(
                name=name,
                current_hp=int(hp_match.group(1)),
                max_hp=int(hp_match.group(2)),
                effects=tuple(effects),
            )
        )
    return tuple(result)


def parse_combat_round(
    text: str,
    button_texts: Iterable[str] = (),
) -> CombatRoundState | None:
    if not text:
        return None

    raw_lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in raw_lines if line]
    round_match = ROUND_RE.search(text)
    skill_uses: list[SkillUse] = []
    attacks: list[AttackEvent] = []
    damage: list[DamageEvent] = []
    healing: list[HealingEvent] = []
    applied_effects: list[AppliedEffect] = []
    blocked: list[str] = []
    dodged: list[str] = []
    turn_actor: str | None = None
    latest_target: str | None = None

    for line in lines:
        if turn_match := TURN_RE.match(line):
            turn_actor = turn_match.group(1).strip()
        if skill_match := USE_SKILL_RE.match(line):
            skill_uses.append(
                SkillUse(
                    actor=skill_match.group(1).strip(),
                    skill=skill_match.group(2).strip(),
                )
            )
        if attack_match := ATTACK_RE.match(line):
            latest_target = attack_match.group(2).strip()
            attacks.append(
                AttackEvent(
                    actor=attack_match.group(1).strip(),
                    target=latest_target,
                )
            )
        if damage_match := DAMAGE_RE.match(line):
            latest_target = damage_match.group(1).strip()
            damage.append(
                DamageEvent(
                    target=latest_target,
                    amount=int(damage_match.group(2)),
                    effect=(damage_match.group(3) or "").strip() or None,
                    modifier=(damage_match.group(4) or "").strip() or None,
                )
            )
        if heal_match := HEAL_RE.match(line):
            latest_target = heal_match.group(1).strip()
            healing.append(
                HealingEvent(
                    target=latest_target,
                    amount=int(heal_match.group(2)),
                    effect=(heal_match.group(3) or "").strip() or None,
                )
            )
        if applied_match := EFFECT_APPLIED_RE.search(line):
            applied_effects.append(
                AppliedEffect(
                    name=applied_match.group(1).strip(),
                    turns=int(applied_match.group(2)),
                    target=latest_target,
                )
            )
        if blocked_match := BLOCKED_RE.match(line):
            blocked.append(blocked_match.group(1).strip())
        if dodged_match := DODGED_RE.match(line):
            dodged.append(dodged_match.group(1).strip())

    available_skills = _parse_available_skills(button_texts)
    combatants = _parse_combatants(raw_lines)
    has_combat_data = bool(
        round_match
        or turn_actor
        or skill_uses
        or attacks
        or damage
        or healing
        or available_skills
        or "Бой завершён" in text
    )
    if not has_combat_data:
        return None

    mana_transition = MANA_TRANSITION_RE.search(text)
    mana_state = MANA_STATE_RE.search(text)
    mana_single = MANA_SINGLE_RE.search(text)
    mana_before: int | None
    mana_after: int | None
    current_mana: int | None
    max_mana: int | None
    if mana_transition:
        mana_before = int(mana_transition.group(1))
        mana_after = int(mana_transition.group(2))
        current_mana = mana_after
        max_mana = None
    elif mana_state:
        mana_before = None
        mana_after = None
        current_mana = int(mana_state.group(1))
        max_mana = int(mana_state.group(2))
    else:
        mana_before = None
        mana_after = None
        current_mana = int(mana_single.group(1)) if mana_single else None
        max_mana = None

    side = None
    if "Левая сторона" in text:
        side = CombatSide.LEFT
    elif "Правая сторона" in text:
        side = CombatSide.RIGHT

    selected_match = SELECTED_SKILL_RE.search(text)
    return CombatRoundState(
        number=int(round_match.group(1)) if round_match else None,
        side=side,
        turn_actor=turn_actor,
        mana_before=mana_before,
        mana_after=mana_after,
        current_mana=current_mana,
        max_mana=max_mana,
        remaining_seconds=_match_int(REMAINING_SECONDS_RE, text),
        selected_skill=selected_match.group(1).strip() if selected_match else None,
        available_skills=available_skills,
        skill_uses=tuple(skill_uses),
        attacks=tuple(attacks),
        damage=tuple(damage),
        healing=tuple(healing),
        applied_effects=tuple(applied_effects),
        combatants=combatants,
        total_damage=_match_int(TOTAL_DAMAGE_RE, text),
        defeated=tuple(match.strip() for match in DEFEATED_RE.findall(text)),
        near_death=tuple(match.strip() for match in NEAR_DEATH_RE.findall(text)),
        blocked=tuple(blocked),
        dodged=tuple(dodged),
    )


def parse_starting_health(text: str) -> tuple[int, int] | None:
    match = STARTING_HP_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))
