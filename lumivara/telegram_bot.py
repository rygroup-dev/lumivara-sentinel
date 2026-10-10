"""Telegram control panel (owner-only).

Renders a live dashboard with inline-button menus. The game state is pushed over
websocket in real time, so the dashboard just reflects the shared GameState and
can auto-refresh on a short interval.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import protocol as P

log = logging.getLogger("lumivara.tg")


def _on(flag: bool) -> str:
    return "🟢 ON" if flag else "⚪ OFF"


def render_dashboard(orch) -> tuple[str, InlineKeyboardMarkup]:
    st = orch.state
    s = st.summary()
    a = orch.automator.flags

    conn = "🟢 connected" if s["connected"] else "🔴 disconnected"
    if not st.self_:
        body = (
            f"<b>🎮 Lumivara Sentinel</b>\n"
            f"Status: {conn}\n\n"
            f"<i>Waiting for character data… make sure the game cookie is set "
            f"and you have a character.</i>"
        )
    else:
        hp = f"{s['hp']}/{s['maxHp']}" if s["maxHp"] else s["hp"]
        sp = f"{s['sp']}/{s['maxSp']}" if s["maxSp"] else s["sp"]
        stats = s["stats"] or {}
        stat_line = " ".join(
            f"{k.upper()}:{stats.get(k, '?')}" for k in ("str", "agi", "vit", "int", "dex", "luk")
        )
        body = (
            f"<b>🎮 {html.escape(str(s['name']))}</b>  ·  {conn}\n"
            f"Lv <b>{s['level']}</b> (job {s.get('jobLevel', '?')}) · {html.escape(str(s['classId']))} · 📍{html.escape(str(s['area']))}\n"
            f"❤️ {hp}  ({s['hp_pct']}%)   💧 {sp}\n"
            f"🪙 Silver <b>{int(s['silver'] or 0):,}</b>   🥇 Gold <b>{int(st.self_.get('gold') or 0):,}</b>   ☠️ Kills {int(s.get('kills') or 0):,}\n"
            f"📊 {stat_line}\n"
        )

    # quest status line
    qline = ""
    if st.self_:
        qs = st.quest_status()
        if qs.get("done_all"):
            qline = "\n🏁 <b>Quest</b>: tutorial complete — free farming\n"
        else:
            flag = "" if qs["auto"] else " ⚠️ needs manual action"
            qline = (
                f"\n📜 <b>Quest {qs['index'] + 1}/10</b>: {html.escape(qs['title'])} "
                f"({qs['have']}/{qs['need']}){flag}\n"
            )

    au = orch.automator
    farm_line = ""
    if st.self_:
        zone = au.desired_zone()
        zname = next((z[1] for z in P.FARM_ZONES if z[0] == zone), zone)
        pots = au.hp_potion_stock()
        pinned = " (pinned)" if orch.cfg.farm_zone in P.FARM_ZONE_IDS else ""
        farm_line = (
            f"\n<b>🌾 Farming</b>\n"
            f"🗺 {html.escape(zname)}{pinned}{' · 😴 resting' if au._resting else ''}"
            f"{' · ' + html.escape(au._zone_pick[1]) if au._zone_pick and not pinned else ''}\n"
            f"🧪 potions <b>{pots if pots is not None else '?'}</b> · "
            f"🏹 arrows {int(au._inv().get(P.ARROW_ENTRY, 0) or 0):,} · deaths {au.counts['revive']}\n"
        )
        farm_line += "\n<b>💰 Economy</b> (this session)\n"
        if orch.cfg.enable_market:
            tc = au.trader.counts
            open_sells = sum(1 for o in au.trader.my_orders() if o.get("side") == "sell")
            farm_line += (
                f"⚖️ sold now +{tc['instant_silver']:,} · filled ~{tc['filled_value']:,} · "
                f"{open_sells} item orders open\n"
                f"🛡 gear on market {len(au.trader.listed_gear_ids())}/{orch.cfg.market_gear_slots} · "
                f"spare {len(au.spare_gear())} · upgrades spent {au._gear_spent:,}/{orch.cfg.gear_daily_budget:,}\n"
            )
        else:
            farm_line += "⚖️ market OFF (ENABLE_MARKET=false)\n"
        price = au.last_gold_price
        farm_line += (
            f"🥇 auto-buy {'ON' if orch.cfg.gold_autobuy else 'OFF'} · bought "
            f"{au.counts.get('gold_bought', 0)} for {au.counts.get('gold_silver_spent', 0):,} · "
            f"price {f'{price:,}' if price else '?'} · keeps {orch.cfg.gold_reserve:,}\n"
        )
        intel = au.intel.summary_lines(st.self_.get("area"))
        if intel:
            farm_line += "\n<b>📈 Maps</b>\n" + "".join(
                f"<code>{html.escape(l)}</code>\n" for l in intel[:5])

    text = (
        body
        + farm_line
        + qline
        + f"\n<i>updated {time.strftime('%H:%M')}</i>"
    )

    kb = [
        [
            InlineKeyboardButton(
                "⏹ Stop Farm" if a["farm"] else "▶️ Start Farm", callback_data="farm"
            ),
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
        ],
        [
            InlineKeyboardButton(f"📜 Quest {_on(a['quest'])}", callback_data="quest"),
            InlineKeyboardButton(f"📊 Stats {_on(a['stats'])}", callback_data="stats"),
            InlineKeyboardButton(f"💀 Revive {_on(a['revive'])}", callback_data="revive"),
        ],
        [
            InlineKeyboardButton(f"🎒 Equip {_on(a['equip'])}", callback_data="equip"),
            InlineKeyboardButton(f"✨ Skills {_on(a['skills'])}", callback_data="skills"),
        ],
        [
            InlineKeyboardButton("⚖️ Market", callback_data="menu:market"),
            InlineKeyboardButton("🥇 Gold", callback_data="menu:gold"),
            InlineKeyboardButton("🎒 Gear", callback_data="menu:equip"),
        ],
        [
            InlineKeyboardButton("🗺 Travel", callback_data="menu:travel"),
            InlineKeyboardButton("🏪 Town run", callback_data="townrun"),
            InlineKeyboardButton("❤️ Heal now", callback_data="heal"),
        ],
        [
            InlineKeyboardButton("🎁 Claim all", callback_data="claimall"),
            InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
            InlineKeyboardButton("🧾 WS Log", callback_data="menu:wslog"),
        ],
    ]
    return text, InlineKeyboardMarkup(kb)


def _travel_menu(orch) -> InlineKeyboardMarkup:
    lvl = int(orch.state.self_.get("level") or 1)
    rows, row = [], []
    for zid, name, lo, hi in P.FARM_ZONES:
        if lo > lvl + 5:
            break  # don't offer maps far above our level
        row.append(InlineKeyboardButton(f"{name} {lo}-{hi}", callback_data=f"travel:{zid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🏘 Town (Return Scroll)", callback_data="townrun")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="refresh")])
    return InlineKeyboardMarkup(rows)


def _equip_menu(orch) -> tuple[str, InlineKeyboardMarkup]:
    au = orch.automator
    worn = orch.state.self_.get("equipped") or {}
    plan = au.best_gear()
    lines = []
    for slot in sorted(set(worn) | set(plan)):
        cur = orch.state.gear.get(worn.get(slot), {}).get("name", "—")
        best = orch.state.gear.get(plan.get(slot), {}).get("name", "—")
        mark = "✅" if worn.get(slot) == plan.get(slot) else "⬆️"
        lines.append(f"{mark} <b>{slot}</b>: {html.escape(str(cur))}"
                     + ("" if mark == "✅" else f" → {html.escape(str(best))}"))
    txt = (
        "<b>🎒 Gear</b> (best item we own per slot for this class)\n\n"
        + ("\n".join(lines) or "<i>no gear data yet</i>")
        + f"\n\nItems in bag: {len(orch.state.gear)} · auto-equip {'ON' if au.flags['equip'] else 'OFF'}"
    )
    kb = [
        [InlineKeyboardButton("⚡ Equip best now", callback_data="equip:now")],
        [InlineKeyboardButton("⬅️ Back", callback_data="refresh")],
    ]
    return txt, InlineKeyboardMarkup(kb)


def _market_text(orch) -> str:
    au = orch.automator
    tr = au.trader
    c = orch.cfg
    if not c.enable_market:
        return ("<b>⚖️ Player market</b>\n\n<i>Off — set ENABLE_MARKET=true in .env and restart.</i>")
    tc = tr.counts
    orders = [o for o in tr.my_orders() if int(o.get("quantity") or 0) > 0]
    lines = ["<b>⚖️ Player market</b> (at the town broker)\n"]
    lines.append(f"Sold instantly: <b>+{tc['instant_silver']:,}</b> silver · listings filled ~{tc['filled_value']:,}")
    lines.append(f"Listed: {tc['listed']} items (~{tc['listed_value']:,}) · gear {tc['gear_listed']} "
                 f"(~{tc['gear_listed_value']:,}) · cheap potions bought {tc['bought_potions']}")
    last = time.strftime("%H:%M", time.localtime(tr.last_pass)) if tr.last_pass else "never"
    lines.append(f"Last broker visit: {last} · next when ≥150 silver to sell, every {c.market_every_min} min")
    lines.append(f"\n<b>My item orders ({len(orders)})</b>")
    for o in orders[:12]:
        lines.append(f"• {html.escape(str(o.get('item')))} {o.get('quantity')}/{o.get('initialQuantity')} "
                     f"@{int(o.get('price') or 0):,}")
    if len(orders) > 12:
        lines.append(f"… and {len(orders) - 12} more")
    lines.append(f"\n<b>Gear</b>: {len(tr.listed_gear_ids())}/{c.market_gear_slots} on market · "
                 f"{len(au.spare_gear())} spare to sell · salvaged {tc['salvaged']} unsold → fragments")
    lines.append(f"<i>Gear unsold for {c.market_gear_relist_hours:g}h is pulled and re-listed 15% cheaper; "
                 f"after {P.GEAR_RELISTS_BEFORE_SALVAGE} unsold listings it is dismantled into fragments "
                 f"(sold here too). Nothing is ever destroyed.</i>")
    lines.append(f"Upgrades: {'ON' if c.market_buy_gear else 'OFF'} · spent today "
                 f"{au._gear_spent:,}/{c.gear_daily_budget:,} · max {c.gear_max_price:,}/piece")
    if tr.prices:
        lines.append("\n<b>Last prices seen</b> (bid / ask / avg)")
        for item, p in sorted(tr.prices.items(), key=lambda kv: -kv[1].get("at", 0))[:8]:
            lines.append(f"• {html.escape(item)}: {p['bid']:,} / {p['ask']:,} / {p['avg']:,.0f}")
    return "\n".join(lines)


def _gold_text(orch) -> str:
    gm = orch.state.gold_market or {}
    depth = gm.get("depth") or {}
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    best_bid = bids[0]["price"] if bids else None   # someone buys gold at this
    best_ask = asks[0]["price"] if asks else None   # someone sells gold at this
    silver = int(orch.state.self_.get("silver") or 0)
    gold = orch.state.self_.get("gold")
    age = time.time() - orch.state.gold_market_at if orch.state.gold_market_at else None
    lines = [
        "<b>💰 Gold Exchange</b> (price = Silver per 1 Gold)\n",
        f"Best ask (buy Gold at): <b>{f'{best_ask:,}' if best_ask else '—'}</b>",
        f"Best bid (sell Gold at): <b>{f'{best_bid:,}' if best_bid else '—'}</b>",
        f"Your silver: <b>{silver:,}</b>" + (f" · gold: {gold}" if gold is not None else ""),
    ]
    price = best_ask or best_bid
    c = orch.cfg
    au = orch.automator
    if price:
        spare = max(0, silver - c.gold_reserve)
        lines += [
            f"\nSpare silver (above {c.gold_reserve:,} reserve) buys ≈ <b>{spare // price}</b> Gold now.",
            f"Premium 30 days = 2,500 Gold ≈ <b>{2500 * price:,}</b> silver.",
        ]
    lines.append(
        f"\n<b>Auto-buy</b> {'🟢 ON' if c.gold_autobuy else '⚪ OFF'} · every 10 min · "
        f"max {c.gold_max_price:,}/gold · bought {au.counts.get('gold_bought', 0)} "
        f"for {au.counts.get('gold_silver_spent', 0):,} silver this session")
    lines.append(f"\n<i>{'updated %ds ago' % age if age is not None else 'no data yet — press Refresh'}</i>")
    return "\n".join(lines)


def _settings_text(orch) -> str:
    c = orch.cfg
    return (
        "<b>⚙️ Settings</b>\n\n"
        f"Action delay: {c.action_delay_min}s + jitter 0–{c.action_delay_jitter}s\n"
        f"Ping: {c.ping_interval}s\n"
        f"Quest interval: {c.quest_interval}s\n"
        f"Stat interval: {c.stat_interval}s\n"
        f"Stat weights: "
        + ", ".join(f"{k.upper()} {v}" for k, v in orch.automator._stat_weights().items())
        + "\n"
        f"Potion HP%: {c.potion_hp_percent}\n"
        f"Farm map: {c.farm_zone} (margin {c.zone_margin} levels)\n"
        f"Farm goal: {c.farm_goal}\n"
        f"Town shopping: {c.enable_sell} · potion target {c.potion_target} · keep {c.potion_keep} · "
        f"budget {c.potion_budget_pct}% of silver · arrows min {c.arrow_min}\n"
        f"Market: {c.enable_market} · cards {c.market_sell_cards} · gear sell {c.market_sell_gear} "
        f"({c.market_gear_slots} slots) · reprice {c.market_reprice_hours}h · every {c.market_every_min} min\n"
        f"Gear upgrades: {c.market_buy_gear} · {c.gear_daily_budget:,}/day · max {c.gear_max_price:,}/piece\n"
        f"Gold auto-buy: {c.gold_autobuy} · reserve {c.gold_reserve:,} · max {c.gold_max_price:,}/gold\n\n"
        "<i>Edit .env and restart to change these.</i>"
    )


def _wslog_text(orch) -> str:
    rows = list(orch.client.ws_log)[-15:]
    if not rows:
        body = "<i>No non-spam messages captured yet.</i>"
    else:
        lines = []
        for r in rows:
            ts = time.strftime("%H:%M:%S", time.localtime(r["t"]))
            data = html.escape(str(r["data"]))[:160]
            lines.append(f"{ts} {r['dir']} <code>{data}</code>")
        body = "\n".join(lines)
    return (
        "<b>🧾 WS Log</b> (recent client/server messages)\n\n"
        + body
        + "\n\n<i>Use this to capture sell / market-order formats while you do them "
        "once in the browser.</i>"
    )


async def _edit(q, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit a menu message; pressing Refresh on an unchanged view is not an error."""
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


