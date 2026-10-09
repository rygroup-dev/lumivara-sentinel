"""In-memory game state, reconstructed from websocket snapshots.

Snapshots are deltas. The first big snapshot carries full `self`, `mobInfo`,
`mobs`, `drops`; later ones carry partial `self` and compact `cf` frames. We
merge everything into one view and expose convenience queries for the farm loop
and the Telegram dashboard.
"""
from __future__ import annotations

import math
import time
from collections import deque

from . import protocol as P


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


class GameState:
    def __init__(self) -> None:
        self.self_: dict = {}
        self.mobinfo: dict[int, dict] = {}       # id -> {key,name,level,maxHp,elite}
        self.mobs: dict[int, list] = {}          # id -> [x, y, hp, alive]
        self.drops: dict[str, dict] = {}         # drop id -> {item,x,y,owner}
        # Characters in the area, keyed by frame slot `n` (from cf.c). maxHp/maxSp
        # for our own character only arrive here, not in `self`.
        self.chars: dict[int, dict] = {}
        self.my_n: int | None = None
        # In field maps player data arrives as a top-level `players` list instead
        # of cf.c. We keep our own entry here; it survives map changes because
        # maxHp/maxSp only change on level-up.
        self._me: dict = {}
        self._hp_peak = 0
        self.gold_market: dict = {}     # {depth:{bids,asks}, orders, history} after goldWatch
        self.gold_market_at = 0.0
        self.raw_tap: list | None = None  # when a list, non-snapshot messages are copied into it
        self.market: dict = {}          # player-market view (board etc.) after marketGetDepth
        self.market_at = 0.0
        self.market_orders: dict = {}   # my item orders, by id
        self.market_listings: dict = {} # my gear listings, by id
        self.mail_pending = 0
        # Equipment items in the bag/worn, from the top-level `gearRows` stream:
        # {id: {slot, template, name, tier, bonuses, locked, ...}}
        self.gear: dict[str, dict] = {}
        self.online: int = 0
        self.server_time: int = 0
        self.events: deque = deque(maxlen=50)
        self.connected: bool = False
        self.last_recv: float = 0.0
        self.last_pong: float = 0.0

    # ---------------------------------------------------------------- ingest
    def apply(self, msg: dict) -> None:
        self.last_recv = time.time()
        t = msg.get("type")
        if self.raw_tap is not None and (t != "snapshot" or "market" in msg or "marketRows" in msg):
            self.raw_tap.append(msg)

        if t == "pong":
            self.last_pong = time.time()
            return

        if isinstance(msg.get("self"), dict):
            if not self.self_:
                self.self_ = {}
            new_area = msg["self"].get("area")
            if new_area and new_area != self.self_.get("area"):
                self.mobinfo = {}       # mob ids are per map
            _deep_merge(self.self_, msg["self"])
            # skillLevels arrives as the full map (even `{}` after a reset), so a
            # merge would keep skills the server already dropped
            if isinstance(msg["self"].get("skillLevels"), dict):
                self.self_["skillLevels"] = dict(msg["self"]["skillLevels"])
            hp = msg["self"].get("hp")
            if isinstance(hp, (int, float)) and hp > self._hp_peak:
                self._hp_peak = hp  # fallback for maxHp until the real value arrives

        if isinstance(msg.get("mobInfo"), list):
            # merged, not replaced: a later snapshot may only describe some
            # mobs, and losing the rest leaves them keyless (no value-based
            # targeting, invisible to the per-mob danger learning). Cleared on
            # map change above.
            for row in msg["mobInfo"]:
                if isinstance(row, dict) and "id" in row:
                    self.mobinfo[row["id"]] = row

        if isinstance(msg.get("mobs"), list):
            new: dict[int, list] = {}
            for row in msg["mobs"]:
                if isinstance(row, list) and len(row) > P.MOB_ALIVE:
                    new[row[P.MOB_ID]] = [row[P.MOB_X], row[P.MOB_Y], row[P.MOB_HP], row[P.MOB_ALIVE]]
            self.mobs = new

        if isinstance(msg.get("drops"), list):
            self.drops = {
                d["id"]: d for d in msg["drops"] if isinstance(d, dict) and "id" in d
            }

        if isinstance(msg.get("online"), int):
            self.online = msg["online"]
        if isinstance(msg.get("time"), int):
            self.server_time = msg["time"]

        for ev in msg.get("events", []) or []:
            self.events.append(ev)

        players = msg.get("players")
        if isinstance(players, list):
            my_id = self.self_.get("id")
            for p in players:
                if isinstance(p, dict) and my_id and p.get("id") == my_id:
                    _deep_merge(self._me, p)

        # Player market (same merge as the client): `market` is the current
        # view (board for the queried item, my orders...), `marketRows` streams
        # my order/listing rows as add/del deltas.
        rows = msg.get("marketRows")
        if isinstance(rows, dict):
            if rows.get("reset"):
                self.market_orders, self.market_listings = {}, {}
            for part, store in (("orders", self.market_orders), ("listings", self.market_listings)):
                sect = rows.get(part) or {}
                for rid in sect.get("del") or []:
                    store.pop(rid, None)
                for r in sect.get("add") or []:
                    if isinstance(r, dict) and "id" in r:
                        store[r["id"]] = r
        if isinstance(msg.get("market"), dict):
            self.market = msg["market"]
            self.market_at = time.time()

        gm = msg.get("goldMarket")
        if isinstance(gm, dict):
            _deep_merge(self.gold_market, gm)
            self.gold_market_at = time.time()

        gr = msg.get("gearRows")
        if isinstance(gr, dict):
            if gr.get("reset"):
                self.gear = {}
            for g in gr.get("add") or []:
                if isinstance(g, dict) and g.get("id"):
                    self.gear[g["id"]] = g
            for g in gr.get("update") or []:
                if isinstance(g, dict) and g.get("id"):
                    self.gear.setdefault(g["id"], {}).update(g)
            for gid in gr.get("remove") or []:
                self.gear.pop(gid if isinstance(gid, str) else (gid or {}).get("id"), None)

        if "mailPending" in msg:
            mp = msg["mailPending"]
            self.mail_pending = len(mp) if isinstance(mp, (list, dict)) else int(bool(mp) and mp)

        cf = msg.get("cf")
        if isinstance(cf, dict):
            self._apply_cf(cf)

    def _apply_cf(self, cf: dict) -> None:
        # c: per-character deltas keyed by slot n; r=1 marks a full frame.
        if cf.get("r"):
            self.chars = {}
            self.my_n = None  # slot numbers are reassigned on full frames
        my_id = self.self_.get("id")
        for ch in cf.get("c") or []:
            if not isinstance(ch, dict) or "n" not in ch:
                continue
            n = ch["n"]
            _deep_merge(self.chars.setdefault(n, {}), ch)
            if my_id and self.chars[n].get("id") == my_id:
                self.my_n = n

        # p: flat [id, x, y, id, x, y, ...] position stream. Update known mobs only.
        p = cf.get("p")
        if isinstance(p, list):
            for i in range(0, len(p) - 2, 3):
                mid, x, y = p[i], p[i + 1], p[i + 2]
                if mid in self.mobs:
                    self.mobs[mid][0] = x
                    self.mobs[mid][1] = y

    # --------------------------------------------------------------- queries
    def me(self) -> dict:
        """Our own character's frame entry (holds maxHp/maxSp), or {}."""
        my_id = self.self_.get("id")
        cur = self.chars.get(self.my_n) if self.my_n is not None else None
        if my_id and (cur is None or cur.get("id") != my_id):
            self.my_n, cur = None, None
            for n, ch in self.chars.items():
                if ch.get("id") == my_id:
                    self.my_n, cur = n, ch
                    break
        return cur or {}

    def max_hp(self):
        mx = self.self_.get("maxHp") or self.me().get("maxHp")
        if mx:
            self._me["maxHp"] = mx
        return mx or self._me.get("maxHp") or (self._hp_peak or None)

    def max_sp(self):
        mx = self.self_.get("maxSp") or self.me().get("maxSp")
        if mx:
            self._me["maxSp"] = mx
        return mx or self._me.get("maxSp")

    def hp_percent(self) -> float:
        hp = self.self_.get("hp")
        mx = self.max_hp()
        if not hp or not mx:
            return 100.0
        try:
            return 100.0 * float(hp) / float(mx)
        except (TypeError, ZeroDivisionError):
            return 100.0

    def position(self) -> tuple[float, float]:
        return float(self.self_.get("x", 0) or 0), float(self.self_.get("y", 0) or 0)

    def is_dead(self) -> bool:
        # hp hits 0 on death; deadUntil is only the respawn timer and can already
        # be in the past while the corpse is still waiting for a revive.
        hp = self.self_.get("hp")
        if isinstance(hp, (int, float)) and hp <= 0:
            return True
        du = self.self_.get("deadUntil") or 0
        return bool(du) and du > self.server_time

    def alive_mobs(self, whitelist: dict | None = None) -> list[tuple[int, list, dict]]:
        """Return [(id, [x,y,hp,alive], info)] for living, allowed mobs."""
        out = []
        for mid, row in self.mobs.items():
            if len(row) < 4 or row[2] <= 0 or row[3] != 1:
                continue
            info = self.mobinfo.get(mid, {})
            key = info.get("key")
            if whitelist is not None and key is not None and not whitelist.get(key, False):
                continue
            out.append((mid, row, info))
        return out

    def nearest_target(self, whitelist: dict | None = None) -> int | None:
        px, py = self.position()
        best, best_d = None, float("inf")
        for mid, row, _info in self.alive_mobs(whitelist):
            d = math.hypot(row[0] - px, row[1] - py)
            if d < best_d:
                best, best_d = mid, d
        return best

    def pickable_drops(self) -> list[str]:
        me = self.self_.get("id")
        ids = []
        for did, d in self.drops.items():
            owner = d.get("owner")
            if owner in (None, "", me):
                ids.append(did)
        return ids

    def quest_status(self) -> dict:
        q = self.self_.get("quest") or {}
        idx = q.get("index", 0)
        if idx >= len(P.QUESTS):
            return {"index": idx, "done_all": True}
        quest = P.QUESTS[idx]
        goal = quest["goal"]
        key = P.quest_counter_key(goal)
        if goal["type"] == "level":
            have = self.self_.get("level", 0) or 0
        else:
            have = (q.get("counters") or {}).get(key, 0)
        need = goal["count"]
        return {
            "done_all": False,
            "index": idx,
            "id": quest["id"],
            "title": quest["title"],
            "goal_type": goal["type"],
            "have": min(have, need),
            "need": need,
            "complete": have >= need,
            "auto": goal["type"] in P.AUTO_QUEST_GOALS,
        }

    # --------------------------------------------------------------- summary
    def summary(self) -> dict:
        s = self.self_
        return {
            "name": s.get("name", "?"),
            "level": s.get("level", "?"),
            "jobLevel": s.get("jobLevel"),
            "classId": s.get("classId", "?"),
            "area": s.get("area", "?"),
            "hp": s.get("hp"),
            "maxHp": self.max_hp(),
            "sp": s.get("sp"),
            "maxSp": self.max_sp(),
            "hp_pct": round(self.hp_percent(), 1),
            "silver": s.get("silver"),
            "points": s.get("points"),
            "kills": s.get("kills"),
            "exp": s.get("exp"),
            "nextExp": s.get("nextExp"),
            "stats": s.get("stats", {}),
            "mobs_alive": len(self.alive_mobs()),
            "drops": len(self.drops),
            "online": self.online,
            "connected": self.connected,
        }
