"""Configuration loader for Lumivara Sentinel.

Reads the local .env file and exposes typed settings. Nothing here performs
network calls; it only parses configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


_VALID_STATS = ("str", "agi", "vit", "int", "dex", "luk")


@dataclass
class Config:
    telegram_token: str = ""
    owner_chat_id: int = 0
    cookie: str = ""

    action_delay_min: float = 0.45
    action_delay_jitter: float = 0.55
    ping_interval: float = 15.0

    quest_interval: float = 90.0
    stat_interval: float = 6.0
    equip_interval: float = 120.0

    stat_build: list[str] = field(default_factory=lambda: ["agi", "dex", "vit"])
    potion_hp_percent: int = 50

    enable_market: bool = False
    enable_sell: bool = False

    farm_zone: str = "auto"   # "auto" (by level) or a travel id like "snow"
    zone_margin: int = 5      # farm a zone only once we are this many levels above its min

    # town merchant run (needs ENABLE_SELL): sell loot, restock potions/arrows
    potion_target: int = 150      # keep this many Red Potions
    potion_budget_pct: int = 30   # spend at most this % of current silver per restock
    arrow_min: int = 1000         # buy arrows when below this

    @classmethod
    def load(cls) -> "Config":
        raw_build = os.getenv("STAT_BUILD", "agi,dex,vit,str,luk,int")
        build = [s.strip().lower() for s in raw_build.split(",") if s.strip().lower() in _VALID_STATS]
        if not build:
            build = ["agi", "dex", "vit", "str", "luk", "int"]

        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            owner_chat_id=_i("TELEGRAM_CHAT_ID", 0),
            cookie=os.getenv("LUMIVARA_COOKIE", "").strip(),
            action_delay_min=_f("ACTION_DELAY_MIN", 0.45),
            action_delay_jitter=_f("ACTION_DELAY_JITTER", 0.55),
            ping_interval=_f("PING_INTERVAL", 15.0),
            quest_interval=_f("QUEST_INTERVAL", 90.0),
            stat_interval=_f("STAT_INTERVAL", 6.0),
            equip_interval=_f("EQUIP_INTERVAL", 120.0),
            stat_build=build,
            potion_hp_percent=_i("POTION_HP_PERCENT", 50),
            enable_market=_b("ENABLE_MARKET", False),
            enable_sell=_b("ENABLE_SELL", False),
            farm_zone=(os.getenv("FARM_ZONE", "auto").strip().lower() or "auto"),
            zone_margin=_i("ZONE_MARGIN", 5),
            potion_target=_i("POTION_TARGET", 150),
            potion_budget_pct=_i("POTION_BUDGET_PCT", 30),
            arrow_min=_i("ARROW_MIN", 1000),
        )

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty if OK)."""
        problems = []
        if not self.telegram_token or ":" not in self.telegram_token:
            problems.append("TELEGRAM_BOT_TOKEN is missing or malformed.")
        if not self.owner_chat_id:
            problems.append("TELEGRAM_CHAT_ID is missing.")
        if not self.cookie:
            problems.append("LUMIVARA_COOKIE is empty — the bot cannot authenticate to the game yet.")
        return problems
