"""صياغة رسائل تيليجرام بالعربية (HTML)."""
from __future__ import annotations

import html

from . import names as N
from .engine import Alert, WatchResult
from .models import Offer


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def route(o: Offer) -> str:
    a = o.origin_airport or o.origin
    b = o.destination_airport or o.destination
    return f"{esc(N.city(o.origin))} ← {esc(N.city(o.destination))} <code>{a}→{b}</code>"


def offer_lines(o: Offer, cur: str) -> list[str]:
    when = N.fmt_date(o.depart)
    if o.ret:
        when += f" ← {N.fmt_date(o.ret)} ({o.stay} أيام)"
    meta = " · ".join(x for x in [N.stops_label(o.transfers), N.airline(o.airline)] if x)
    trust = "✅ سعر مؤكد الآن من Google Flights" if o.verified else "ℹ️ سعر من ذاكرة بحث Aviasales (آخر 48 ساعة)"
    lines = [f"✈️ {route(o)}", f"💰 <b>{N.fmt_price(o.price, cur)}</b> للشخص", f"📅 {when}"]
    if meta:
        lines.append(f"🛫 {esc(meta)}")
    lines.append(trust)
    d = discount_line(o, cur)
    if d:
        lines.append(d)
    lines.append(links(o))
    return lines


def links(o: Offer) -> str:
    parts = []
    if o.google_link:
        parts.append(f'<a href="{esc(o.google_link)}">Google Flights</a>')
    if o.link:
        parts.append(f'<a href="{esc(o.link)}">Aviasales</a>')
    out = "🔗 " + " | ".join(parts) if parts else ""
    if o.compare_links:
        cmp = " | ".join(f'<a href="{esc(c["url"])}">{esc(c["name"])}</a>' for c in o.compare_links)
        out += ("\n" if out else "") + f"🔁 قارن: {cmp}"
    return out


def discount_line(o: Offer, cur: str) -> str | None:
    if o.discount_price is None:
        return None
    return f"💳 مع {esc(o.discount_name)}: ~{N.fmt_price(o.discount_price, cur)} (تقدير)"


def alert_message(a: Alert, watch_name: str, cur: str) -> str:
    if a.kind == "threshold":
        o = a.payload["offer"]
        head = [f"🚨 <b>نزل السعر تحت حدّك!</b>", f"📌 {esc(watch_name)}",
                f"🎯 حدّك: {N.fmt_price(a.payload['limit'], cur)}"]
        if a.payload.get("drop"):
            head.append(f"🔥 أقل من المعتاد بـ {a.payload['drop']}٪ "
                        f"(المعتاد {N.fmt_price(a.payload['baseline'], cur)})")
        return "\n".join(head + [""] + offer_lines(o, cur))
    if a.kind == "deal":
        o = a.payload["offer"]
        head = [f"🔥 <b>سعر أقل من المعتاد بـ {a.payload['drop']}٪</b>", f"📌 {esc(watch_name)}",
                f"📊 المعتاد (آخر 30 يوماً): {N.fmt_price(a.payload['baseline'], cur)}"]
        return "\n".join(head + [""] + offer_lines(o, cur))
    raise ValueError(a.kind)


def special_offers_message(items: list[dict], cur: str) -> str:
    lines = ["🏷️ <b>عروض أسعار غير معتادة</b> (Aviasales)"]
    for i in items[:8]:
        when = N.fmt_date(i["depart"], with_weekday=False) if i.get("depart") else ""
        al = i.get("airline_title") or N.airline(i.get("airline"))
        link = f' — <a href="{esc(i["link"])}">افتح</a>' if i.get("link") else ""
        lines.append(f"• {esc(N.city(i['origin']))} ← {esc(N.city(i['destination']))}: "
                     f"<b>{N.fmt_price(i['price'], cur)}</b> · {when} · {esc(al)}{link}")
    return "\n".join(lines)


def news_message(items: list[dict]) -> str:
    lines = ["📰 <b>إعلانات تخفيضات جديدة</b>"]
    for i in items[:8]:
        src = f" — {esc(i['source'])}" if i.get("source") else ""
        lines.append(f'• <a href="{esc(i["link"])}">{esc(i["title"])}</a>{src}')
    return "\n".join(lines)


