"""روابط المقارنة: تفتح نفس الرحلة مباشرة في كل موقع/تطبيق.

على الآيفون، إذا كان التطبيق مثبتاً ويدعم الروابط العامة (Universal Links)
يفتح الرابط داخل التطبيق مباشرة.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CompareSite
from .models import Offer

_en_names: dict[str, str] = {}


def load_en_names(path: Path) -> None:
    if path.exists():
        try:
            _en_names.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass


def slug(code: str) -> str:
    name = _en_names.get(code.upper()) or code
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or code.lower()


class _Missing(dict):
    def __missing__(self, key):
        raise KeyError(key)


def fields(o: Offer, adults: int, currency: str) -> dict:
    a = (o.origin_airport or o.origin).upper()
    b = (o.destination_airport or o.destination).upper()
    f = {
        "FROM": a, "TO": b, "from": a.lower(), "to": b.lower(),
        "FROM_CITY": o.origin.upper(), "TO_CITY": o.destination.upper(),
        "from_slug": slug(o.origin), "to_slug": slug(o.destination),
        "depart": o.depart.isoformat(), "depart_yymmdd": o.depart.strftime("%y%m%d"),
        "depart_ddmmyyyy": o.depart.strftime("%d-%m-%Y"),
        "adults": max(1, adults), "currency": currency, "currency_l": currency.lower(),
    }
    if o.ret:
        f.update({"return": o.ret.isoformat(), "return_yymmdd": o.ret.strftime("%y%m%d"),
                  "return_ddmmyyyy": o.ret.strftime("%d-%m-%Y")})
    return f


def build(sites: list[CompareSite], o: Offer, adults: int, currency: str) -> list[dict]:
    f = _Missing(fields(o, adults, currency))
    out = []
    for s in sites:
        tpl = s.round_trip if o.ret else s.one_way
        if not tpl:
            continue   # الموقع لا يملك قالباً لهذا النوع من الرحلات
        try:
            out.append({"name": s.name, "url": tpl.format_map(f)})
        except (KeyError, ValueError, IndexError):
            continue   # قالب فيه متغير غير معروف — نتجاهله بدل رابط خاطئ
    return out
