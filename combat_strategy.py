from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum

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
    urgent: bool = False


@dataclass
class ObservedRange:
    minimum: int | None = None
    maximum: int | None = None
    samples: int = 0
    total: int = 0

    def add(self, value: int) -> None:
        if value <= 0:
            return
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.samples += 1
        self.total += value

    def average(self, fallback: float) -> float:
        return self.total / self.samples if self.samples else fallback


@dataclass
class RecentCombatKnowledge:
    """Short in-memory history, isolated per monster and current farmer run."""

    sample_limit: int = 12
    incoming: dict[str, list[int]] = field(default_factory=dict)
    outgoing: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    direct_healing: list[int] = field(default_factory=list)
    renewal_healing: list[int] = field(default_factory=list)

    def _append(self, samples: list[int], value: int) -> None:
        if value <= 0:
            return
        samples.append(value)
        del samples[: max(0, len(samples) - self.sample_limit)]

    def add_incoming(self, target_name: str | None, value: int) -> None:
        target = normalize(target_name or "")
        if target:
            self._append(self.incoming.setdefault(target, []), value)

    def add_outgoing(self, target_name: str | None, skill_name: str, value: int) -> None:
        target = normalize(target_name or "")
        if not target:
            return
        skills = self.outgoing.setdefault(target, {})
        self._append(skills.setdefault(skill_name, []), value)

    def add_direct_healing(self, value: int) -> None:
        self._append(self.direct_healing, value)

    def add_renewal_healing(self, value: int) -> None:
        self._append(self.renewal_healing, value)

    def load_into(self, memory: CombatMemory) -> None:
        target = normalize(memory.target_name or "")
        for value in self.incoming.get(target, []):
            memory.incoming_damage.add(value)
        for skill_name, values in self.outgoing.get(target, {}).items():
            observed = memory.outgoing_damage.setdefault(skill_name, ObservedRange())
            for value in values:
                observed.add(value)
        for value in self.direct_healing:
            memory.direct_healing.add(value)
        for value in self.renewal_healing:
            memory.renewal_healing.add(value)


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
    pending_urgent: bool = False
    knowledge: RecentCombatKnowledge = field(default_factory=RecentCombatKnowledge)

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
        self.pending_urgent = False

    def begin(self, target_name: str | None, text: str = "") -> None:
        self.reset()
        self.target_name = target_name
        self.knowledge.load_into(self)
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
                        self.knowledge.add_incoming(self.target_name, amount)
                elif player_skill is not None:
                    self.outgoing_damage.setdefault(player_skill, ObservedRange()).add(amount)
                    self.knowledge.add_outgoing(self.target_name, player_skill, amount)

            heal_match = HEAL_RE.match(line)
            if heal_match and character in normalize(heal_match.group(1)):
                amount = int(heal_match.group(2))
                effect = normalize(heal_match.group(3) or "")
                if effect in {"обновление", "renew"}:
                    self.renewal_healing.add(amount)
                    self.knowledge.add_renewal_healing(amount)
                elif player_skill == "лечение":
                    self.direct_healing.add(amount)
                    self.knowledge.add_direct_healing(amount)

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

    def damage_floor(self, skill_name: str) -> int:
        observed = self.outgoing_damage.get(skill_name)
        if observed is None or observed.samples < 2:
            # Один результат может оказаться критическим ударом и не даёт
            # безопасной гарантии для добивания.
            return 0
        return observed.minimum or 0

    def predicted_incoming(self, *, after_current_tick: bool = False) -> int | None:
        if self.incoming_damage.samples <= 0 or self.incoming_damage.maximum is None:
            return None
        uncertainty = 1.35 if self.incoming_damage.samples == 1 else 1.25
        if self.incoming_damage.samples >= 4:
            uncertainty = 1.15
        direct = math.ceil(self.incoming_damage.maximum * uncertainty)
        required_turns = 1 if after_current_tick else 0
        periodic = self.periodic_damage if self.periodic_damage_turns > required_turns else 0
        return direct + periodic

    def expected_incoming(self, *, after_current_tick: bool = False) -> int | None:
        if self.incoming_damage.samples <= 0:
            return None
        uncertainty = 1.20 if self.incoming_damage.samples == 1 else 1.15
        if self.incoming_damage.samples >= 4:
            uncertainty = 1.10
        direct = max(1, math.ceil(self.incoming_damage.average(0.0) * uncertainty))
        required_turns = 1 if after_current_tick else 0
        periodic = self.periodic_damage if self.periodic_damage_turns > required_turns else 0
        return direct + periodic

    def renewal_tick(self) -> int | None:
        return self.renewal_healing.minimum

    def direct_heal(self) -> int | None:
        return self.direct_healing.minimum

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
        if skill is None:
            continue
        if memory.damage_floor(skill_name) >= memory.enemy_current_hp:
            candidates.append((skill.mana_cost, priority[skill_name], skill_name))

    return min(candidates)[2] if candidates else None


