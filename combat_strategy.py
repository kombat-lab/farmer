from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum

from config import UNDEAD_MONSTERS
from parser import HEART_HP_RE, normalize
from skills import HEALING_MANA_RESERVE, SkillButton, available_skills, parse_current_mana

KNOWN_SKILLS = {
    "лечение",
    "обновление",
    "святое свечение",
    "атака аколита",
}

USE_SKILL_RE = re.compile(r"использует\s+([^\n\r]+)", re.IGNORECASE)
DAMAGE_RE = re.compile(r"^(.+?)\s+получает\s+(\d+)\s+урона(?:\s*·\s*(.+))?$", re.IGNORECASE)
HEAL_RE = re.compile(r"^(.+?)\s+восстанавливает\s+(\d+)\s+HP(?:\s*·\s*(.+))?$", re.IGNORECASE)
EFFECT_TURNS_RE = re.compile(r"(?:✦|🔥)?\s*(.+?)\s*·\s*(\d+)\s+ход", re.IGNORECASE)
STARTING_HP_RE = re.compile(r"(\d+)❤️\s+из\s+(\d+)❤️", re.IGNORECASE)


class SkillTarget(Enum):
    SELF = "self"
    ENEMY = "enemy"


@dataclass(frozen=True)
class CombatDecision:
    skill_name: str
    target: SkillTarget
    reason: str


@dataclass
class ObservedRange:
    minimum: int | None = None
    maximum: int | None = None
    samples: int = 0

    def add(self, value: int) -> None:
        if value <= 0:
            return
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.samples += 1

    def floor(self, fallback: int) -> int:
        if self.minimum is None:
            return fallback
        # Одиночный критический удар нельзя принимать за гарантированный.
        # Из наблюдений разрешено только понижать консервативную границу.
        return min(fallback, self.minimum)

    def ceiling(self, fallback: int) -> int:
        if self.maximum is None:
            return fallback
        return max(fallback, self.maximum)


@dataclass(frozen=True)
class EnemyProfile:
    incoming_ceiling: int
    skill_damage_floors: dict[str, int]


ENEMY_PROFILES: dict[str, EnemyProfile] = {
    normalize("Фонарщик"): EnemyProfile(
        incoming_ceiling=100,
        skill_damage_floors={
            "лечение": 89,
            "святое свечение": 77,
            "атака аколита": 31,
        },
    ),
    normalize("Пепельник"): EnemyProfile(
        incoming_ceiling=70,
        skill_damage_floors={
            "лечение": 100,
            "святое свечение": 97,
            "атака аколита": 37,
        },
    ),
    normalize("Костяной заяц"): EnemyProfile(
        incoming_ceiling=45,
        skill_damage_floors={
            "лечение": 120,
            "святое свечение": 106,
            "атака аколита": 47,
        },
    ),
}


