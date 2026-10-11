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
    adults: int = 1

    @property
    def id(self) -> str:
        # مبني على الاسم فقط حتى يبقى سجل الأسعار عند تعديل المراقبة
        return hashlib.sha1(self.name.strip().encode()).hexdigest()[:10]

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


def _to_date(v, label, end_of_month: bool = False, today: dt.date | None = None) -> dt.date:
    """يقبل YYYY-MM-DD أو YYYY-MM (أول/آخر الشهر) أو today/اليوم."""
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    t = str(v).strip()
    if t.lower() in ("today", "now", "اليوم", "الآن", "الان"):
        return today or dt.date.today()
    if MONTH_RE.match(t):
        y, m = map(int, t.split("-"))
        if end_of_month:
            nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
            return nxt - dt.timedelta(days=1)
        return dt.date(y, m, 1)
    try:
        return dt.date.fromisoformat(t)
    except ValueError as e:
        raise ConfigError(f"تاريخ غير صالح في {label}: {v} (الصيغة 2027-06-30 أو 2027-06)") from e


def parse_watch(raw: dict, today: dt.date | None = None) -> Watch:
    today = today or dt.date.today()
    name = str(raw.get("name") or "مراقبة بدون اسم").strip()
    trip = str(raw.get("trip", "one-way")).strip().lower()
    if trip not in ("one-way", "round-trip"):
        raise ConfigError(f"[{name}] trip يجب أن يكون one-way أو round-trip")

    date_from = date_to = None
    if raw.get("dates"):
        d = raw["dates"]
        if not d.get("to"):
            raise ConfigError(f"[{name}] حدّد نهاية الفترة dates.to")
        # بداية الفترة اختيارية: بدونها = من اليوم (فترة متحركة)
        date_from = _to_date(d["from"], "dates.from", today=today) if d.get("from") else None
        date_to = _to_date(d["to"], "dates.to", end_of_month=True, today=today)
        start = max(date_from or today, today)
        if date_to < (date_from or today):
            raise ConfigError(f"[{name}] نهاية الفترة قبل بدايتها")
        if (date_to - start).days > 366:
            raise ConfigError(f"[{name}] الفترة أطول من سنة — بيانات الأسعار لا تغطي أبعد من ذلك")
        months = _months_between(start, date_to)
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
    adults = int(raw.get("adults", 1) or 1)
    if not 1 <= adults <= 9:
        raise ConfigError(f"[{name}] عدد الأشخاص يجب أن يكون بين 1 و 9")
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
        adults=adults,
    )


def load_watches_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("watches", []) or [])


def load_config(path: str | Path = "config.yaml", today: dt.date | None = None,
                watches_path: str | Path | None = None) -> Config:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = data.get("settings", {}) or {}
    settings = Settings(**{k: v for k, v in s.items() if k in Settings.__dataclass_fields__})
    settings.currency = str(settings.currency).upper()
    if settings.currency not in USD_RATES:
        raise ConfigError(f"العملة {settings.currency} غير مدعومة — استخدم: {', '.join(USD_RATES)}")
    settings.market = (settings.market or "").strip().lower()

    raw_watches = list(data.get("watches", []) or [])
    wp = Path(watches_path) if watches_path else path.with_name("watches.yaml")
    raw_watches += load_watches_file(wp)
    for w in raw_watches:   # الإعداد العام لعدد الأشخاص إن لم يُحدَّد للمراقبة
        w.setdefault("adults", settings.adults)
    watches = [parse_watch(w, today=today) for w in raw_watches]
    names = [w.name for w in watches]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ConfigError(f"يوجد أكثر من مراقبة بنفس الاسم: {'، '.join(dup)}")

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
