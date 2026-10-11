import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

from radar import engine as E
from radar import messages as M
from radar.config import ConfigError, load_config, parse_watch
from radar.deals import parse_rss
from radar.notify import split
from radar.runner import Paths, run_daily
from radar.travelpayouts import TravelpayoutsClient
from tests.fakes import FakeHTTP, FakeTelegram, fake_live

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 10, 11)
NOW = dt.datetime(2026, 10, 11, 6, 15, tzinfo=dt.timezone(dt.timedelta(hours=3)))


@pytest.fixture
def cfg():
    return load_config(ROOT / "tests" / "sample_config.yaml")


def tp(http=None, rate=3.75):
    return TravelpayoutsClient("tok", usd_rate=rate, session=http or FakeHTTP())


# ---------------------------------------------------------------- config
def test_config_loads(cfg):
    assert len(cfg.watches) == 3
    w = cfg.watches[2]
    assert w.is_any and w.round_trip and w.stay_days == (3, 7)
    assert cfg.settings.usd_rate == 3.75


def test_user_config_is_valid():
    load_config(ROOT / "config.yaml")


def test_past_months_explained(cfg):
    w = parse_watch({"name": "old", "origins": ["RUH"], "destinations": ["DXB"], "months": ["2026-01"]})
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    assert not r.offers and "انتهت" in r.errors[0]


def test_config_validation():
    with pytest.raises(ConfigError):
        parse_watch({"name": "x", "origins": ["RIYADH"], "months": ["2026-12"]})
    with pytest.raises(ConfigError):
        parse_watch({"name": "x", "origins": ["RUH"], "months": ["12-2026"]})
    w = parse_watch({"name": "x", "origins": "ruh", "destinations": "any",
                     "dates": {"from": "2026-11-25", "to": "2026-12-05"}})
    assert w.months == ["2026-11", "2026-12"] and w.is_any
    assert not w.date_ok(dt.date(2026, 11, 24), TODAY)
    assert w.date_ok(dt.date(2026, 12, 5), TODAY)


# ---------------------------------------------------------------- collect
def test_collect_specific_route_calendar(cfg):
    w = cfg.watches[1]  # RUH→DXB one-way Nov+Dec
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    assert r.offers and not r.errors
    assert all(o.ret is None for o in r.offers)
    assert all(o.depart >= TODAY for o in r.offers)          # التذاكر القديمة مستبعدة
    prices = [o.price for o in r.offers]
    assert prices == sorted(prices)
    cal = r.calendar()
    assert len(cal["2026-11"]) == 30 and len(cal["2026-12"]) == 31
    cheapest = r.cheapest_per_month()
    assert cheapest["2026-11"].price == min(o.price for o in cal["2026-11"].values())
    assert r.best.link.startswith("https://www.aviasales.com/search/")
    assert "google.com/travel/flights" in r.best.google_link


def test_collect_round_trip_stay_filter(cfg):
    w = cfg.watches[0]
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    assert r.offers
    assert all(o.ret and 4 <= o.stay <= 10 for o in r.offers)
    assert {o.destination for o in r.offers} <= {"IST", "SAW"}


def test_direct_only_filter(cfg):
    w = parse_watch({"name": "d", "origins": ["RUH"], "destinations": ["DXB"], "months": ["2026-11"],
                     "direct_only": True})
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    assert r.offers and all(o.transfers == 0 for o in r.offers)


def test_collect_any_destination(cfg):
    w = cfg.watches[2]
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    dests = r.by_destination()
    assert len(dests) >= 5
    assert [o.price for o in dests] == sorted(o.price for o in dests)
    per = {}
    for o in r.offers:
        per[o.destination] = per.get(o.destination, 0) + 1
    assert max(per.values()) <= 5


def test_live_verification_replaces_price(cfg):
    w = cfg.watches[1]
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=fake_live(1.05))
    assert 3 <= r.live_checked <= 5
    verified = [o for o in r.offers if o.verified]
    assert len(verified) == r.live_checked
    from tests.fakes import price_for
    for o in verified:
        assert o.price == pytest.approx(price_for("RUH", "DXB", o.depart, False) * 3.75 * 1.05, rel=0.001)
        assert o.cached_price is not None


def test_errors_do_not_crash(cfg):
    class Broken(FakeHTTP):
        def get(self, url, params=None, headers=None, timeout=None):
            from tests.fakes import Resp
            return Resp({"success": False, "error": "boom"}, status=500)
    c = TravelpayoutsClient("tok", usd_rate=3.75, session=Broken(), retries=1)
    r = E.collect(cfg.watches[1], c, cfg.settings, TODAY, live_fn=None)
    assert not r.offers and r.errors


