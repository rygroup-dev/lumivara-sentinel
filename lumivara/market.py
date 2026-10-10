"""Player-market trading (silver), done at the town broker.

Selling: every material, refine stone, card and spare potion in the bag is
priced against the live board for that item:

* a buyer already bids at least ~85% of the recent average and the bid nets
  more than the NPC pays -> sell straight into that bid;
* otherwise list it one silver under the cheapest seller (never below ~85% of
  what it has actually traded at) if that still beats the NPC after fees;
* otherwise leave it for the NPC merchant (or keep it, if the NPC won't buy).

Buying: potions come from the market when its cheapest seller undercuts the
NPC's 10 silver.

Listings that haven't sold after MARKET_REPRICE_HOURS are cancelled and
re-listed at the current price on the next pass.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time

from . import protocol as P

log = logging.getLogger("lumivara.market")

MIN_AVG_SHARE = 0.85      # never sell below this share of the recent average price
HISTORY_BUCKETS = 24      # recent history buckets used for the average


class Trader:
    def __init__(self, cfg, state, client) -> None:
        self.cfg = cfg
        self.state = state
        self.client = client
        self.prices: dict[str, dict] = {}     # item -> last analysed board
        self.last_pass = 0.0
        self.counts = {"instant_silver": 0, "listed": 0, "listed_value": 0, "bought_potions": 0,
                       "filled_value": 0, "gear_listed": 0, "gear_listed_value": 0, "salvaged": 0}
        # how often each piece of gear came back unsold (survives restarts)
        self._relist_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "data", "gear_relists.json")
        try:
            with open(self._relist_file, encoding="utf-8") as f:
                self.relists: dict[str, int] = {k: int(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            self.relists = {}
        self.gear_prices: dict[str, dict] = {}
        self._listed_now: set[str] = set()    # gear ids we listed this session
        self._refused_gear: set[str] = set()  # gear the market wouldn't take this session
        self.spare_gear = lambda: []          # replaced by Automator.spare_gear
        self._ask = 100
        self.notify = lambda *a, **k: None    # replaced by Automator.notify
        self._known_orders: dict[str, dict] = {}   # my open orders seen on the last pass
        self._cancelled: set[str] = set()
        self._viewed: set[str] = set()

    # -------------------------------------------------------------- boards
    async def board(self, item: str) -> dict | None:
        """Live board for one item (needs us next to the broker)."""
        before = self.state.market_at
        self._ask += 1
        await self.client.send(P.market_depth(item, ask=self._ask))
        for _ in range(12):
            await asyncio.sleep(0.4)
            m = self.state.market
            if self.state.market_at > before and m.get("selectedItem") == item:
                info = self.analyse(m)
                self.prices[item] = {**info, "at": time.time()}
                return info
        return None

    @staticmethod
    def analyse(m: dict) -> dict:
        dep = m.get("depth") or {}
        bids = sorted((b for b in dep.get("bids") or [] if b.get("price")), key=lambda b: -b["price"])
        asks = sorted((a for a in dep.get("asks") or [] if a.get("price")), key=lambda a: a["price"])
        hist = (m.get("history") or [])[-HISTORY_BUCKETS:]
        vol = sum(int(h.get("v") or 0) for h in hist)
        sil = sum(int(h.get("s") or 0) for h in hist)
        return {
            "bid": int(bids[0]["price"]) if bids else 0,
            "bid_qty": int(bids[0].get("quantity") or 0) if bids else 0,
            "ask": int(asks[0]["price"]) if asks else 0,
            "ask_qty": int(asks[0].get("quantity") or 0) if asks else 0,
            "avg": sil / vol if vol else 0.0,
            "volume": vol,
        }

    # ------------------------------------------------------------ deciding
    def candidates(self, keep_potions: int) -> list[tuple[str, int]]:
        s = self.state.self_
        inv = s.get("inventory") if isinstance(s.get("inventory"), dict) else {}
        locked = set(s.get("lockedItems") or [])
        # `gifted` = reward/gift units of an item: they can't be traded, and an
        # order for more than the tradable rest is refused outright
        gifted = s.get("gifted") if isinstance(s.get("gifted"), dict) else {}
        out = []
        for item, total in inv.items():
            total = int(total or 0) if isinstance(total, (int, float)) else 0
            tradable = total - int(gifted.get(item) or 0)
            if tradable <= 0 or item in locked or item in P.UNTRADABLE:
                continue
            # how many to keep for our own use (gifted units count towards it)
            if item in P.LOOT_ITEM_MOB or item == "refine_stone":
                keep = 0
            elif item.endswith("_card"):
                if not self.cfg.market_sell_cards:
                    continue
                keep = 0
            elif item in P.MARKET_SELL_KEEP:
                keep = P.MARKET_SELL_KEEP[item]
            elif item == P.POTION_BUY_ENTRY:
                keep = keep_potions + 20
            else:
                continue
            n = min(tradable, total - keep)
            if n > 0:
                out.append((item, n))
        return out

    @staticmethod
    def plan(item: str, qty: int, b: dict) -> tuple:
        """("instant", price, qty) | ("list", price, qty) | ("npc",) | ("keep",)"""
        npc = P.npc_price(item)
        bid, ask, avg = b["bid"], b["ask"], b["avg"]
        floor_price = math.ceil(avg * MIN_AVG_SHARE) if avg else 0
        if bid and bid >= floor_price and P.market_net(bid) > npc:
            return ("instant", bid, min(qty, b["bid_qty"]) or qty)
        if not ask and not avg:
            return ("npc",) if npc else ("keep",)   # no market for it: don't guess a price
        price = ask - 1 if ask else round(avg)
        price = max(price, floor_price, bid + 1 if bid else 1)
        if ask:
            price = min(price, ask)     # never list above the cheapest seller: it would never fill
        if P.market_net(price) <= npc * 1.15:
            return ("npc",) if npc else ("keep",)
        return ("list", price, qty)

    def my_orders(self) -> list[dict]:
        """Our own orders. marketRows streams every order on the boards we
        looked at (other players' too), so filter by our player id."""
        me = self.state.self_.get("id")
        return [o for o in self.state.market_orders.values() if me and o.get("playerId") == me]

    def _open_sell_items(self) -> set[str]:
        return {o.get("item") for o in self.my_orders()
                if o.get("side") == "sell" and int(o.get("quantity") or 0) > 0}

    # ------------------------------------------------------------- actions
    async def reprice_stale(self) -> int:
        """Cancel sell listings older than MARKET_REPRICE_HOURS so they get
        re-listed at today's price."""
        cutoff = time.time() - self.cfg.market_reprice_hours * 3600
        n = 0
        for o in self.my_orders():
            created = o.get("createdAt") or 0
            created = created / 1000 if created > 1e11 else created     # ms -> s
            if o.get("side") == "sell" and created and created < cutoff:
                self._cancelled.add(o["id"])
                await self.client.send(P.market_cancel_order(o["id"]))
                n += 1
                await asyncio.sleep(0.8)
        # gear gets MARKET_GEAR_RELIST_HOURS (fewer buyers per model); each pull
        # makes the next listing cheaper, and the second pull salvages it
        gear_cutoff = time.time() - self.cfg.market_gear_relist_hours * 3600
        for x in self.my_listings():
            created = x.get("createdAt") or 0
            created = created / 1000 if created > 1e11 else created
            if x.get("id") and created and created < gear_cutoff:
                await self.client.send(P.market_cancel_listing(x["id"]))
                gid = (x.get("gear") or {}).get("id")
                self._listed_now.discard(gid)
                if gid:
                    self.relists[gid] = self.relists.get(gid, 0) + 1   # came back unsold
                    self._save_relists()
                n += 1
                await asyncio.sleep(0.8)
        if n:
            log.info("market: cancelled %d stale listing(s) for re-pricing", n)
        return n

    async def sell_pass(self, keep_potions: int) -> dict:
        """One selling round at the broker. Returns a summary."""
        self.last_pass = time.time()
        tap: list = []
        self.state.raw_tap = tap
        try:
            return await self._sell_pass(keep_potions)
        finally:
            self.state.raw_tap = None
            notes = [m for m in tap if m.get("type") not in ("pong", "chatline", "snapshot")]
            for m in notes[:15]:
                log.info("market reply: %s", str(m)[:300])

    async def _sell_pass(self, keep_potions: int) -> dict:
        self._viewed = set()
        await self.reprice_stale()
        # refresh the boards of items we have listed but no longer carry, so
        # their order rows (filled or not) come back
        carried = {i for i, _ in self.candidates(keep_potions)}
        for item in {o.get("item") for o in self._known_orders.values()} - carried:
            if item and await self.board(item) is not None:
                self._viewed.add(item)
                await asyncio.sleep(0.6)
        open_items = self._open_sell_items()
        summary = {"instant": [], "listed": [], "npc": [], "keep": []}
        silver0 = int(self.state.self_.get("silver") or 0)
        for item, qty in self.candidates(keep_potions):
            if item in open_items:
                continue
            b = await self.board(item)
            if b is None:
                log.info("market: no board for %s (not at the broker?)", item)
                break
            self._viewed.add(item)
            action = self.plan(item, qty, b)
            kind = action[0]
            if kind == "instant":
                _, price, q = action
                await self.client.send(P.market_order("sell", item, price, q, instant=True))
                summary["instant"].append(f"{item} x{q}@{price}")
            elif kind == "list":
                _, price, q = action
                await self.client.send(P.market_order("sell", item, price, q))
                self.counts["listed"] += q
                self.counts["listed_value"] += int(P.market_net(price, q))
                summary["listed"].append(f"{item} x{q}@{price}")
            else:
                summary[kind].append(item)
            await asyncio.sleep(1.2)
        try:
            spare = sorted(self.spare_gear(), key=lambda g: -int(g.get("tier") or 0))
            summary["gear"] = await self.sell_gear(spare)
        except Exception:  # noqa: BLE001
            log.exception("gear listing failed")
            summary["gear"] = []
        await self.client.send(P.market_close())
        await asyncio.sleep(1.0)
        gained = int(self.state.self_.get("silver") or 0) - silver0
        if gained > 0:
            self.counts["instant_silver"] += gained
        log.info("market pass: sold now %s | listed %s | gear listed %s | left for NPC %s | kept %s | "
                 "silver %+d", summary["instant"] or "-", summary["listed"] or "-",
                 summary.get("gear") or "-", summary["npc"] or "-", summary["keep"] or "-", gained)
        mine = self.my_orders()
        log.info("market: my open orders: %s", ", ".join(
            f"{o.get('side')} {o.get('item')} {o.get('quantity')}/{o.get('initialQuantity')}@{o.get('price')}"
            for o in mine) or "none seen")

        # listings that vanished since the last pass (and weren't cancelled by us)
        # sold — judged only for items whose board we refreshed this pass
        now_ids = {o["id"]: o for o in mine if o.get("id")}
        filled = []
        for oid, o in self._known_orders.items():
            if oid in now_ids or oid in self._cancelled or o.get("side") != "sell":
                continue
            if o.get("item") in self._viewed:
                filled.append(o)
            else:
                now_ids[oid] = o            # unknown yet: keep watching it
        # partly filled ones count too
        for oid, o in now_ids.items():
            old = self._known_orders.get(oid)
            if old and int(old.get("quantity") or 0) > int(o.get("quantity") or 0):
                filled.append({**o, "quantity": int(old["quantity"]) - int(o["quantity"])})
        self._known_orders = now_ids
        self._cancelled.clear()
        filled_value = int(sum(P.market_net(int(o.get("price") or 0), int(o.get("quantity") or 0))
                               for o in filled))
        self.counts["filled_value"] += filled_value

        lines = []
        if gained >= 200 or summary["instant"] and gained >= 100:
            lines.append(f"sold now: {', '.join(summary['instant'])} → <b>{gained:+,}</b> silver")
        if filled_value >= 200:
            lines.append("listings sold: " + ", ".join(
                f"{o.get('item')} x{o.get('quantity')}@{o.get('price')}" for o in filled)
                + f" → ~<b>{filled_value:,}</b> silver")
        listed_value = sum(int(P.market_net(int(x.rsplit('@', 1)[1]), int(x.split(' x')[1].split('@')[0])))
                           for x in summary["listed"])
        if listed_value >= 500:
            lines.append(f"listed ~{listed_value:,} silver: {', '.join(summary['listed'])}")
        if summary.get("gear"):
            lines.append(f"gear listed ({len(summary['gear'])}): {', '.join(summary['gear'][:8])}"
                         + (" …" if len(summary["gear"]) > 8 else ""))
        if lines:
            self.notify("⚖️ <b>Market</b>\n" + "\n".join(lines) +
                        f"\nsilver now {int(self.state.self_.get('silver') or 0):,}",
                        key=f"market{time.time()}", cooldown=0)
        return summary

    async def buy_cheap(self, item: str, want: int, budget: int) -> int:
        """Buy from players when their cheapest offer undercuts the NPC
        (Red Potion 10, Return Scroll 30). Returns how many we got."""
        npc = P.NPC_BUY_PRICES.get(item)
        if not npc or want <= 0 or budget <= 0:
            return 0
        b = await self.board(item)
        if not b or not b["ask"] or b["ask"] * (1 + P.MARKET_BUY_FEE) >= npc:
            return 0
        qty = min(want, b["ask_qty"], int(budget // (b["ask"] * (1 + P.MARKET_BUY_FEE))))
        if qty <= 0:
            return 0
        before = int((self.state.self_.get("inventory") or {}).get(item) or 0)
        await self.client.send(P.market_order("buy", item, b["ask"], qty, instant=True))
        await asyncio.sleep(1.5)
        got = int((self.state.self_.get("inventory") or {}).get(item) or 0) - before
        if got > 0:
            if item == P.POTION_BUY_ENTRY:
                self.counts["bought_potions"] += got
            log.info("market: bought %s x%d at %d (NPC sells at %d)", item, got, b["ask"], npc)
        return max(got, 0)

    # ---------------------------------------------------------------- gear
    @staticmethod
    def gear_model(g: dict) -> str:
        """Client's price-group key for a piece of gear."""
        return f"gear:{g.get('template') or g.get('slot')}:{int(g.get('tier') or 0)}"

    async def gear_price(self, model: str) -> dict | None:
        """{low, avg, count} of player listings for one gear model."""
        cached = self.gear_prices.get(model)
        if cached and time.time() - cached["at"] < 600:
            return cached
        before = self.state.market_at
        self._ask += 1
        await self.client.send(P.market_depth(P.POTION_BUY_ENTRY, ask=self._ask, model=model))
        for _ in range(12):
            await asyncio.sleep(0.4)
            mdl = (self.state.market.get("board") or {}).get("model") or {}
            if self.state.market_at > before and mdl.get("key") == model:
                info = {"low": int(mdl.get("low") or 0), "avg": float(mdl.get("avg") or 0),
                        "count": int(mdl.get("count") or 0), "at": time.time()}
                self.gear_prices[model] = info
                return info
        return None

    async def browse_gear(self, **query) -> list[dict] | None:
        """Other players' gear listings matching a market filter
        (cat / part / tier / job), cheapest first. Each new query resets the
        listing rows, so they're read right after the answer."""
        self._ask += 1
        ask = self._ask
        await self.client.send(P.market_depth(P.POTION_BUY_ENTRY, ask=ask, size=40, **query))
        for _ in range(12):
            await asyncio.sleep(0.4)
            if (self.state.market.get("board") or {}).get("ask") == ask:
                break
        else:
            return None
        await asyncio.sleep(0.6)        # listing rows follow the board
        me = self.state.self_.get("id")
        rows = [r for r in self.state.market_listings.values()
                if r.get("playerId") != me and isinstance(r.get("gear"), dict) and r.get("price")]
        return sorted(rows, key=lambda r: r["price"])

    async def buy_listing(self, row: dict) -> bool:
        """Buy one gear listing; True once the piece is in our bag."""
        gid = (row.get("gear") or {}).get("id")
        silver0 = int(self.state.self_.get("silver") or 0)
        await self.client.send(P.market_buy_listing(row["id"]))
        for _ in range(8):
            await asyncio.sleep(0.5)
            if gid in self.state.gear or int(self.state.self_.get("silver") or 0) < silver0:
                return True
        return False

    def my_listings(self) -> list[dict]:
        me = self.state.self_.get("id")
        return [x for x in self.state.market_listings.values() if me and x.get("playerId") == me]

    def listed_gear_ids(self) -> set[str]:
        """Gear we have on the market. Listed gear stays in our bag (the client
        hides it from the sell list the same way)."""
        ids = {(x.get("gear") or {}).get("id") for x in self.my_listings()}
        return {i for i in ids if i} | self._listed_now

    async def sell_gear(self, spare: list[dict]) -> list[str]:
        """List spare gear (another class's, or worse than what we wear) up to
        the listing cap, priced just under the cheapest listing of its model
        but never below a tenth of the board average."""
        if not self.cfg.market_sell_gear or not spare:
            return []
        # our listing rows only arrive with a board answer: make sure we have one,
        # otherwise a full listing cap looks empty and every listing is refused
        await self.board(P.POTION_BUY_ENTRY)
        on_market = self.listed_gear_ids()
        log.info("market: %d/%d gear listings in use", len(on_market), self.cfg.market_gear_slots)
        free = self.cfg.market_gear_slots - len(on_market)
        listed: list[str] = []
        salvage: list[dict] = []
        refused = 0
        for g in spare:
            gid = g["id"]
            if gid in on_market or gid in self._refused_gear:
                continue
            if self.relists.get(gid, 0) >= P.GEAR_RELISTS_BEFORE_SALVAGE:
                salvage.append(g)       # nobody wants it at market price: take the fragments
                continue
            if free <= 0 or refused >= 4:      # cap reached (or the server keeps saying no)
                continue
            model = self.gear_model(g)
            p = await self.gear_price(model)
            if p is not None and not p["count"]:
                if int(g.get("tier") or 0) <= 2:
                    salvage.append(g)   # common low-tier piece nobody lists: take the fragments
                continue                # higher tiers with no listings may be rare: keep them
            if not p or not p["low"]:
                continue
            # Undercut the cheapest listing. The board average is dragged up by
            # wishful listings (an armor model with a 950 floor showed a 12,886
            # average), so it only guards against an absurd lowball floor.
            price = max(p["low"] - 1, int(p["avg"] * 0.1), 1)
            # came back unsold before: 15% cheaper per earlier listing
            price = max(int(price * (1 - 0.15 * self.relists.get(gid, 0))), 1)
            silver0 = int(self.state.self_.get("silver") or 0)
            await self.client.send(P.market_list_gear(gid, price))
            await asyncio.sleep(1.2)
            # accepted = it shows up in our listings, or the listing fee was taken
            fee_taken = int(self.state.self_.get("silver") or 0) < silver0
            if gid in self.listed_gear_ids() or fee_taken:
                self._listed_now.add(gid)
                free -= 1
                refused = 0
                self.counts["gear_listed"] += 1
                self.counts["gear_listed_value"] += int(P.market_net(price))
                listed.append(f"{g.get('name')}@{price}")
            else:
                # usually already listed (from before a restart) or in a preset:
                # don't retry it this session, move on to the next piece
                refused += 1
                self._refused_gear.add(gid)
                log.info("market: gear %s (%s) wasn't accepted at %d — skipping it this session",
                         g.get("name"), model, price)
        if salvage:
            await self.salvage(salvage)
        return listed

    async def salvage(self, gear: list[dict]) -> None:
        """Dismantle gear into fragments (sold on the market next pass).
        Never `destroy` — that gives nothing back."""
        frags0 = {f: int((self.state.self_.get("inventory") or {}).get(f) or 0) for f in P.FRAGMENTS}
        ids = [g["id"] for g in gear]
        for i in range(0, len(ids), 20):
            await self.client.send(P.dismantle(ids[i:i + 20]))
            await asyncio.sleep(1.2)
        gone = [g for g in gear if g["id"] not in self.state.gear]
        for g in gone:
            self.relists.pop(g["id"], None)
        self._save_relists()
        inv = self.state.self_.get("inventory") or {}
        got = {f: int(inv.get(f) or 0) - n for f, n in frags0.items() if int(inv.get(f) or 0) > n}
        self.counts["salvaged"] += len(gone)
        log.info("market: salvaged %d/%d unsold gear -> %s", len(gone), len(gear), got or "no fragments seen yet")

    def _save_relists(self) -> None:
        try:
            with open(self._relist_file, "w", encoding="utf-8") as f:
                json.dump(self.relists, f)
        except OSError:
            log.debug("could not save relist counts", exc_info=True)

    def stock_value(self, keep_potions: int) -> float:
        """Rough market value of what we'd sell (last known prices, else 5x NPC)."""
        total = 0.0
        if self.cfg.market_sell_gear and len(self.my_listings()) < self.cfg.market_gear_slots:
            total += 100 * len(self.spare_gear())     # spare gear lists for ~120-500 each
        for item, qty in self.candidates(keep_potions):
            p = self.prices.get(item)
            unit = ((p["bid"] or p["avg"]) if p
                    else P.MARKET_PRICE_HINT.get(item) or P.npc_price(item) * 5)
            total += unit * qty
        return total
