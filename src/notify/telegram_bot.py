"""Basit Telegram bildirim istemcisi. Token/chat_id ortam değişkenlerinden okunur.
notify.telegram.enabled: false ise (varsayılan) hiçbir şey göndermez, sadece loglar."""
from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger("exitpump_bot.notify")


class TelegramNotifier:
    def __init__(self, cfg: dict):
        tg_cfg = cfg.get("notify", {}).get("telegram", {})
        self.enabled = bool(tg_cfg.get("enabled", False))
        self.token = os.environ.get(tg_cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"), "")
        self.chat_id = os.environ.get(tg_cfg.get("chat_id_env", "TELEGRAM_CHAT_ID"), "")
        if self.enabled and (not self.token or not self.chat_id):
            logger.warning("Telegram enabled=true ama token/chat_id env değişkenleri boş -- bildirimler devre dışı")
            self.enabled = False

    async def send(self, text: str) -> None:
        logger.info("[NOTIFY] %s", text)
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Telegram gönderim hatası (%s): %s", resp.status, body)
        except Exception as e:  # noqa: BLE001
            logger.warning("Telegram gönderim istisnası: %s", e)
