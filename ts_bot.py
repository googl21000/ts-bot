"""
TS Strategy Bot - XAUUSD
الوقت + السحب (Sweep) + MSS + دخول OB/FVG  ->  تنبيه تيليجرام
كل الأوقات بتوقيت نيويورك.
"""
import os, time, json
from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ===================== الإعدادات =====================
NY = ZoneInfo("America/New_York")
SYMBOL = "XAU/USD"
GOLD_TIMES = [(2, 0), (2, 30), (7, 0), (7, 30), (9, 0), (9, 30)]
ACTIVE_MIN = 60
SL_BUFFER = 0.30
MIN_RR = 1.5
POLL_SECONDS = 60
SEEN_FILE = "seen.json"

TD_KEY = os.getenv("TWELVEDATA_KEY", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT", "")
AI_KEY = os.getenv("ANTHROPIC_API_KEY", "")


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


def swing_points(df, n=2):
    hi, lo = [], []
    for i in range(n, len(df) - n):
        w = df.iloc[i - n:i + n + 1]
        if df.high.iloc[i] == w.high.max():
            hi.append(df.high.iloc[i])
        if df.low.iloc[i] == w.low.min():
            lo.append(df.low.iloc[i])
    return hi, lo


def poi_zones(df30, look=60):
    df = df30.tail(look)
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


def confirm(df3, s, d, liq_lo, liq_hi):
    sweep = df3.iloc[s]
    hist = df3.iloc[max(0, s - 40):s]
    sh, sl = swing_points(hist)
    sell = d == "sell"
    pts = sl if sell else sh
    if not pts:
        return None
    mss = pts[-1]

    j = None
    for k in range(s + 1, len(df3)):
        c = df3.iloc[k]
        if sell:
            if c.high > sweep.high:
                return None
            if c.close < mss:
                j = k
                break
        else:
            if c.low < sweep.low:
                return None
            if c.close > mss:
                j = k
                break
    if j is None:
        return None

    seg = df3.iloc[s:j + 1]
    zone = None
    for i in range(2, len(seg)):
        a, c = seg.iloc[i - 2], seg.iloc[i]
        if sell and c.high < a.low:
            zone = ("FVG", c.high, a.low)
        if (not sell) and c.low > a.high:
            zone = ("FVG", a.high, c.low)
    if zone is None:
        for i in range(len(seg) - 2, -1, -1):
            o = seg.iloc[i]
            if (sell and o.close > o.open) or ((not sell) and o.close < o.open):
                zone = ("OB", o.low, o.high)
                break
    if zone is None:
        return None

    kind, lo, hi = zone
    entry = (lo + hi) / 2
    if sell:
        sl_price = sweep.high + SL_BUFFER
        risk = sl_price - entry
        cands = [v for v in liq_lo if v < entry - risk * MIN_RR]
        tp = max(cands) if cands else None
    else:
        sl_price = sweep.low - SL_BUFFER
        risk = entry - sl_price
        cands = [v for v in liq_hi if v > entry + risk * MIN_RR]
        tp = min(cands) if cands else None
    if risk <= 0 or tp is None:
        return None

    after = df3.iloc[j + 1:]
    if len(after):
        if sell and ((after.low <= tp).any() or (after.high >= sl_price).any()):
            return None
        if (not sell) and ((after.high >= tp).any() or (after.low <= sl_price).any()):
            return None
        filled = (after.high >= entry).any() if sell else (after.low <= entry).any()
    else:
        filled = False

    return dict(dir=d, entry_zone=kind, entry=round(entry, 2), sl=round(sl_price, 2),
                tp=round(tp, 2), rr=round(abs(tp - entry) / risk, 2),
                sweep_time=str(df3.index[s]), mss_time=str(df3.index[j]),
                status="تم الدخول" if filled else "أمر معلّق (Limit)")


def find_setups(df3, df30, zones, start):
    res = []
    p30 = df30[df30.index < start].tail(1)
    p_hi = [p30.high.iloc[0]] if len(p30) else []
    p_lo = [p30.low.iloc[0]] if len(p30) else []

    h30, l30 = swing_points(df30.tail(80))
    h3, l3 = swing_points(df3.tail(200))
    liq_hi, liq_lo = h30 + h3 + p_hi, l30 + l3 + p_lo

    for s in range(len(df3)):
        t = df3.index[s]
        if not (start <= t < start + timedelta(minutes=ACTIVE_MIN)):
            continue
        c = df3.iloc[s]
        sh, sl = swing_points(df3.iloc[max(0, s - 40):s])
        for d in ("sell", "buy"):
            if d == "sell":
                lv = [v for v in sh + p_hi if c.high > v > c.close]
                zs = [z for z in zones if z["dir"] == "bear" and c.high >= z["lo"] and c.low <= z["hi"]]
            else:
                lv = [v for v in sl + p_lo if c.low < v < c.close]
                zs = [z for z in zones if z["dir"] == "bull" and c.high >= z["lo"] and c.low <= z["hi"]]
            if lv and zs:
                r = confirm(df3, s, d, liq_lo, liq_hi)
                if r:
                    r["poi"] = zs[0]["kind"] + " 30m"
                    r["golden"] = start.strftime("%H:%M")
                    res.append(r)
    return res


def scan(df1, now):
    starts = active_starts(now)
    if not starts:
        return []
    df3, df30 = resample(df1, "3min"), resample(df1, "30min")
    zones = poi_zones(df30)
    out = []
    for st in starts:
        out += find_setups(df3, df30, zones, st)
    return out


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
                "content": "أنت مراجع لاستراتيجية TS (سحب سيولة + MSS) على الذهب. "
                           "راجع هذا الـ setup باختصار شديد (3 أسطر): نقاط القوة والخطر وتقييم من 10.\n"
                           + json.dumps(setup, ensure_ascii=False)}]),
            timeout=30)
        return "\n\n🤖 مراجعة Claude:\n" + r.json()["content"][0]["text"]
    except Exception:
        return ""


def fmt(s):
    arrow = "🔴 SELL" if s["dir"] == "sell" else "🟢 BUY"
    return (f"{arrow} XAUUSD\n"
            f"⏰ الشمعة الذهبية: {s['golden']} (NY)\n"
            f"📍 المنطقة: {s['poi']}\n"
            f"🎯 الدخول ({s['entry_zone']} 3m): {s['entry']}\n"
            f"🛑 الوقف: {s['sl']}\n"
            f"✅ الهدف: {s['tp']}  (RR {s['rr']})\n"
            f"📌 الحالة: {s['status']}\n"
            f"Sweep: {s['sweep_time'][11:16]} | MSS: {s['mss_time'][11:16]}")


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
    print("TS bot started")
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
                    key = f"{s['dir']}|{s['sweep_time']}"
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
