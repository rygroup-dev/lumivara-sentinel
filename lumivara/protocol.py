"""Lumivara websocket/HTTP protocol.

All message shapes here were taken from observed traffic (HAR export + live
capture on the owner's own session). Only VERIFIED client->server messages are
exposed as helpers. Currency-moving messages (NPC sell, gold-market orders)
were NOT observed and are intentionally left as "unconfirmed" stubs that the
owner must capture before enabling.

Key findings encoded here:
* "Auto Play" in the game is CLIENT-SIDE. There is no server "autopilot on"
  message; the browser client simply emits attack/skill/pickup/potion based on
  its Bot Settings. So this bot reproduces that loop itself.
* attack{mob:id} makes the character approach + attack server-side (no separate
  move needed in the common case).
* mobInfo = per-instance table [{id,key,maxHp,level,elite}]; mobs = per-instance
  rows [id, x, y, hp, alive, _]. Join by id; `key` matches the monster whitelist.
"""
from __future__ import annotations

import copy
import math

WS_URL = "wss://lumivaraonline.com/api/ws?v=8&chat=id"
API_BASE = "https://lumivaraonline.com"
ORIGIN = "https://lumivaraonline.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# ---- mobs row indices (observed) ----
MOB_ID, MOB_X, MOB_Y, MOB_HP, MOB_ALIVE = 0, 1, 2, 3, 4

# Field maps for farming, from the client's zone table: (travel id, name, min Lv, max Lv).
# Towns/instances (rome, arena, tower, ...) are not reachable with `travel`.
FARM_ZONES = [
    ("field", "Verdant Forest", 1, 10),
    ("meadow", "Honeybloom Meadow", 11, 20),
    ("snow", "Frostveil Pass", 20, 30),
    ("desert", "Sunscar Desert", 31, 40),
    ("swamp", "Mirewillow Marsh", 41, 50),
    ("wildwood", "Elderwood Forest", 51, 60),
    ("dunes", "Crimson Dunes", 61, 70),
    ("glacier", "Frostfang Glacier", 71, 80),
    ("caldera", "Emberfall Caldera", 81, 90),
    ("moor", "Gloomveil Moor", 91, 100),
    ("crystal", "Amethyst Hollows", 101, 110),
    ("tempest", "Stormcrest Peaks", 110, 120),
    ("citadel", "Sunfallen Citadel", 121, 130),
]
FARM_ZONE_IDS = {z[0] for z in FARM_ZONES}

# Portal graph (zone -> zones reachable with one `travel`). The server only
# accepts travel to a connected zone, so multi-map trips go hop by hop.
ZONE_LINKS = {
    "arena": ["field"], "training": ["field"], "tower": ["rome"],
    "field": ["arena", "crystal", "meadow", "rome", "training", "wildwood"],
    "rome": ["desert", "field", "snow", "swamp"],
    "grove": ["field", "ruins", "temple"], "ruins": ["grove", "snow"],
    "snow": ["glacier", "rome", "swamp", "tempest"],
    "desert": ["caldera", "citadel", "dunes", "glacier", "rome"],
    "swamp": ["moor", "rome", "snow", "wildwood"],
    "wildwood": ["field", "swamp"], "meadow": ["dunes", "field"],
    "dunes": ["desert", "meadow"], "glacier": ["desert", "snow"],
    "caldera": ["desert"], "moor": ["swamp"], "crystal": ["field"],
    "tempest": ["snow"], "citadel": ["desert"], "cove": ["field"],
    "temple": ["grove"], "inn": ["field"],
}


# Where each portal stands: PORTALS[zone][destination] = (x, y). The server
# only honours `travel` when the character is standing at the matching portal
# (towns are lenient). Extracted from the client zone table.
PORTALS = {
    "arena": {"field": (1536, 3012)}, "training": {"field": (960, 1150)},
    "tower": {"rome": (1536, 3012)},
    "field": {"rome": (1456, 140), "training": (2832, 240), "wildwood": (180, 1536),
              "meadow": (2992, 1120), "crystal": (1024, 2992), "arena": (240, 240)},
    "rome": {"field": (960, 1060), "snow": (960, 222), "desert": (1690, 640), "swamp": (226, 640)},
    "grove": {"field": (96, 640), "ruins": (1800, 640), "temple": (960, 1120)},
    "ruins": {"grove": (96, 640), "snow": (1800, 640)},
    "snow": {"rome": (1536, 3012), "swamp": (60, 1536), "glacier": (3012, 1536), "tempest": (1536, 60)},
    "desert": {"rome": (60, 1536), "glacier": (1536, 60), "dunes": (1536, 3012),
               "caldera": (3012, 1536), "citadel": (3012, 2560)},
    "swamp": {"rome": (3012, 1536), "snow": (1536, 60), "wildwood": (1536, 3012), "moor": (1536, 60)},
    "wildwood": {"swamp": (1716, 80), "field": (2972, 1656)},
    "meadow": {"field": (60, 1536), "dunes": (3012, 1536)},
    "dunes": {"desert": (1536, 60), "meadow": (60, 1536)},
    "glacier": {"snow": (60, 1536), "desert": (1616, 2972)},
    "caldera": {"desert": (60, 1536)}, "moor": {"swamp": (1536, 3012)},
    "crystal": {"field": (1536, 60)}, "tempest": {"snow": (1536, 3012)},
    "citadel": {"desert": (60, 1536)}, "cove": {"field": (360, 880)},
    "temple": {"grove": (64, 512)}, "inn": {"field": (480, 534)},
}
PORTAL_RANGE = 90