# ---------------------------------------------------------------- alerts
def test_threshold_alert_once_then_rearm(cfg):
    w = parse_watch({"name": "t", "origins": ["RUH"], "destinations": ["DXB"], "months": ["2026-11"],
                     "max_price": 10000})
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    hist, state = {}, {}
    a1 = E.evaluate(r, hist, state, cfg.settings, NOW)
    assert [a.kind for a in a1] == ["threshold"]
    a2 = E.evaluate(r, hist, state, cfg.settings, NOW + dt.timedelta(hours=5))
    assert a2 == []                                     # نفس السعر خلال فترة التهدئة
    a3 = E.evaluate(r, hist, state, cfg.settings, NOW + dt.timedelta(days=4))
    assert [a.kind for a in a3] == ["threshold"]        # بعد انتهاء التهدئة
    # ارتفع فوق الحد → إعادة تسليح
    w.max_price = 1
    assert E.evaluate(r, hist, state, cfg.settings, NOW + dt.timedelta(days=4, hours=1)) == []
    assert f"{w.id}:threshold" not in state["alerts"]


def test_threshold_realerts_on_further_drop(cfg):
    w = parse_watch({"name": "t", "origins": ["RUH"], "destinations": ["DXB"], "months": ["2026-11"],
                     "max_price": 10000})
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    state = {}
    E.evaluate(r, {}, state, cfg.settings, NOW)
    r.offers[0].price *= 0.9
    assert [a.kind for a in E.evaluate(r, {}, state, cfg.settings, NOW + dt.timedelta(hours=1))] == ["threshold"]


def test_deal_alert_vs_baseline(cfg):
    w = parse_watch({"name": "t", "origins": ["RUH"], "destinations": ["DXB"], "months": ["2026-11"]})
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    best = r.best.price
    hist = {w.id: [{"d": (TODAY - dt.timedelta(days=i)).isoformat(), "p": best * 1.4, "k": "x"}
                   for i in range(8, 0, -1)]}
    alerts = E.evaluate(r, hist, {}, cfg.settings, NOW)
    deal = [a for a in alerts if a.kind == "deal"]
    assert deal and deal[0].payload["drop"] == 29
    # سجل قصير (أقل من 5 أيام) لا يكفي
    short = {w.id: hist[w.id][-3:]}
    assert not [a for a in E.evaluate(r, short, {}, cfg.settings, NOW) if a.kind == "deal"]


def test_threshold_and_deal_merge_into_one(cfg):
    w = parse_watch({"name": "t", "origins": ["RUH"], "destinations": ["DXB"], "months": ["2026-11"],
                     "max_price": 10000})
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    hist = {w.id: [{"d": (TODAY - dt.timedelta(days=i)).isoformat(), "p": r.best.price * 2, "k": "x"}
                   for i in range(6, 0, -1)]}
    state = {}
    alerts = E.evaluate(r, hist, state, cfg.settings, NOW)
    assert len(alerts) == 1 and alerts[0].kind == "threshold" and alerts[0].payload["drop"] == 50
    assert "أقل من المعتاد" in M.alert_message(alerts[0], "t", "SAR")
    assert E.evaluate(r, hist, state, cfg.settings, NOW + dt.timedelta(hours=1)) == []


def test_history_one_entry_per_day(cfg):
    w = cfg.watches[1]
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    h = {}
    E.record_history(h, r, TODAY)
    E.record_history(h, r, TODAY)
    assert len(h[w.id]) == 1


# ---------------------------------------------------------------- end to end
def test_run_daily_end_to_end(cfg, tmp_path):
    paths = Paths(tmp_path)
    http = FakeHTTP()
    tg = FakeTelegram()
    latest = run_daily(cfg, tp(http), tg, paths, now=NOW, live_fn=fake_live(), http=http)

    assert paths.latest.exists() and paths.history.exists() and paths.state.exists()
    data = json.loads(paths.latest.read_text(encoding="utf-8"))
    assert len(data["watches"]) == 3
    w0 = data["watches"][0]
    assert w0["best"] and w0["calendar"][0]["month"] == "2026-12"
    assert data["deals"]["special"] and data["deals"]["news"]
    # رسائل: تنبيه(ات) + عروض خاصة ذات صلة + ملخص (الأخبار صامتة في أول تشغيل)
    joined = "\n".join(tg.sent)
    assert "ملخص اليوم" in joined and "أرخص يوم في" in joined
    assert "عروض أسعار غير معتادة" in joined
    assert "إعلانات تخفيضات" not in joined
    assert "مدينة اختبار" not in joined
    n_first = len(tg.sent)

    # اليوم التالي: الأسعار أرخص 20% → مقارنة، ولا تكرار للعروض الخاصة
    tg2 = FakeTelegram()
    http2 = FakeHTTP(bump=-0.2)
    run_daily(cfg, tp(http2), tg2, paths, now=NOW + dt.timedelta(days=1), live_fn=fake_live(), http=http2)
    joined2 = "\n".join(tg2.sent)
    assert "🔻" in joined2
    assert "عروض أسعار غير معتادة" not in joined2
    hist = json.loads(paths.history.read_text(encoding="utf-8"))
    assert all(len(v) == 2 for v in hist.values())
    assert n_first >= 2


