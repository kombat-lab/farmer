from __future__ import annotations

from collections.abc import Iterable

from battle_records import ItemDrop, RewardBundle
from rewards import BattleReward, parse_item_stack


def reward_bundle_from_raw(
    *, xp: int = 0, dust: int = 0, crystals: int = 0, items: Iterable[str] = ()
) -> RewardBundle:
    """Normalize current Russian display labels before entering the core ledger."""
    normalized: list[ItemDrop] = []
    for raw_item in items:
        name, quantity = parse_item_stack(raw_item)
        normalized.append(ItemDrop(
            name=name, quantity=quantity,
            is_card=name.casefold().startswith(("карта ", "🃏карта ", "🃏 карта ")),
        ))
    return RewardBundle(xp=xp, dust=dust, crystals=crystals, items=tuple(normalized))


def reward_bundle_from_reward(reward: BattleReward) -> RewardBundle:
    return reward_bundle_from_raw(
        xp=reward.xp, dust=reward.dust, crystals=reward.crystals, items=reward.items
    )
