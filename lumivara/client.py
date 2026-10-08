"""Websocket + HTTP client for Lumivara.

Responsibilities:
* authenticate using the owner's session cookie,
* keep a websocket connected (auto-reconnect with backoff),
* merge incoming snapshots into GameState,
* send outgoing actions with paced, jittered timing,
* keep a small ring buffer of recent non-spam messages so the owner can
  capture un-reverse-engineered message formats (sell / market orders).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from typing import Awaitable, Callable

import httpx
import websockets

from . import protocol as P
from .config import Config
from .state import GameState

log = logging.getLogger("lumivara.client")

# Messages we never store in the capture log (too noisy / not useful).
_SPAM_TYPES = {"snapshot", "ping", "pong"}


class LumivaraClient:
    def __init__(self, cfg: Config, state: GameState) -> None:
        self.cfg = cfg
        self.state = state
        self._ws = None                 # active websocket connection (or None)
        self._http = None               # lazily-created httpx.AsyncClient
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._stop = False
        self.ws_log: deque = deque(maxlen=200)
        self.listeners: list[Callable[[dict], Awaitable[None] | None]] = []
        self.last_error: str = ""

    # ------------------------------------------------------------- headers
    def _headers(self) -> dict:
        return {
            "Cookie": self.cfg.cookie,
            "Origin": P.ORIGIN,
            "User-Agent": P.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        }

    # ---------------------------------------------------------------- HTTP
    async def http_get(self, path: str):
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers=self._headers(), timeout=20.0, follow_redirects=True
            )
        resp = await self._http.get(P.API_BASE + path)
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return resp.json()
        return resp.text

    async def verify_auth(self) -> dict:
        """Return /api/me; raises on network error."""
        return await self.http_get("/api/me")

    # ------------------------------------------------------------ outgoing
    async def send(self, obj: dict) -> bool:
        """Paced send with jitter. Returns True if the message went out."""
        async with self._send_lock:
            now = time.monotonic()
            wait = (self.cfg.action_delay_min + random.uniform(0, self.cfg.action_delay_jitter)) - (
                now - self._last_send
            )
            if wait > 0:
                await asyncio.sleep(wait)
            ok = await self._raw_send(obj)
            self._last_send = time.monotonic()
            return ok

    async def send_now(self, obj: dict) -> bool:
        """Unpaced send (used for ping / keepalive)."""
        return await self._raw_send(obj)

    async def _raw_send(self, obj: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(obj))
            self._record("SEND", obj)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("send failed: %s", exc)
            return False

    # ------------------------------------------------------------ incoming
    def _record(self, direction: str, obj: dict) -> None:
        t = obj.get("type", "?")
        if t in _SPAM_TYPES:
            return
        self.ws_log.append({"t": time.time(), "dir": direction, "type": t, "data": obj})

    def _probe(self, raw: str, obj: dict) -> None:
        """Debug only: report unseen top-level keys and where maxHp shows up."""
        seen = getattr(self, "_probe_keys", set())
        new = set(obj) - seen
        if new:
            log.debug("probe: new top-level keys %s (type=%s)", sorted(new), obj.get("type"))
            self._probe_keys = seen | new
        if "maxHp" in raw and not getattr(self, "_probe_hp", False):
            i = raw.find("maxHp")
            log.debug("probe: maxHp context …%s…", raw[max(0, i - 250): i + 60])
            self._probe_hp = True

    async def _handle(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        if log.isEnabledFor(logging.DEBUG):
            self._probe(raw, obj)
        self.state.apply(obj)
        self._record("RECV", obj)
        for cb in self.listeners:
            try:
                res = cb(obj)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("listener error")

    def _connect(self):
        """Open a websocket, tolerating the header-kwarg rename across
        websockets versions (extra_headers < 14, additional_headers >= 14)."""
        kw = dict(max_size=None, ping_interval=None, open_timeout=20)
        try:
            return websockets.connect(P.WS_URL, additional_headers=self._headers(), **kw)
        except TypeError:
            return websockets.connect(P.WS_URL, extra_headers=self._headers(), **kw)

    # ------------------------------------------------------------- run loop
    async def run(self) -> None:
        backoff = 2
        while not self._stop:
            try:
                async with self._connect() as ws:
                    self._ws = ws
                    self.state.connected = True
                    self.last_error = ""
                    backoff = 2
                    log.info("websocket connected")
                    # Entering the world.
                    await self._raw_send(P.spawn_ready())
                    async for raw in ws:
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                rcvd = getattr(exc, "rcvd", None)
                code = getattr(rcvd, "code", None)
                reason = getattr(rcvd, "reason", "") or ""
                if code == 4100:  # normal server-driven map change
                    log.info("map change (%s) — reconnecting", reason or "moved")
                    self.last_error = ""
                    reconnect_delay = 1  # fast: we expect to come back in the new map
                elif code == 4002:  # another session took over
                    self.last_error = "kicked: another session/tab is connected"
                    log.warning(
                        "KICKED (4002 %s): another session is connected. "
                        "Run ONLY the bot — close the game in your browser/other devices.",
                        reason,
                    )
                    reconnect_delay = 5
                else:
                    self.last_error = f"closed {code}: {reason}"
                    log.warning("websocket closed %s: %s", code, reason)
                    reconnect_delay = backoff
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("websocket error: %s", self.last_error)
                reconnect_delay = backoff
            else:
                reconnect_delay = backoff
            finally:
                self._ws = None
                self.state.connected = False
                # drop per-map state so we re-evaluate cleanly after reconnect
                self.state.mobs = {}
                self.state.drops = {}
                self.state.chars = {}
                self.state.my_n = None
            if self._stop:
                break
            await asyncio.sleep(reconnect_delay)
            backoff = min(backoff * 2, 30)

    async def ping_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(self.cfg.ping_interval)
            if self._ws is not None:
                await self.send_now(P.ping())

    async def close(self) -> None:
        self._stop = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._http is not None:
            await self._http.aclose()
