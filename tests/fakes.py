"""خادم وهمي يحاكي Travelpayouts وأخبار Google للاختبارات (بدون إنترنت)."""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
from urllib.parse import parse_qs, urlparse

DESTS_ANY = ["DXB", "IST", "CAI", "KUL", "TBS", "AMM", "BAH", "MLE"]
BASE_USD = {"DXB": 90, "IST": 260, "SAW": 240, "CAI": 150, "KUL": 420, "TBS": 230,
            "AMM": 140, "BAH": 70, "MLE": 380, "JED": 60, "RUH": 60}


def price_for(origin, dest, day: dt.date, rt: bool, bump: float = 0) -> float:
    h = int(hashlib.md5(f"{origin}{dest}{day}".encode()).hexdigest(), 16)
    base = BASE_USD.get(dest, 200) * (1.8 if rt else 1.0)
    wiggle = (h % 40) / 100 - 0.1            # -10%..+30%
    weekend = 0.12 if day.weekday() in (3, 4) else 0
    return round(base * (1 + wiggle + weekend) * (1 + bump), 0)


class Resp:
    def __init__(self, data=None, status=200, text=None):
        self._data, self.status_code = data, status
        self.text = text if text is not None else json.dumps(data or {})
        self.ok = status < 400

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class FakeHTTP:
    def __init__(self, bump: float = 0.0, today: dt.date = dt.date(2026, 10, 11)):
        self.bump, self.today, self.calls = bump, today, []

    def _item(self, origin, dest, day, rt, transfers):
        ret = day + dt.timedelta(days=5) if rt else None
        return {
            "origin": origin, "destination": dest, "origin_airport": origin, "destination_airport": dest,
            "price": price_for(origin, dest, day, rt, self.bump) + transfers * -15,
            "airline": {"DXB": "FZ", "IST": "TK", "SAW": "PC", "CAI": "MS"}.get(dest, "XY"),
            "flight_number": "123", "departure_at": f"{day}T08:30:00+03:00",
            "return_at": f"{ret}T20:00:00+03:00" if ret else None,
            "transfers": transfers, "return_transfers": transfers if rt else 0, "duration": 180,
            "link": f"/search/{origin}{day:%d%m}{dest}1?t=fake",
        }

    def _days(self, month):
        y, m = map(int, month[:7].split("-"))
        return [dt.date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        u = urlparse(url)
        p = params or {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.endswith("grouped_prices"):
            rt = p.get("min_trip_duration") is not None
            out = {}
            for d in self._days(p["departure_at"]):
                out[d.isoformat()] = self._item(p["origin"], p["destination"], d, rt, d.day % 3 == 0)
            return Resp({"success": True, "data": out})
        if u.path.endswith("prices_for_dates"):
            rt = p.get("one_way") == "false"
            dests = [p["destination"]] if p.get("destination") else DESTS_ANY
            data = []
            for dest in dests:
                for d in self._days(p["departure_at"])[::4]:
                    data.append(self._item(p["origin"], dest, d, rt, 1))
            # تذكرة قديمة يجب أن تُستبعد
            data.append(self._item(p["origin"], dests[0], dt.date(2026, 9, 1), rt, 0))
            return Resp({"success": True, "data": data})
        if u.path.endswith("get_special_offers"):
            o = p["origin"]
            return Resp({"success": True, "data": [
                {"origin": o, "destination": "DXB", "airline": "FZ", "airline_title": "flydubai",
                 "departure_at": "2026-11-20T10:00:00+03:00", "price": 55, "signature": f"sig-{o}-dxb",
                 "link": "/RUH2011DXB1?t=x", "title": "deal"},
                {"origin": o, "destination": "LHR", "airline": "BA", "departure_at": "2026-11-22T10:00:00+03:00",
                 "price": 300, "signature": f"sig-{o}-lhr", "link": "/x", "title": "deal"}]})
        if "/data/en/cities.json" in u.path:
            return Resp([{"code": "RUH", "name": "Riyadh"}, {"code": "DXB", "name": "Dubai"},
                         {"code": "IST", "name": "Istanbul"}, {"code": "JED", "name": "Jeddah"}])
        if "cities.json" in u.path:
            return Resp([{"code": "TBS", "name": "تبليسي"}, {"code": "XYZ", "name": "مدينة اختبار"}])
        if "news.google.com" in u.netloc:
            q = p.get("q", "")
            pub = dt.datetime(2026, 10, 10, 9, 0, tzinfo=dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            rss = f"""<?xml version="1.0"?><rss><channel>
              <item><title>طيران ناس يطلق عرض خصم 30% على الرحلات الدولية - سبق</title>
                <link>https://example.com/a</link><pubDate>{pub}</pubDate><source>سبق</source></item>
              <item><title>خبر لا علاقة له - موقع</title><link>https://example.com/b</link>
                <pubDate>{pub}</pubDate></item>
              <item><title>Old flyadeal sale</title><link>https://example.com/c</link>
                <pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
            </channel></rss>"""
            return Resp(None, text=rss)
        return Resp({"success": False, "error": "unknown"}, status=404)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.enabled = True

    def send(self, text):
        self.sent.append(text)
        return True


def fake_live(markup: float = 1.05):
    from radar.models import Offer

    def live(origin, dest, depart, ret, adults, currency, direct_only):
        p = price_for(origin, dest, depart, ret is not None) * 3.75 * markup
        return Offer(source="google", origin=origin, destination=dest, depart=depart, ret=ret,
                     price=p, transfers=0, google_link="https://www.google.com/travel/flights/search?tfs=x")
    return live