def _zone_min_level(zone: str) -> int:
    return next((lo for z, _n, lo, _hi in FARM_ZONES if z == zone), 0)


def next_hop(src: str | None, dst: str, level: int | None = None) -> str:
    """First zone to travel to on the shortest portal path src -> dst
    (falls back to dst when the route is unknown). With `level`, the route
    never passes *through* a map whose monsters are well above us — walking
    across Frostfang Glacier at Lv49 is a death trip; going via town is not."""
    if not src or src == dst or src not in ZONE_LINKS:
        return dst

    def passable(z: str) -> bool:
        return z == dst or level is None or _zone_min_level(z) <= level + 5

    prev = {src: None}
    queue = [src]
    while queue:
        cur = queue.pop(0)
        if cur == dst:
            break
        for nxt in ZONE_LINKS.get(cur, []):
            if nxt not in prev and passable(nxt):
                prev[nxt] = cur
                queue.append(nxt)
    if dst not in prev and level is not None:
        return next_hop(src, dst)       # no safe route: fall back to the shortest
    if dst not in prev:
        return dst
    hop = dst
    while prev[hop] != src:
        hop = prev[hop]
    return hop


def gold_order(side: str, price: int, quantity: int, budget: int | None = None) -> dict:
    """Gold Exchange order (from client code). side is "buy" or "sell"; price is
    the limit in silver per gold; `instant` fills against the book now. For an
    instant buy the client also sends `budget` = most silver it may spend
    (fees included) — the server never takes more than that."""
    m = {"type": "goldOrder", "side": side, "price": int(price), "quantity": int(quantity), "instant": True}
    if budget is not None:
        m["budget"] = int(budget)
    return m


def market_depth(item: str, page: int = 0, size: int = 20, ask: int = 1, **query) -> dict:
    """Player-market board for one item (read-only), as the client sends it."""
    return {"type": "marketGetDepth", "item": item,
            "query": {"sort": "priceAsc", "page": page, "size": size, **query, "ask": ask}}


def market_order(side: str, item: str, price: int, quantity: int, instant: bool = False) -> dict:
    """Item order on the player market (silver). side "sell" lists our stack,
    "buy" places a bid; instant=True fills against the book immediately."""
    m = {"type": "marketCreateOrder", "side": side, "item": item,
         "price": int(price), "quantity": int(quantity)}
    if instant:
        m["instant"] = True
    return m


def market_list_gear(gear_id: str, price: int) -> dict:
    return {"type": "marketListGear", "gearId": gear_id, "price": int(price)}


def market_cancel_order(order_id: str) -> dict:
    return {"type": "marketCancelOrder", "orderId": order_id}


def market_buy_listing(listing_id: str) -> dict:
    return {"type": "marketBuyListing", "listingId": listing_id}


# Market filter for each worn slot: (cat, part). Weapons are filtered by job.
SLOT_MARKET_QUERY = {
    "sword": ("weapon", None), "armor": ("armor", "armor"), "head": ("armor", "head"),
    "face": ("armor", "face"), "mouth": ("armor", "mouth"), "garment": ("armor", "garment"),
    "shoes": ("armor", "shoes"), "accessory1": ("accessory", None), "accessory2": ("accessory", None),
    "shield": ("other", None),     # bow users: the quiver ("ammo") is listed under "other"
}


def market_cancel_listing(listing_id: str) -> dict:
    return {"type": "marketCancelListing", "listingId": listing_id}


def market_close() -> dict:
    return {"type": "marketClose"}


def gold_cancel(order_id: str) -> dict:
    return {"type": "goldCancel", "orderId": order_id}


def mail_claim() -> dict:
    return {"type": "mailClaim"}


def claim_hunt() -> dict:
    """Daily hunting-journal reward (100 kills)."""
    return {"type": "claimHunt"}


def sell_batch(items: list[dict]) -> dict:
    """items: [{"item": key, "quantity": n}] or [{"gearId": id}]. Needs the town merchant."""
    return {"type": "sellBatch", "items": items}


def shop_buy(entry: str, quantity: int) -> dict:
    """NPC merchant purchase. Needs the town merchant; entry ids not yet confirmed."""
    return {"type": "buy", "entry": entry, "quantity": int(quantity)}


