"""
TS Strategy Bot V2 - XAUUSD
منطقة الساعة (1H) + سحب قمة/قاع شمعة 30د السابقة + CISD أو MSS + أهداف TP1/TP2
كل الأوقات بتوقيت نيويورك.
"""
import os, time, json
from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

NY = ZoneInfo("America/New_York")
SYMBOL = "XAU/USD"
GOLD_TIMES = [(2, 0), (2, 30), (7, 0), (7, 30), (9, 0), (9, 30)]
ACTIVE_MIN = 60        # مدة المراقبة بعد بداية الشمعة الذهبية
SWEEP_MIN = 30         # السحب يجب أن يحدث داخل نصف ساعة الشمعة الذهبية نفسها
SL_BUFFER = 0.30
MIN_RR = 1.5
TP_GAP = 1.0           # أقل مسافة (دولار) بين TP1 و TP2
POLL_SECONDS = 60
SEEN_FILE = "seen.json"

TD_KEY = os.getenv("TWELVEDATA_KEY", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT", "")
AI_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ===================== البيانات =====================
def fetch_1m():
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params=dict(symbol=SYMBOL, interval="1min", outputsize=5000,
                    timezone="America/New_York", apikey=TD_KEY),
        timeout=20,
    )
    j = r.json()
    if "values" not in j:
        raise RuntimeError(j)
    df = pd.DataFrame(j["values"])
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(
        NY, ambiguous="NaT", nonexistent="NaT")
    df = df.dropna(subset=["datetime"]).set_index("datetime").sort_index()
    return df[["open", "high", "low", "close"]].astype(float)


def resample(df1, rule):
    r = df1.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    end = r.index + pd.Timedelta(rule)
    last = df1.index[-1] + pd.Timedelta("1min")
    return r[end <= last]


def mirror(df):
    """قلب السعر: يحوّل الشراء إلى بيع حتى نكتب المنطق مرة واحدة"""
    return pd.DataFrame({"open": -df.open, "high": -df.low,
                         "low": -df.high, "close": -df.close}, index=df.index)


# ===================== أدوات هيكلية =====================
def swing_points(df, n=2):
    hi, lo = [], []
    for i in range(n, len(df) - n):
        w = df.iloc[i - n:i + n + 1]
        if df.high.iloc[i] == w.high.max():
            hi.append(df.high.iloc[i])
        if df.low.iloc[i] == w.low.min():
            lo.append(df.low.iloc[i])
    return hi, lo


def poi_zones(df, look=72):
    """مناطق الاهتمام على فريم الساعة: FVG, OB, IFVG"""
    df = df.tail(look)
    zones = []
    for i in range(2, len(df)):
        a, c = df.iloc[i - 2], df.iloc[i]
        if c.low > a.high:
            lo, hi, dr = a.high, c.low, "bull"
        elif c.high < a.low:
            lo, hi, dr = c.high, a.low, "bear"
        else:
            continue
        later = df.iloc[i + 1:]
        inverted = (later.close < lo).any() if dr == "bull" else (later.close > hi).any()
        if inverted:
            zones.append(dict(kind="IFVG", dir="bear" if dr == "bull" else "bull", lo=lo, hi=hi))
            continue
        zones.append(dict(kind="FVG", dir=dr, lo=lo, hi=hi))
        for k in range(i - 2, max(i - 7, -1), -1):
            o = df.iloc[k]
            if (dr == "bull" and o.close < o.open) or (dr == "bear" and o.close > o.open):
                zones.append(dict(kind="OB", dir=dr, lo=o.low, hi=o.high))
                break
    return zones


def active_starts(now):
    out = []
    for h, m in GOLD_TIMES:
        st = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if st <= now < st + timedelta(minutes=ACTIVE_MIN):
            out.append(st)
    return out


# ===================== منطق البيع (الشراء = نفسه على السعر المقلوب) =====================
def pick_targets(entry, risk, lows):
    """TP1 = أول قاع على اليسار يعطي MIN_RR ، TP2 = القاع الذي بعده"""
    cand = []
    for v in sorted({round(v, 2) for v in lows if v < entry}, reverse=True):
        if not cand or cand[-1] - v >= TP_GAP:   # دمج القيعان المتقاربة
            cand.append(v)
    for n, v in enumerate(cand):
        if entry - v >= MIN_RR * risk:
            return v, (cand[n + 1] if n + 1 < len(cand) else None)
    return None, None


