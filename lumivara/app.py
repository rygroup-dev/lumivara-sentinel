"""Application wiring and entrypoint for Lumivara Sentinel."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import time

from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ApplicationBuilder

from .automation import Automator
from .client import LumivaraClient
from .config import Config
from .state import GameState
from .telegram_bot import LumivaraTelegram

log = logging.getLogger("lumivara")


class Orchestrator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = GameState()
        self.client = LumivaraClient(cfg, self.state)
        self.automator = Automator(cfg, self.state, self.client)
        self.tasks: list[asyncio.Task] = []


async def _post_init(app: Application) -> None:
    orch: Orchestrator = app.bot_data["orch"]
    tg: LumivaraTelegram = app.bot_data["tg"]
    loop = asyncio.get_running_loop()

    async def _notify(text: str) -> None:
        await app.bot.send_message(orch.cfg.owner_chat_id, text, parse_mode="HTML",
                                   disable_notification=False)
    orch.automator.notify_cb = _notify

    orch.tasks = [
        loop.create_task(orch.client.run(), name="ws-run"),
        loop.create_task(orch.client.ping_loop(), name="ws-ping"),
        loop.create_task(orch.automator.farm_loop(), name="farm"),
        loop.create_task(orch.automator.quest_loop(), name="quest"),
        loop.create_task(orch.automator.stat_loop(), name="stats"),
        loop.create_task(orch.automator.status_loop(), name="status"),
        loop.create_task(orch.automator.equip_loop(), name="equip"),
        loop.create_task(orch.automator.skill_loop(), name="skills"),
        loop.create_task(orch.automator.gold_loop(), name="gold"),
        loop.create_task(tg.refresh_loop(), name="dash-refresh"),
    ]

    warn = ""
    if not orch.cfg.cookie:
        warn = (
            "\n\n⚠️ <b>LUMIVARA_COOKIE is empty</b> — I can't connect to the game "
            "yet. Paste your cookie into .env and restart (see README)."
        )
    try:
        await app.bot.send_message(
            orch.cfg.owner_chat_id,
            "✅ <b>Lumivara Sentinel online.</b>\nSend /menu to open the dashboard." + warn,
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        log.warning("could not send startup message (check TELEGRAM_CHAT_ID)")


async def _post_shutdown(app: Application) -> None:
    orch: Orchestrator = app.bot_data["orch"]
    orch.automator.stop()
    await orch.client.close()
    for t in orch.tasks:
        t.cancel()


def main() -> None:
    # Console + logs/bot.log (so the run can be checked without the CMD window).
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), file_handler],
    )
    # Telegram long-polling logs every request at INFO; keep the console readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if os.getenv("LUMIVARA_DEBUG"):
        logging.getLogger("lumivara").setLevel(logging.DEBUG)
    cfg = Config.load()

    problems = cfg.validate()
    fatal = [p for p in problems if "COOKIE" not in p]  # cookie is non-fatal (bot still starts)
    if fatal:
        for p in fatal:
            log.error(p)
        raise SystemExit(
            "Fix the problems above in your .env file, then run again."
        )
    for p in problems:
        log.warning(p)

    if not _single_instance(os.path.dirname(log_dir)):
        log.error("Lumivara Sentinel is already running from this folder — not starting a second copy "
                  "(two copies would kick each other off the game).")
        raise SystemExit(1)

    # Telegram may be unreachable for a moment (flaky network, PC just woke up).
    # Instead of exiting, wait and try again with a fresh application.
    delay = 10
    while True:
        orch = Orchestrator(cfg)
        tg = LumivaraTelegram(orch)
        app = (
            ApplicationBuilder()
            .token(cfg.telegram_token)
            .post_init(_post_init)
            .post_shutdown(_post_shutdown)
            .build()
        )
        tg.register(app)
        app.add_error_handler(_on_error)

        log.info("starting Telegram polling…")
        try:
            app.run_polling(poll_interval=0.0, timeout=10, drop_pending_updates=True, close_loop=False)
            return  # stopped normally (Ctrl+C)
        except (NetworkError, TimedOut) as exc:
            log.warning("can't reach Telegram (%s) — retrying in %ds", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 120)


async def _on_error(update, context) -> None:
    """Network blips are expected on a home PC; log them in one line."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("telegram network error: %s", err)
    else:
        log.error("telegram handler error", exc_info=err)


_LOCK_HANDLE = None


def _single_instance(root: str) -> bool:
    """Hold an OS file lock for the life of the process (released automatically on exit)."""
    global _LOCK_HANDLE
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    fh = open(os.path.join(root, "data", "bot.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCK_HANDLE = fh
    return True


if __name__ == "__main__":
    main()
