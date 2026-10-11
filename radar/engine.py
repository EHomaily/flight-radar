"""قلب الرادار: جمع الأسعار، بناء تقويم أرخص الأيام، وتقييم التنبيهات."""
from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import dataclass, field
from typing import Callable

from . import google_flights as gf
from . import links as L
from .config import CompareSite, Discount, Settings, Watch
from .models import Offer
from .travelpayouts import TPError, TravelpayoutsClient

log = logging.getLogger(__name__)

LiveFn = Callable[..., "Offer | None"]


@dataclass
class WatchResult:
    watch: Watch
    offers: list[Offer] = field(default_factory=list)      # كل العروض الصالحة، مرتبة بالسعر
    errors: list[str] = field(default_factory=list)
    live_checked: int = 0

    @property
    def best(self) -> Offer | None:
        return self.offers[0] if self.offers else None

    def calendar(self) -> dict[str, dict[str, Offer]]:
        """{الشهر: {اليوم: أرخص عرض في ذلك اليوم}}"""
        cal: dict[str, dict[str, Offer]] = {m: {} for m in self.watch.months}
        for o in self.offers:
            m = o.depart.strftime("%Y-%m")
            day = o.depart.isoformat()
            if m in cal and (day not in cal[m] or o.price < cal[m][day].price):
                cal[m][day] = o
        return cal

    def cheapest_per_month(self) -> dict[str, Offer]:
        return {m: min(days.values(), key=lambda o: o.price)
                for m, days in self.calendar().items() if days}

    def by_destination(self) -> list[Offer]:
        best: dict[str, Offer] = {}
        for o in self.offers:
            if o.destination not in best or o.price < best[o.destination].price:
                best[o.destination] = o
        return sorted(best.values(), key=lambda o: o.price)


# ------------------------------------------------------------------ collect
def _passes(o: Offer, w: Watch, today: dt.date) -> bool:
    if not o.origin or not o.destination or o.origin == o.destination:
        return False
    if not w.date_ok(o.depart, today) or not w.stay_ok(o.depart, o.ret):
        return False
    if w.direct_only and ((o.transfers or 0) > 0 or (o.return_transfers or 0) > 0):
        return False
    if w.is_any and (o.destination in w.exclude or (o.destination_airport or "") in w.exclude):
        return False
    return True


def apply_discounts(o: Offer, discounts: list[Discount], today: dt.date) -> None:
    best = None
    for d in discounts:
        if d.active(today) and d.applies(o.airline, o.price):
            after = round(o.price - d.amount(o.price), 2)
            if best is None or after < best[1]:
                best = (d, after)
    if best:
        o.discount_name, o.discount_price, o.discount_for_alerts = best[0].name, best[1], best[0].use_for_alerts
    else:
        o.discount_name = o.discount_price = None
        o.discount_for_alerts = False


def collect(w: Watch, tp: TravelpayoutsClient, settings: Settings, today: dt.date,
            live_fn: LiveFn | None = gf.live_price, sites: list[CompareSite] | None = None,
            discounts: list[Discount] | None = None) -> WatchResult:
    res = WatchResult(watch=w)
    raw: list[Offer] = []
    months = [m for m in w.months if m >= today.strftime("%Y-%m")]
    stay = w.stay_days or (None, None)
    if not months:
        res.errors.append("كل الأشهر المحددة انتهت — حدّث months في config.yaml")
        return res

    def safe(label, fn, *a, **kw):
        try:
            raw.extend(fn(*a, **kw))
        except TPError as e:
            res.errors.append(f"{label}: {e}")
        except Exception as e:  # لا نسمح لخطأ واحد بإيقاف الرادار
            log.exception(label)
            res.errors.append(f"{label}: خطأ غير متوقع {type(e).__name__}")

    for origin in w.origins:
        for m in months:
            if w.is_any:
                for unique in (True, False):
                    safe(f"{origin}→أي وجهة {m}", tp.prices_for_dates, origin, None, m,
                         one_way=not w.round_trip, direct=w.direct_only, unique=unique)
            else:
                for dest in w.destinations:
                    if dest == origin:
                        continue
                    label = f"{origin}→{dest} {m}"
                    safe(label, tp.grouped_prices, origin, dest, m, direct=w.direct_only,
                         min_trip=stay[0] if w.round_trip else None,
                         max_trip=stay[1] if w.round_trip else None)
                    safe(label, tp.prices_for_dates, origin, dest, m,
                         one_way=not w.round_trip, direct=w.direct_only)

    # تصفية + إزالة التكرار (نُبقي الأرخص لكل مسار وتاريخ)
    uniq: dict[str, Offer] = {}
    for o in raw:
        if not _passes(o, w, today):
            continue
        k = o.key()
        if k not in uniq or o.price < uniq[k].price:
            uniq[k] = o
    offers = sorted(uniq.values(), key=lambda o: o.price)
    if w.is_any:
        # لأي وجهة: نحتفظ بأرخص 5 تواريخ لكل وجهة لتخفيف الحجم
        per: dict[str, int] = {}
        kept = []
        for o in offers:
            per[o.destination] = per.get(o.destination, 0) + 1
            if per[o.destination] <= 5:
                kept.append(o)
        offers = kept

    for o in offers[:300]:
        o.google_link = gf.search_url(o.origin_airport or o.origin, o.destination_airport or o.destination,
                                      o.depart, o.ret, settings.adults, settings.currency, w.direct_only)

    # التحقق الحي من أرخص المرشحين (تواريخ/وجهات مختلفة)
    if live_fn and settings.google_live_check and offers:
        candidates, seen_dest = [], set()
        for o in offers:
            if w.is_any:
                if o.destination in seen_dest:
                    continue
                seen_dest.add(o.destination)
            candidates.append(o)
            if len(candidates) >= settings.google_checks_per_watch:
                break
        tried: set[str] = set()

        def verify(o: Offer) -> None:
            tried.add(o.key())
            live = live_fn(o.origin_airport or o.origin, o.destination_airport or o.destination,
                           o.depart, o.ret, settings.adults, settings.currency, w.direct_only)
            res.live_checked += 1
            if live and live.price > 0:
                o.cached_price = o.price
                o.price = round(live.price, 2)
                o.verified = True
                o.google_link = live.google_link or o.google_link
                if live.transfers is not None:
                    o.transfers = live.transfers
                if live.airline and not o.airline:
                    o.airline = live.airline

        for o in candidates:
            verify(o)
        offers.sort(key=lambda o: o.price)
        # إن صار الأرخص الآن عرضاً غير مؤكد (لأن المؤكدة ارتفعت)، نتحقق منه أيضاً — بحد أقصى محاولتين
        for _ in range(2):
            top = offers[0]
            if top.verified or top.key() in tried:
                break
            verify(top)
            offers.sort(key=lambda o: o.price)

    for o in offers[:300]:
        o.compare_links = L.build(sites or [], o, settings.adults, settings.currency)
    for o in offers:
        apply_discounts(o, discounts or [], today)
    res.offers = offers
    return res