# ---- town NPCs (area "rome"), from the client layout table ----
TOWN = "rome"
NPC_RANGE = 110
MERCHANT_POS = (1090, 700)   # Silver merchant: buys loot, sells potions/arrows
BROKER_POS = (1320, 740)     # Market broker: the player market only answers next to him
POTION_BUY_ENTRY = "potion"  # Red Potion, 10 silver, best silver-per-HP
POTION_PRICE = 10
ARROW_ENTRY = "iron_arrow"   # 0.1 silver each, one per shot
RETURN_SCROLL = "butterfly_wing"  # "Return Scroll": warps to the town fountain (30 silver)
SELL_PROTECT_WORDS = ("card",)  # never sell cards even if unknown to the item table


def is_sellable_loot(item: str) -> bool:
    return item not in KNOWN_ITEMS and not any(w in item for w in SELL_PROTECT_WORDS)


# ---- per-class gear / skill / stat preferences ----
# Weapons all sit in the "sword" slot and differ by template (seen in gearRows).
WEAPON_SLOT = "sword"
TWO_HANDED = {"bow"}            # bow users wear the quiver in the shield hand
MAGIC_CLASSES = {"mage", "acolyte", "nekobaku"}
TIER_LEVEL = [1, 20, 40, 60, 80]   # min level for tier I..V gear
CLASS_WEAPONS = {
    "archer": ["bow"],
    "swordman": ["blade", "falchion", "sword"],
    "thief": ["dagger_pair", "knife"],
    "mage": ["staff", "quarterstaff", "rod"],
    "acolyte": ["quarterstaff", "staff", "rod"],
    "merchant": ["hammer"],
    "kensei": ["katana"],
    "mamushi": ["kunai"],
    "nekobaku": ["flask"],
}
# Order to learn skills in. Damage passives first (they boost every hit and
# cost no SP), then the main attack skill, then AoE / self-buffs. From the
# client skill table; keys the server doesn't know are simply rejected.
CLASS_SKILLS = {
    "archer": ["owleye", "vultureeye", "doublestrafe", "piercingshot", "arrowshower",
               "spiritinstinct", "leapshot"],
    "swordman": ["swordmastery", "hprecovery", "ironbody", "bash", "magnum", "challenging"],
    "mage": ["sp_recovery", "mentality", "firebolt", "coldbolt", "thunderstorm", "tierra",
             "magemastery"],
    "acolyte": ["demonbane", "lightshadow", "holylight", "heal", "holyguard", "blessing"],
    "merchant": ["weighttraining", "overcharge", "discount", "mammonite", "cartstrike",
                 "hammerfall", "overthrust"],
    "thief": ["doubleattack", "fatalsense", "improvedodge", "twinfang", "shadowburst",
              "venomknife", "vanish"],
    "mamushi": ["venommastery", "predator", "numbingtoxin", "viperfang", "venomrupture",
                "dokugiri", "dokunuri"],
    "kensei": ["katanamastery", "zanshin", "shukuchi", "iaigiri", "issen", "oboro", "meikyo"],
    "nekobaku": ["alchemymastery", "catreflex", "ninelives", "nekoflask", "bigkaboom",
                 "tarflask", "catbrew"],
}
# SP cost per cast (client skill table). Used to keep SP for the skills that
# matter instead of spamming casts the server refuses.
SKILL_SP = {
    "doublestrafe": 7, "arrowshower": 9, "piercingshot": 9, "leapshot": 9, "spiritinstinct": 20,
    "firebolt": 42, "coldbolt": 42, "thunderstorm": 42, "tierra": 42, "magemastery": 35,
    "holylight": 56, "heal": 69, "holyguard": 15, "mammonite": 15, "cartstrike": 18,
    "hammerfall": 27, "overthrust": 205, "twinfang": 7, "venomknife": 3, "shadowburst": 6,
    "vanish": 15, "viperfang": 7, "dokugiri": 20, "venomrupture": 30, "dokunuri": 30,
    "iaigiri": 7, "oboro": 6, "issen": 3, "meikyo": 30, "nekoflask": 12, "tarflask": 38,
    "bigkaboom": 69, "catbrew": 30,
}
# Self-buffs worth keeping up while farming: skill -> recast interval (s).
CLASS_BUFFS = {
    "archer": {"spiritinstinct": 21},
    "mamushi": {"dokunuri": 31},
    "kensei": {"meikyo": 31},
    "nekobaku": {"catbrew": 31},
    "merchant": {"overthrust": 31},
}
# Stat weights: free points are spent to keep stats close to these ratios.
CLASS_STAT_WEIGHTS = {
    "archer": {"dex": 3, "agi": 2, "vit": 1},
    "swordman": {"str": 3, "vit": 2, "agi": 1},
    "thief": {"agi": 3, "str": 2, "dex": 1},
    "mage": {"int": 3, "dex": 2, "vit": 1},
    "acolyte": {"int": 2, "vit": 2, "dex": 1},
    "merchant": {"str": 3, "vit": 2, "dex": 1},
}
# How much each gear bonus is worth when choosing armor/accessories.
GEAR_SCORE = {
    "def": 2.0, "mdef": 1.0, "flee": 2.0, "hit": 1.5, "crit": 1.0, "aspd": 3.0,
    "hp": 0.05, "sp": 0.02, "atk": 1.0, "matk": 1.0,
    "str": 1.0, "agi": 1.0, "vit": 1.0, "int": 1.0, "dex": 1.0, "luk": 0.5,
}

