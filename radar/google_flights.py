"""التحقق الحي من الأسعار عبر Google Flights (مكتبة fast-flights، بدون مفتاح).

Google قد يحجب بعض الطلبات أحياناً؛ لذلك كل فشل هنا يُتجاهل بهدوء
ويبقى سعر Aviasales هو المرجع.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import logging

from .models import Offer

log = logging.getLogger(__name__)
ERRORS: list[str] = []   # أسباب فشل التحقق الحي (تُحفظ في بيانات اللوحة للتشخيص)

try:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False


def build_query(origin: str, dest: str, depart: dt.date, ret: dt.date | None,
                adults: int, currency: str, direct_only: bool):
    legs = [FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=dest,
                        max_stops=0 if direct_only else None)]
    if ret:
        legs.append(FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=origin,
                                max_stops=0 if direct_only else None))
    return create_query(
        flights=legs, trip="round-trip" if ret else "one-way",
        passengers=Passengers(adults=max(1, adults)), currency=currency, language="ar",
        max_stops=0 if direct_only else None,
    )


def search_url(origin: str, dest: str, depart: dt.date, ret: dt.date | None = None,
               adults: int = 1, currency: str = "SAR", direct_only: bool = False) -> str:
    if AVAILABLE:
        try:
            return build_query(origin, dest, depart, ret, adults, currency, direct_only).url()
        except Exception:
            pass
    q = f"Flights from {origin} to {dest} on {depart.isoformat()}"
    if ret:
        q += f" returning {ret.isoformat()}"
    return "https://www.google.com/travel/flights?q=" + q.replace(" ", "%20") + f"&curr={currency}"


def live_price(origin: str, dest: str, depart: dt.date, ret: dt.date | None, adults: int,
               currency: str, direct_only: bool, timeout: int = 45) -> Offer | None:
    """أرخص سعر حي على Google Flights لهذا المسار والتاريخ."""
    if not AVAILABLE:
        return None
    q = build_query(origin, dest, depart, ret, adults, currency, direct_only)

    def _run():
        return get_flights(q)

    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            results = ex.submit(_run).result(timeout=timeout)
    except Exception as e:
        msg = f"{origin}→{dest} {depart}: {type(e).__name__}: {str(e)[:200]}"
        log.info("Google Flights %s", msg)
        ERRORS.append(msg)
        return None

    if not results:
        ERRORS.append(f"{origin}→{dest} {depart}: لا نتائج")
    best = None
    for f in results or []:
        price = getattr(f, "price", None)
        if not price or price <= 0:
            continue
        if best is None or price < best.price:
            legs = getattr(f, "flights", []) or []
            airlines = getattr(f, "airlines", []) or []
            best = Offer(
                source="google", origin=origin, destination=dest, depart=depart, ret=ret,
                price=float(price) / max(1, adults),   # سعر الشخص الواحد
                airline=airlines[0] if airlines else None,
                transfers=max(0, len(legs) - 1) if legs else None,
                duration=sum(getattr(l, "duration", 0) or 0 for l in legs) or None,
                google_link=q.url(),
            )
    if results and best is None:
        ERRORS.append(f"{origin}→{dest} {depart}: {len(results)} نتيجة بدون أسعار")
    return best
