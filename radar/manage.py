"""إدارة المراقبات من نموذج GitHub (Issue Form): إضافة، تعديل، حذف.

النموذج يصل كنص Markdown بهذا الشكل:
    ### اسم المراقبة

    إجازة الربيع

    ### الوجهات

    _No response_
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import yaml

from . import names as N
from .config import ConfigError, load_config, parse_watch

# العنوان في النموذج ← المفتاح الداخلي (يجب أن تطابق .github/ISSUE_TEMPLATE)
WATCH_LABELS = {
    "اسم المراقبة": "name",
    "مطارات المغادرة": "origins",
    "مطارات مغادرة أخرى": "origins_extra",
    "الوجهات": "destinations",
    "بداية الفترة": "date_from",
    "نهاية الفترة": "date_to",
    "نوع الرحلة": "trip",
    "مدة الإقامة بالأيام": "stay",
    "عدد الأشخاص": "adults",
    "حدّك السعري للشخص": "max_price",
    "وجهات تستبعدها": "exclude",
    "خيارات": "options",
}
REMOVE_LABEL = "اسم المراقبة المراد حذفها"
NO_RESPONSE = {"_no response_", "none", ""}
ANY_WORDS = {"any", "أي وجهة", "اي وجهة", "أي مكان", "اي مكان", "الكل", "*"}


class FormError(ValueError):
    pass


def parse_form(body: str) -> dict[str, str]:
    """يحوّل نص النموذج إلى {العنوان: القيمة}."""
    out, label, buf = {}, None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            if label is not None:
                out[label] = "\n".join(buf).strip()
            label, buf = m.group(1).strip(), []
        elif label is not None:
            buf.append(line)
    if label is not None:
        out[label] = "\n".join(buf).strip()
    return {k: ("" if v.strip().lower() in NO_RESPONSE else v.strip()) for k, v in out.items()}


def _split(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,،\n؛;]+", value or "") if p.strip()]


def _codes(value: str, field: str, extra_names: dict[str, str]) -> list[str]:
    codes, unknown = [], []
    for part in _split(value):
        part = re.split(r"\s+[—–-]\s+", part)[0]   # "RUH — الرياض" → "RUH"
        try:
            c = N.resolve_code(part, extra_names)
        except ValueError:
            unknown.append(part)
            continue
        if c not in codes:
            codes.append(c)
    if unknown:
        raise FormError(f"لم أتعرف على: {'، '.join(unknown)} في «{field}». استخدم رمز المطار المكوّن من 3 أحرف (مثل IST) أو اسم المدينة بالعربي")
    return codes


def form_to_watch(fields: dict[str, str], extra_names: dict[str, str] | None = None) -> dict:
    extra_names = extra_names or {}
    f = {key: fields.get(label, "") for label, key in WATCH_LABELS.items()}
    if not f["name"]:
        raise FormError("اسم المراقبة مطلوب")

    origins = _codes(f["origins"], "مطارات المغادرة", extra_names) + _codes(f["origins_extra"], "مطارات مغادرة أخرى", extra_names)
    origins = list(dict.fromkeys(origins))
    if not origins:
        raise FormError("اختر مطار مغادرة واحداً على الأقل")

    dest_raw = f["destinations"].strip()
    if not dest_raw or dest_raw.lower() in ANY_WORDS:
        destinations = ["ANY"]
    else:
        destinations = _codes(dest_raw, "الوجهات", extra_names)

    if not f["date_to"]:
        raise FormError("نهاية الفترة مطلوبة (مثال: 2027-06-30 أو 2027-06)")
    dates = {"to": _date_text(f["date_to"])}
    if f["date_from"]:
        dates["from"] = _date_text(f["date_from"])

    trip = "round-trip" if "عودة" in f["trip"] else "one-way"
    w: dict = {"name": f["name"], "origins": origins, "destinations": destinations,
               "dates": dates, "trip": trip}

    if trip == "round-trip" and f["stay"]:
        nums = [int(n) for n in re.findall(r"\d+", _western_digits(f["stay"]))]
        if not nums:
            raise FormError("مدة الإقامة غير مفهومة — اكتبها مثل 4-10")
        w["stay_days"] = [min(nums), max(nums)]

    adults = re.findall(r"\d+", _western_digits(f["adults"] or "1"))
    w["adults"] = int(adults[0]) if adults else 1

    if f["max_price"]:
        nums = re.findall(r"\d+(?:\.\d+)?", _western_digits(f["max_price"]).replace(",", ""))
        if not nums:
            raise FormError("الحد السعري يجب أن يكون رقماً، مثل 900")
        w["max_price"] = float(nums[0]) if "." in nums[0] else int(nums[0])

    if "[x] مباشر فقط" in f["options"].lower():
        w["direct_only"] = True
    if destinations == ["ANY"] and f["exclude"]:
        w["exclude"] = _codes(f["exclude"], "وجهات تستبعدها", extra_names)
        w["top"] = 12
    return w


def _western_digits(s: str) -> str:
    return s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))


def _date_text(s: str) -> str:
    s = _western_digits(s.strip()).replace("/", "-")
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", s)     # 30-06-2027 → 2027-06-30
    if m:
        s = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", s)
    if m:
        s = f"{m.group(1)}-{int(m.group(2)):02d}" + (f"-{int(m.group(3)):02d}" if m.group(3) else "")
    return s


# ------------------------------------------------------------------ apply
def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return list((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("watches", []) or [])


HEADER = ("# مراقبات الرادار — يُحدَّث تلقائياً من نموذج «إضافة أو تعديل مراقبة» في GitHub.\n"
          "# يمكنك تعديله يدوياً أيضاً، وبنفس الصيغة الموجودة في config.yaml.\n")


def _save(path: Path, watches: list[dict]) -> None:
    text = yaml.safe_dump({"watches": watches}, allow_unicode=True, sort_keys=False, width=120)
    path.write_text(HEADER + text, encoding="utf-8")


def describe(w: dict, today: dt.date) -> str:
    pw = parse_watch(w, today=today)
    org = "، ".join(N.city(c) for c in pw.origins)
    dst = "أي وجهة" if pw.is_any else "، ".join(N.city(c) for c in pw.destinations)
    start = N.fmt_date(pw.date_from, False) if pw.date_from else "اليوم"
    end = N.fmt_date(pw.date_to, False) if pw.date_to else "، ".join(N.fmt_month(m) for m in pw.months)
    trip = "ذهاب وعودة" + (f" ({pw.stay_days[0]}–{pw.stay_days[1]} أيام)" if pw.stay_days else "") if pw.round_trip else "ذهاب فقط"
    parts = [f"**{org} ← {dst}**", f"من {start} حتى {end}", trip, f"{pw.adults} {'شخص' if pw.adults == 1 else 'أشخاص'}"]
    if pw.max_price:
        parts.append(f"حدّك {round(pw.max_price):,} للشخص")
    if pw.direct_only:
        parts.append("مباشر فقط")
    return " · ".join(parts)


def apply_form(body: str, root: Path, today: dt.date | None = None) -> tuple[bool, str]:
    """يطبّق النموذج على watches.yaml. يعيد (تغيّر شيء؟, رسالة للرد بالعربية)."""
    today = today or dt.date.today()
    wpath = root / "watches.yaml"
    cpath = root / "config.yaml"
    N.load_extra_cities(root / "data" / "cities.json")
    extra_en = {}
    try:
        import json
        p = root / "data" / "cities_en.json"
        if p.exists():
            extra_en = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    fields = parse_form(body)
    watches = _load(wpath)

    try:
        cfg_names = {w.name for w in load_config(cpath, today=today, watches_path=root / "__none__.yaml").watches}
    except ConfigError:
        cfg_names = set()

    if REMOVE_LABEL in fields:
        name = fields[REMOVE_LABEL].strip()
        if not name:
            return False, "❌ اكتب اسم المراقبة التي تريد حذفها."
        if name in cfg_names:
            return False, f"❌ المراقبة «{name}» معرّفة داخل config.yaml وليس في النموذج — احذفها من هناك."
        kept = [w for w in watches if str(w.get("name", "")).strip() != name]
        if len(kept) == len(watches):
            existing = "، ".join(f"«{w.get('name')}»" for w in watches) or "لا توجد مراقبات"
            return False, f"❌ لا توجد مراقبة باسم «{name}».\n\nالمراقبات الحالية: {existing}"
        _save(wpath, kept)
        return True, f"🗑️ تم حذف المراقبة «{name}»."

    try:
        new = form_to_watch(fields, extra_en)
        if new["name"] in cfg_names:
            raise FormError(f"الاسم «{new['name']}» مستخدم لمراقبة داخل config.yaml — اختر اسماً آخر")
        parse_watch(new, today=today)          # تحقق قبل الحفظ
    except (FormError, ConfigError) as e:
        return False, f"❌ لم أحفظ المراقبة: {e}\n\nعدّل النموذج وأرسله من جديد."

    idx = next((i for i, w in enumerate(watches) if str(w.get("name", "")).strip() == new["name"]), None)
    verb = "تعديل" if idx is not None else "إضافة"
    if idx is None:
        watches.append(new)
    else:
        watches[idx] = new
    before = wpath.read_text(encoding="utf-8") if wpath.exists() else None
    _save(wpath, watches)
    try:
        load_config(cpath, today=today)        # تحقق نهائي من الإعدادات كاملة
    except ConfigError as e:
        if before is None:
            wpath.unlink()
        else:
            wpath.write_text(before, encoding="utf-8")
        return False, f"❌ لم أحفظ المراقبة: {e}"
    return True, f"✅ تم {verb} المراقبة «{new['name']}»\n\n{describe(new, today)}\n\nيعمل الرادار الآن، وتصلك النتيجة على تيليجرام خلال دقائق."