# ---- monster table (client bundle) ----
# key -> (hp, atk, def, flee, exp). `flee` is what our HIT has to beat.
MONSTERS = {
    "prism-hopper": (40, 6, 0, 101, 10), "leaf-sprout": (61, 10, 0, 103, 23),
    "dew-bunny": (92, 15, 0, 106, 43), "moss-mushroom": (138, 20, 0, 108, 59),
    "thorn-pixie": (179, 24, 0, 110, 78), "bramble-hare": (261, 31, 0, 113, 107),
    "honey-moth": (288, 33, 0, 115, 126), "rootling": (362, 38, 0, 118, 159),
    "amber-beetle": (450, 42, 0, 120, 183), "frost-puff": (680, 52, 0, 125, 255),
    "icicle-hare": (1119, 66, 0, 132, 370), "snow-owl": (1358, 78, 0, 138, 483),
    "aurora-fox": (2259, 91, 0, 145, 634), "dune-gecko": (2805, 100, 0, 150, 753),
    "scarab-sentinel": (3224, 114, 0, 157, 939), "sunscale-cobra": (4584, 126, 0, 163, 1112),
    "cactus-imp": (5915, 138, 0, 170, 1332), "tide-crab": (8497, 147, 0, 175, 1503),
    "bog-toad": (9217, 158, 0, 180, 1682), "mud-newt": (10279, 167, 0, 185, 1871),
    "mire-jelly": (12184, 177, 0, 190, 2072), "reed-mantis": (18443, 387, 0, 333, 2282),
    "forest-wolf": (15240, 199, 90, 354, 2502), "lantern-wisp": (15625, 209, 30, 205, 2731),
    "ember-antler": (20995, 216, 90, 298, 2973), "moon-owl": (22793, 226, 60, 347, 3223),
    "vine-lynx": (27332, 237, 0, 500, 3485), "wild-boar": (39489, 246, 60, 225, 3752),
    "sand-drake": (45583, 285, 60, 245, 4933), "cave-bat": (48588, 296, 90, 355, 5249),
    "pebble-golem": (56436, 331, 90, 270, 6631), "magma-salamander": (89052, 365, 90, 424, 7001),
    "ember-hound": (94472, 412, 90, 389, 7536), "basalt-tortoise": (104503, 368, 150, 389, 8010),
    "obsidian-scorpion": (124941, 684, 90, 455, 8582), "shade-raven": (174122, 393, 45, 700, 9002),
    "bone-hound": (262474, 407, 66, 389, 9607), "bog-wraith": (210176, 418, 45, 467, 10141),
    "grave-knight": (304162, 431, 180, 100, 10782), "quartz-crawler": (177331, 600, 192, 333, 11252),
    "geode-armadillo": (154411, 399, 243, 333, 11927), "prism-serpent": (197496, 466, 210, 345, 12521),
    "amethyst-wyvern": (254142, 569, 225, 345, 13232), "thunder-ram": (288140, 589, 195, 657, 13752),
    "storm-roc": (239523, 788, 165, 747, 14497), "cloud-serpent": (301844, 666, 210, 694, 16541),
    "tempest-drake": (424146, 897, 255, 500, 21000),
    "gilded-sentinel": (1150000, 1250, 255, 450, 105000),
    "radiant-griffin": (980000, 1450, 200, 760, 98000),
    "fallen-seraph": (900000, 1150, 180, 600, 95000),
    "sunfire-colossus": (1600000, 1800, 255, 380, 160000),
}
# Monsters per farm map; within a map they are ordered weakest -> strongest and
# spread evenly over the map's loot-level range (LOOT_LEVELS).
ZONE_MOBS = {
    "field": ["prism-hopper", "leaf-sprout", "dew-bunny", "moss-mushroom", "thorn-pixie"],
    "meadow": ["bramble-hare", "honey-moth", "rootling", "amber-beetle"],
    "snow": ["frost-puff", "icicle-hare", "snow-owl", "aurora-fox"],
    "desert": ["dune-gecko", "scarab-sentinel", "sunscale-cobra", "cactus-imp"],
    "swamp": ["tide-crab", "bog-toad", "mud-newt", "mire-jelly", "reed-mantis"],
    "wildwood": ["lantern-wisp", "forest-wolf", "ember-antler", "moon-owl", "vine-lynx"],
    "dunes": ["wild-boar", "sand-drake"],
    "glacier": ["cave-bat", "pebble-golem"],
    "caldera": ["magma-salamander", "ember-hound", "basalt-tortoise", "obsidian-scorpion"],
    "moor": ["shade-raven", "bone-hound", "bog-wraith", "grave-knight"],
    "crystal": ["quartz-crawler", "geode-armadillo", "prism-serpent", "amethyst-wyvern"],
    "tempest": ["thunder-ram", "storm-roc", "cloud-serpent", "tempest-drake"],
    "citadel": ["gilded-sentinel", "radiant-griffin", "fallen-seraph", "sunfire-colossus"],
}
LOOT_LEVELS = {
    "field": (1, 10), "meadow": (11, 20), "snow": (25, 45), "desert": (50, 70),
    "swamp": (75, 95), "wildwood": (100, 120), "dunes": (125, 145), "glacier": (150, 170),
    "caldera": (175, 195), "moor": (200, 220), "crystal": (225, 245), "tempest": (250, 270),
    "citadel": (272, 290),
}
LOOT_DROP_RATE = 0.3   # chance a kill drops its monster material


