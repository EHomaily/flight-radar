"""تحميل ملف الإعدادات والتحقق منه."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ANY = "ANY"
IATA_RE = re.compile(r"^[A-Z]{3}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# أسعار صرف ثابتة (العملات الخليجية مربوطة بالدولار)
USD_RATES = {"USD": 1.0, "SAR": 3.75, "AED": 3.6725}


class ConfigError(ValueError):
    pass


@dataclass
class Settings:
    currency: str = "SAR"
    adults: int = 1
    market: str = ""
    google_live_check: bool = True
    google_checks_per_watch: int = 3
    daily_digest: bool = True
    deal_drop_percent: float = 15
    alert_cooldown_days: float = 3

    @property
    def usd_rate(self) -> float:
        return USD_RATES[self.currency]


@dataclass
class Watch:
    name: str
    origins: list[str]
    destinations: list[str]
    months: list[str]
    trip: str = "one-way"
    stay_days: tuple[int, int] | None = None
    direct_only: bool = False
    max_price: float | None = None
    top: int = 10
    exclude: list[str] = field(default_factory=list)
    date_from: dt.date | None = None
    date_to: dt.date | None = None

    @property
    def id(self) -> str:
        key = "|".join([",".join(self.origins), ",".join(self.destinations), self.trip])
        return hashlib.sha1(f"{self.name}|{key}".encode()).hexdigest()[:10]

    @property
    def is_any(self) -> bool:
        return self.destinations == [ANY]

    @property
    def round_trip(self) -> bool:
        return self.trip == "round-trip"

    def date_ok(self, d: dt.date, today: dt.date) -> bool:
        if d < today:
            return False
        if self.date_from and d < self.date_from:
            return False
        if self.date_to and d > self.date_to:
            return False
        return d.strftime("%Y-%m") in self.months

    def stay_ok(self, depart: dt.date, ret: dt.date | None) -> bool:
        if not self.round_trip:
            return ret is None
        if ret is None:
            return False
        stay = (ret - depart).days
        if stay < 0:
            return False
        if self.stay_days:
            lo, hi = self.stay_days
            return lo <= stay <= hi
        return True


@dataclass
class DealsConfig:
    special_offers: bool = True
    news_lookback_days: int = 3
    news_queries: list[str] = field(default_factory=list)
    news_keywords: list[str] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)


@dataclass
class CompareSite:
    """موقع/تطبيق للمقارنة: قالب رابط يفتح نفس الرحلة مباشرة."""
    name: str
    one_way: str
    round_trip: str | None = None


@dataclass
class Discount:
    """خصم معروف (كود تطبيق، عرض بطاقة بنكية…) يُحتسب كتقدير."""
    name: str
    percent: float
    max: float | None = None          # أقصى خصم بعملة العرض
    until: dt.date | None = None      # تاريخ الانتهاء
    airlines: list[str] = field(default_factory=list)   # فارغ = كل الشركات
    min_price: float | None = None    # حد أدنى للحجز
    use_for_alerts: bool = False      # احسب السعر بعد الخصم عند مقارنته بحدّك

    def active(self, today: dt.date) -> bool:
        return self.until is None or today <= self.until

    def applies(self, airline: str | None, price: float) -> bool:
        if self.airlines and (airline or "") not in self.airlines:
            return False
        return self.min_price is None or price >= self.min_price

    def amount(self, price: float) -> float:
        a = price * self.percent / 100
        return min(a, self.max) if self.max else a


DEFAULT_COMPARE_SITES = [
    CompareSite(
        name="Trip.com",
        one_way="https://www.trip.com/flights/{from_slug}-to-{to_slug}/tickets-{from}-{to}/?ddate={depart}&flighttype=s",
        round_trip="https://www.trip.com/flights/{from_slug}-to-{to_slug}/tickets-{from}-{to}/?ddate={depart}&rdate={return}&flighttype=d",
    ),
    CompareSite(
        name="Skyscanner",
        one_way="https://www.skyscanner.net/transport/flights/{from}/{to}/{depart_yymmdd}/?adultsv2={adults}&cabinclass=economy",
        round_trip="https://www.skyscanner.net/transport/flights/{from}/{to}/{depart_yymmdd}/{return_yymmdd}/?adultsv2={adults}&cabinclass=economy",
    ),
    CompareSite(
        name="Kayak",
        one_way="https://www.kayak.com/flights/{FROM}-{TO}/{depart}?sort=price_a",
        round_trip="https://www.kayak.com/flights/{FROM}-{TO}/{depart}/{return}?sort=price_a",
    ),
]


@dataclass
class Config:
    settings: Settings
    watches: list[Watch]
    deals: DealsConfig
    compare_sites: list[CompareSite] = field(default_factory=lambda: list(DEFAULT_COMPARE_SITES))
    discounts: list[Discount] = field(default_factory=list)


def _codes(value, label: str, allow_any: bool = False) -> list[str]:
    if value is None:
        raise ConfigError(f"الحقل {label} مطلوب")
    if isinstance(value, str):
        value = [value]
    out = []
    for v in value:
        v = str(v).strip().upper()
        if allow_any and v == ANY:
            return [ANY]
        if not IATA_RE.match(v):
            raise ConfigError(f"رمز غير صالح في {label}: {v} (المطلوب 3 أحرف مثل RUH)")
        out.append(v)
    if not out:
        raise ConfigError(f"الحقل {label} فارغ")
    return out


def _months_between(a: dt.date, b: dt.date) -> list[str]:
    months, cur = [], dt.date(a.year, a.month, 1)
    while cur <= b:
        months.append(cur.strftime("%Y-%m"))
        cur = dt.date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return months


def _to_date(v, label) -> dt.date:
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError as e:
        raise ConfigError(f"تاريخ غير صالح في {label}: {v}") from e


def parse_watch(raw: dict) -> Watch:
    name = str(raw.get("name") or "مراقبة بدون اسم")
    trip = str(raw.get("trip", "one-way")).strip().lower()
    if trip not in ("one-way", "round-trip"):
        raise ConfigError(f"[{name}] trip يجب أن يكون one-way أو round-trip")

    date_from = date_to = None
    if raw.get("dates"):
        d = raw["dates"]
        date_from = _to_date(d.get("from"), "dates.from")
        date_to = _to_date(d.get("to"), "dates.to")
        if date_to < date_from:
            raise ConfigError(f"[{name}] dates.to قبل dates.from")
        months = _months_between(date_from, date_to)
    else:
        months_raw = raw.get("months")
        if not months_raw:
            raise ConfigError(f"[{name}] حدّد months أو dates")
        if isinstance(months_raw, str):
            months_raw = [months_raw]
        months = []
        for m in months_raw:
            m = m.strftime("%Y-%m") if isinstance(m, dt.date) else str(m).strip()
            if not MONTH_RE.match(m):
                raise ConfigError(f"[{name}] شهر غير صالح: {m} (الصيغة YYYY-MM)")
            months.append(m)

    stay = raw.get("stay_days")
    if stay is not None:
        if isinstance(stay, int):
            stay = (stay, stay)
        else:
            stay = (int(stay[0]), int(stay[1] if len(stay) > 1 else stay[0]))
        if stay[0] > stay[1] or stay[0] < 0:
            raise ConfigError(f"[{name}] stay_days غير منطقي: {stay}")

    max_price = raw.get("max_price")
    return Watch(
        name=name,
        origins=_codes(raw.get("origins"), "origins"),
        destinations=_codes(raw.get("destinations", [ANY]), "destinations", allow_any=True),
        months=sorted(set(months)),
        trip=trip,
        stay_days=tuple(stay) if (stay and trip == "round-trip") else None,
        direct_only=bool(raw.get("direct_only", False)),
        max_price=float(max_price) if max_price is not None else None,
        top=int(raw.get("top", 10)),
        exclude=[c.upper() for c in raw.get("exclude", []) or []],
        date_from=date_from,
        date_to=date_to,
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    s = data.get("settings", {}) or {}
    settings = Settings(**{k: v for k, v in s.items() if k in Settings.__dataclass_fields__})
    settings.currency = str(settings.currency).upper()
    if settings.currency not in USD_RATES:
        raise ConfigError(f"العملة {settings.currency} غير مدعومة — استخدم: {', '.join(USD_RATES)}")
    settings.market = (settings.market or "").strip().lower()

    watches = [parse_watch(w) for w in data.get("watches", []) or []]
    ids = [w.id for w in watches]
    if len(ids) != len(set(ids)):
        raise ConfigError("يوجد مراقبتان بنفس الاسم والمسار — غيّر اسم إحداهما")

    d = data.get("deals", {}) or {}
    deals = DealsConfig(**{k: v for k, v in d.items() if k in DealsConfig.__dataclass_fields__})
    deals.news_queries = [str(q) for q in deals.news_queries or []]
    # YAML يحوّل off/on/yes إلى قيم منطقية — نعيدها نصاً
    bool_words = {True: "on", False: "off"}
    deals.news_keywords = [bool_words.get(k, k) if isinstance(k, bool) else str(k)
                           for k in deals.news_keywords or []]
    deals.pages = deals.pages or []

    if "compare_sites" in data and data["compare_sites"] is not None:
        sites = []
        for c in data["compare_sites"] or []:
            if not c.get("name") or not c.get("one_way"):
                raise ConfigError("كل موقع في compare_sites يحتاج name و one_way")
            sites.append(CompareSite(name=str(c["name"]), one_way=str(c["one_way"]),
                                     round_trip=str(c["round_trip"]) if c.get("round_trip") else None))
    else:
        sites = list(DEFAULT_COMPARE_SITES)

    discounts = []
    for d in data.get("discounts", []) or []:
        try:
            discounts.append(Discount(
                name=str(d["name"]), percent=float(d["percent"]),
                max=float(d["max"]) if d.get("max") is not None else None,
                until=_to_date(d["until"], "discounts.until") if d.get("until") else None,
                airlines=[str(a).upper() for a in d.get("airlines", []) or []],
                min_price=float(d["min_price"]) if d.get("min_price") is not None else None,
                use_for_alerts=bool(d.get("use_for_alerts", False)),
            ))
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"خصم غير صالح في discounts: {d} — يحتاج name و percent") from e
        if not 0 < discounts[-1].percent < 100:
            raise ConfigError(f"نسبة الخصم في '{discounts[-1].name}' يجب أن تكون بين 0 و 100")
    return Config(settings=settings, watches=watches, deals=deals, compare_sites=sites, discounts=discounts)
