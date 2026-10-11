#!/usr/bin/env python3
"""رادار أسعار التذاكر.

الاستخدام:
  python run.py daily                 # التشغيل اليومي (مراقبة + تنبيهات + تحديث اللوحة)
  python run.py daily --dry-run       # يطبع الرسائل بدل إرسالها لتيليجرام
  python run.py search --from RUH,JED --to ANY --month 2026-12 --trip round-trip --stay 3-7
  python run.py telegram-chat-id      # يعرض رقم المحادثة بعد مراسلة البوت
  python run.py check-config          # يتحقق من صحة config.yaml
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from radar.config import ConfigError, load_config, parse_watch
from radar.notify import ConsoleNotifier, Telegram
from radar.runner import Paths, run_daily, run_search
from radar.travelpayouts import TPError, TravelpayoutsClient

ROOT = Path(__file__).resolve().parent


def make_clients(cfg, dry: bool):
    tp = TravelpayoutsClient(os.environ.get("TRAVELPAYOUTS_TOKEN", ""), usd_rate=cfg.settings.usd_rate,
                             market=cfg.settings.market)
    tg = ConsoleNotifier() if dry else Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                                                os.environ.get("TELEGRAM_CHAT_ID", ""))
    return tp, tg


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="رادار أسعار التذاكر")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daily")
    d.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("search")
    s.add_argument("--from", dest="origins", required=True, help="مثال: RUH أو RUH,JED")
    s.add_argument("--to", dest="dests", default="ANY", help="مثال: IST أو IST,SAW أو ANY")
    s.add_argument("--month", required=True, help="مثال: 2026-12 أو 2026-11,2026-12")
    s.add_argument("--trip", default="one-way", choices=["one-way", "round-trip"])
    s.add_argument("--stay", default="", help="للذهاب والعودة: 3-7")
    s.add_argument("--direct", action="store_true")
    s.add_argument("--dry-run", action="store_true")

    sub.add_parser("telegram-chat-id")
    sub.add_parser("check-config")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"❌ خطأ في config.yaml: {e}")
        return 2

    if a.cmd == "check-config":
        print(f"✅ الإعدادات سليمة — {len(cfg.watches)} مراقبة:")
        for w in cfg.watches:
            print(f"  • {w.name}: {','.join(w.origins)} → {','.join(w.destinations)} | {', '.join(w.months)} | {w.trip}")
        return 0

    if a.cmd == "telegram-chat-id":
        tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""), "x")
        ids = tg.find_chat_ids()
        if not ids:
            print("لا توجد رسائل — أرسل أي رسالة للبوت أولاً ثم أعد المحاولة.")
        for cid, nm in ids:
            print(f"TELEGRAM_CHAT_ID={cid}   ({nm})")
        return 0

    try:
        tp, tg = make_clients(cfg, getattr(a, "dry_run", False))
    except TPError as e:
        print(f"❌ {e}")
        return 2

    if a.cmd == "daily":
        latest = run_daily(cfg, tp, tg, Paths(ROOT))
        errs = [e for w in latest["watches"] for e in w["errors"]]
        print(f"✅ انتهى: {len(latest['watches'])} مراقبة، {len(latest['alerts'])} تنبيه، "
              f"{tp.calls} طلب API، {len(errs)} خطأ")
        for e in errs[:10]:
            print("  ⚠️", e)
        # نفشل فقط إذا فشلت كل الطلبات (مثلاً رمز خاطئ) حتى تلاحظ ذلك في GitHub
        all_failed = latest["watches"] and all(not w["best"] and w["errors"] for w in latest["watches"])
        return 1 if all_failed else 0

    if a.cmd == "search":
        stay = None
        if a.stay:
            lo, _, hi = a.stay.partition("-")
            stay = [int(lo), int(hi or lo)]
        raw = {
            "name": f"{a.origins} → {a.dests} ({a.month})",
            "origins": [x.strip() for x in a.origins.split(",") if x.strip()],
            "destinations": [x.strip() for x in a.dests.split(",") if x.strip()],
            "months": [x.strip() for x in a.month.split(",") if x.strip()],
            "trip": a.trip, "stay_days": stay, "direct_only": a.direct, "top": 10,
        }
        try:
            w = parse_watch(raw)
        except ConfigError as e:
            print(f"❌ {e}")
            return 2
        r = run_search(w, cfg, tp, tg, Paths(ROOT))
        print(f"✅ {len(r.offers)} عرض")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
