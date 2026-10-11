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

    def find_chat_ids(self, private_only: bool = False) -> list[tuple[str, str]]:
        r = self.http.get(f"https://api.telegram.org/bot{self.token}/getUpdates", timeout=20)
        r.raise_for_status()
        out = {}
        for u in r.json().get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") and (not private_only or chat.get("type") == "private"):
                out[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
        return list(out.items())

    def ensure_chat(self, state: dict) -> str | None:
        """إن لم يُحدَّد TELEGRAM_CHAT_ID: نكتشف محادثتك من أول رسالة أرسلتها للبوت ونحفظها."""
        if not self.token:
            return None
        if self.chat_id:
            return "secret"
        saved = state.get("telegram_chat_id")
        if saved:
            self.chat_id = saved
            return "saved"
        try:
            ids = self.find_chat_ids(private_only=True)
        except Exception as e:  # noqa: BLE001
            log.warning("تعذّر اكتشاف محادثة تيليجرام: %s", e)
            return None
        if not ids:
            log.warning("أرسل أي رسالة للبوت في تيليجرام ثم شغّل الرادار مرة أخرى")
            return None
        self.chat_id = ids[0][0]          # أول من راسل البوت = صاحبه
        state["telegram_chat_id"] = self.chat_id
        return "new"


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
