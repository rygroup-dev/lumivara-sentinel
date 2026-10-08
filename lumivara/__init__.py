"""Lumivara Sentinel — owner-only Telegram automation for lumivaraonline.com.

Built from observed protocol (HAR + live capture). Combat/farming is delegated
to the game's own server-side autopilot (botSettings); this package adds the
pieces the autopilot skips: quests, stat allocation, equipment, and a Telegram
control panel. Currency-moving actions are gated off until their message format
is confirmed.
"""

__version__ = "0.1.0"