@dataclass
class CombatMemory:
    target_name: str | None = None
    enemy_current_hp: int | None = None
    enemy_max_hp: int | None = None
    renewal_turns: int = 0
    periodic_damage: int = 0
    periodic_damage_turns: int = 0
    incoming_damage: ObservedRange = field(default_factory=ObservedRange)
    outgoing_damage: dict[str, ObservedRange] = field(default_factory=dict)
    direct_healing: ObservedRange = field(default_factory=ObservedRange)
    renewal_healing: ObservedRange = field(default_factory=ObservedRange)
    pending_skill: str | None = None
    pending_target: SkillTarget | None = None

    def reset(self) -> None:
        self.target_name = None
        self.enemy_current_hp = None
        self.enemy_max_hp = None
        self.renewal_turns = 0
        self.periodic_damage = 0
        self.periodic_damage_turns = 0
        self.incoming_damage = ObservedRange()
        self.outgoing_damage.clear()
        self.direct_healing = ObservedRange()
        self.renewal_healing = ObservedRange()
        self.pending_skill = None
        self.pending_target = None

    def begin(self, target_name: str | None, text: str = "") -> None:
        self.reset()
        self.target_name = target_name
        if target_name:
            match = STARTING_HP_RE.search(text)
            if match:
                self.enemy_current_hp = int(match.group(1))
                self.enemy_max_hp = int(match.group(2))

    def observe(self, text: str, character_name: str) -> None:
        if not text:
            return

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        used_skills = [
            normalize(match)
            for match in USE_SKILL_RE.findall(text)
            if normalize(match) in KNOWN_SKILLS
        ]
        player_skill = used_skills[-1] if used_skills else self.pending_skill
        character = normalize(character_name)

        for index, line in enumerate(lines):
            damage_match = DAMAGE_RE.match(line)
            if damage_match:
                recipient = normalize(damage_match.group(1))
                amount = int(damage_match.group(2))
                effect = normalize(damage_match.group(3) or "")
                if character in recipient:
                    if effect:
                        self.periodic_damage = amount
                    else:
                        self.incoming_damage.add(amount)
                elif player_skill is not None:
                    self.outgoing_damage.setdefault(player_skill, ObservedRange()).add(amount)

            heal_match = HEAL_RE.match(line)
            if heal_match and character in normalize(heal_match.group(1)):
                amount = int(heal_match.group(2))
                effect = normalize(heal_match.group(3) or "")
                if effect in {"обновление", "renew"}:
                    self.renewal_healing.add(amount)
                elif player_skill == "лечение":
                    self.direct_healing.add(amount)

            if self.target_name and normalize(self.target_name) in normalize(line):
                for nearby in lines[index + 1 : index + 3]:
                    hp_match = HEART_HP_RE.search(nearby)
                    if hp_match:
                        self.enemy_current_hp = int(hp_match.group(1))
                        self.enemy_max_hp = int(hp_match.group(2))
                        break

        effect_turns = {
            normalize(name): int(turns) for name, turns in EFFECT_TURNS_RE.findall(text)
        }
        if "обновление" in effect_turns:
            self.renewal_turns = effect_turns["обновление"]
        elif self._contains_player_health_block(lines, character):
            self.renewal_turns = 0

        periodic_effects = [
            turns
            for name, turns in effect_turns.items()
            if name in {"горение", "кровотечение", "яд"}
        ]
        if periodic_effects:
            self.periodic_damage_turns = max(periodic_effects)
        elif self._contains_player_health_block(lines, character):
            self.periodic_damage_turns = 0
            self.periodic_damage = 0

        if used_skills:
            self.pending_skill = None

    @staticmethod
    def _contains_player_health_block(lines: list[str], character: str) -> bool:
        for index, line in enumerate(lines):
            if character not in normalize(line):
                continue
            if any(HEART_HP_RE.search(nearby) for nearby in lines[index + 1 : index + 3]):
                return True
        return False

    def profile(self) -> EnemyProfile:
        normalized_target = normalize(self.target_name or "")
        return ENEMY_PROFILES.get(
            normalized_target,
            EnemyProfile(
                incoming_ceiling=100,
                skill_damage_floors={
                    "лечение": 80,
                    "святое свечение": 70,
                    "атака аколита": 25,
                },
            ),
        )

    def damage_floor(self, skill_name: str) -> int:
        fallback = self.profile().skill_damage_floors.get(skill_name, 0)
        observed = self.outgoing_damage.get(skill_name)
        return observed.floor(fallback) if observed else fallback

    def predicted_incoming(self, *, after_current_tick: bool = False) -> int:
        direct = self.incoming_damage.ceiling(self.profile().incoming_ceiling)
        required_turns = 1 if after_current_tick else 0
        periodic = self.periodic_damage if self.periodic_damage_turns > required_turns else 0
        return direct + periodic

    def renewal_tick(self) -> int:
        return self.renewal_healing.floor(40)

    def is_undead(self) -> bool:
        target = normalize(self.target_name or "")
        return any(normalize(name) == target for name in UNDEAD_MONSTERS)


def _can_use_as_attack(memory: CombatMemory, skill_name: str) -> bool:
    return skill_name != "лечение" or memory.is_undead()