def _mob_loot_level() -> dict[str, int]:
    out = {}
    for zone, mobs in ZONE_MOBS.items():
        lo, hi = LOOT_LEVELS[zone]
        ordered = sorted(mobs, key=lambda k: MONSTERS[k][0])
        for i, k in enumerate(ordered):
            out.setdefault(k, round((lo + hi) / 2) if len(ordered) == 1
                           else round(lo + (hi - lo) * i / (len(ordered) - 1)))
    return out


MOB_LOOT_LEVEL = _mob_loot_level()


def loot_price(mob_key: str) -> int:
    """NPC sell price of a monster's material (client formula, capped at 50)."""
    lv = MOB_LOOT_LEVEL.get(mob_key, 1)
    return min(50, max(1, round(50 * (max(1, lv) / 270) ** 1.5)))


def mob_silver_per_kill(mob_key: str) -> float:
    return LOOT_DROP_RATE * loot_price(mob_key)


# monster material item -> monster (client table)
LOOT_ITEM_MOB = {
    "jelly": "prism-hopper", "sprout_leaf": "leaf-sprout", "dewdrop_tuft": "dew-bunny",
    "moss_spore_cap": "moss-mushroom", "pixie_thorn": "thorn-pixie",
    "bramble_fur": "bramble-hare", "honey_wing_dust": "honey-moth", "twisted_root": "rootling",
    "amber_shell": "amber-beetle", "frost_fluff": "frost-puff", "icicle_whisker": "icicle-hare",
    "snow_feather": "snow-owl", "aurora_tail": "aurora-fox", "gecko_scale": "dune-gecko",
    "scarab_carapace": "scarab-sentinel", "cobra_fang": "sunscale-cobra",
    "cactus_needle": "cactus-imp", "crab_claw": "tide-crab", "toad_wart": "bog-toad",
    "newt_tail": "mud-newt", "mire_gel": "mire-jelly", "mantis_blade": "reed-mantis",
    "wolf_fang": "forest-wolf", "wisp_ember": "lantern-wisp",
    "smoldering_antler": "ember-antler", "moonlit_plume": "moon-owl", "lynx_claw": "vine-lynx",
    "boar_tusk": "wild-boar", "drake_scale": "sand-drake", "bat_wing": "cave-bat",
    "golem_core": "pebble-golem", "magma_scale": "magma-salamander",
    "cinder_fang": "ember-hound", "basalt_shell": "basalt-tortoise",
    "obsidian_stinger": "obsidian-scorpion", "shade_feather": "shade-raven",
    "hound_bone": "bone-hound", "wraith_shroud": "bog-wraith", "grave_crest": "grave-knight",
    "quartz_leg": "quartz-crawler", "geode_plate": "geode-armadillo",
    "prism_scale": "prism-serpent", "wyvern_scale": "amethyst-wyvern",
    "thunder_horn": "thunder-ram", "roc_plume": "storm-roc", "cloud_pearl": "cloud-serpent",
    "tempest_dragon_scale": "tempest-drake", "sun_plate": "gilded-sentinel",
    "griffin_quill": "radiant-griffin", "fallen_halo": "fallen-seraph",
    "sun_core": "sunfire-colossus",
}
# Consumables worth more to other players than to us: sell everything above
# the amount kept for our own use. Market prices (2026-10-09): blue potion
# ~62, relic box ~1375, concentration potion ~85, fly/butterfly wing ~10-14.
# EXP/drop scrolls are kept (worth using), throwing blades have no buyers.
MARKET_SELL_KEEP = {
    "blue_potion": 20,          # the farm loop doesn't drink SP potions
    "fly_wing": 10,
    "butterfly_wing": 15,       # Return Scrolls: we use a few
    "concentration_potion": 0,
    "relic_box": 0,
    "card_album": 0,            # players pay ~9,000-11,800 (NPC 500); was never sold
}
# Salvage fragments from dismantled gear (~40-90 silver each on the market).
FRAGMENTS = [f"{k}_fragment_{t}" for k in ("weapon", "equipment") for t in range(1, 6)]
MARKET_SELL_KEEP.update({f: 0 for f in FRAGMENTS})
GEAR_RELISTS_BEFORE_SALVAGE = 2   # unsold this many times -> dismantle into fragments


