"""Farm intelligence: learn what is worth farming for *this* character.

Two things are measured while the bot plays and kept on disk:

* per map  - time spent, EXP gained, loot silver, potion silver, deaths
             (decayed so old data fades as the character gets stronger)
* per mob  - time-to-kill and share of our max HP lost per kill

Map choice and target choice are then made from those numbers instead of
from level alone: a map that burns potions faster than its loot pays, or
keeps killing us, is dropped for one we can farm on natural HP regen.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time

from . import protocol as P

log = logging.getLogger("lumivara.intel")

HALF_LIFE = 3 * 3600          # zone stats lose half their weight every 3h
MIN_SAMPLE = 15 * 60          # seconds on a map before its numbers are trusted
EMA = 0.25                    # weight of the newest kill in per-mob averages
DANGER_HP = 0.45              # avg share of max HP lost per kill that marks a mob "dangerous"


def _blank_zone() -> dict:
    return {"secs": 0.0, "exp": 0.0, "silver": 0.0, "pots": 0.0, "deaths": 0.0, "kills": 0.0,
            "at": time.time()}


class FarmIntel:
    def __init__(self, path: str) -> None:
        self.path = path
        self.zones: dict[str, dict] = {}
        self.mobs: dict[str, dict] = {}
        self.dps = 0.0                    # learned monster-HP per second while engaged
        self._load()
        self._last_tick = 0.0
        self._last_save = time.time()
        self._lvl_exp: tuple[int, int, int] | None = None   # (level, exp, nextExp)
        self._kills: int | None = None
        self._hp: float | None = None
        self._eng: dict | None = None     # current engagement {mid,key,start,hp_lost}

    # --------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.zones = d.get("zones", {})
            self.mobs = d.get("mobs", {})
            self.dps = float(d.get("dps", 0.0))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"zones": self.zones, "mobs": self.mobs, "dps": self.dps}, f)
            os.replace(tmp, self.path)
        except OSError:
            log.debug("could not save farm intel", exc_info=True)

    def reset(self) -> None:
        self.zones, self.mobs, self.dps = {}, {}, 0.0
        self.save()

    # ----------------------------------------------------------- recording
    def _zone(self, zone: str) -> dict:
        z = self.zones.setdefault(zone, _blank_zone())
        now = time.time()
        f = 0.5 ** ((now - z.get("at", now)) / HALF_LIFE)
        if f < 0.999:
            for k in ("secs", "exp", "silver", "pots", "deaths", "kills"):
                z[k] *= f
        z["at"] = now
        return z

    def note_potion(self, zone: str | None, cost: float) -> None:
        if zone in P.FARM_ZONE_IDS:
            self._zone(zone)["pots"] += cost

    def note_death(self, zone: str | None) -> None:
        if zone in P.FARM_ZONE_IDS:
            self._zone(zone)["deaths"] += 1
        if self._eng:   # whatever we were fighting nearly killed us
            self._learn_mob(self._eng["key"], None, 1.0)
            self._eng = None

    def engage(self, mid: int, key: str | None) -> None:
        if self._eng and self._eng["mid"] == mid:
            return
        self._eng = {"mid": mid, "key": key, "start": time.time(), "hp_lost": 0.0}

    def _learn_mob(self, key: str | None, ttk: float | None, hp_frac: float) -> None:
        if not key:
            return
        m = self.mobs.setdefault(key, {"n": 0, "ttk": None, "hp": 0.0})
        m["n"] += 1
        a = 1.0 if m["n"] == 1 else EMA
        m["hp"] = (1 - a) * m["hp"] + a * hp_frac
        if ttk is not None:
            m["ttk"] = ttk if m["ttk"] is None else (1 - a) * m["ttk"] + a * ttk
            hp = P.MONSTERS.get(key, (0,))[0]
            if hp and ttk > 0.3:
                d = hp / ttk
                self.dps = d if not self.dps else 0.85 * self.dps + 0.15 * d

    def tick(self, state) -> None:
        """Call every farm-loop pass: accrues time, EXP, kills and HP lost."""
        now = time.time()
        dt = min(5.0, now - self._last_tick) if self._last_tick else 0.0
        self._last_tick = now
        s = state.self_
        if not s or not state.connected:
            return
        area = s.get("area")
        farming = area in P.FARM_ZONE_IDS
        z = self._zone(area) if farming else None
        if z is not None:
            z["secs"] += dt
            z["lvl"] = int(s.get("level") or 0)
            z["seen"] = now

        # EXP (handles level-ups: the bar resets to 0)
        lvl, exp, nxt = int(s.get("level") or 0), int(s.get("exp") or 0), int(s.get("nextExp") or 0)
        if self._lvl_exp and z is not None:
            plvl, pexp, pnxt = self._lvl_exp
            if lvl == plvl and exp > pexp:
                z["exp"] += exp - pexp
            elif lvl > plvl:
                z["exp"] += max(0, pnxt - pexp) + exp
        self._lvl_exp = (lvl, exp, nxt)

        # HP lost while fighting
        hp = s.get("hp")
        mx = state.max_hp() or 0
        if isinstance(hp, (int, float)):
            if self._hp is not None and hp < self._hp and hp > 0 and self._eng and mx:
                self._eng["hp_lost"] += (self._hp - hp) / mx
            self._hp = hp

        # kills: credit the mob we were fighting
        kills = s.get("kills")
        if isinstance(kills, int):
            if self._kills is not None and kills > self._kills:
                n = kills - self._kills
                if z is not None:
                    z["kills"] += n
                if self._eng:
                    key = self._eng["key"]
                    if z is not None and key:
                        z["silver"] += n * P.mob_silver_per_kill(key)
                    self._learn_mob(key, now - self._eng["start"], self._eng["hp_lost"])
                    self._eng = None
            self._kills = kills

        if now - self._last_save > 60:
            self._last_save = now
            self.save()

    # ------------------------------------------------------------- queries
    def zone_report(self, zone: str) -> dict | None:
        z = self.zones.get(zone)
        if not z or z["secs"] < 60:
            return None
        h = z["secs"] / 3600
        return {
            "hours": h,
            "exp_h": z["exp"] / h,
            "silver_h": z["silver"] / h,
            "pots_h": z["pots"] / h,
            "net_h": (z["silver"] - z["pots"]) / h,
            "deaths_h": z["deaths"] / h,
            "kills_h": z["kills"] / h,
        }

    # EXP is the investment (higher maps drop pricier loot), silver the income.
    # A map may cost at most ~10 potions/h more than its loot pays.
    MAX_DEFICIT = 100

    def zone_ok(self, rep: dict) -> bool:
        """Sustainable: potions (mostly) paid by loot and we rarely die."""
        return rep["net_h"] >= -self.MAX_DEFICIT and rep["deaths_h"] < 1.0

    def zone_score(self, zone: str) -> float | None:
        rep = self.zone_report(zone)
        if not rep or rep["hours"] * 3600 < MIN_SAMPLE:
            return None
        score = rep["exp_h"] + 40 * rep["net_h"]    # EXP first, silver as a tie-breaker
        if rep["net_h"] < -self.MAX_DEFICIT:
            score *= 0.4        # paying to farm here
        if rep["deaths_h"] >= 1:
            score *= 0.3
        return score

    def choose_zone(self, level: int, baseline: str, banned: dict, now: float) -> tuple[str, str]:
        """(zone, reason). Best measured map we can sustain; probe one map up
        when the current best is comfortably sustainable."""
        # results measured 3+ levels ago, or not refreshed for 2h, no longer
        # describe this character: forget them so the map gets re-tested
        for zid in [z for z, d in self.zones.items()
                    if level - int(d.get("lvl") or 0) >= 3
                    or now - d.get("seen", d.get("at", now)) > 7200]:
            del self.zones[zid]
        eligible = [z for z, _n, lo, _hi in P.FARM_ZONES
                    if lo <= level + 2 and banned.get(z, 0) < now]
        if baseline not in eligible:
            eligible.append(baseline)
        scored = {z: self.zone_score(z) for z in eligible}
        measured = {z: s for z, s in scored.items() if s is not None}
        if not measured:
            # nothing trusted yet: finish sampling a map we already started, else baseline
            started = [z for z in eligible if self.zone_report(z)]
            return (max(started, key=lambda z: self.zones[z]["secs"])
                    if started else baseline), "sampling"
        best = max(measured, key=measured.get)
        rep = self.zone_report(best)
        order = [z for z, *_ in P.FARM_ZONES]
        if rep and not self.zone_ok(rep):
            # even the best map costs more than it pays: try one map down
            i = order.index(best) - 1
            prv = order[i] if i >= 0 else None
            if prv in eligible and scored.get(prv) is None:
                return prv, f"stepping down ({best} not sustainable)"
        if rep and self.zone_ok(rep) and rep["pots_h"] <= 0.5 * rep["silver_h"] + 30:
            # probe only the next map up; if it already failed, stay put
            i = order.index(best) + 1
            nxt = order[i] if i < len(order) else None
            if nxt in eligible and scored.get(nxt) is None:
                return nxt, f"probing (best so far {best})"
        return best, "best measured"

    def mob_danger(self, key: str | None) -> float:
        m = self.mobs.get(key or "")
        return m["hp"] if m and m["n"] >= 3 else 0.0

    def est_ttk(self, key: str | None) -> float:
        m = self.mobs.get(key or "")
        if m and m.get("ttk"):
            return m["ttk"]
        hp = P.MONSTERS.get(key or "", (0,))[0]
        return hp / self.dps if self.dps and hp else 6.0

    def target_score(self, key: str | None, dist: float, hp_pct: float) -> float:
        """Value per second of going for this mob; <=0 means avoid."""
        exp = P.MONSTERS.get(key or "", (0, 0, 0, 0, 0))[4] or 1
        value = exp + 40 * P.mob_silver_per_kill(key or "")
        secs = self.est_ttk(key) + dist / 180.0
        danger = self.mob_danger(key)
        if danger >= DANGER_HP:
            value *= 0.05            # nearly kills us — only if nothing else is around
        elif hp_pct < 60:
            value *= max(0.1, 1 - 1.5 * danger)   # hurt: prefer mobs that hit softly
        return value / max(0.5, secs)

    def summary_lines(self, current: str | None) -> list[str]:
        out = []
        for z, name, *_ in P.FARM_ZONES:
            rep = self.zone_report(z)
            if not rep:
                continue
            mark = "▶" if z == current else " "
            out.append(f"{mark}{name}: {rep['exp_h']:,.0f} exp/h · loot {rep['silver_h']:.0f}"
                       f" − pots {rep['pots_h']:.0f} = {rep['net_h']:+.0f} silver/h · "
                       f"{rep['deaths_h']:.1f} deaths/h ({rep['hours']*60:.0f}m)")
        return out


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