def _lethal_skill(
    available: dict[str, SkillButton],
    memory: CombatMemory,
) -> str | None:
    if memory.enemy_current_hp is None:
        return None

    candidates: list[tuple[int, int, str]] = []
    priority = {"атака аколита": 0, "святое свечение": 1, "лечение": 2}
    for skill_name in ("атака аколита", "святое свечение", "лечение"):
        skill = available.get(skill_name)
        if skill is None or not _can_use_as_attack(memory, skill_name):
            continue
        if memory.damage_floor(skill_name) >= memory.enemy_current_hp:
            candidates.append((skill.mana_cost, priority[skill_name], skill_name))

    return min(candidates)[2] if candidates else None


def _estimated_enemy_turns(memory: CombatMemory, available: dict[str, SkillButton]) -> int:
    if memory.enemy_current_hp is None:
        return 3
    damages = [
        memory.damage_floor(name)
        for name in available
        if _can_use_as_attack(memory, name) and memory.damage_floor(name) > 0
    ]
    best_damage = max(damages, default=memory.damage_floor("атака аколита"))
    return max(1, math.ceil(memory.enemy_current_hp / max(1, best_damage)))


def choose_combat_action(
    message,
    *,
    memory: CombatMemory,
    current_hp: int | None,
    max_hp: int | None,
    heal_threshold: int,
) -> CombatDecision | None:
    available = available_skills(message)
    if not available:
        return None

    current_mana = parse_current_mana(getattr(message, "raw_text", "") or "")
    lethal = _lethal_skill(available, memory)
    if lethal is not None:
        return CombatDecision(lethal, SkillTarget.ENEMY, "добивание до ответного удара")

    if current_hp is None or max_hp is None or max_hp <= 0:
        basic = available.get("атака аколита")
        if basic:
            return CombatDecision(basic.name, SkillTarget.ENEMY, "HP не распознано")
        return None

    incoming = memory.predicted_incoming(after_current_tick=True)
    margin = max(10, math.ceil(max_hp * 0.03))
    renewal_credit = memory.renewal_tick() if memory.renewal_turns > 0 else 0
    periodic_now = memory.periodic_damage if memory.periodic_damage_turns > 0 else 0
    effective_hp = max(0, min(max_hp, current_hp + renewal_credit) - periodic_now)
    missing_hp = max_hp - effective_hp
    can_survive_one = effective_hp > incoming + margin
    can_survive_two = effective_hp > incoming * 2 + margin
    enemy_turns = _estimated_enemy_turns(memory, available)

    treatment = available.get("лечение")
    renewal = available.get("обновление")

    if treatment is not None and not can_survive_one:
        return CombatDecision("лечение", SkillTarget.SELF, "следующий удар может быть смертельным")

    should_prepare_renewal = (
        renewal is not None
        and memory.renewal_turns <= 0
        and missing_hp >= memory.renewal_tick() * 2
        and enemy_turns >= 3
        and can_survive_one
        and (treatment is None or can_survive_two)
        and effective_hp
        <= max(
            heal_threshold + memory.renewal_tick() * 3 + margin,
            incoming * 3 + margin,
        )
    )
    if should_prepare_renewal:
        return CombatDecision("обновление", SkillTarget.SELF, "упреждающее лечение на три хода")

    if current_hp <= heal_threshold and treatment is not None:
        if memory.is_undead() and can_survive_two and enemy_turns > 1:
            return CombatDecision(
                "лечение",
                SkillTarget.ENEMY,
                "атака нежити безопаснее затягивания боя",
            )
        return CombatDecision("лечение", SkillTarget.SELF, "достигнут порог лечения")

    holy_light = available.get("святое свечение")
    if (
        holy_light is not None
        and current_mana is not None
        and current_mana - holy_light.mana_cost >= HEALING_MANA_RESERVE
    ):
        return CombatDecision("святое свечение", SkillTarget.ENEMY, "лучший обычный урон")

    if treatment is not None and memory.is_undead() and can_survive_two:
        return CombatDecision("лечение", SkillTarget.ENEMY, "лечение наносит урон нежити")

    if renewal is not None and current_hp <= heal_threshold and memory.renewal_turns <= 0:
        return CombatDecision("обновление", SkillTarget.SELF, "мгновенное лечение недоступно")

    basic = available.get("атака аколита")
    if basic is not None:
        return CombatDecision("атака аколита", SkillTarget.ENEMY, "сохранение маны")

    return None