def dismantle(gear_ids: list[str]) -> dict:
    """Salvage gear into fragments (client: up to a batch per message)."""
    return {"type": "dismantle", "gearIds": list(gear_ids)}
# Drops worth a Telegram ping (cards are matched by their "_card" suffix).
RARE_DROPS = {"relic_box", "card_album", "white_potion"}

# Never accepted by the player market (client list), on top of cosmetics.
UNTRADABLE = {"exp_scroll", "drop_scroll", "break_protection_stone", "gym_pass",
              "rename_ticket", "megaphone", "skin_voucher"}

# Rough player-market prices (bid side) used before a live board is read.
MARKET_PRICE_HINT = {
    "blue_potion": 45, "relic_box": 1000, "concentration_potion": 50, "fly_wing": 11,
    "butterfly_wing": 11, "refine_stone": 43, "jelly": 25,
    **{f: 40 for f in FRAGMENTS},
}
# What the town merchant charges, so we can buy from players when cheaper.
NPC_BUY_PRICES = {"potion": 10, "butterfly_wing": 30, "orange_potion": 50, "yellow_potion": 120}
# Main healing potion: players sell Yellow Potions (300-350 HP) for ~13 silver,
# ~7x more HP per silver than a Red Potion (37-53 HP) from the merchant at 10.
BIG_POTION = "yellow_potion"
BIG_POTION_HEAL = 325
# bigger heals first when drinking
HEAL_ORDER = ("white_potion", "yellow_potion", "orange_potion")

# NPC merchant buy-back price for items that aren't monster materials, from the
# client item table (`price`). refine_stone (50) is left out on purpose: the
# merchant run only sells monster loot, so a refine stone routed "to the NPC"
# would just pile up instead of selling on the market.
NPC_PRICES = {
    "potion": 2, "orange_potion": 3, "yellow_potion": 4, "white_potion": 5,
    "blue_potion": 10, "concentration_potion": 60, "awakening_potion": 125,
    "berserk_potion": 250, "fly_wing": 15, "butterfly_wing": 15,
    "relic_box": 250, "card_album": 500, "sacred_feather": 250,
}

# Bag weight & NPC gear prices (client tables). Above 90% of max weight the
# server refuses attacks and skills ("น้ำหนักเกิน 90%"), so carried gear must be
# kept in check. Worn gear weighs nothing; loot materials and cards weigh 0.1.
GEAR_WEIGHT = {"head": 20, "face": 10, "mouth": 5, "sword": 50, "shield": 40, "armor": 80,
               "garment": 20, "shoes": 25, "accessory1": 5, "accessory2": 5, "ammo": 1}
GEAR_NPC_BASE = {"head": 60, "face": 35, "mouth": 25, "sword": 100, "shield": 80, "armor": 120,
                 "garment": 50, "shoes": 45, "accessory1": 75, "accessory2": 75, "ammo": 10}
OVERWEIGHT_MARK = "90%"
FRAGMENT_VALUE_PER_TIER = 40   # rough market value of one fragment per gear tier


def gear_weight(g: dict) -> float:
    return 0 if g.get("gift") else GEAR_WEIGHT.get(g.get("slot"), 0)


def gear_npc_price(g: dict) -> int:
    """What the town merchant pays for a piece (verified: T1 coat with 7 bonus = 78)."""
    base = GEAR_NPC_BASE.get(g.get("slot"), 0)
    base += 5 * sum(v for v in (g.get("bonuses") or {}).values() if isinstance(v, (int, float)))
    return max(1, round(base * 0.5)) if base > 0 else 0


# Player-market fees (client constants): 2.5% listing fee paid up front,
# 8% tax on what sells (less with premium), 1% on buy orders.
MARKET_LISTING_FEE = 0.025
MARKET_SALE_TAX = 0.08
MARKET_BUY_FEE = 0.01


def npc_price(item: str) -> int:
    """Silver the town merchant pays per unit (0 = unknown / not sellable there)."""
    mob = LOOT_ITEM_MOB.get(item)
    if mob:
        return loot_price(mob)
    return NPC_PRICES.get(item, 0)


def market_net(price: float, qty: int = 1) -> float:
    """Silver kept from selling qty at price on the player market (after fees)."""
    total = price * qty
    return total - math.floor(total * MARKET_SALE_TAX) - math.ceil(total * MARKET_LISTING_FEE)


# HP potion items used by the "potion" hotbar action (red first, per in-game label)
HP_POTIONS = ("potion", "orange_potion", "yellow_potion", "white_potion")

