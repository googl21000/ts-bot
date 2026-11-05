"""
News Bot - أخبار مهمة للذهب XAUUSD و EURUSD و GBPUSD  ->  تيليجرام

1) ملخص صباحي بأخبار اليوم عالية التأثير (من تقويم اقتصادي مجاني)
2) تنبيه قبل كل خبر مهم بـ 30 دقيقة (قابل للتعديل)
3) عناوين أخبار عاجلة مصفّاة (Google News RSS)، مع ترجمة عربية اختيارية عبر Claude

المتغيرات (Secrets / Environment):
  TG_TOKEN, TG_CHAT                 (إجباري)
  ANTHROPIC_API_KEY                 (اختياري: ترجمة العناوين للعربية)
  LOCAL_TZ        توقيتك (الافتراضي Africa/Tunis = غرينتش+1)
  NEWS_CURRENCIES الافتراضي USD,EUR,GBP
  MIN_IMPACT      High (افتراضي) أو Medium
  ALERT_BEFORE_MIN الافتراضي 30
  DIGEST_HOUR_NY  ساعة الملخص بتوقيت نيويورك (افتراضي 1)
  NEWS_MIN_SCORE  حدّ أهمية العنوان (افتراضي 3)

الاستخدام:
  python news_bot.py            # تشغيل عادي (يُستدعى كل 10 دقائق)
  python news_bot.py --test     # رسالة اختبار
  python news_bot.py --digest   # إرسال ملخص اليوم الآن
  python news_bot.py --dry      # طباعة بدل الإرسال (بدون حفظ حالة)
ليست نصيحة مالية.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests

# ============================ الإعدادات ============================
NY = ZoneInfo("America/New_York")
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ") or "Africa/Tunis")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT", "")
AI_KEY = os.getenv("ANTHROPIC_API_KEY", "")

CURRENCIES = {c.strip().upper() for c in (os.getenv("NEWS_CURRENCIES") or "USD,EUR,GBP").split(",") if c.strip()}
MIN_IMPACT = (os.getenv("MIN_IMPACT") or "High").strip().capitalize()
ALERT_BEFORE_MIN = int(os.getenv("ALERT_BEFORE_MIN") or 30)
DIGEST_HOUR_NY = int(os.getenv("DIGEST_HOUR_NY") or 1)
MIN_SCORE = int(os.getenv("NEWS_MIN_SCORE") or 3)

CAL_REFRESH_MIN = 360        # التقويم يُحدَّث كل 6 ساعات (المصدر يحدّ الطلبات)
HEADLINE_EVERY_MIN = 20      # جلب العناوين كل 20 دقيقة
MAX_HEADLINES = 4            # أقصى عدد عناوين في المرة الواحدة
MAX_AGE_MIN = 180            # تجاهل العناوين الأقدم من 3 ساعات
STATE_FILE = "news_state.json"

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
CAL_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
GN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
QUERIES = [
    "gold OR XAUUSD OR bullion when:6h",
    "Federal Reserve OR FOMC OR Powell OR Treasury yields when:6h",
    "ECB OR Lagarde OR eurozone inflation when:6h",
    "Bank of England OR BoE OR UK inflation when:6h",
    "US CPI OR payrolls OR jobs report OR PCE when:6h",
]
EXTRA_FEEDS = ["https://www.fxstreet.com/rss/news"]

IMPACT_RANK = {"Low": 1, "Medium": 2, "High": 3}
IMPACT_ICON = {"High": "🔴", "Medium": "🟠", "Low": "🟡"}

STRONG = [r"\bfomc\b", r"\bpowell\b", r"\bnon-?farm\b", r"\bpayrolls?\b", r"\bcpi\b", r"\bpce\b",
          r"\becb\b", r"\blagarde\b", r"\bbank of england\b", r"\bboe\b", r"\bbailey\b",
          r"\brate (decision|cut|hike)s?\b", r"\binterest rates?\b", r"\bgdp\b", r"\bppi\b",
          r"\bgold\b", r"\bxau", r"\bbullion\b", r"\bsafe[- ]haven\b"]
WEAK = [r"\bfed\b", r"\binflation\b", r"\bjobs?\b", r"\bdollar\b", r"\bdxy\b", r"\byields?\b",
        r"\btreasur", r"\btariffs?\b", r"\bsanctions?\b", r"\bwar\b", r"\bceasefire\b", r"\boil\b",
        r"\bchina\b", r"\brecession\b", r"\bshutdown\b", r"\beuro\b", r"\bsterling\b", r"\bpound\b",
        r"\beurozone\b", r"\buk\b", r"\bbritish\b"]
NEG = [r"olympic", r"medal", r"jewel", r"wedding", r"lottery", r"fantasy", r"horoscope",
       r"gold coast", r"golden", r"stocks? to buy", r"\bgoldman\b"]


# ============================== التقويم ==============================
def fetch_calendar():
    events = []
    for url in CAL_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            if r.status_code != 200:
                print(f"calendar {url.rsplit('/', 1)[-1]}: HTTP {r.status_code}", flush=True)
                continue
            for e in r.json():
                try:
                    t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
                except Exception:
                    continue
                events.append(dict(title=str(e.get("title", "")).strip(),
                                   cur=str(e.get("country", "")).strip().upper(),
                                   impact=str(e.get("impact", "")).strip().capitalize(),
                                   t=t.isoformat(),
                                   forecast=str(e.get("forecast") or "").strip(),
                                   previous=str(e.get("previous") or "").strip()))
        except Exception as ex:
            print("calendar error:", ex, flush=True)
    print(f"calendar: {len(events)} events", flush=True)
    return events


def relevant(e):
    if e["cur"] not in CURRENCIES:
        return False
    if e["impact"] == "Holiday":
        return True
    return IMPACT_RANK.get(e["impact"], 0) >= IMPACT_RANK.get(MIN_IMPACT, 3)


def pairs_for(cur):
    if cur == "USD":
        return "XAUUSD · EURUSD · GBPUSD"
    if cur == "EUR":
        return "EURUSD"
    if cur == "GBP":
        return "GBPUSD"
    return "XAUUSD"


def t_str(t):
    return f"{t.astimezone(NY):%H:%M} نيويورك | {t.astimezone(LOCAL_TZ):%H:%M} بتوقيتك"


def event_line(e):
    t = datetime.fromisoformat(e["t"])
    icon = "🏖" if e["impact"] == "Holiday" else IMPACT_ICON.get(e["impact"], "⚪")
    extra = []
    if e["forecast"]:
        extra.append(f"توقع {e['forecast']}")
    if e["previous"]:
        extra.append(f"سابق {e['previous']}")
    tail = (" | " + " | ".join(extra)) if extra else ""
    return f"{icon} {t_str(t)}\n   {e['cur']} - {e['title']}{tail}"


def build_digest(events, now):
    end = now + timedelta(hours=24)
    sel = sorted((e for e in events if relevant(e) and now <= datetime.fromisoformat(e["t"]) < end),
                 key=lambda e: e["t"])
    head = f"📅 أخبار الـ 24 ساعة القادمة ({', '.join(sorted(CURRENCIES))})"
    if not sel:
        return head + "\nلا أخبار مهمة مجدولة. (يبقى احتمال أخبار عاجلة غير مجدولة)"
    body = "\n".join(event_line(e) for e in sel)
    return (f"{head}\n\n{body}\n\n"
            f"سيصلك تنبيه قبل كل خبر بـ {ALERT_BEFORE_MIN} دقيقة. وقت الخبر يتسع السبريد وتكثر الحركات الحادة.")


def build_prealert(e, mins):
    t = datetime.fromisoformat(e["t"])
    icon = IMPACT_ICON.get(e["impact"], "⚠️")
    lines = [f"{icon} خبر مهم بعد ~{mins} دقيقة", f"{e['cur']} - {e['title']}", t_str(t)]
    if e["forecast"] or e["previous"]:
        lines.append(f"توقع: {e['forecast'] or '-'} | سابق: {e['previous'] or '-'}")
    lines.append(f"يؤثر على: {pairs_for(e['cur'])}")
    return "\n".join(lines)


# ============================== العناوين ==============================
def score(title):
    t = title.lower()
    if any(re.search(p, t) for p in NEG):
        return -5
    return 2 * sum(bool(re.search(p, t)) for p in STRONG) + sum(bool(re.search(p, t)) for p in WEAK)


def tags(title):
    t = title.lower()
    out = []
    if re.search(r"\bgold\b|\bxau|\bbullion\b", t):
        out.append("XAUUSD")
    if re.search(r"\bfed\b|\bfomc\b|\bpowell\b|\bdollar\b|\bdxy\b|\bcpi\b|\bpce\b|\bpayrolls?\b|\bnon-?farm\b|\btreasur|\byields?\b", t):
        out += [x for x in ("XAUUSD", "EURUSD", "GBPUSD") if x not in out]
    if re.search(r"\becb\b|\blagarde\b|\beuro(zone)?\b", t) and "EURUSD" not in out:
        out.append("EURUSD")
    if re.search(r"\bboe\b|\bbank of england\b|\bbailey\b|\bsterling\b|\bpound\b|\buk\b|\bbritish\b", t) and "GBPUSD" not in out:
        out.append("GBPUSD")
    return out or ["عام"]


def norm(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:70]


def parse_rss(content, default_source=""):
    items = []
    root = ET.fromstring(content)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or default_source).strip()
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            continue
        if " - " in title:                      # Google News يضيف اسم المصدر في آخر العنوان
            title, _, tail = title.rpartition(" - ")
            src = src or tail
        if title:
            items.append(dict(title=title.strip(), link=link, source=src, t=dt))
    return items


def fetch_headlines():
    urls = [GN.format(q=quote_plus(q)) for q in QUERIES] + EXTRA_FEEDS
    items = []
    for u in urls:
        try:
            r = requests.get(u, headers=UA, timeout=25)
            if r.status_code != 200:
                print(f"feed HTTP {r.status_code}: {u[:70]}", flush=True)
                continue
            items += parse_rss(r.content, "FXStreet" if "fxstreet" in u else "")
        except Exception as ex:
            print("feed error:", str(ex)[:120], flush=True)
    print(f"headlines fetched: {len(items)}", flush=True)
    return items


def pick_headlines(items, now, seen):
    out, used = [], set()
    for it in sorted(items, key=lambda x: x["t"], reverse=True):
        age = (now - it["t"]).total_seconds() / 60
        if age < -5 or age > MAX_AGE_MIN:
            continue
        s = score(it["title"])
        if s < MIN_SCORE:
            continue
        key = "h|" + hashlib.md5(norm(it["title"]).encode()).hexdigest()[:12]
        if key in seen or key in used:
            continue
        used.add(key)
        out.append(dict(it, score=s, key=key, age=int(max(age, 0))))
    out.sort(key=lambda x: (-x["score"], x["age"]))
    return out[:MAX_HEADLINES]


def translate_ar(titles):
    if not AI_KEY or not titles:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=dict(model="claude-sonnet-5-5", max_tokens=700, messages=[{
                "role": "user",
                "content": "ترجم العناوين الإخبارية التالية إلى العربية بإيجاز ودقة، عنوان واحد في كل سطر "
                           "وبنفس الترتيب، بدون ترقيم أو أي إضافات:\n" + "\n".join(titles)}]),
            timeout=40)
        lines = [l.strip() for l in r.json()["content"][0]["text"].strip().split("\n") if l.strip()]
        return lines if len(lines) == len(titles) else None
    except Exception as ex:
        print("translate error:", str(ex)[:120], flush=True)
        return None


def build_headline(h, ar):
    lines = [f"📰 {' · '.join(tags(h['title']))}"]
    if ar:
        lines += [ar, f"EN: {h['title']}"]
    else:
        lines.append(h["title"])
    lines.append(f"{h['source'] or 'مصدر'} | قبل {h['age']} دقيقة")
    if h["link"]:
        lines.append(h["link"])
    return "\n".join(lines)


# ============================== الحالة ==============================
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"sent": {}, "cal": None, "last_headlines": None, "digest_date": None}


def save_state(st, now):
    cut = (now - timedelta(days=3)).isoformat()
    st["sent"] = {k: v for k, v in st.get("sent", {}).items() if v >= cut}
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=1, sort_keys=True)


def minutes_since(iso, now):
    return (now - datetime.fromisoformat(iso)).total_seconds() / 60 if iso else 1e9


# ============================== التشغيل ==============================
def run(now, st, send, force_digest=False):
    sent = 0
    # 1) التقويم (مخزّن مؤقتاً)
    cal = st.get("cal")
    if not cal or minutes_since(cal["at"], now) > CAL_REFRESH_MIN:
        ev = fetch_calendar()
        if ev:
            st["cal"] = cal = {"at": now.isoformat(), "events": ev}
    events = cal["events"] if cal else []

    # 2) ملخص اليوم
    ny = now.astimezone(NY)
    today = ny.date().isoformat()
    due = ny.weekday() < 5 and DIGEST_HOUR_NY <= ny.hour < DIGEST_HOUR_NY + 8 and st.get("digest_date") != today
    if force_digest or due:
        send(build_digest(events, now))
        st["digest_date"] = today
        sent += 1

    # 3) تنبيه قبل الأخبار
    for e in sorted(events, key=lambda x: x["t"]):
        if not relevant(e) or e["impact"] == "Holiday":
            continue
        mins = (datetime.fromisoformat(e["t"]) - now).total_seconds() / 60
        if 0 < mins <= ALERT_BEFORE_MIN:
            key = f"pre|{e['cur']}|{e['title']}|{e['t']}"
            if key not in st["sent"]:
                send(build_prealert(e, max(1, round(mins))))
                st["sent"][key] = now.isoformat()
                sent += 1

    # 4) العناوين العاجلة
    if minutes_since(st.get("last_headlines"), now) >= HEADLINE_EVERY_MIN:
        st["last_headlines"] = now.isoformat()
        picks = pick_headlines(fetch_headlines(), now, st["sent"])
        ar = translate_ar([p["title"] for p in picks]) if picks else None
        for i, p in enumerate(picks):
            send(build_headline(p, ar[i] if ar else None))
            st["sent"][p["key"]] = now.isoformat()
            sent += 1
    return sent


def telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[no telegram config]\n" + text, flush=True)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data=dict(chat_id=TG_CHAT, text=text[:4000], disable_web_page_preview="true"),
                          timeout=20)
        if r.status_code != 200:
            print("telegram error:", r.status_code, r.text[:200], flush=True)
    except Exception as ex:
        print("telegram exception:", ex, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.test:
        telegram("News Bot: الاتصال بتيليجرام يعمل ✅")
        return
    now = datetime.now(timezone.utc)
    st = load_state()
    send = (lambda t: print("[DRY]\n" + t + "\n", flush=True)) if a.dry else telegram
    n = run(now, st, send, force_digest=a.digest)
    if not a.dry:
        save_state(st, now)
    print(f"done: sent={n}", flush=True)


if __name__ == "__main__":
    main()