def test_messages_escape_and_split():
    long = "\n".join(["سطر <b>" + "x" * 100] * 100)
    parts = split(long)
    assert all(len(p) <= 3900 for p in parts) and len(parts) > 1
    assert M.esc("<a&b>") == "&lt;a&amp;b&gt;"


def test_parse_rss():
    xml = """<rss><channel><item><title>عرض - مصدر</title><link>https://x</link>
    <pubDate>Sat, 10 Oct 2026 09:00:00 GMT</pubDate><source>مصدر</source></item></channel></rss>"""
    items = parse_rss(xml)
    assert items[0]["title"] == "عرض - مصدر" and items[0]["source"] == "مصدر"


# ---------------------------------------------------------------- compare & discounts
def test_compare_links_built(cfg, tmp_path):
    from radar import links as L
    from radar.models import Offer
    (tmp_path / "en.json").write_text(json.dumps({"RUH": "Riyadh", "DXB": "Dubai"}), encoding="utf-8")
    L.load_en_names(tmp_path / "en.json")
    o = Offer(source="aviasales", origin="RUH", destination="DXB", depart=dt.date(2026, 12, 5),
              ret=dt.date(2026, 12, 10), price=500)
    got = {c["name"]: c["url"] for c in L.build(cfg.compare_sites, o, 1, "SAR")}
    assert got["Trip.com"] == ("https://www.trip.com/flights/riyadh-to-dubai/tickets-ruh-dxb/"
                               "?ddate=2026-12-05&rdate=2026-12-10&flighttype=d")
    assert got["Skyscanner"].startswith("https://www.skyscanner.net/transport/flights/ruh/dxb/261205/261210/")
    assert got["Kayak"] == "https://www.kayak.com/flights/RUH-DXB/2026-12-05/2026-12-10?sort=price_a"
    o.ret = None
    one = {c["name"]: c["url"] for c in L.build(cfg.compare_sites, o, 1, "SAR")}
    assert "rdate" not in one["Trip.com"] and "flighttype=s" in one["Trip.com"]


def test_bad_template_is_skipped():
    from radar import links as L
    from radar.config import CompareSite
    from radar.models import Offer
    o = Offer(source="x", origin="RUH", destination="DXB", depart=dt.date(2026, 12, 5), price=1)
    sites = [CompareSite("bad", "https://x/{nope}"), CompareSite("rt-only", "https://y/{FROM}", None),
             CompareSite("ok", "https://z/{FROM}-{TO}")]
    assert [c["name"] for c in L.build(sites, o, 1, "SAR")] == ["rt-only", "ok"]


def test_discounts_parse_and_apply(tmp_path):
    from radar.config import Discount
    from radar.models import Offer
    o = Offer(source="x", origin="RUH", destination="DXB", depart=dt.date(2026, 12, 5), price=2000, airline="XY")
    ds = [Discount("كود", 15, max=200), Discount("بطاقة", 10, max=1000, until=dt.date(2026, 10, 1)),
          Discount("ناس فقط", 20, airlines=["SV"])]
    E.apply_discounts(o, ds, TODAY)
    assert o.discount_name == "كود" and o.discount_price == 1800      # 15% محدود بـ 200، والبطاقة منتهية
    assert o.alert_price == 2000                                       # لا يُحتسب في التنبيه افتراضياً
    ds[0].use_for_alerts = True
    E.apply_discounts(o, ds, TODAY)
    assert o.alert_price == 1800
    p = tmp_path / "c.yaml"
    p.write_text((ROOT / "tests" / "sample_config.yaml").read_text(encoding="utf-8")
                 + "\ndiscounts:\n  - {name: x, percent: 120}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_discount_triggers_threshold_when_enabled(cfg):
    from radar.config import Discount
    w = parse_watch({"name": "t", "origins": ["RUH"], "destinations": ["DXB"], "months": ["2026-11"]})
    r = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None)
    w.max_price = r.best.price * 0.9          # الحد أقل من أرخص سعر خام
    assert E.evaluate(r, {}, {}, cfg.settings, NOW) == []
    r2 = E.collect(w, tp(), cfg.settings, TODAY, live_fn=None,
                   discounts=[Discount("كود", 15, use_for_alerts=True)])
    alerts = E.evaluate(r2, {}, {}, cfg.settings, NOW)
    assert [a.kind for a in alerts] == ["threshold"]
    msg = M.alert_message(alerts[0], "t", "SAR")
    assert "💳 مع كود" in msg
