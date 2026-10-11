from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field


@dataclass
class Offer:
    source: str                 # aviasales | google
    origin: str
    destination: str
    depart: dt.date
    price: float                # بعملة العرض المختارة
    ret: dt.date | None = None
    origin_airport: str | None = None
    destination_airport: str | None = None
    airline: str | None = None
    flight_number: str | None = None
    transfers: int | None = None
    return_transfers: int | None = None
    duration: int | None = None
    link: str | None = None          # رابط الحجز/البحث في Aviasales
    google_link: str | None = None   # رابط البحث في Google Flights
    verified: bool = False           # تم التحقق من السعر حياً على Google Flights
    cached_price: float | None = None  # سعر ذاكرة Aviasales قبل التحقق
    compare_links: list = field(default_factory=list)  # [{name, url}] روابط المقارنة
    discount_name: str | None = None      # أفضل خصم معروف ينطبق
    discount_price: float | None = None   # السعر التقديري بعد الخصم
    discount_for_alerts: bool = False

    @property
    def alert_price(self) -> float:
        """السعر الذي يُقارن بحدّك (بعد الخصم إن طلبت ذلك)."""
        if self.discount_for_alerts and self.discount_price is not None:
            return self.discount_price
        return self.price

    @property
    def stay(self) -> int | None:
        return (self.ret - self.depart).days if self.ret else None

    def key(self) -> str:
        return f"{self.origin}-{self.destination}-{self.depart}-{self.ret or ''}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["depart"] = self.depart.isoformat()
        d["ret"] = self.ret.isoformat() if self.ret else None
        d["stay"] = self.stay
        return d


def parse_date(v) -> dt.date | None:
    if not v:
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None