def finish(df3, j, e, entry, ext, lows, kind):
    sl = ext + SL_BUFFER
    risk = sl - entry
    if risk <= 0:
        return None
    tp1, tp2 = pick_targets(entry, risk, lows)
    if tp1 is None:
        return None
    after = df3.iloc[j + 1:]
    filled = False
    if len(after):
        if (after.high >= sl).any() or (after.low <= tp1).any():
            return None
        filled = bool((after.high >= entry).any())
    return dict(kind=kind, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                rr1=round((entry - tp1) / risk, 2),
                sweep_time=str(df3.index[e]), confirm_time=str(df3.index[j]),
                status="تم الدخول" if filled else "أمر معلّق (Limit)")


def cisd_signal(df3, b, lows):
    """CISD: بداية آخر موجة صاعدة قبل قمة السحب. إغلاق تحتها ثم الدخول عند اختبارها"""
    ext, e = df3.high.iloc[b], b
    bull = lambda i: df3.close.iloc[i] > df3.open.iloc[i]
    for k in range(b + 1, len(df3)):
        c = df3.iloc[k]
        if c.high > ext:
            ext, e = c.high, k
            continue
        end = e
        while end >= 0 and not bull(end):
            end -= 1
        if end < 0:
            continue
        r = end
        while r - 1 >= 0 and bull(r - 1):
            r -= 1
        cisd = df3.open.iloc[r]
        if cisd < ext and c.close < cisd:
            return finish(df3, k, e, cisd, ext, lows, "CISD")
    return None


def mss_signal(df3, b, lows):
    """MSS: كسر آخر قاع هيكلي، ثم الدخول عند FVG أو OB على 3 دقائق"""
    ext, e = df3.high.iloc[b], b
    for k in range(b + 1, len(df3)):
        c = df3.iloc[k]
        if c.high > ext:
            ext, e = c.high, k
            continue
        _, sl_pts = swing_points(df3.iloc[max(0, e - 40):e])
        if not sl_pts or c.close >= sl_pts[-1]:
            continue
        seg = df3.iloc[e:k + 1]
        zone = None
        for i in range(2, len(seg)):
            a, cc = seg.iloc[i - 2], seg.iloc[i]
            if cc.high < a.low:
                zone = ("FVG", cc.high, a.low)
        if zone is None:
            for i in range(len(seg) - 2, -1, -1):
                o = seg.iloc[i]
                if o.close > o.open:
                    zone = ("OB", o.low, o.high)
                    break
        if zone is None:
            return None
        return finish(df3, k, e, (zone[1] + zone[2]) / 2, ext, lows, "MSS+" + zone[0])
    return None


def second_half_ok(df30, start):
    """شمعة :30 تُعتمد فقط إذا لم تسحب شمعة :00 قمة أو قاع الشمعة التي قبلها"""
    if start.minute != 30:
        return True
    a = df30[df30.index == start - pd.Timedelta(minutes=30)]
    b = df30[df30.index == start - pd.Timedelta(minutes=60)]
    if not len(a) or not len(b):
        return False
    return bool(a.high.iloc[0] <= b.high.iloc[0] and a.low.iloc[0] >= b.low.iloc[0])


def find_breach(df3, level, start):
    """أول شمعة 3د تتجاوز المستوى داخل نصف ساعة الشمعة الذهبية"""
    for i in range(1, len(df3)):
        t = df3.index[i]
        if start <= t < start + timedelta(minutes=SWEEP_MIN):
            if df3.high.iloc[i] > level and df3.close.iloc[i - 1] <= level:
                return i
    return None


