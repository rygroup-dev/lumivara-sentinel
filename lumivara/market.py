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
import logging
import math
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
        self.counts = {"instant_silver": 0, "listed": 0, "listed_value": 0, "bought_potions": 0}
        self._ask = 100

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
        out = []
        for item, n in inv.items():
            n = int(n or 0) if isinstance(n, (int, float)) else 0
            if n <= 0 or item in locked:
                continue
            if item in P.LOOT_ITEM_MOB or item == "refine_stone":
                out.append((item, n))
            elif self.cfg.market_sell_cards and item.endswith("_card"):
                out.append((item, n))
            elif item == P.POTION_BUY_ENTRY and n > keep_potions + 20:
                out.append((item, n - keep_potions))
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
                await self.client.send(P.market_cancel_order(o["id"]))
                n += 1
                await asyncio.sleep(0.8)
        if n:
            log.info("market: cancelled %d stale listing(s) for re-pricing", n)
        return n

    async def sell_pass(self, keep_potions: int) -> dict:
        """One selling round at the broker. Returns a summary."""
        self.last_pass = time.time()
        await self.reprice_stale()
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
        await self.client.send(P.market_close())
        await asyncio.sleep(1.0)
        gained = int(self.state.self_.get("silver") or 0) - silver0
        if gained > 0:
            self.counts["instant_silver"] += gained
        log.info("market pass: sold now %s | listed %s | left for NPC %s | kept %s | silver %+d",
                 summary["instant"] or "-", summary["listed"] or "-", summary["npc"] or "-",
                 summary["keep"] or "-", gained)
        mine = self.my_orders()
        log.info("market: my open orders (me=%s, %d rows seen): %s", self.state.self_.get("id"),
                 len(self.state.market_orders), ", ".join(
            f"{o.get('side')} {o.get('item')} {o.get('quantity')}/{o.get('initialQuantity')}@{o.get('price')}"
            for o in mine) or "none seen")
        return summary

    async def buy_potions(self, want: int, budget: int) -> int:
        """Buy Red Potions from players when cheaper than the NPC's 10 silver."""
        if want <= 0 or budget <= 0:
            return 0
        b = await self.board(P.POTION_BUY_ENTRY)
        if not b or not b["ask"] or b["ask"] >= P.POTION_PRICE:
            return 0
        qty = min(want, b["ask_qty"], int(budget // (b["ask"] * (1 + P.MARKET_BUY_FEE))))
        if qty <= 0:
            return 0
        before = int((self.state.self_.get("inventory") or {}).get(P.POTION_BUY_ENTRY) or 0)
        await self.client.send(P.market_order("buy", P.POTION_BUY_ENTRY, b["ask"], qty, instant=True))
        await asyncio.sleep(1.5)
        got = int((self.state.self_.get("inventory") or {}).get(P.POTION_BUY_ENTRY) or 0) - before
        if got > 0:
            self.counts["bought_potions"] += got
            log.info("market: bought %d potions at %d (NPC sells at %d)", got, b["ask"], P.POTION_PRICE)
        return max(got, 0)

    def stock_value(self, keep_potions: int) -> float:
        """Rough market value of what we'd sell (last known prices, else 5x NPC)."""
        total = 0.0
        for item, qty in self.candidates(keep_potions):
            p = self.prices.get(item)
            unit = (p["bid"] or p["avg"]) if p else P.npc_price(item) * 5
            total += unit * qty
        return total
