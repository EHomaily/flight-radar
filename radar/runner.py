"""التشغيل اليومي والبحث السريع."""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from . import deals as D
from . import engine as E
from . import links as L
from . import messages as M
from . import names as N
from .config import Config, Watch
from .notify import Telegram
from .store import load, save
from .travelpayouts import TPError, TravelpayoutsClient

log = logging.getLogger(__name__)
RIYADH = dt.timezone(dt.timedelta(hours=3))


@dataclass
class Paths:
    root: Path

    @property
    def history(self) -> Path:
        return self.root / "data" / "history.json"

    @property
    def state(self) -> Path:
        return self.root / "data" / "state.json"

    @property
    def cities(self) -> Path:
        return self.root / "data" / "cities.json"

    @property
    def cities_en(self) -> Path:
        return self.root / "data" / "cities_en.json"

    @property
    def latest(self) -> Path:
        return self.root / "docs" / "data" / "latest.json"


def dashboard_url() -> str | None:
    if os.environ.get("DASHBOARD_URL"):
        return os.environ["DASHBOARD_URL"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and os.environ.get("ENABLE_PAGES", "").lower() == "true":
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def ensure_city_names(tp: TravelpayoutsClient, paths: Paths) -> None:
    if not paths.cities.exists():
        names = tp.city_names(("ar", "en"))
        if names:
            save(paths.cities, names)
    if not paths.cities_en.exists():   # للروابط مثل trip.com/flights/riyadh-to-dubai
        names = tp.city_names(("en",))
        if names:
            save(paths.cities_en, names)
    N.load_extra_cities(paths.cities)
    L.load_en_names(paths.cities_en)


# --------------------------------------------------------------- dashboard
def offer_view(o, cur) -> dict:
    d = o.to_dict()
    d.update({
        "origin_name": N.city(o.origin), "destination_name": N.city(o.destination),
        "airline_name": N.airline(o.airline), "stops": N.stops_label(o.transfers),
        "depart_label": N.fmt_date(o.depart), "ret_label": N.fmt_date(o.ret) if o.ret else None,
    })
    return d


def _ref_view(o, refs: dict, w, cur) -> dict | None:
    """سعر مطار المرجع (الرياض) لنفس الوجهة — أو رابط بحث منه إن لم يتوفر سعر محفوظ."""
    ref_code = w.reference_origin
    if not ref_code or ref_code in (o.origin, o.origin_airport):
        return None
    if o.destination in refs:
        return offer_view(refs[o.destination], cur)
    return {"missing": True, "origin_name": N.city(ref_code),
            "google_link": E.gf.search_url(ref_code, o.destination_airport or o.destination,
                                           o.depart, o.ret, w.adults, cur, w.direct_only)}


def watch_view(r: E.WatchResult, history: dict, prev: float | None, atl: float | None,
               cur: str) -> dict:
    w = r.watch
    cal = r.calendar()
    months = []
    for m in w.months:
        days = cal.get(m, {})
        cheapest = min(days.values(), key=lambda o: o.price) if days else None
        months.append({
            "month": m, "label": N.fmt_month(m),
            "days": [{"date": d, "price": o.price, "destination": o.destination,
                      "destination_name": N.city(o.destination), "verified": o.verified,
                      "ret": o.ret.isoformat() if o.ret else None,
                      "link": o.link, "google_link": o.google_link}
                     for d, o in sorted(days.items())],
            "cheapest": {"date": cheapest.depart.isoformat(), "label": N.fmt_date(cheapest.depart),
                         "price": cheapest.price} if cheapest else None,
        })
    multi = w.is_any or len(w.destinations) > 1
    refs = r.reference_by_destination()
    return {
        "id": w.id, "name": w.name, "origins": w.origins, "destinations": w.destinations,
        "origins_names": [N.city(c) for c in w.origins],
        "destinations_names": ["أي وجهة"] if w.is_any else [N.city(c) for c in w.destinations],
        "months": w.months, "trip": w.trip, "stay_days": list(w.stay_days) if w.stay_days else None,
        "direct_only": w.direct_only, "max_price": w.max_price, "is_any": w.is_any,
        "adults": w.adults, "exclude": w.exclude,
        "date_from": w.date_from.isoformat() if w.date_from else None,
        "date_to": w.date_to.isoformat() if w.date_to else None,
        "best": offer_view(r.best, cur) if r.best else None,
        "previous": prev, "all_time_low": atl,
        "top": [offer_view(o, cur) for o in r.offers[:10]],
        "reference_origin": w.reference_origin,
        "reference_name": N.city(w.reference_origin) if w.reference_origin else None,
        "destinations_table": [dict(offer_view(o, cur), ref=_ref_view(o, refs, w, cur))
                               for o in r.by_destination()[:max(w.top, len(w.destinations))]] if multi else [],
        "missing_destinations": [N.city(c) for c in w.destinations
                                 if not w.is_any and c not in {o.destination for o in r.offers}],
        "calendar": months,
        "history": [{"d": e["d"], "p": e["p"]} for e in history.get(w.id, [])],
        "live_checked": r.live_checked,
        "live_errors": r.live_errors[:5],
        "errors": r.errors,
    }


# --------------------------------------------------------------- daily
def run_daily(cfg: Config, tp: TravelpayoutsClient, tg: Telegram, paths: Paths,
              now: dt.datetime | None = None, live_fn=E.gf.live_price, http=None) -> dict:
    now = now or dt.datetime.now(RIYADH)
    today = now.date()
    cur = cfg.settings.currency
    history = load(paths.history, {})
    state = load(paths.state, {})
    first_run = not state.get("alerts") and not state.get("special_seen")
    linked = tg.ensure_chat(state) if hasattr(tg, "ensure_chat") else None
    if linked == "new":
        tg.send("✅ <b>تم ربط رادار التذاكر بهذه المحادثة</b>\nمن الآن تصلك هنا التنبيهات والملخص اليومي.")
    try:
        ensure_city_names(tp, paths)
    except Exception as e:  # الأسماء تحسين فقط
        log.info("city names: %s", e)

    alerts_before = dict(state.get("alerts", {}))
    results, alerts, prev_map, atl_map = [], [], {}, {}
    for w in cfg.watches:
        log.info("مراقبة: %s", w.name)
        r = E.collect(w, tp, cfg.settings, today, live_fn=live_fn,
                      sites=cfg.compare_sites, discounts=cfg.discounts)
        prev_map[w.id] = E.previous_best(history, w.id, today)
        atl_map[w.id] = E.all_time_low(history, w.id, today)
        alerts += E.evaluate(r, history, state, cfg.settings, now)
        E.record_history(history, r, today)
        results.append(r)

    # إزالة سجل مراقبات حُذفت من الإعدادات
    live_ids = {w.id for w in cfg.watches}
    for k in list(history):
        if k not in live_ids:
            history.pop(k)

    # -------- الخصومات
    special_all, special_new, news_all, news_new, page_new, page_status = [], [], [], [], [], []
    if cfg.deals.special_offers:
        seen = set(state.get("special_seen", []))
        for origin in sorted({o for w in cfg.watches for o in w.origins}):
            try:
                for item in tp.special_offers(origin):
                    item["origin_name"], item["destination_name"] = N.city(item["origin"]), N.city(item["destination"])
                    item["relevant"] = _relevant_special(item, cfg.watches)
                    special_all.append(item)
                    if item["id"] not in seen and item["relevant"]:
                        special_new.append(item)
                    seen.add(item["id"])
            except TPError as e:
                log.info("special offers %s: %s", origin, e)
        state["special_seen"] = sorted(seen)[-2000:]
    special_all.sort(key=lambda i: i["price"])

    if cfg.deals.news_queries:
        news_all = D.fetch_news(cfg.deals, now, http=http)
        seen = set(state.get("news_seen", []))
        news_new = [i for i in news_all if i["id"] not in seen]
        if "news_seen" not in state:
            news_new = []   # أول تشغيل: نسجّل فقط بدون إغراقك بالتنبيهات
        state["news_seen"] = sorted(seen | {i["id"] for i in news_all})[-3000:]

    if cfg.deals.pages:
        page_new, page_status = D.check_pages(cfg.deals, state, http=http)

    # -------- الإرسال
    by_id = {w.id: w for w in cfg.watches}
    if not getattr(tg, "enabled", True):
        # تيليجرام غير جاهز: لا نسجّل التنبيهات كمرسلة حتى تصلك في أول تشغيل بعد الربط
        state["alerts"] = alerts_before
    res_by_id = {r.watch.id: r for r in results}
    for a in alerts:
        ref = res_by_id[a.watch_id].reference_by_destination().get(a.payload["offer"].destination)
        tg.send(M.alert_message(a, by_id[a.watch_id].name, cur, by_id[a.watch_id].adults, ref=ref))
    if special_new:
        tg.send(M.special_offers_message(special_new, cur))
    if news_new:
        tg.send(M.news_message(news_new))
    if page_new:
        tg.send(M.page_message(page_new))
    if cfg.settings.daily_digest:
        extras = []
        if special_all and not special_new:
            extras.append(f"\n🏷️ عروض Aviasales الخاصة اليوم: {len(special_all)} (في اللوحة)")
        if first_run:
            extras.append("\n👋 هذا أول تشغيل للرادار — من الغد ستظهر مقارنة الأسعار مع اليوم السابق.")
        tg.send(M.digest(results, prev_map, cur, dashboard_url(), extras))

    # -------- الحفظ
    latest = {
        "generated_at": now.isoformat(), "generated_label": N.fmt_date(today) + now.strftime(" — %H:%M"),
        "currency": cur, "currency_label": N.CURRENCY_AR.get(cur, cur),
        "repo": os.environ.get("GITHUB_REPOSITORY"),
        "api_calls": tp.calls,
        "telegram": getattr(tg, "diag", None),
        "watches": [watch_view(r, history, prev_map[r.watch.id], atl_map[r.watch.id], cur) for r in results],
        "alerts": [{"kind": a.kind, "watch": by_id[a.watch_id].name,
                    "offer": offer_view(a.payload["offer"], cur),
                    **{k: v for k, v in a.payload.items() if k != "offer"}} for a in alerts],
        "deals": {"special": special_all[:40], "news": news_all[:30], "pages": page_status},
    }
    save(paths.history, history)
    save(paths.state, state)
    save(paths.latest, latest)
    return latest


def _relevant_special(item: dict, watches: list[Watch]) -> bool:
    for w in watches:
        if item["origin"] not in w.origins:
            continue
        if w.is_any:
            if w.max_price is None or item["price"] <= w.max_price:
                return True
        elif item["destination"] in w.destinations:
            return True
    return False


# --------------------------------------------------------------- search
def run_search(watch: Watch, cfg: Config, tp: TravelpayoutsClient, tg: Telegram,
               paths: Paths | None = None, live_fn=E.gf.live_price) -> E.WatchResult:
    today = dt.datetime.now(RIYADH).date()
    if paths:
        try:
            ensure_city_names(tp, paths)
        except Exception:
            pass
        if hasattr(tg, "ensure_chat"):
            tg.ensure_chat(load(paths.state, {}))
    r = E.collect(watch, tp, cfg.settings, today, live_fn=live_fn,
                  sites=cfg.compare_sites, discounts=cfg.discounts)
    tg.send(M.search_message(r, cfg.settings.currency))
    return r