def page_message(items: list[dict]) -> str:
    lines = ["🌐 <b>تغيّر في صفحات العروض</b>"]
    for i in items[:10]:
        lines.append(f'• <a href="{esc(i["url"])}">{esc(i["name"])}</a>: {esc(i["text"][:180])}')
    return "\n".join(lines)


def trend(price: float, prev: float | None, cur: str) -> str:
    if prev is None:
        return ""
    diff = price - prev
    if abs(diff) < 1:
        return " (بدون تغيّر)"
    arrow = "🔻" if diff < 0 else "🔺"
    return f" {arrow} {N.fmt_price(abs(diff), cur)}"


def digest(results: list[WatchResult], prev: dict[str, float | None], cur: str,
           dashboard_url: str | None, extras: list[str]) -> str:
    out = ["📡 <b>رادار التذاكر — ملخص اليوم</b>"]
    for r in results:
        w = r.watch
        out.append("")
        out.append(f"📌 <b>{esc(w.name)}</b>")
        b = r.best
        if not b:
            msg = "لا توجد أسعار متاحة حالياً"
            if r.errors:
                msg += f" ⚠️ ({esc(r.errors[0][:120])})"
            out.append(f"   {msg}")
            continue
        limit = f" · حدّك {N.fmt_price(w.max_price, cur)}" if w.max_price else ""
        flag = " ✅" if b.verified else ""
        out.append(f"   الأفضل: <b>{N.fmt_price(b.price, cur)}</b>{flag}{trend(b.price, prev.get(w.id), cur)}{limit}")
        when = N.fmt_date(b.depart)
        if b.ret:
            when += f" ← {N.fmt_date(b.ret, with_weekday=False)}"
        out.append(f"   {esc(N.city(b.origin))} ← {esc(N.city(b.destination))} · {when}")
        d = discount_line(b, cur)
        if d:
            out.append("   " + d)
        if w.is_any:
            tops = r.by_destination()[:5]
            out.append("   أرخص الوجهات: " + "، ".join(
                f"{esc(N.city(o.destination))} {N.fmt_price(o.price, cur)}" for o in tops))
        else:
            for m, o in r.cheapest_per_month().items():
                out.append(f"   📆 أرخص يوم في {N.fmt_month(m)}: {N.fmt_date(o.depart)} — {N.fmt_price(o.price, cur)}")
        out.append("   " + links(b).replace("\n", "\n   "))
    out.extend(extras)
    if dashboard_url:
        out.append("")
        out.append(f'📊 <a href="{esc(dashboard_url)}">افتح لوحة الرادار</a>')
    return "\n".join(out)


def search_message(r: WatchResult, cur: str) -> str:
    w = r.watch
    out = [f"🔎 <b>نتيجة البحث: {esc(w.name)}</b>"]
    if not r.offers:
        out.append("لم أجد أسعاراً لهذا البحث في ذاكرة البيانات.")
        if r.errors:
            out.append(f"⚠️ {esc(r.errors[0][:200])}")
        return "\n".join(out)
    out.append("")
    out.append("<b>أفضل الأسعار:</b>")
    picks = r.by_destination()[:10] if w.is_any or len(w.destinations) > 1 else r.offers[:6]
    for o in picks:
        when = N.fmt_date(o.depart)
        if o.ret:
            when += f" ← {N.fmt_date(o.ret, with_weekday=False)}"
        flag = " ✅" if o.verified else ""
        g = f' · <a href="{esc(o.google_link)}">Google</a>' if o.google_link else ""
        out.append(f"• {esc(N.city(o.destination))}: <b>{N.fmt_price(o.price, cur)}</b>{flag} — {when}"
                   f" · {esc(N.stops_label(o.transfers))}{g}")
    if not w.is_any:
        out.append("")
        for m, o in r.cheapest_per_month().items():
            out.append(f"📆 أرخص يوم في {N.fmt_month(m)}: {N.fmt_date(o.depart)} — {N.fmt_price(o.price, cur)}")
    return "\n".join(out)