def scan_sell(df3, df30, df1h, start):
    if not second_half_ok(df30, start):
        return []
    p = df30[df30.index < start].tail(1)
    if not len(p):
        return []
    level = p.high.iloc[0]                      # قمة شمعة 30د السابقة

    # 1) مناطق الساعة على اليسار (قبل الوقت الذهبي)، وليست كلها تحت المستوى
    h1 = df1h[df1h.index + pd.Timedelta("60min") <= start]
    zrel = [z for z in poi_zones(h1) if z["hi"] >= level]
    if not zrel:
        return []

    # 2) السحب داخل نصف ساعة الشمعة الذهبية
    b = find_breach(df3, level, start)
    if b is None:
        return []

    _, l30 = swing_points(df30[df30.index < start].tail(80))
    _, l3 = swing_points(df3.iloc[:b].tail(200))
    lows = l30 + l3

    out = []
    for fn in (cisd_signal, mss_signal):
        r = fn(df3, b, lows)
        if not r:
            continue
        ext = r["sl"] - SL_BUFFER               # أعلى نقطة وصلها السحب
        # 3) المنطقة تحتوي المستوى، أو لمسها السعر أثناء السحب
        zt = [z for z in zrel if z["lo"] <= ext]
        if not zt:
            continue
        zt.sort(key=lambda z: abs(z["lo"] - level))
        r["poi"] = zt[0]["kind"] + " 1H"
        r["level"] = level
        r["golden"] = start.strftime("%H:%M")
        out.append(r)
    return out


def unmirror(r):
    r = dict(r)
    for k in ("entry", "sl", "tp1", "tp2", "level"):
        if r.get(k) is not None:
            r[k] = -r[k]
    return r


def scan(df1, now):
    starts = active_starts(now)
    if not starts:
        return []
    d3, d30, d1h = resample(df1, "3min"), resample(df1, "30min"), resample(df1, "60min")
    m3, m30, m1h = mirror(d3), mirror(d30), mirror(d1h)
    out = []
    for st in starts:
        for r in scan_sell(d3, d30, d1h, st):
            r["dir"] = "sell"
            out.append(r)
        for r in scan_sell(m3, m30, m1h, st):
            r = unmirror(r)
            r["dir"] = "buy"
            out.append(r)
    seen_k, uniq = set(), []
    for r in out:                               # حذف المكرر بين شمعتين ذهبيتين
        k = (r["dir"], r["kind"], r["confirm_time"])
        if k not in seen_k:
            seen_k.add(k)
            uniq.append(r)
    return uniq


# ===================== التنبيهات =====================
def telegram(text):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  data=dict(chat_id=TG_CHAT, text=text), timeout=15)


def ai_review(setup):
    if not AI_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=dict(model="claude-sonnet-5-5", max_tokens=300, messages=[{
                "role": "user",
                "content": "راجع هذا الـ setup لاستراتيجية TS على الذهب باختصار (3 أسطر): "
                           "القوة والخطر وتقييم من 10.\n" + json.dumps(setup, ensure_ascii=False)}]),
            timeout=30)
        return "\n\n🤖 مراجعة Claude:\n" + r.json()["content"][0]["text"]
    except Exception:
        return ""


def fmt(s):
    arrow = "🔴 SELL" if s["dir"] == "sell" else "🟢 BUY"
    tp2 = f"{s['tp2']:.2f}" if s["tp2"] is not None else "—"
    return (f"{arrow} XAUUSD\n"
            f"⏰ الشمعة الذهبية: {s['golden']} (NY)\n"
            f"📍 المنطقة: {s['poi']}\n"
            f"🎯 تأكيد: {s['kind']}\n"
            f"➡️ الدخول: {s['entry']:.2f}\n"
            f"🛑 الوقف: {s['sl']:.2f}\n"
            f"✅ TP1: {s['tp1']:.2f} (RR {s['rr1']})\n"
            f"✅ TP2: {tp2}\n"
            f"📌 الحالة: {s['status']}\n"
            f"Sweep: {s['sweep_time'][11:16]} | تأكيد: {s['confirm_time'][11:16]}")


def load_seen():
    try:
        return set(json.load(open(SEEN_FILE)))
    except Exception:
        return set()


def upcoming(now, mins=35):
    for h, m in GOLD_TIMES:
        st = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < st <= now + timedelta(minutes=mins):
            return True
    return False


def main():
    seen = load_seen()
    one_shot = bool(os.getenv("ONE_SHOT"))
    print("TS bot V2 started")
    while True:
        try:
            now = pd.Timestamp.now(tz=NY)
            busy = bool(active_starts(now))
            if one_shot and not busy and not upcoming(now):
                print("no window now, exiting")
                return
            if busy:
                df1 = fetch_1m()
                for s in scan(df1, now):
                    key = f"{s['dir']}|{s['kind']}|{s['confirm_time']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    json.dump(list(seen), open(SEEN_FILE, "w"))
                    telegram(fmt(s) + ai_review(s))
        except Exception as e:
            print("error:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

