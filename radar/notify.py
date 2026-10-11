"""إرسال الرسائل إلى تيليجرام."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)
LIMIT = 3900  # حد تيليجرام 4096 حرفاً


def split(text: str, limit: int = LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += (("\n" if cur else "") + line)[:limit]
    if cur:
        parts.append(cur)
    return parts


class Telegram:
    def __init__(self, token: str, chat_id: str, http=None):
        self.token, self.chat_id = token, chat_id
        self.http = http or requests
        self.sent: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.warning("تيليجرام غير مُعد — الرسالة لم تُرسل:\n%s", text)
            return False
        ok = True
        for part in split(text):
            for attempt in range(3):
                try:
                    r = self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={
                        "chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                        "disable_web_page_preview": True}, timeout=20)
                    if r.status_code == 429:
                        time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)))
                        continue
                    if not r.ok:
                        log.error("Telegram %s: %s", r.status_code, r.text[:300])
                        ok = False
                    break
                except requests.RequestException as e:
                    log.error("Telegram: %s", e)
                    time.sleep(2)
            else:
                ok = False
            self.sent.append(part)
        return ok

    def find_chat_ids(self) -> list[tuple[str, str]]:
        r = self.http.get(f"https://api.telegram.org/bot{self.token}/getUpdates", timeout=20)
        r.raise_for_status()
        out = {}
        for u in r.json().get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                out[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
        return list(out.items())


class ConsoleNotifier(Telegram):
    """للتجربة المحلية: يطبع الرسائل بدل إرسالها."""

    def __init__(self):
        super().__init__("", "")

    @property
    def enabled(self) -> bool:
        return True

    def send(self, text: str) -> bool:
        print("\n" + "=" * 60 + "\n" + text)
        self.sent.append(text)
        return True