# ---- starter quest chain (extracted from the client bundle) ----
# The whole quest system is this 10-step tutorial; after it, play is free-form.
# goal.type -> counter key: killNamed uses "kill:<mob>", everything else uses the
# type name; "level" compares against the character's base level directly.
QUESTS = [
    {"id": "first-steps", "title": "First Steps", "goal": {"type": "killNamed", "count": 1, "mob": "Prism Hopper"}},
    {"id": "field-loot", "title": "Field Loot", "goal": {"type": "pickup", "count": 3}},
    {"id": "basic-power", "title": "Basic Power", "goal": {"type": "stat", "count": 1}},
    {"id": "first-gear", "title": "First Gear", "goal": {"type": "equip", "count": 1}},
    {"id": "life-force", "title": "Life Force", "goal": {"type": "potion", "count": 1}},
    {"id": "first-skill", "title": "First Skill", "goal": {"type": "learn", "count": 1}},
    {"id": "true-class", "title": "True Class (change job)", "goal": {"type": "changeClass", "count": 1}},
    {"id": "border-economy", "title": "Border Economy (sell+buy)", "goal": {"type": "sell", "count": 1}},
    {"id": "wider-world", "title": "Wider World (travel)", "goal": {"type": "travel", "count": 1}},
    {"id": "true-conqueror", "title": "True Conqueror (Base Lv.10)", "goal": {"type": "level", "count": 10}},
]

# goal types the bot naturally completes on its own (farming / auto-stat / auto-travel)
AUTO_QUEST_GOALS = {"killNamed", "kill", "pickup", "potion", "stat", "travel", "level"}
# goal types that need a human decision or an uncaptured message
MANUAL_QUEST_GOALS = {"changeClass", "sell", "equip", "learn"}


def quest_counter_key(goal: dict) -> str:
    if goal.get("type") == "killNamed":
        return f"kill:{goal.get('mob')}"
    return goal.get("type", "")

# ---- default Bot Settings (captured in full; 55-monster whitelist) ----
DEFAULT_BOT_SETTINGS = {
    "skills": {},
    "skillDefaults": 2,
    "rotation": {
        "enabled": True,
        "steps": {
            "swordman": ["magnum", "bash"],
            "novice": [],
            "mage": ["firebolt", "coldbolt", "thunderstorm", "tierra"],
            "archer": ["arrowshower", "doublestrafe", "piercingshot", "leapshot"],
            "acolyte": ["holylight"],
            "merchant": ["cartstrike", "hammerfall", "mammonite"],
            "thief": ["shadowburst", "twinfang", "venomknife"],
            "mamushi": ["dokugiri", "viperfang", "venomrupture"],
            "kensei": ["iaigiri", "oboro", "issen"],
            "nekobaku": ["tarflask", "bigkaboom", "nekoflask"],
        },
    },
    "potion": {"enabled": True, "hpPercent": 50},
    "sp": {"enabled": False, "percent": 30},
    "aspdPotion": {"enabled": False},
    "range": {"enabled": False, "radius": 400},
    "priority": "loot",
    "loot": {"consumption": True, "etc": True, "card": True, "equipment": True},
    "monsters": {
        "moss-mushroom": True, "amber-beetle": True, "cave-bat": True, "tide-crab": True,
        "leaf-sprout": True, "forest-wolf": True, "wild-boar": True, "lantern-wisp": True,
        "pebble-golem": True, "rootling": True, "prism-hopper": True, "dew-bunny": True,
        "thorn-pixie": True, "honey-moth": True, "bramble-hare": True, "frost-puff": True,
        "icicle-hare": True, "snow-owl": True, "aurora-fox": True, "dune-gecko": True,
        "scarab-sentinel": True, "sunscale-cobra": True, "cactus-imp": True, "bog-toad": True,
        "mud-newt": True, "mire-jelly": True, "reed-mantis": True, "ember-antler": True,
        "moon-owl": True, "vine-lynx": True, "sand-drake": True, "magma-salamander": True,
        "ember-hound": True, "basalt-tortoise": True, "obsidian-scorpion": True,
        "shade-raven": True, "bone-hound": True, "bog-wraith": True, "grave-knight": True,
        "quartz-crawler": True, "geode-armadillo": True, "prism-serpent": True,
        "amethyst-wyvern": True, "thunder-ram": True, "storm-roc": True, "cloud-serpent": True,
        "tempest-drake": True, "gilded-sentinel": True, "radiant-griffin": True,
        "fallen-seraph": True, "sunfire-colossus": True, "poring": True, "monster": True,
        "king": False, "worldboss": False,
    },
}


def bot_settings(hp_percent: int | None = None) -> dict:
    """A deep copy of the default bot settings, optionally with a custom HP%."""
    s = copy.deepcopy(DEFAULT_BOT_SETTINGS)
    if hp_percent is not None:
        s["potion"]["hpPercent"] = int(hp_percent)
    return {"type": "botSettings", "settings": s}


# ---------- verified client -> server messages ----------
def ping() -> dict:
    return {"type": "ping"}


def spawn_ready() -> dict:
    return {"type": "spawnReady"}


def revive() -> dict:
    return {"type": "revive"}


def storage_open() -> dict:
    return {"type": "storageOpen"}


def move(x: int, y: int) -> dict:
    return {"type": "move", "x": int(x), "y": int(y)}


def stop() -> dict:
    return {"type": "stop"}


def attack(mob_id: int) -> dict:
    return {"type": "attack", "mob": int(mob_id)}


