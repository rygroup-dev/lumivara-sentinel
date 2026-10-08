"""Automation loops.

Only SAFE, high-confidence behaviours run automatically:
  * farm   - reproduce the client's auto-play loop (attack / rotation skill /
             pickup / potion / revive). Combat is self-driven because the
             game's "Auto Play" is client-side.
  * quest  - periodically claim completed quests + the daily reward.
  * stats  - spend free stat points following a configured build order.

Everything that could damage the account if sent wrong (auto-equip with an
explicit set, currency moves) is left to explicit manual actions.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import time

from . import protocol as P
from .client import LumivaraClient
from .config import Config
from .intel import FarmIntel
from .state import GameState

log = logging.getLogger("lumivara.auto")


class Automator:
    def __init__(self, cfg: Config, state: GameState, client: LumivaraClient) -> None:
        self.cfg = cfg
        self.state = state
        self.client = client
        # Full automation on by default (owner requested). Each is still
        # toggleable from the Telegram dashboard.
        self.flags = {
            "farm": True,
            "quest": True,
            "stats": True,
            "revive": True,
            "equip": True,    # wear the best gear we own for our class
            "skills": True,   # spend job points on class skills
        }
        self._stat_cycle = itertools.cycle(cfg.stat_build)
        self._rot_counter = 0
        self._daily_last = 0.0
        self._stop = False
        self._bs_sent = False       # have we pushed botSettings this connection?
        self._last_travel = 0.0     # cooldown for auto-travel-to-field
        self._no_mob_since = 0.0
        self.counts = {"attack": 0, "pickup": 0, "potion": 0, "revive": 0, "travel": 0, "kite": 0}
        self._last_kite = 0.0
        self._last_potion = 0.0
        self._drop_tries: dict[str, tuple[int, float]] = {}
        self._deaths: list[tuple[float, str]] = []      # (time, area)
        self._zone_ban: dict[str, float] = {}           # zone -> banned until
        self._load_bans()
        self._resting = False
        self._last_claims = 0.0
        self._last_town_run = 0.0
        self._last_recall = 0.0
        self._equip_tries: dict[str, tuple[int, float]] = {}
        self._force_town = False
        self._potion_log: list[tuple[float, str]] = []
        self.counts.update({"sold_silver": 0, "potions_bought": 0})
        # skill learning: skill -> (attempts without progress, blocked until)
        self._skill_fail: dict[str, tuple[int, float]] = {}
        self._skill_last: tuple[str, int] | None = None
        # A captured autoEquip payload (slot -> item id). Filled when the owner
        # captures their in-game "auto equip" action; re-applied on demand.
        self.saved_equip: dict | None = None
        # learned per-map / per-mob performance (see intel.py)
        self.intel = FarmIntel(os.path.join(os.path.dirname(self._BAN_FILE), "farm_intel.json"))
        self._zone_pick: tuple[str, str, float] | None = None   # (zone, reason, chosen at)
        self._target: int | None = None
        self._target_since = 0.0
        self._skip_mobs: dict[int, float] = {}
        self._buff_at: dict[str, float] = {}
        self._equip_refused: set[str] = set()
        self._respec_try = 0.0
        self._travel_fail: dict[str, int] = {}

    # ------------------------------------------------------------ helpers
    def _sp(self) -> int:
        return int(self.state.self_.get("sp") or 0)

    def _rotation_skill(self) -> str | None:
        """Next learned attack skill we have the SP for (None -> plain attack,
        which also lets SP regenerate instead of wasting refused casts)."""
        cls = self.state.self_.get("classId")
        learned = self.state.self_.get("skillLevels") or {}
        sp = self._sp()
        steps = [
            s for s in P.DEFAULT_BOT_SETTINGS["rotation"]["steps"].get(cls, [])
            if (learned.get(s) or 0) > 0 and sp >= P.SKILL_SP.get(s, 0)
        ]
        if not steps:
            return None
        self._rot_counter += 1
        return steps[self._rot_counter % len(steps)]

    def _due_buff(self) -> str | None:
        """A learned self-buff whose recast time has come and that we can pay for
        while keeping SP for at least one attack skill."""
        cls = self.state.self_.get("classId")
        learned = self.state.self_.get("skillLevels") or {}
        now = time.time()
        for sk, every in P.CLASS_BUFFS.get(cls, {}).items():
            if (learned.get(sk) or 0) <= 0 or now - self._buff_at.get(sk, 0) < every:
                continue
            if self._sp() >= P.SKILL_SP.get(sk, 0) + 10:
                return sk
        return None

    def pick_target(self) -> int | None:
        """Stick with the mob we're already hitting; otherwise take the one with
        the best learned value per second (EXP + loot, time-to-kill, walk time),
        steering clear of mob types that keep taking big chunks of our HP."""
        whitelist = P.DEFAULT_BOT_SETTINGS["monsters"]
        mobs = self.state.alive_mobs(whitelist)
        if not mobs:
            return None
        now = time.time()
        if self._target is not None and any(mid == self._target for mid, _r, _i in mobs):
            key = self.state.mobinfo.get(self._target, {}).get("key")
            if now - self._target_since < max(30.0, 4 * self.intel.est_ttk(key)):
                return self._target
            # not dying (out of reach / stuck behind a wall) — give up on it for a minute
            self._skip_mobs[self._target] = now + 60
        self._skip_mobs = {m: t for m, t in self._skip_mobs.items() if t > now}
        px, py = self.state.position()
        hp_pct = self.state.hp_percent()
        best, best_s = None, float("-inf")
        for mid, row, info in mobs:
            if mid in self._skip_mobs:
                continue
            d = ((row[0] - px) ** 2 + (row[1] - py) ** 2) ** 0.5
            s = self.intel.target_score(info.get("key"), d, hp_pct)
            # pulling a mob out of a pack drags the pack along: prefer loners
            pack = sum(1 for m2, r2, _i in mobs
                       if m2 != mid and (r2[0] - row[0]) ** 2 + (r2[1] - row[1]) ** 2 <= 200 ** 2)
            s /= 1 + 0.6 * pack
            if s > best_s:
                best, best_s = mid, s
        if best != self._target:
            self._target_since = now
        return best

    def _potion_ready(self) -> bool:
        if time.time() - self._last_potion < 0.3:  # game cooldown is 0.2s shared by all potions
            return False
        ready_at = self.state.self_.get("potionReadyAt") or 0
        return self.state.server_time == 0 or self.state.server_time >= ready_at

    def hp_potion_stock(self) -> int | None:
        """Total HP potions in the bag (None if inventory not loaded yet)."""
        inv = self.state.self_.get("inventory")
        if not isinstance(inv, dict):
            return None
        return sum(int(inv.get(k) or 0) for k in P.HP_POTIONS)

    def desired_zone(self) -> str:
        """Farm map chosen from measured results (EXP/h, loot vs potion
        silver, deaths). Re-evaluated every 10 minutes, or at once when the
        current pick gets banned. FARM_ZONE in .env pins a map instead."""
        if self.cfg.farm_zone in P.FARM_ZONE_IDS:
            return self.cfg.farm_zone
        now = time.time()
        pick = self._zone_pick
        if pick and now - pick[2] < 600 and self._zone_ban.get(pick[0], 0) < now:
            return pick[0]
        lvl = int(self.state.self_.get("level") or 1)
        zone, reason = self.intel.choose_zone(lvl, self._baseline_zone(), self._zone_ban, now)
        if not pick or pick[0] != zone:
            log.info("farm map -> %s (%s)", zone, reason)
        self._zone_pick = (zone, reason, now)
        return zone

    def _baseline_zone(self) -> str:
        """Starting guess before anything is measured: the highest zone whose
        min level is at least ZONE_MARGIN below ours, skipping banned zones."""
        lvl = int(self.state.self_.get("level") or 1)
        now = time.time()
        margin = self.cfg.zone_margin
        if self.hp_potion_stock() == 0:
            margin += 10  # no potions: farm a clearly easier map
        best = "field"
        for zid, _name, lo, _hi in P.FARM_ZONES:
            if lo + margin <= lvl and self._zone_ban.get(zid, 0) < now:
                best = zid
        return best

    # ------------------------------------------------------ build helpers
    def _stat_weights(self) -> dict[str, float]:
        cls = self.state.self_.get("classId")
        if cls in P.CLASS_STAT_WEIGHTS:
            return P.CLASS_STAT_WEIGHTS[cls]
        # fall back to STAT_BUILD order: first stat weighs most
        n = len(self.cfg.stat_build)
        return {s: n - i for i, s in enumerate(self.cfg.stat_build)}

    def next_stat(self) -> str:
        """Stat that is furthest below its target ratio."""
        stats = self.state.self_.get("stats") or {}
        w = self._stat_weights()
        return min(w, key=lambda s: (stats.get(s, 1) or 1) / w[s])

    def _gear_score(self, g: dict) -> float:
        bonus = g.get("bonuses") or {}
        weights = self._stat_weights()
        score = 0.0
        for k, v in bonus.items():
            if not isinstance(v, (int, float)):
                continue
            base = P.GEAR_SCORE.get(k, 0.3)
            if k in ("str", "agi", "vit", "int", "dex", "luk"):
                base = 0.5 + weights.get(k, 0)  # stats we build for count more
            score += base * v
        return score + 0.5 * (g.get("refine") or 0) + 0.2 * (g.get("tier") or 0)

    def best_gear(self) -> dict[str, str]:
        """{slot: gear id} — the best item we own for each slot, for our class."""
        cls = self.state.self_.get("classId")
        allowed = P.CLASS_WEAPONS.get(cls)
        best: dict[str, tuple[float, str]] = {}
        for gid, g in self.state.gear.items():
            slot = g.get("slot")
            if not slot or gid in self._equip_refused:
                continue
            if slot == P.WEAPON_SLOT:
                if allowed and g.get("template") not in allowed:
                    continue
                bonus = g.get("bonuses") or {}
                magic = cls in ("mage", "acolyte", "nekobaku")
                score = (bonus.get("matk", 0) if magic else bonus.get("atk", 0)) * 10 + self._gear_score(g)
            else:
                score = self._gear_score(g)
            if slot not in best or score > best[slot][0]:
                best[slot] = (score, gid)
        return {slot: gid for slot, (_s, gid) in best.items()}

    # ---------------------------------------------------------- town run
    def _inv(self) -> dict:
        inv = self.state.self_.get("inventory")
        return inv if isinstance(inv, dict) else {}

    def _sellable(self) -> list[tuple[str, int]]:
        return [(k, int(v)) for k, v in self._inv().items()
                if isinstance(v, (int, float)) and v > 0 and P.is_sellable_loot(k)]

    def _town_needed(self) -> bool:
        if self._force_town:
            return True
        inv = self._inv()
        return bool(
            self._sellable()
            or int(inv.get(P.POTION_BUY_ENTRY) or 0) < self.cfg.potion_target // 2
            or (self.state.self_.get("classId") == "archer"
                and int(inv.get(P.ARROW_ENTRY) or 0) < self.cfg.arrow_min)
            or int(inv.get(P.RETURN_SCROLL) or 0) < 3
        )

    async def town_run(self) -> None:
        """Walk to the Silver merchant, sell monster loot, restock consumables.
        Potion spending is capped at POTION_BUDGET_PCT of current silver so the
        premium savings keep growing."""
        self._last_town_run = time.time()
        self._force_town = False
        mx, my = P.MERCHANT_POS
        await self._stand()
        await self.client.send(P.move(mx, my + 20))
        for _ in range(24):
            await asyncio.sleep(0.5)
            px, py = self.state.position()
            if (px - mx) ** 2 + (py - my) ** 2 <= (P.NPC_RANGE - 15) ** 2:
                break
        else:
            log.warning("town run: couldn't reach the merchant")
            return

        silver0 = int(self.state.self_.get("silver") or 0)
        loot = self._sellable()
        if loot:
            for i in range(0, len(loot), 20):
                await self.client.send(P.sell_batch(
                    [{"item": k, "quantity": q} for k, q in loot[i:i + 20]]))
            await asyncio.sleep(1.5)
            gained = int(self.state.self_.get("silver") or 0) - silver0
            self.counts["sold_silver"] += max(gained, 0)
            log.info("sold %d loot stacks for %d silver (silver now %s)",
                     len(loot), gained, self.state.self_.get("silver"))

        async def buy(entry: str, qty: int, unit: float) -> None:
            silver = int(self.state.self_.get("silver") or 0)
            qty = min(qty, int(silver // unit)) if unit else qty
            if qty <= 0:
                return
            before = int(self._inv().get(entry) or 0)
            await self.client.send(P.shop_buy(entry, qty))
            await asyncio.sleep(1.2)
            got = int(self._inv().get(entry) or 0) - before
            log.info("bought %s x%d (asked %d) — silver %s", entry, got, qty,
                     self.state.self_.get("silver"))
            if entry == P.POTION_BUY_ENTRY:
                self.counts["potions_bought"] += max(got, 0)

        inv = self._inv()
        silver = int(self.state.self_.get("silver") or 0)
        want = self.cfg.potion_target - int(inv.get(P.POTION_BUY_ENTRY) or 0)
        budget = silver * self.cfg.potion_budget_pct // 100
        if want > 0:
            await buy(P.POTION_BUY_ENTRY, min(want, budget // P.POTION_PRICE), P.POTION_PRICE)
        if int(inv.get(P.RETURN_SCROLL) or 0) < 3:
            await buy(P.RETURN_SCROLL, 3, 30)
        if (self.state.self_.get("classId") == "archer"
                and int(inv.get(P.ARROW_ENTRY) or 0) < self.cfg.arrow_min):
            await buy(P.ARROW_ENTRY, 2000, 0.1)

    async def go_to(self, want: str) -> bool:
        """Take one portal hop toward `want`: stop fighting, walk to the portal,
        then travel. Keeps drinking potions on the way. Returns True if the
        travel request was sent (the server then closes the socket with 4100)."""
        area = self.state.self_.get("area")
        hop = P.next_hop(area, want)
        portal = P.PORTALS.get(area, {}).get(hop)
        self._last_travel = time.time()
        self.counts["travel"] += 1
        if portal is None:
            log.info("travel %s -> %s (no portal data, trying directly)", area, hop)
            await self.client.send(P.travel(hop))
            return True
        log.info("heading to %s: walking to %s portal at %s", want, hop, portal)
        await self._stand()
        await self.client.send(P.stop())
        start_pos = self.state.position()
        deadline = time.time() + 60
        last_move = 0.0
        while time.time() < deadline and self.state.connected:
            if self.state.is_dead() or self.state.self_.get("area") != area:
                return False
            px, py = self.state.position()
            if (px - portal[0]) ** 2 + (py - portal[1]) ** 2 <= P.PORTAL_RANGE ** 2:
                self._travel_fail.pop(want, None)
                await self.client.send(P.travel(hop))
                return True
            stock = self.hp_potion_stock()
            if (self.state.hp_percent() <= self.cfg.potion_hp_percent and self._potion_ready()
                    and (stock is None or stock > 0)):
                self._last_potion = time.time()
                await self.client.send_now(P.potion())
            if time.time() - last_move > 3:
                last_move = time.time()
                await self.client.send(P.move(*portal))
            await asyncio.sleep(0.5)
        fails = self._travel_fail.get(want, 0) + 1
        self._travel_fail[want] = fails
        log.warning("couldn't reach the %s portal in %s (%d/3) — from %s, ended at %s",
                    hop, area, fails, start_pos, self.state.position())
        if fails >= 3:
            # don't spend the session walking into a wall: farm here for a while
            self._travel_fail.pop(want, None)
            self._ban_zone(want, 1200)
            self._zone_pick = None
            log.warning("giving up on %s for 20 min", want)
        return False

    async def _stand(self) -> None:
        """A sitting character ignores move orders, and the server keeps the
        sit across reconnects/restarts — so stand up before walking anywhere."""
        self._resting = False
        await self.client.send(P.sit(False))

    def _watch_hp_spike(self) -> None:
        """Log what is around us when we lose a quarter of our HP in 4s, so
        hard hitters (bosses, packs) can be identified from the log."""
        now = time.time()
        hp = self.state.self_.get("hp")
        mx = self.state.max_hp()
        if not isinstance(hp, (int, float)) or not mx:
            return
        self._hp_hist = [(t, h) for t, h in getattr(self, "_hp_hist", []) if now - t <= 4] + [(now, hp)]
        peak = max(h for _t, h in self._hp_hist)
        if peak - hp < 0.25 * mx or now - getattr(self, "_spike_logged", 0) < 20:
            return
        self._spike_logged = now
        px, py = self.state.position()
        near = []
        for mid, row in self.state.mobs.items():
            if len(row) < 4 or row[3] != 1:
                continue
            d = ((row[0] - px) ** 2 + (row[1] - py) ** 2) ** 0.5
            if d <= 350:
                info = self.state.mobinfo.get(mid, {})
                near.append(f"{info.get('key')}(L{info.get('level')}{' ELITE' if info.get('elite') else ''},"
                            f" {d:.0f}px)")
        log.info("HP spike %d -> %d in 4s @%s | near: %s", peak, hp, self.state.self_.get("area"),
                 ", ".join(sorted(near)) or "nothing")

    async def _kite(self) -> bool:
        """With 3+ mobs on top of us, move ~260px directly away from them."""
        px, py = self.state.position()
        close = [(r[0], r[1]) for _m, r, _i in self.state.alive_mobs()
                 if (r[0] - px) ** 2 + (r[1] - py) ** 2 <= 140 ** 2]
        if len(close) < 3:
            return False
        cx = sum(x for x, _ in close) / len(close)
        cy = sum(y for _, y in close) / len(close)
        dx, dy = px - cx, py - cy
        n = (dx * dx + dy * dy) ** 0.5
        if n < 1:       # surrounded evenly: pick any direction
            dx, dy, n = 1.0, 0.0, 1.0
        tx = max(60, px + dx / n * 260)
        ty = max(60, py + dy / n * 260)
        self._last_kite = time.time()
        self._target = None
        self.counts["kite"] += 1
        log.debug("kiting: %d mobs close, hp %.0f%%", len(close), self.state.hp_percent())
        await self.client.send(P.move(int(tx), int(ty)))
        await asyncio.sleep(1.0)
        return True

    def _mobs_near(self, radius: float) -> int:
        px, py = self.state.position()
        return sum(
            1 for _mid, row, _i in self.state.alive_mobs()
            if (row[0] - px) ** 2 + (row[1] - py) ** 2 <= radius * radius
        )

    def _note_potion_burn(self) -> None:
        """Potions cost silver; a map that eats more than ~6/min is losing money,
        so park it for 20 minutes and farm an easier one."""
        now = time.time()
        area = self.state.self_.get("area") or "?"
        self._potion_log = [t for t in self._potion_log if now - t[0] < 180] + [(now, area)]
        used = sum(1 for _, a in self._potion_log if a == area)
        if area in P.FARM_ZONE_IDS and used > 12 and area != "field":
            self._ban_zone(area, 3600)
            self._potion_log = [t for t in self._potion_log if t[1] != area]
            log.warning("%s burns %d potions in 3 min — too costly, farming an easier map for 60 min",
                        area, used)

    # Zone bans survive restarts so the bot doesn't walk back into a map that
    # was just killing it or burning silver.
    _BAN_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "zone_bans.json")

    def _load_bans(self) -> None:
        try:
            with open(self._BAN_FILE, encoding="utf-8") as f:
                now = time.time()
                self._zone_ban = {k: float(v) for k, v in json.load(f).items() if float(v) > now}
        except (OSError, ValueError):
            self._zone_ban = {}

    def _ban_zone(self, zone: str, seconds: float) -> None:
        self._zone_ban[zone] = time.time() + seconds
        try:
            os.makedirs(os.path.dirname(self._BAN_FILE), exist_ok=True)
            tmp = self._BAN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._zone_ban, f)
            os.replace(tmp, self._BAN_FILE)
        except OSError:
            log.debug("could not save zone bans", exc_info=True)

    def _record_death(self) -> None:
        now = time.time()
        area = self.state.self_.get("area") or "?"
        self._deaths = [d for d in self._deaths if now - d[0] < 900] + [(now, area)]
        if area in P.FARM_ZONE_IDS and sum(1 for _, a in self._deaths if a == area) >= 3:
            self._ban_zone(area, 1800)
            self._deaths = [d for d in self._deaths if d[1] != area]
            log.warning("died 3x in %s within 15 min — avoiding it for 30 min", area)

    def _next_drop(self) -> str | None:
        """First pickable drop we haven't given up on. A drop that's still on the
        ground after a few attempts (too heavy, protected, out of reach) gets
        skipped so it can't freeze the loop."""
        now = time.time()
        for did in self.state.pickable_drops():
            tries, first = self._drop_tries.get(did, (0, now))
            if tries >= 3 or now - first > 8:
                continue
            self._drop_tries[did] = (tries + 1, first)
            return did
        # forget drops that are gone
        live = set(self.state.drops)
        self._drop_tries = {d: v for d, v in self._drop_tries.items() if d in live}
        return None

    # -------------------------------------------------------- farm control
    async def start_farm(self) -> None:
        # Persist bot settings (heal threshold etc.) then enable our loop.
        await self.client.send(P.bot_settings(self.cfg.potion_hp_percent))
        self.flags["farm"] = True
        log.info("farm enabled")

    async def stop_farm(self) -> None:
        self.flags["farm"] = False
        await self.client.send(P.stop())
        log.info("farm disabled")

    # --------------------------------------------------------- farm loop
    async def farm_loop(self) -> None:
        while not self._stop:
            if not self.state.connected:
                self._bs_sent = False
                await asyncio.sleep(0.8)
                continue
            if not self.flags["farm"]:
                await asyncio.sleep(0.8)
                continue

            # push bot settings (heal threshold, loot filter) once per connection
            if not self._bs_sent:
                await self.client.send(P.bot_settings(self.cfg.potion_hp_percent))
                await self._stand()
                self._bs_sent = True

            self.intel.tick(self.state)
            self._watch_hp_spike()

            # 1) dead -> revive
            if self.state.is_dead():
                if self.flags["revive"]:
                    log.info("died in %s — reviving", self.state.self_.get("area"))
                    self.counts["revive"] += 1
                    self.intel.note_death(self.state.self_.get("area"))
                    self._record_death()
                    self._resting = False
                    self._target = None
                    await self.client.send(P.revive())
                await asyncio.sleep(1.0)
                continue

            # 2) heal. A potion costs 10 silver while a kill pays ~0.3-15, so
            #    potions are only an emergency brake: drink when something is
            #    hitting us and HP is under POTION_HP_PERCENT (or nearly dead).
            #    Otherwise finish the fight and sit to regenerate for free.
            stock = self.hp_potion_stock()
            hp_pct = self.state.hp_percent()
            threat = self._mobs_near(260)
            if (
                ((threat and hp_pct <= self.cfg.potion_hp_percent) or hp_pct < 15)
                and self._potion_ready()
                and (stock is None or stock > 0)
            ):
                self.counts["potion"] += 1
                self._last_potion = time.time()
                self.intel.note_potion(self.state.self_.get("area"), P.POTION_PRICE)
                self._note_potion_burn()
                inv = self._inv()
                # critical: use the bigger heals first (orange/yellow/white), else the hotbar potion
                big = next((k for k in ("white_potion", "yellow_potion", "orange_potion")
                            if int(inv.get(k) or 0) > 0), None)
                if self.state.hp_percent() < 30 and big:
                    await self.client.send_now(P.use_item(big))
                else:
                    await self.client.send_now(P.potion())
                continue

            # 2b) hurt and nothing nearby -> sit to regenerate, stand when healthy
            if self._resting and threat:
                # something walked up to us — standing still just gets us killed
                self._resting = False
                await self.client.send(P.sit(False))
            shop_first = (self.cfg.enable_sell and self.state.self_.get("area") == P.TOWN
                          and time.time() - self._last_town_run > 300 and self._town_needed())
            if not threat and not shop_first and (self._resting or hp_pct < 60):
                if not self._resting:
                    log.debug("hp %.0f%% and no mobs near — resting", hp_pct)
                    self._resting = True
                    await self.client.send(P.sit(True))
                elif hp_pct >= 90:
                    log.info("rested to %.0f%% — back to farming", hp_pct)
                    self._resting = False
                    await self.client.send(P.sit(False))
                    continue
                await asyncio.sleep(1.5)
                continue

            # 3) loot
            did = self._next_drop()
            if did:
                self.counts["pickup"] += 1
                await self.client.send(P.pickup(did))
                continue

            now = time.time()
            area = self.state.self_.get("area")

            # 3) free build reset (passive skills / class stats) at the Reset Master
            if self.respec_due():
                if area == P.TOWN:
                    await self.respec_if_needed()
                    continue
                if (area in P.FARM_ZONE_IDS and int(self._inv().get(P.RETURN_SCROLL) or 0) > 0
                        and now - self._last_recall > 120):
                    log.info("build reset pending — returning to town for the Reset Master")
                    self._last_recall = now
                    await self.client.send(P.use_item(P.RETURN_SCROLL))
                    await asyncio.sleep(2)
                    continue

            # 3a) in town: sell loot + restock before heading out
            if (self.cfg.enable_sell and area == P.TOWN
                    and now - self._last_town_run > 300 and self._town_needed()):
                await self.town_run()
                continue

            # 3a') running low on potions in the field: read a Return Scroll home
            if (self.cfg.enable_sell and area in P.FARM_ZONE_IDS
                    and stock is not None and stock < 5
                    and int(self._inv().get(P.RETURN_SCROLL) or 0) > 0
                    and int(self.state.self_.get("silver") or 0) >= 100
                    and now - self._last_recall > 120):
                log.info("only %d HP potions left — returning to town to restock", stock)
                self._last_recall = now
                await self.client.send(P.use_item(P.RETURN_SCROLL))
                await asyncio.sleep(2)
                continue

            # 3b) wrong farm map for our level (or one we keep dying in) -> move
            want = self.desired_zone()
            if area in P.FARM_ZONE_IDS and area != want and now - self._last_travel > 30:
                log.info("Lv%s: leaving %s for %s", self.state.self_.get("level"), area, want)
                await self.go_to(want)
                continue

            # 3c) swarmed and getting low: step back out of the pack instead of
            #     standing in it drinking potions (we out-range melee mobs)
            if hp_pct < 55 and now - self._last_kite > 2.5 and await self._kite():
                continue

            # 4) fight: best-value target we can handle (learned per mob)
            target = self.pick_target()
            if target is not None:
                self._no_mob_since = 0.0
                self._target = target
                self.intel.engage(target, self.state.mobinfo.get(target, {}).get("key"))
                buff = self._due_buff()
                if buff:
                    self._buff_at[buff] = time.time()
                    await self.client.send(P.skill(buff))
                    continue
                # Interleave a rotation skill every few actions (only with SP for it).
                if self._rot_counter % 3 == 0:
                    sk = self._rotation_skill()
                    if sk:
                        await self.client.send(P.skill(sk, target))
                        continue
                self._rot_counter += 1
                self.counts["attack"] += 1
                await self.client.send(P.attack(target))
                continue

            # 5) no mobs here. If we've been empty for a while (e.g. spawned in a
            #    town/hub), travel to a field to find monsters.
            now = time.time()
            if self._no_mob_since == 0.0:
                self._no_mob_since = now
            if now - self._no_mob_since > 8 and now - self._last_travel > 15:
                want = self.desired_zone()
                log.info("no monsters in %s — heading to %s", self.state.self_.get("area"), want)
                await self.go_to(want)
                self._no_mob_since = time.time()
            await asyncio.sleep(1.2)

    # -------------------------------------------------------- status log
    async def status_loop(self, interval: float = 30.0) -> None:
        """Periodic one-line status in the console so the owner can watch CMD."""
        built = False
        last_report = time.time()
        while not self._stop:
            await asyncio.sleep(interval)
            s = self.state.self_
            if s and not built and s.get("stats"):
                built = True
                worn = s.get("equipped") or {}
                log.info("BUILD %s Lv%s job%s | stats %s | skills %s | worn %s",
                         s.get("classId"), s.get("level"), s.get("jobLevel"), s.get("stats"),
                         s.get("skillLevels"),
                         {k: self.state.gear.get(v, {}).get("name", v) for k, v in worn.items()})
            if s and time.time() - last_report > 600:
                last_report = time.time()
                self.intel.save()
                for line in self.intel.summary_lines(s.get("area")) or ["(no map data yet)"]:
                    log.info("FARM %s", line)
            qs = self.state.quest_status() if s else {}
            log.info(
                "STATUS %s | Lv%s %s @%s (target %s)%s | hp %s/%s | hp-pots %s | silver %s | gold %s | kills %s | pts %s | "
                "mobs %d drops %d | quest %s | sent atk=%d pick=%d pot=%d rev=%d trv=%d kite=%d",
                "online" if self.state.connected else "OFFLINE",
                s.get("level"), s.get("classId"), s.get("area"),
                self.desired_zone(), " RESTING" if self._resting else "",
                s.get("hp"), self.state.max_hp(), self.hp_potion_stock(),
                s.get("silver"), s.get("gold"), s.get("kills"), s.get("points"),
                len(self.state.alive_mobs()), len(self.state.drops),
                qs.get("id", "-"),
                self.counts["attack"], self.counts["pickup"], self.counts["potion"],
                self.counts["revive"], self.counts["travel"], self.counts["kite"],
            )
            if s and self.state.max_hp() is None:
                log.debug(
                    "maxHp unknown: my_id=%s my_n=%s chars=%s",
                    s.get("id"), self.state.my_n,
                    [(n, c.get("id"), "maxHp" in c) for n, c in list(self.state.chars.items())[:8]],
                )

    # --------------------------------------------------------- equip loop
    async def equip_loop(self) -> None:
        await asyncio.sleep(10)  # let gearRows arrive after connecting
        while not self._stop:
            if (self.flags["equip"] and self.state.connected and self.state.gear
                    and not self.state.is_dead()):
                worn = self.state.self_.get("equipped") or {}
                now = time.time()
                for slot, gid in self.best_gear().items():
                    if worn.get(slot) == gid:
                        self._equip_tries.pop(gid, None)
                        continue
                    tries, until = self._equip_tries.get(gid, (0, 0))
                    if until > now:
                        continue
                    if tries >= 2:
                        # server keeps refusing (class/level/two-handed bow...):
                        # drop it from the candidates so the next best is worn
                        self._equip_refused.add(gid)
                        self._equip_tries.pop(gid, None)
                        log.info("equip %s refused twice — %s won't be tried again",
                                 slot, self.state.gear.get(gid, {}).get("name"))
                        continue
                    g = self.state.gear.get(gid, {})
                    log.info("equip %s: %s %s", slot, g.get("name"), g.get("bonuses"))
                    self._equip_tries[gid] = (tries + 1, 0)
                    await self.client.send(P.equip(gid))
            await asyncio.sleep(min(self.cfg.equip_interval, 60))

    # -------------------------------------------------------------- respec
    _RESPEC_FILE = os.path.join(os.path.dirname(_BAN_FILE), "respec.json")

    def _respec_log(self) -> dict:
        try:
            with open(self._RESPEC_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _respec_mark(self, what: str) -> None:
        d = self._respec_log()
        d.setdefault(str(self.state.self_.get("id")), {})[what] = time.time()
        try:
            os.makedirs(os.path.dirname(self._RESPEC_FILE), exist_ok=True)
            with open(self._RESPEC_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except OSError:
            pass

    def respec_needed(self) -> tuple[bool, bool]:
        """(skills, stats) worth a free reset: the class's damage passives were
        skipped, or points went into stats the class doesn't use."""
        s = self.state.self_
        if int(s.get("level") or 999) > P.FREE_RESET_MAX_LEVEL:
            return False, False
        cls = s.get("classId")
        done = self._respec_log().get(str(s.get("id")), {})
        learned = s.get("skillLevels") or {}
        order = P.CLASS_SKILLS.get(cls) or []
        passives = order[:2]
        spent_elsewhere = sum(v for k, v in learned.items() if k in order and k not in passives)
        skills = (bool(passives) and "skills" not in done
                  and all((learned.get(p) or 0) == 0 for p in passives) and spent_elsewhere >= 10)
        stats = s.get("stats") or {}
        weights = P.CLASS_STAT_WEIGHTS.get(cls) or {}
        wasted = sum(max(0, int(v or 1) - 1) for k, v in stats.items() if weights and k not in weights)
        return skills, bool(weights) and "stats" not in done and wasted >= 15

    def respec_due(self) -> bool:
        return (self.flags["skills"] and time.time() - self._respec_try > 1800
                and any(self.respec_needed()))

    async def respec_if_needed(self) -> None:
        """Walk to the Reset Master in town and reset skills/stats (free under
        Lv80); skill_loop / stat_loop then re-spend the points class-first."""
        if not self.state.connected or self.state.is_dead() or not self.respec_due():
            return
        if self.state.self_.get("area") != P.TOWN:
            return
        self._respec_try = time.time()
        skills, stats = self.respec_needed()
        mx, my = P.STAT_MASTER_POS
        await self._stand()
        await self.client.send(P.move(mx, my + 20))
        for _ in range(30):
            await asyncio.sleep(0.5)
            px, py = self.state.position()
            if (px - mx) ** 2 + (py - my) ** 2 <= (P.NPC_RANGE - 15) ** 2:
                break
        else:
            log.warning("respec: couldn't reach the Reset Master")
            return
        s = self.state.self_
        if skills:
            before = dict(s.get("skillLevels") or {})
            log.info("RESPEC skills (free under Lv%d): %s -> passives first %s",
                     P.FREE_RESET_MAX_LEVEL, before, P.CLASS_SKILLS.get(s.get("classId"))[:2])
            await self.client.send(P.reset_skills())
            await asyncio.sleep(2)
            after = s.get("skillLevels") or {}
            log.info("RESPEC skills %s (now %s)", "done" if after != before
                     else "had no effect — retrying in 30 min", after)
            if after != before:
                self._respec_mark("skills")
                self._skill_fail.clear()
                self._skill_last = None
                self._forget_old_build()
        if stats:
            before = dict(s.get("stats") or {})
            log.info("RESPEC stats (free under Lv%d): %s -> %s build", P.FREE_RESET_MAX_LEVEL,
                     before, P.CLASS_STAT_WEIGHTS.get(s.get("classId")))
            await self.client.send(P.reset_stats())
            await asyncio.sleep(2)
            after = s.get("stats") or {}
            log.info("RESPEC stats %s (now %s, %s points free)", "done" if after != before
                     else "had no effect — retrying in 30 min", after, s.get("points"))
            if after != before:
                self._respec_mark("stats")
                self._forget_old_build()

    def _forget_old_build(self) -> None:
        """A new build fights differently: drop map/mob results and map bans
        learned with the old one so they don't steer the new one."""
        self.intel.reset()
        self._zone_ban = {}
        self._ban_zone("_", 0)      # rewrites the ban file without old entries
        self._zone_pick = None
        self._deaths.clear()
        self._potion_log.clear()

    # --------------------------------------------------------- skill loop
    async def skill_loop(self) -> None:
        """Learn class skills one level at a time. The server rejects requests
        without job points / prerequisites, so a skill that doesn't level after
        two tries is parked for 10 minutes and the next one is tried."""
        await asyncio.sleep(15)
        delay = 20.0
        while not self._stop:
            await asyncio.sleep(delay)
            delay = 20.0
            if not (self.flags["skills"] and self.state.connected):
                continue
            if self.respec_due():
                continue    # don't spend points we're about to get refunded
            cls = self.state.self_.get("classId")
            order = P.CLASS_SKILLS.get(cls)
            if not order:
                continue
            levels = self.state.self_.get("skillLevels") or {}
            now = time.time()
            # judge the previous attempt
            if self._skill_last:
                sk, before = self._skill_last
                if (levels.get(sk) or 0) > before:
                    log.info("learned %s -> Lv%s", sk, levels.get(sk))
                    self._skill_fail.pop(sk, None)
                    delay = 3.0     # points available: keep going quickly
                else:
                    fails, _ = self._skill_fail.get(sk, (0, 0))
                    fails += 1
                    self._skill_fail[sk] = (fails, now + 600 if fails >= 2 else 0)
                self._skill_last = None
            for sk in order:
                cur = levels.get(sk) or 0
                fails, until = self._skill_fail.get(sk, (0, 0))
                if cur >= 10 or until > now:
                    continue
                if until and until <= now:
                    self._skill_fail.pop(sk, None)
                self._skill_last = (sk, cur)
                await self.client.send(P.learn_skills({sk: cur + 1}))
                break

    # --------------------------------------------------------- quest loop
    async def quest_loop(self) -> None:
        # daily once at startup, then roughly every 24h
        while not self._stop:
            if self.flags["quest"] and self.state.connected:
                await self.client.send(P.claim_quest())
                now = time.time()
                if now - self._daily_last > 23 * 3600:
                    await self.client.send(P.claim_daily())
                    self._daily_last = now
                # mail rewards + daily hunting-journal reward; no-ops when nothing is claimable
                if now - self._last_claims > 600 or self.state.mail_pending:
                    await self.client.send(P.mail_claim())
                    await self.client.send(P.claim_hunt())
                    self._last_claims = now
            await asyncio.sleep(self.cfg.quest_interval)

    # --------------------------------------------------------- stat loop
    async def stat_loop(self) -> None:
        """Spend stat points by weight. Raising a stat costs more points the
        higher it is, so when a stat doesn't take we skip it until we have more
        points (next level-up) instead of resending it forever."""
        skip: set[str] = set()
        skip_at_points = -1
        while not self._stop:
            if (self.flags["stats"] and self.state.connected and not self.state.is_dead()
                    and not self.respec_due()):
                points = int(self.state.self_.get("points") or 0)
                if points != skip_at_points:
                    skip.clear()
                if points > 0:
                    stats = self.state.self_.get("stats") or {}
                    w = {k: v for k, v in self._stat_weights().items() if k not in skip}
                    if w:
                        st = min(w, key=lambda s: (stats.get(s, 1) or 1) / w[s])
                        await self.client.send(P.stat(st))
                        await asyncio.sleep(1.5)  # wait for the server to echo points
                        if int(self.state.self_.get("points") or 0) >= points:
                            skip.add(st)          # too expensive right now
                            skip_at_points = points
                        continue
            await asyncio.sleep(self.cfg.stat_interval)

    # ----------------------------------------------------- manual actions
    async def travel(self, to: str) -> None:
        await self.client.send(P.travel(to))

    async def apply_saved_equip(self) -> bool:
        if not self.saved_equip:
            return False
        await self.client.send(P.auto_equip(self.saved_equip))
        return True

    async def equip_item(self, item_id: str) -> None:
        await self.client.send(P.equip(item_id))

    async def open_storage(self) -> None:
        await self.client.send(P.storage_open())

    async def watch_gold(self) -> None:
        await self.client.send(P.gold_watch())

    def stop(self) -> None:
        self._stop = True