# ------------------------------------------------------------------ alerts
@dataclass
class Alert:
    kind: str          # threshold | deal | special | news | page
    watch_id: str | None
    payload: dict


def _age_days(ts: str | None, now: dt.datetime) -> float:
    if not ts:
        return 1e9
    return (now - dt.datetime.fromisoformat(ts)).total_seconds() / 86400


def record_history(history: dict, res: WatchResult, today: dt.date, keep: int = 180) -> None:
    best = res.best
    if not best:
        return
    series = history.setdefault(res.watch.id, [])
    entry = {"d": today.isoformat(), "p": best.price, "k": best.key()}
    if series and series[-1]["d"] == entry["d"]:
        if best.price <= series[-1]["p"]:
            series[-1] = entry
    else:
        series.append(entry)
    del series[:-keep]


def baseline(history: dict, watch_id: str, today: dt.date, days: int = 30) -> tuple[float | None, int]:
    """متوسط (وسيط) أفضل سعر يومي خلال آخر N يوماً، بدون اليوم."""
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    vals = [e["p"] for e in history.get(watch_id, []) if cutoff <= e["d"] < today.isoformat()]
    return (statistics.median(vals) if vals else None), len(vals)


def evaluate(res: WatchResult, history: dict, state: dict, settings: Settings,
             now: dt.datetime) -> list[Alert]:
    """تُستدعى قبل record_history لليوم الحالي."""
    w, alerts = res.watch, []
    st = state.setdefault("alerts", {})
    if not res.best:
        return alerts
    raw_best = res.best
    # للحد: الأرخص بعد الخصومات المحتسبة في التنبيهات (قد يكون عرضاً غير الأرخص الخام)
    best = min(res.offers[:300], key=lambda o: (o.alert_price, o.price))

    def should_send(key: str, price: float) -> bool:
        last = st.get(key)
        if not last:
            return True
        return price < last["price"] * 0.97 or _age_days(last["ts"], now) >= settings.alert_cooldown_days

    def mark(key: str, o: Offer, price: float):
        st[key] = {"price": price, "ts": now.isoformat(), "offer": o.key()}

    k1, k2 = f"{w.id}:threshold", f"{w.id}:deal"
    under = w.max_price is not None and best.alert_price <= w.max_price
    base, n = baseline(history, w.id, now.date())
    is_deal = bool(base and n >= 5 and raw_best.price <= base * (1 - settings.deal_drop_percent / 100))
    deal_info = {"baseline": base, "drop": round((1 - raw_best.price / base) * 100)} if is_deal else {}

    if not under:
        st.pop(k1, None)   # إعادة تسليح التنبيه إذا ارتفع السعر فوق الحد
    if not is_deal:
        st.pop(k2, None)

    send_threshold = under and should_send(k1, best.alert_price)
    send_deal = is_deal and should_send(k2, raw_best.price)
    if send_threshold:
        # تنبيه واحد يجمع الحالتين إن تحققتا معاً على نفس العرض
        extra = deal_info if (is_deal and best is raw_best) else {}
        alerts.append(Alert("threshold", w.id, {"offer": best, "limit": w.max_price, **extra}))
        mark(k1, best, best.alert_price)
        if extra:
            mark(k2, raw_best, raw_best.price)
            send_deal = False
    if send_deal:
        alerts.append(Alert("deal", w.id, {"offer": raw_best, **deal_info}))
        mark(k2, raw_best, raw_best.price)
    return alerts


def previous_best(history: dict, watch_id: str, today: dt.date) -> float | None:
    prev = [e for e in history.get(watch_id, []) if e["d"] < today.isoformat()]
    return prev[-1]["p"] if prev else None


def all_time_low(history: dict, watch_id: str, today: dt.date) -> float | None:
    vals = [e["p"] for e in history.get(watch_id, []) if e["d"] < today.isoformat()]
    return min(vals) if vals else None