def skill(name: str, mob_id: int | None = None) -> dict:
    m = {"type": "skill", "skill": name}
    if mob_id is not None:
        m["mob"] = int(mob_id)
    return m


def pickup(drop_id: str) -> dict:
    return {"type": "pickup", "id": drop_id}


def use_item(item: str) -> dict:
    return {"type": "use", "item": item}


def potion() -> dict:
    return {"type": "potion"}


def stat(stat_name: str) -> dict:
    return {"type": "stat", "stat": stat_name}


def claim_quest() -> dict:
    return {"type": "claimQuest"}


def claim_daily() -> dict:
    return {"type": "claimDaily"}


def travel(to: str) -> dict:
    return {"type": "travel", "to": to}


def auto_equip(equipped: dict | None = None) -> dict:
    # Format confirmed; semantics (auto-pick best vs. set exact) still to verify.
    return {"type": "autoEquip", "equipped": equipped or {}}


def equip(item_id: str) -> dict:
    return {"type": "equip", "id": item_id}


def change_class(class_id: str) -> dict:
    # captured: {"type":"changeClass","classId":"archer"}
    return {"type": "changeClass", "classId": class_id}


def hotbar(class_id: str, layout: dict) -> dict:
    # captured alongside changeClass; sets the per-class skill/item bar
    return {"type": "hotbar", "classId": class_id, "layout": layout}


def learn_skills(levels: dict) -> dict:
    # captured: {"type":"learnSkills","levels":{"firstaid":5}} (target levels)
    return {"type": "learnSkills", "levels": levels}


def reset_skills() -> dict:
    """Refund the current class's skill points (free up to Lv80)."""
    return {"type": "resetSkills"}


def reset_stats() -> dict:
    """All stats back to 1, points refunded (free up to Lv80)."""
    return {"type": "resetStats"}


FREE_RESET_MAX_LEVEL = 80
STAT_MASTER_POS = (585, 720)   # "Reset Master" NPC in town; resets only work next to him


def gold_watch() -> dict:
    return {"type": "goldWatch"}


def gold_close() -> dict:
    return {"type": "goldClose"}


def sit(down: bool = True, facing: str = "north") -> dict:
    return {"type": "sit", "sit": bool(down), "facing": facing}


def dash(x: int, y: int) -> dict:
    return {"type": "dash", "x": int(x), "y": int(y)}


def mail_open() -> dict:
    return {"type": "mailOpen"}


def mail_close() -> dict:
    return {"type": "mailClose"}


def friends_open() -> dict:
    return {"type": "friendsOpen"}


def guild_open() -> dict:
    return {"type": "guildOpen"}



# Items defined in the client item table (usable / material / cash). Anything in the
# bag that is NOT in here is monster loot and safe to sell to the merchant.
KNOWN_ITEMS = frozenset({
    'amethyst_witch', 'aqua_tide', 'aviator_owl', 'awakening_potion', 'azure_kensei',
    'berserk_potion', 'bloodfang_vampire', 'blossom_witch', 'blue_potion',
    'brass_gyrocopter', 'break_protection_stone', 'butterfly_wing', 'canvas_ornithopter',
    'card_album', 'celestial_star', 'clockwork_brass', 'concentration_potion',
    'crimson_demon', 'crimson_kunoichi', 'crimson_rogue', 'crimson_shade',
    'crimson_swordsman', 'demon_fox_kunoichi', 'demon_fox_ninja', 'demon_wings',
    'divine_wings', 'dream_cloud', 'drop_scroll', 'duskwing_sled', 'empyrean_drake',
    'exp_scroll', 'fly_wing', 'frost_alchemist_neko', 'frostveil_skystag', 'gym_pass',
    'high_priestess', 'indigo_sister', 'iron_arrow', 'kitsune_kensei', 'meadow_pegasus',
    'megaphone', 'midnight_alchemist_neko', 'moonlit_kunoichi', 'moonlit_skywolf',
    'orange_potion', 'pastel_merchant', 'pathfinder_merchant', 'potion', 'princess_knight',
    'pumpkin_ghost_neko', 'refine_stone', 'relic_box', 'rename_ticket', 'royal_crimson',
    'sacred_feather', 'saffron_monk', 'sakura_bloom', 'sakura_cloud_fox',
    'sapphire_bishop', 'scarlet_assassin', 'seraph_wings', 'shadow_archer',
    'shadow_hunter', 'shard', 'silverwind_ranger', 'skin_voucher', 'skytide_manta',
    'snowfur_alchemist', 'soulreaper_scythes', 'starfall_phoenix', 'starlight_mage',
    'starlull_cloud', 'stormbolt_wings', 'sunfin_goldfish', 'sunsail_skiff', 'sylvan_sage',
    'throwing_blades', 'verdant_leafboat', 'verdant_vine', 'violet_eclipse',
    'wayfarer_merchant', 'white_potion', 'witchwind_carpet', 'wrathflame_wings',
    'yellow_potion',
})