MENU_BUTTON = "📋 Menu"
MENU_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton(MENU_BUTTON)]], resize_keyboard=True,
                                    is_persistent=True)
COMMANDS = [
    BotCommand("menu", "Open the dashboard"),
    BotCommand("status", "Same as /menu"),
    BotCommand("market", "Player market: orders, gear, prices"),
    BotCommand("gold", "Gold exchange and auto-buy"),
]


class LumivaraTelegram:
    def __init__(self, orch) -> None:
        self.orch = orch
        self.app: Application | None = None

    def register(self, app: Application) -> None:
        self.app = app
        app.bot_data["orch"] = self.orch
        app.bot_data["tg"] = self
        app.add_handler(CommandHandler(["start", "menu", "status"], self.cmd_menu))
        app.add_handler(CommandHandler("market", self.cmd_market))
        app.add_handler(CommandHandler("gold", self.cmd_gold))
        app.add_handler(MessageHandler(filters.Text([MENU_BUTTON]), self.cmd_menu))
        app.add_handler(CallbackQueryHandler(self.on_callback))

    async def setup_commands(self) -> None:
        """Command list shown in Telegram's ☰ menu next to the chat box."""
        try:
            await self.app.bot.set_my_commands(COMMANDS)
        except Exception:  # noqa: BLE001
            log.debug("set_my_commands failed", exc_info=True)

    async def _send_view(self, update: Update, text: str, kb: InlineKeyboardMarkup,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        context.bot_data["dash"] = {"chat_id": msg.chat_id, "message_id": msg.message_id,
                                    "last": None, "view": "sub"}

    async def cmd_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        await self._send_view(update, _market_text(self.orch), InlineKeyboardMarkup([
            [InlineKeyboardButton("⚖️ Sell now", callback_data="market:sell"),
             InlineKeyboardButton("🛡 Shop gear now", callback_data="market:gear")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="market:refresh"),
             InlineKeyboardButton("⬅️ Dashboard", callback_data="refresh")],
        ]), context)

    async def cmd_gold(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        await self.orch.client.send(P.gold_watch())
        await asyncio.sleep(1.5)
        await self._send_view(update, _gold_text(self.orch), InlineKeyboardMarkup([
            [InlineKeyboardButton("🥇 Buy gold now", callback_data="gold:buy"),
             InlineKeyboardButton("🔄 Refresh", callback_data="gold:refresh")],
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="gold:close")],
        ]), context)

    # ------------------------------------------------------------- guards
    def _is_owner(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and chat.id == self.orch.cfg.owner_chat_id)

    # ------------------------------------------------------------ handlers
    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            await update.effective_message.reply_text("⛔ Not authorized.")
            return
        if not context.bot_data.get("menu_kb_sent"):
            # one-time: put a permanent "📋 Menu" button under the chat box
            await update.effective_message.reply_text(
                "Tap <b>📋 Menu</b> below any time to open the dashboard.",
                reply_markup=MENU_KEYBOARD, parse_mode="HTML")
            context.bot_data["menu_kb_sent"] = True
        text, kb = render_dashboard(self.orch)
        msg = await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        context.bot_data["dash"] = {
            "chat_id": msg.chat_id,
            "message_id": msg.message_id,
            "last": text,
            "view": "main",
        }

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self._is_owner(update):
            await q.answer("Not authorized", show_alert=True)
            return
        data = q.data or ""
        orch = self.orch
        note = None
        dash = context.bot_data.setdefault("dash", {})

        def mark_sub() -> None:
            dash.update(
                {
                    "chat_id": q.message.chat_id,
                    "message_id": q.message.message_id,
                    "view": "sub",  # pause auto-refresh while a submenu is open
                    "last": None,
                }
            )

        try:
            if data == "farm":
                if orch.automator.flags["farm"]:
                    await orch.automator.stop_farm()
                    note = "Farm stopped"
                else:
                    await orch.automator.start_farm()
                    note = "Farm started"
            elif data in ("quest", "stats", "revive", "equip", "skills"):
                orch.automator.flags[data] = not orch.automator.flags[data]
                note = f"{data} -> {'ON' if orch.automator.flags[data] else 'OFF'}"
            elif data == "heal":
                await orch.client.send(P.potion())
                note = "Potion sent"
            elif data == "claimall":
                for m in (P.claim_quest(), P.claim_daily(), P.mail_claim(), P.claim_hunt()):
                    await orch.client.send(m)
                note = "Claimed quest/daily/mail/hunt"
            elif data in ("marketnow", "market:sell", "market:gear"):
                au = orch.automator
                if not orch.cfg.enable_market:
                    note = "Market is off (ENABLE_MARKET=false in .env)"
                else:
                    if data == "market:gear":
                        au._last_gear_shop = 0.0     # allow the 3-hourly upgrade check now
                    au.market_requested = True       # farm loop goes to the broker (Return Scroll if needed)
                    au._last_town_run = 0.0
                    note = ("Going to the broker to shop for gear upgrades" if data == "market:gear"
                            else "Going to the market broker")
            elif data == "gold:buy":
                au = orch.automator
                n = await au.buy_gold()
                note = f"Ordered {n} Gold" if n else "Not enough spare silver / price above max"
            elif data == "townrun":
                au = orch.automator
                au._last_town_run = 0.0  # allow a shopping run right away
                au._force_town = True    # the farm loop runs it (no double actions)
                if orch.state.self_.get("area") == P.TOWN:
                    note = "Shopping now"
                elif int(au._inv().get(P.RETURN_SCROLL) or 0) > 0:
                    await orch.client.send(P.use_item(P.RETURN_SCROLL))
                    note = "Return Scroll used — will shop in town"
                else:
                    note = "No Return Scroll in bag"
            elif data == "equip:now":
                au = orch.automator
                au._equip_tries.clear()
                worn = orch.state.self_.get("equipped") or {}
                n = 0
                for slot, gid in au.best_gear().items():
                    if worn.get(slot) != gid:
                        await orch.client.send(P.equip(gid))
                        n += 1
                note = f"Equipping {n} item(s)" if n else "Already wearing the best gear"
            elif data.startswith("travel:"):
                zid = data.split(":", 1)[1]
                if zid == "auto":
                    orch.cfg.farm_zone = "auto"
                    note = "Map: auto by level"
                else:
                    # pin it; the farm loop walks there through the portals
                    # (a bare travel request is ignored away from the portal)
                    orch.cfg.farm_zone = zid
                    orch.automator._zone_pick = None
                    orch.automator._last_travel = 0.0
                    note = f"Heading to {zid} (pinned — pick Auto to undo)"
            # ----- menu navigation (edit in place) -----
            elif data == "menu:travel":
                await q.answer()
                kb = _travel_menu(orch).inline_keyboard
                kb = [[InlineKeyboardButton(
                    f"🤖 Auto by level {'✅' if orch.cfg.farm_zone == 'auto' else ''}",
                    callback_data="travel:auto")]] + list(kb)
                await q.edit_message_text(
                    "<b>🗺 Travel</b>\nPick a farm map (pins it), or Auto to follow your level:",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
                )
                mark_sub()
                return
            elif data in ("menu:gold", "gold:refresh"):
                await q.answer("Reading market…")
                await orch.client.send(P.gold_watch())
                await asyncio.sleep(1.5)
                await _edit(q, _gold_text(orch), InlineKeyboardMarkup([
                    [InlineKeyboardButton("🥇 Buy gold now", callback_data="gold:buy"),
                     InlineKeyboardButton("🔄 Refresh", callback_data="gold:refresh")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="gold:close")],
                ]))
                mark_sub()
                return
            elif data in ("menu:market", "market:refresh"):
                await q.answer()
                await _edit(q, _market_text(orch), InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚖️ Sell now", callback_data="market:sell"),
                     InlineKeyboardButton("🛡 Shop gear now", callback_data="market:gear")],
                    [InlineKeyboardButton("🔄 Refresh", callback_data="market:refresh"),
                     InlineKeyboardButton("⬅️ Back", callback_data="refresh")],
                ]))
                mark_sub()
                return
            elif data == "gold:close":
                await orch.client.send(P.gold_close())
                note = None
            elif data == "menu:equip":
                await q.answer()
                txt, kb = _equip_menu(orch)
                await q.edit_message_text(txt, reply_markup=kb, parse_mode="HTML")
                mark_sub()
                return
            elif data == "menu:settings":
                await q.answer()
                await q.edit_message_text(
                    _settings_text(orch),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Back", callback_data="refresh")]]
                    ),
                    parse_mode="HTML",
                )
                mark_sub()
                return
            elif data == "menu:wslog":
                await q.answer()
                await q.edit_message_text(
                    _wslog_text(orch),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🔄 Refresh log", callback_data="menu:wslog")],
                            [InlineKeyboardButton("⬅️ Back", callback_data="refresh")],
                        ]
                    ),
                    parse_mode="HTML",
                )
                mark_sub()
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("callback error")
            await q.answer(f"Error: {exc}", show_alert=True)
            return

        await q.answer(note or "")
        # redraw the main dashboard
        text, kb = render_dashboard(orch)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        context.bot_data["dash"] = {
            "chat_id": q.message.chat_id,
            "message_id": q.message.message_id,
            "last": text,
            "view": "main",
        }

    # --------------------------------------------------- auto refresh loop
    async def refresh_loop(self, interval: float = 5.0) -> None:
        """Background task: keep the open dashboard message in sync with state."""
        import asyncio

        app = self.app
        while True:
            await asyncio.sleep(interval)
            dash = app.bot_data.get("dash")
            if not dash:
                continue
            if dash.get("view") != "main":
                continue  # a submenu is open; don't overwrite it
            text, kb = render_dashboard(self.orch)
            if text == dash.get("last"):
                continue
            try:
                await app.bot.edit_message_text(
                    text,
                    chat_id=dash["chat_id"],
                    message_id=dash["message_id"],
                    reply_markup=kb,
                    parse_mode="HTML",
                )
                dash["last"] = text
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    log.debug("refresh skipped: %s", exc)
            except Exception:  # noqa: BLE001
                log.debug("refresh error", exc_info=True)