def _estimated_enemy_turns(
    memory: CombatMemory,
    available: dict[str, SkillButton],
) -> int | None:
    if memory.enemy_current_hp is None:
        return None
    damages = [
        memory.damage_floor(name)
        for name in available
        if memory.damage_floor(name) > 0
    ]
    if not damages:
        return None
    return max(1, math.ceil(memory.enemy_current_hp / max(damages)))


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
        return CombatDecision(
            lethal,
            SkillTarget.ENEMY,
            "добивание до ответного удара",
            urgent=True,
        )

    if current_hp is None or max_hp is None or max_hp <= 0:
        basic = available.get("атака аколита")
        if basic:
            return CombatDecision(basic.name, SkillTarget.ENEMY, "HP не распознано")
        return None

    worst_incoming = memory.predicted_incoming(after_current_tick=True)
    expected_incoming = memory.expected_incoming(after_current_tick=True)
    incoming_known = worst_incoming is not None and expected_incoming is not None
    margin = max(10, math.ceil(max_hp * 0.03))
    if current_hp <= heal_threshold:
        # Порог включает более внимательную оценку риска, но сам по себе
        # больше не приказывает использовать лечение.
        margin += math.ceil(max_hp * 0.02)

    renewal_tick = memory.renewal_tick() or 0
    renewal_credit = renewal_tick if memory.renewal_turns > 0 else 0
    periodic_now = memory.periodic_damage if memory.periodic_damage_turns > 0 else 0
    effective_hp = max(0, min(max_hp, current_hp + renewal_credit) - periodic_now)
    missing_hp = max_hp - effective_hp
    enemy_turns = _estimated_enemy_turns(memory, available)
    future_renewal = max(0, memory.renewal_turns - 1) * renewal_tick
    survival_turns = (
        max(
            0,
            math.floor(
                (effective_hp + future_renewal - margin) / max(1, expected_incoming)
            ),
        )
        if expected_incoming is not None
        else None
    )
    race_is_safe = (
        survival_turns >= enemy_turns
        if survival_turns is not None and enemy_turns is not None
        else None
    )
    can_survive_worst_hit = (
        effective_hp > worst_incoming + margin if worst_incoming is not None else None
    )
    victory_forecast = (
        f"до победы≈{enemy_turns} ход."
        if enemy_turns is not None
        else "урон по врагу изучается"
    )
    if incoming_known:
        survival_forecast = (
            f"запас≈{survival_turns} ход., входящий урон≈"
            f"{expected_incoming}/{worst_incoming}"
        )
    else:
        survival_forecast = "входящий урон изучается"
    forecast = f"{victory_forecast}, {survival_forecast}"

    treatment = available.get("лечение")
    renewal = available.get("обновление")

    if treatment is not None and can_survive_worst_hit is False:
        return CombatDecision(
            "лечение",
            SkillTarget.SELF,
            f"следующий максимальный удар может быть смертельным; {forecast}",
            urgent=True,
        )

    renewed_survival_turns = (
        max(
            0,
            math.floor(
                (effective_hp + future_renewal + renewal_tick * 3 - margin)
                / max(1, expected_incoming)
            ),
        )
        if expected_incoming is not None and renewal_tick > 0
        else None
    )
    renewal_makes_race_safe = (
        renewal is not None
        and memory.renewal_turns <= 0
        and renewal_tick > 0
        and missing_hp >= renewal_tick * 2
        and enemy_turns is not None
        and enemy_turns >= 3
        and can_survive_worst_hit is True
        and race_is_safe is False
        and renewed_survival_turns is not None
        and renewed_survival_turns >= enemy_turns
    )
    if renewal_makes_race_safe:
        return CombatDecision(
            "обновление",
            SkillTarget.SELF,
            f"периодическое лечение меняет прогноз на безопасный; {forecast}",
        )

    direct_heal = memory.direct_heal() or 0
    healed_hp = min(max_hp, effective_hp + direct_heal)
    healed_survival_turns = (
        max(
            0,
            math.floor(
                (healed_hp + future_renewal - margin) / max(1, expected_incoming)
            ),
        )
        if expected_incoming is not None and direct_heal > 0
        else None
    )

    unknown_enemy_emergency_hp = max(margin * 2, math.ceil(max_hp * 0.25))
    if (
        treatment is not None
        and not incoming_known
        and current_hp <= unknown_enemy_emergency_hp
    ):
        return CombatDecision(
            "лечение",
            SkillTarget.SELF,
            f"критический запас HP, а сила нового противника ещё изучается; {forecast}",
            urgent=True,
        )

    if (
        treatment is not None
        and race_is_safe is False
        and survival_turns is not None
        and (current_hp <= heal_threshold or survival_turns <= 2)
        and enemy_turns is not None
        and (survival_turns <= 2 or enemy_turns - survival_turns >= 2)
        and healed_survival_turns is not None
        and (
            healed_survival_turns >= enemy_turns
            or (survival_turns <= 2 and healed_survival_turns > survival_turns)
        )
        and direct_heal > 0
        and missing_hp >= direct_heal // 2
    ):
        return CombatDecision(
            "лечение",
            SkillTarget.SELF,
            f"мгновенное лечение меняет прогноз на безопасный; {forecast}",
            urgent=survival_turns <= 1,
        )

    if (
        renewal is not None
        and memory.renewal_turns <= 0
        and race_is_safe is False
        and treatment is None
        and renewal_tick > 0
        and missing_hp >= renewal_tick * 2
        and renewed_survival_turns is not None
        and survival_turns is not None
        and renewed_survival_turns > survival_turns
        and can_survive_worst_hit is True
    ):
        return CombatDecision(
            "обновление",
            SkillTarget.SELF,
            f"увеличивает запас ходов при недоступном мгновенном лечении; {forecast}",
        )

    holy_light = available.get("святое свечение")
    if (
        holy_light is not None
        and current_mana is not None
        and current_mana - holy_light.mana_cost >= HEALING_MANA_RESERVE
    ):
        return CombatDecision(
            "святое свечение",
            SkillTarget.ENEMY,
            f"лучший обычный урон; {forecast}",
        )

    if treatment is not None and can_survive_worst_hit is not False:
        return CombatDecision(
            "лечение",
            SkillTarget.ENEMY,
            f"урон сокращает бой выгоднее лечения себя; {forecast}",
        )

    if (
        renewal is not None
        and memory.renewal_turns <= 0
        and (
            race_is_safe is False
            or (race_is_safe is None and current_hp <= heal_threshold)
        )
    ):
        return CombatDecision(
            "обновление",
            SkillTarget.SELF,
            f"единственный доступный способ увеличить запас ходов; {forecast}",
        )

    basic = available.get("атака аколита")
    if basic is not None:
        return CombatDecision(
            "атака аколита",
            SkillTarget.ENEMY,
            f"сохранение маны; {forecast}",
        )

    return None
