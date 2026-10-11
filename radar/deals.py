"""تتبّع خصومات المواقع: عروض Aviasales الخاصة + أخبار التخفيضات + مراقبة صفحات ثابتة."""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from .config import DealsConfig

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "ar,en;q=0.8"}


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


_AR_PREFIXES = ("وبال", "فال", "وال", "بال", "كال", "لل", "ال", "و", "ب", "ل", "ف")


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[\w%]+", text.lower()):
        out.add(w)
        for p in _AR_PREFIXES:
            if w.startswith(p) and len(w) - len(p) >= 3:
                out.add(w[len(p):])
    return out


def _has_keyword(text: str, keywords: list[str]) -> bool:
    """تطابق بداية الكلمة (خصم ← خصومات، بخصم) وليس أي جزء منها (عرض ≠ تعرض)."""
    if not keywords:
        return True
    words = _words(text)
    for k in (str(k).lower() for k in keywords):
        if not re.fullmatch(r"\w+", k):          # رموز مثل %
            if k in text:
                return True
        elif len(k) <= 3:                          # كلمات قصيرة: تطابق كامل فقط (off ≠ official)
            if k in words:
                return True
        elif any(w.startswith(k) for w in words):
            return True
    return False


# ---------------------------------------------------------------- news RSS
def news_url(query: str) -> str:
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
            + "&hl=ar&gl=SA&ceid=SA:ar")


def parse_rss(xml_text: str) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        when = None
        if pub:
            try:
                when = email.utils.parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                pass
        src = it.find("source")
        items.append({"title": title, "link": link,
                      "published": when.isoformat() if when else None,
                      "source": (src.text or "").strip() if src is not None else ""})
    return items


def fetch_news(cfg: DealsConfig, now: dt.datetime, http=None) -> list[dict]:
    http = http or requests
    cutoff = now - dt.timedelta(days=cfg.news_lookback_days)
    seen, out = set(), []
    for q in cfg.news_queries:
        try:
            r = http.get(news_url(q), headers=UA, timeout=25)
            r.raise_for_status()
            items = parse_rss(r.text)
        except Exception as e:
            log.info("news %s: %s", q, e)
            continue
        for it in items:
            if it["published"]:
                pub = dt.datetime.fromisoformat(it["published"])
                if pub.tzinfo and pub < cutoff:
                    continue
            if not _has_keyword(it["title"], cfg.news_keywords):
                continue
            norm = re.sub(r"\W+", "", it["title"].split(" - ")[0].lower())
            if norm in seen:
                continue
            seen.add(norm)
            out.append({**it, "id": _hash(norm), "query": q})
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return out


# ---------------------------------------------------------------- pages
def page_lines(html: str) -> list[str]:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text("\n")
    except ImportError:  # pragma: no cover
        text = re.sub(r"<[^>]+>", "\n", html)
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    return [l for l in lines if 6 <= len(l) <= 300]


def check_pages(cfg: DealsConfig, state: dict, http=None) -> tuple[list[dict], list[dict]]:
    """يعيد (عناصر جديدة, حالة كل صفحة) ويحدّث state['pages']."""
    http = http or requests
    pages_state = state.setdefault("pages", {})
    new_items, status = [], []
    for p in cfg.pages:
        url, name = p.get("url"), p.get("name") or p.get("url")
        kws = p.get("keywords") or cfg.news_keywords
        try:
            r = http.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            lines = [l for l in page_lines(r.text) if _has_keyword(l, kws)]
        except Exception as e:
            status.append({"name": name, "url": url, "ok": False, "note": f"تعذّر الجلب: {e}"[:160]})
            continue
        if not lines:
            status.append({"name": name, "url": url, "ok": False,
                           "note": "لا يوجد نص عروض في الصفحة (غالباً مبنية بجافاسكربت)"})
            continue
        hashes = {_hash(l): l for l in lines}
        prev = set(pages_state.get(url, []))
        if prev:  # أول تشغيل يسجّل الوضع الحالي فقط بدون تنبيه
            for h, l in hashes.items():
                if h not in prev:
                    new_items.append({"name": name, "url": url, "text": l, "id": h})
        pages_state[url] = sorted(set(hashes) | prev)[-500:]
        status.append({"name": name, "url": url, "ok": True, "note": f"{len(lines)} سطر عروض"})
    return new_items, status
