"""عميل واجهة Aviasales Data API (Travelpayouts).

البيانات من ذاكرة بحث مستخدمي Aviasales خلال آخر 48 ساعة تقريباً، لذا
نعتبرها "مؤشر أسعار" ونتحقق من أرخص التواريخ حياً عبر Google Flights.
"""
from __future__ import annotations

import logging
import time

import requests

from .models import Offer, parse_date

log = logging.getLogger(__name__)

BASE = "https://api.travelpayouts.com"
AVIASALES = "https://www.aviasales.com"


class TPError(RuntimeError):
    pass


class TravelpayoutsClient:
    def __init__(self, token: str, usd_rate: float = 1.0, market: str = "", session=None,
                 retries: int = 3, timeout: int = 30):
        if not token:
            raise TPError("TRAVELPAYOUTS_TOKEN غير موجود")
        self.token = token
        self.usd_rate = usd_rate     # نطلب بالدولار ونحوّل لعملة العرض
        self.market = market
        self.http = session or requests.Session()
        self.retries = retries
        self.timeout = timeout
        self.calls = 0

    # ---------------------------------------------------------------- HTTP
    def _get(self, path: str, params: dict) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params.setdefault("currency", "usd")
        if self.market and "market" not in params:
            params["market"] = self.market
        headers = {"X-Access-Token": self.token, "Accept-Encoding": "gzip, deflate"}
        last = None
        for attempt in range(self.retries):
            try:
                self.calls += 1
                r = self.http.get(BASE + path, params=params, headers=headers, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code in (401, 403):
                    raise TPError("رمز Travelpayouts مرفوض — تأكد من TRAVELPAYOUTS_TOKEN وانضمامك لبرنامج Aviasales")
                r.raise_for_status()
                data = r.json()
                if data.get("success") is False:
                    raise TPError(f"{path}: {data.get('error')}")
                return data
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise TPError(f"{path}: فشل الاتصال ({last})")

    def _price(self, value) -> float:
        return round(float(value) * self.usd_rate, 2)

    @staticmethod
    def _link(link: str | None) -> str | None:
        if not link:
            return None
        if link.startswith("http"):
            return link
        if not link.startswith("/search"):
            link = "/search" + (link if link.startswith("/") else "/" + link)
        return AVIASALES + link

    def _offer(self, item: dict) -> Offer | None:
        depart = parse_date(item.get("departure_at"))
        if not depart or item.get("price") is None:
            return None
        return Offer(
            source="aviasales",
            origin=item.get("origin") or item.get("origin_code"),
            destination=item.get("destination") or item.get("destination_code"),
            depart=depart,
            ret=parse_date(item.get("return_at")),
            price=self._price(item["price"]),
            origin_airport=item.get("origin_airport"),
            destination_airport=item.get("destination_airport"),
            airline=item.get("airline"),
            flight_number=str(item.get("flight_number") or "") or None,
            transfers=item.get("transfers"),
            return_transfers=item.get("return_transfers"),
            duration=item.get("duration"),
            link=self._link(item.get("link")),
        )

    # ------------------------------------------------------------ endpoints
    def prices_for_dates(self, origin: str, destination: str | None, departure_at: str,
                         one_way: bool = True, direct: bool = False, unique: bool = False,
                         limit: int = 1000, sorting: str = "price") -> list[Offer]:
        """أرخص التذاكر لشهر/يوم محدد. بدون destination = لأي وجهة."""
        data = self._get("/aviasales/v3/prices_for_dates", {
            "origin": origin, "destination": destination, "departure_at": departure_at,
            "one_way": str(one_way).lower(), "direct": str(direct).lower(),
            "unique": str(unique).lower(), "limit": limit, "sorting": sorting, "page": 1,
        })
        return [o for o in (self._offer(i) for i in data.get("data") or []) if o]

    def grouped_prices(self, origin: str, destination: str, departure_at: str,
                       direct: bool = False, min_trip: int | None = None,
                       max_trip: int | None = None) -> list[Offer]:
        """أرخص تذكرة لكل يوم في الشهر (تقويم الأسعار)."""
        data = self._get("/aviasales/v3/grouped_prices", {
            "origin": origin, "destination": destination, "departure_at": departure_at,
            "group_by": "departure_at", "direct": str(direct).lower(),
            "min_trip_duration": min_trip, "max_trip_duration": max_trip,
        })
        items = data.get("data") or {}
        if isinstance(items, dict):
            items = list(items.values())
        return [o for o in (self._offer(i) for i in items) if o]

    def special_offers(self, origin: str) -> list[dict]:
        """أسعار منخفضة بشكل غير معتاد من مطار معيّن."""
        data = self._get("/aviasales/v3/get_special_offers", {"origin": origin, "locale": "en"})
        out = []
        for i in data.get("data") or []:
            if i.get("price") is None:
                continue
            out.append({
                "id": i.get("signature") or i.get("search_id") or f"{i.get('origin')}-{i.get('destination')}-{i.get('departure_at')}",
                "origin": i.get("origin"),
                "destination": i.get("destination"),
                "airline": i.get("airline"),
                "airline_title": i.get("airline_title"),
                "depart": (i.get("departure_at") or "")[:10],
                "ret": (i.get("return_at") or "")[:10] or None,
                "price": self._price(i["price"]),
                "title": i.get("title"),
                "link": self._link(i.get("link")),
            })
        return out

    def city_names(self, langs: tuple[str, ...] = ("ar", "en")) -> dict[str, str]:
        """قاعدة أسماء المدن (عامة بدون رمز). نجرب اللغات بالترتيب."""
        for lang in langs:
            try:
                r = self.http.get(f"{BASE}/data/{lang}/cities.json", timeout=self.timeout)
                if r.ok:
                    rows = r.json()
                    names = {c["code"]: (c.get("name") or (c.get("name_translations") or {}).get("en"))
                             for c in rows if c.get("code")}
                    names = {k: v for k, v in names.items() if v}
                    if names:
                        return names
            except (requests.RequestException, ValueError):
                continue
        return {}
