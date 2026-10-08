"""Interactive first-run setup: Telegram bot token, owner chat id, game cookie.

Run with:  python -m lumivara.setup_wizard            (full setup)
           python -m lumivara.setup_wizard --cookie   (only refresh the cookie)

Uses only the standard library so it works before dependencies are installed.
Every value is checked against the real service before it is written to .env.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
EXAMPLE_PATH = os.path.join(ROOT, ".env.example")
GAME = "https://lumivaraonline.com"
COOKIE_NAME = "pixelrpg_google_session"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


# ----------------------------------------------------------------- helpers
def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str, secret: bool = False, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = getpass.getpass(f"{prompt}{suffix}: ") if secret else input(f"{prompt}{suffix}: ")
    except EOFError:
        val = ""
    return val.strip() or default


def get_json(url: str, headers: dict | None = None, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    out[m.group(1)] = m.group(2)
    return out


def write_env(values: dict[str, str]) -> None:
    """Update keys in .env, creating it from .env.example; other lines are kept."""
    src = ENV_PATH if os.path.exists(ENV_PATH) else EXAMPLE_PATH
    with open(src, encoding="utf-8") as f:
        lines = f.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            done.add(m.group(1))
    for k, v in values.items():
        if k not in done:
            lines.append(f"{k}={v}")
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, ENV_PATH)
    if os.name != "nt":
        os.chmod(ENV_PATH, 0o600)


# ---------------------------------------------------------------- telegram
def tg(token: str, method: str, **params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return get_json(f"https://api.telegram.org/bot{token}/{method}" + (f"?{q}" if q else ""))


def setup_telegram(env: dict[str, str]) -> dict[str, str]:
    say("\n=== 1/3  Telegram bot ===")
    say("Buat bot sendiri: buka @BotFather di Telegram -> kirim /newbot -> ikuti langkahnya")
    say("-> copy token yang bentuknya  123456789:AAH...")
    current = env.get("TELEGRAM_BOT_TOKEN", "")
    have = bool(current) and ":" in current and "your-bot-token" not in current
    while True:
        token = ask("Bot token" + (" (Enter = pakai yang lama)" if have else ""), secret=True)
        if not token and have:
            token = current
        try:
            me = tg(token, "getMe")
            if me.get("ok"):
                bot = me["result"]["username"]
                say(f"  OK -> bot @{bot}")
                break
        except (urllib.error.URLError, ValueError, KeyError):
            pass
        say("  Token ditolak Telegram. Cek lagi (copy semua, termasuk angka sebelum ':').")

    say("\n=== 2/3  ID Telegram kamu (pemilik bot) ===")
    say(f"Buka https://t.me/{bot} lalu tekan START / kirim /start. Menunggu pesan kamu...")
    chat_id = ""
    try:
        tg(token, "deleteWebhook")
        offset = 0
        deadline = time.time() + 180
        while time.time() < deadline and not chat_id:
            res = tg(token, "getUpdates", timeout=20, offset=offset)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if msg.get("chat", {}).get("type") == "private":
                    chat_id = str(msg["chat"]["id"])
                    who = msg.get("from", {}).get("username") or msg.get("from", {}).get("first_name")
                    say(f"  OK -> pesan dari {who}, chat id {chat_id}")
                    break
        if offset:
            tg(token, "getUpdates", offset=offset)  # mark them read
    except urllib.error.URLError:
        pass
    if not chat_id:
        say("  Belum ada pesan masuk. Kamu bisa ketik ID manual (cek di @userinfobot).")
        while not chat_id.lstrip("-").isdigit():
            chat_id = ask("Chat ID (angka)", default=env.get("TELEGRAM_CHAT_ID", "") if
                          env.get("TELEGRAM_CHAT_ID", "").lstrip("-").isdigit() else "")
    try:
        tg(token, "sendMessage", chat_id=chat_id,
           text="Lumivara%20Sentinel%3A%20setup%20OK%20%E2%9C%85%20%E2%80%94%20bot%20ini%20cuma%20nurut%20sama%20kamu.")
    except urllib.error.URLError:
        pass
    return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}


# ------------------------------------------------------------------ cookie
def normalize_cookie(raw: str) -> str:
    """Accept the whole Cookie header, `name=value`, or just the value."""
    raw = raw.strip().strip('"').strip("'")
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    m = re.search(rf"{COOKIE_NAME}=([^;\s]+)", raw)
    if m:
        return f"{COOKIE_NAME}={m.group(1)}"
    if raw.startswith("eyJ"):
        return f"{COOKIE_NAME}={raw}"
    return raw


def cookie_expiry(cookie: str) -> str:
    # The session value is "<payload>.<sig>" (sometimes a full 3-part JWT); find the part with `exp`.
    value = cookie.split("=", 1)[-1]
    for part in value.split("."):
        try:
            data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("exp"):
            return time.strftime("%d %b %Y", time.localtime(data["exp"]))
    return "?"


def setup_cookie(env: dict[str, str]) -> dict[str, str]:
    say("\n=== 3/3  Login game (cookie) ===")
    say("Game login pakai Google. Bot butuh cookie sesi dari browser yang SUDAH login:")
    say("  1. Buka https://lumivaraonline.com di Chrome/Edge di PC, login Google sampai masuk game")
    say("  2. Tekan F12 -> tab 'Application' (kalau nggak kelihatan, klik '>>')")
    say("  3. Kiri: Storage -> Cookies -> https://lumivaraonline.com")
    say(f"  4. Klik baris '{COOKIE_NAME}' -> copy isi kolom Value (panjang, mulai 'eyJ')")
    say("  (Console/document.cookie TIDAK bisa — cookie ini HttpOnly.)")
    current = env.get("LUMIVARA_COOKIE", "")
    while True:
        raw = ask("Paste cookie" + (" (Enter = pakai yang lama)" if current else ""), secret=True)
        cookie = normalize_cookie(raw) if raw else current
        if not cookie:
            continue
        try:
            me = get_json(f"{GAME}/api/me", {"Cookie": cookie, "Referer": GAME + "/"})
        except (urllib.error.URLError, ValueError):
            say("  Gagal menghubungi server game, coba lagi.")
            continue
        if me.get("signedIn"):
            chars = []
            try:
                chars = get_json(f"{GAME}/api/characters",
                                 {"Cookie": cookie, "Referer": GAME + "/"}).get("characters", [])
            except (urllib.error.URLError, ValueError):
                pass
            say(f"  OK -> login sebagai {me.get('name')}  (berlaku sampai ±{cookie_expiry(cookie)})")
            for c in chars:
                say(f"     karakter: {c.get('name')} Lv{c.get('level')} {c.get('classId')} @ {c.get('area')}")
            if not chars:
                say("  ! Akun ini belum punya karakter. Buat dulu di game, lalu jalankan bot.")
            return {"LUMIVARA_COOKIE": cookie}
        say("  Cookie ditolak (belum login / salah copy / sudah kedaluwarsa). Coba lagi.")


# -------------------------------------------------------------------- main
def is_configured() -> bool:
    env = read_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    return (":" in token and env.get("TELEGRAM_CHAT_ID", "").lstrip("-").isdigit()
            and bool(env.get("LUMIVARA_COOKIE")))


def main() -> None:
    if "--check" in sys.argv:  # used by the launchers: exit 0 when .env is complete
        sys.exit(0 if is_configured() else 1)
    cookie_only = "--cookie" in sys.argv
    say("Lumivara Sentinel — setup")
    say("Semua isian disimpan di file .env di komputer ini saja (tidak dikirim ke mana pun).")
    env = read_env()
    values: dict[str, str] = {}
    if not cookie_only:
        values.update(setup_telegram(env))
    values.update(setup_cookie({**env, **values}))
    write_env(values)
    say(f"\nTersimpan di {ENV_PATH}")
    say("Jalankan bot: Windows -> run.bat   |   Linux/Mac -> ./run.sh")
    say("Lalu buka bot kamu di Telegram dan kirim /menu")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nDibatalkan.")
        sys.exit(1)
