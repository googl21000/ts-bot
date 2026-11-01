"""
LAB: مقارنة ثلاث استراتيجيات على الذهب بحكم Backtest صارم
A) Trend-Pullback (H1)
B) London Open Range Breakout (15m)
C) Asia Range Sweep & Reclaim (15m)
كل القواعد والمعاملات ثابتة مسبقاً. آخر 6 أشهر محجوزة ولا تُفحص إلا لمرشح واحد.
"""
import os, time
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests

NY = ZoneInfo("America/New_York")
KEY = os.getenv("TWELVEDATA_KEY", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT", "")

YEARS = float(os.getenv("LAB_YEARS") or "3")
COST = float(os.getenv("LAB_COST") or "0.6")
HOLDOUT_DAYS = 183
RR = 2.0
CONF = 1 - 0.05 / 3
MIN_TRADES = 100
MAX_DD = 20.0
ATR_BUF = 0.25

SL_ATR, MAX_HOLD_A, SESSION_A = 0.5, 48, (3, 12)


def download(years):
    end = pd.Timestamp.now(tz=NY).floor("15min")
    start = end - pd.Timedelta(days=int(365 * years))
    frames, cur = [], end
    while cur > start:
        params = dict(symbol="XAU/USD", interval="15min", outputsize=5000,
                      timezone="America/New_York",
                      end_date=cur.strftime("%Y-%m-%d %H:%M:%S"), apikey=KEY)
        j = None
        for _ in range(4):
            j = requests.get("https://api.twelvedata.com/time_series",
                             params=params, timeout=30).json()
            if "values" in j or j.get("code") != 429:
                break
            time.sleep(65)
        if not j or "values" not in j:
            print("توقف التحميل:", j)
            break
        df = pd.DataFrame(j["values"])
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(
            NY, ambiguous="NaT", nonexistent="NaT")
        df = df.dropna(subset=["datetime"]).set_index("datetime").sort_index()
        frames.append(df)
        print("حُمّل حتى", df.index[0])
        if df.index[0] >= cur:
            break
        cur = df.index[0] - pd.Timedelta(minutes=15)
        time.sleep(8)
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated()]
    return out[["open", "high", "low", "close"]].astype(float).loc[start:]


def to_h1(m15):
    h = m15.resample("1h").agg({"open": "first", "high": "max",
                                "low": "min", "close": "last"}).dropna()
    return h[h.index + pd.Timedelta("1h") <= m15.index[-1] + pd.Timedelta("15min")]


def atr_series(h1):
    pc = h1.close.shift(1)
    tr = pd.concat([h1.high - h1.low, (h1.high - pc).abs(), (h1.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()


def atr_for_15m(m15, h1):
    a = atr_series(h1)
    a.index = a.index + pd.Timedelta("1h")
    return a.reindex(m15.index, method="ffill").values


def arrays(df, sign):
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    return (o, h, l, c) if sign == 1 else (-o, -l, -h, -c)


def at(D, h, m, shift=0):
    base = pd.Timestamp(D.year, D.month, D.day, 12, tz=NY) + pd.Timedelta(days=shift)
    return base.replace(hour=h, minute=m)


def simulate(arr, e, entry, sl, tp, risk, last):
    o, h, l, c = arr
    for k in range(e, last + 1):
        if k > e and (o[k] <= sl or o[k] >= tp):
            return k, (o[k] - entry) / risk
        if l[k] <= sl:
            return k, -1.0
        if h[k] >= tp:
            return k, RR
    return last, (c[last] - entry) / risk


def execute(df, sigs, per_day):
    arrs = {1: arrays(df, 1), -1: arrays(df, -1)}
    trades, exit_idx, cnt = [], -1, {}
    for e, sign, sl_s, last, a in sorted(sigs, key=lambda s: s[0]):
        if e <= exit_idx or e > last or e >= len(df) or np.isnan(a):
            continue
        entry = arrs[sign][0][e]
        risk = entry - sl_s
        if risk < 0.5 * a or risk > 3 * a:
            continue
        day = df.index[e].date()
        if cnt.get(day, 0) >= per_day:
            continue
        k, r = simulate(arrs[sign], e, entry, sl_s, entry + RR * risk, risk, last)
        r -= COST / risk
        cnt[day] = cnt.get(day, 0) + 1
        exit_idx = k
        trades.append(dict(time=df.index[e], dir="buy" if sign == 1 else "sell", R=r))
    return pd.DataFrame(trades, columns=["time", "dir", "R"])


def cand_A(h1):
    atr = atr_series(h1).values
    ema = {n: h1.close.ewm(span=n, adjust=False).mean().values for n in (20, 50, 200)}
    hours, n = h1.index.hour.values, len(h1)
    sigs = []
    for sign in (1, -1):
        o, h, l, c = arrays(h1, sign)
        e20, e50, e200 = (ema[20] * sign, ema[50] * sign, ema[200] * sign)
        for i in range(210, n - 1):
            if not (SESSION_A[0] <= hours[i + 1] < SESSION_A[1]) or np.isnan(atr[i]):
                continue
            if e50[i] > e200[i] and c[i] > e200[i] and l[i] <= e20[i] and c[i] > e20[i] and c[i] > o[i]:
                sigs.append((i + 1, sign, l[i] - SL_ATR * atr[i], min(n - 1, i + 1 + MAX_HOLD_A), atr[i]))
    return sigs


def weekdays(df):
    return [d for d in sorted(set(df.index.date)) if d.weekday() < 5]


def cand_B(m15, atr):
    t, n = m15.index, len(m15)
    h, l = m15.high.values, m15.low.values
    sigs = []
    for D in weekdays(m15):
        i0, i1 = t.searchsorted(at(D, 3, 0)), t.searchsorted(at(D, 4, 0))
        if i1 - i0 != 4:
            continue
        rh, rl = h[i0:i1].max(), l[i0:i1].min()
        w0, w1 = i1, t.searchsorted(at(D, 9, 0))
        last = t.searchsorted(at(D, 12, 0)) - 1
        best = None
        for sign in (1, -1):
            _, _, _, c = arrays(m15, sign)
            level = rh if sign == 1 else -rl
            for j in range(w0, min(w1, n - 1)):
                if c[j] > level:
                    cand = (j + 1, sign, rl if sign == 1 else -rh, last, atr[j + 1] if j + 1 < n else np.nan)
                    if best is None or cand[0] < best[0]:
                        best = cand
                    break
        if best:
            sigs.append(best)
    return sigs


def cand_C(m15, atr):
    t, n = m15.index, len(m15)
    h, l = m15.high.values, m15.low.values
    sigs = []
    for D in weekdays(m15):
        i0, i1 = t.searchsorted(at(D, 20, 0, -1)), t.searchsorted(at(D, 2, 0))
        if i1 - i0 < 20:
            continue
        ah, al = h[i0:i1].max(), l[i0:i1].min()
        w0, w1 = i1, t.searchsorted(at(D, 8, 0))
        i9 = t.searchsorted(at(D, 9, 0))
        last = t.searchsorted(at(D, 12, 0)) - 1
        best = None
        for sign in (1, -1):
            _, _, L, C = arrays(m15, sign)
            level = al if sign == 1 else -ah
            j = w0
            while j < w1:
                if L[j] < level:
                    lowest, m = L[j], None
                    for q in range(j, min(j + 4, n)):
                        lowest = min(lowest, L[q])
                        if C[q] > level:
                            m = q
                            break
                    if m is not None and m + 1 < min(i9, n):
                        e = m + 1
                        cand = (e, sign, lowest - ATR_BUF * atr[e], last, atr[e])
                        if best is None or cand[0] < best[0]:
                            best = cand
                        break
                    j += 4
                else:
                    j += 1
        if best:
            sigs.append(best)
    return sigs


def stats(R, label=""):
    R = np.asarray(R, dtype=float)
    n = len(R)
    if n == 0:
        return dict(text=f"{label}: لا صفقات", n=0, mean=0.0, lo=0.0, hi=0.0, pf=0.0, dd=0.0)
    gp, gl = R[R > 0].sum(), -R[R < 0].sum()
    pf = gp / gl if gl > 0 else float("inf")
    cum = np.cumsum(R)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    boots = np.random.default_rng(0).choice(R, (4000, n)).mean(axis=1)
    a = (1 - CONF) / 2 * 100
    lo, hi = np.percentile(boots, [a, 100 - a])
    text = (f"{label}{n} صفقة | نجاح {100*(R>0).mean():.0f}% | متوسط {R.mean():+.2f}R "
            f"[{lo:+.2f}، {hi:+.2f}] | PF {pf:.2f} | {R.sum():+.1f}R | تراجع {dd:.1f}R")
    return dict(text=text, n=n, mean=float(R.mean()), lo=float(lo), hi=float(hi), pf=pf, dd=dd)


def analyze(m15):
    h1 = to_h1(m15)
    atr15 = atr_for_15m(m15, h1)
    runs = {
        "A Trend-Pullback H1": execute(h1, cand_A(h1), 2),
        "B London Breakout": execute(m15, cand_B(m15, atr15), 1),
        "C Asia Sweep": execute(m15, cand_C(m15, atr15), 1),
    }
    cut = m15.index[-1] - pd.Timedelta(days=HOLDOUT_DAYS)
    L = [f"🔬 LAB XAUUSD | {m15.index[0].date()} → {m15.index[-1].date()} | تكلفة {COST}$",
         f"تصميم حتى {cut.date()} | محجوز بعدها | حدّ الثقة {CONF*100:.1f}% | RR {RR}", ""]
    passers = {}
    for name, tr in runs.items():
        d = tr[tr.time < cut] if len(tr) else tr
        s = stats(d.R.values, "تصميم: ")
        L += [f"■ {name}", s["text"]]
        if len(d):
            for y, g in d.groupby(d.time.dt.year):
                L.append(f"   {y}: {len(g)} صفقة | متوسط {g.R.mean():+.2f}R | {g.R.sum():+.1f}R")
        ok = [("≥%d صفقة" % MIN_TRADES, s["n"] >= MIN_TRADES),
              ("حد الثقة الأدنى > 0", s["lo"] > 0),
              ("تراجع < %dR" % MAX_DD, s["dd"] < MAX_DD)]
        L.append("   " + " ".join(("✅" if v else "❌") + k for k, v in ok))
        L.append("")
        if all(v for _, v in ok):
            passers[name] = (s, tr)
    L.append("الحكم:")
    if not passers:
        L += ["لا مرشح اجتاز مرحلة التصميم → لا نتداول أياً منها، ولا نضبط معاملاتها لتجميل النتيجة.",
              "(الفترة المحجوزة لم تُفتح)"]
    else:
        best = max(passers, key=lambda k: passers[k][0]["lo"])
        tr = passers[best][1]
        ho = tr[tr.time >= cut]
        hs = stats(ho.R.values, "")
        L += [f"المرشح الأفضل في التصميم: {best}", "اختبار الفترة المحجوزة (مرة واحدة):", hs["text"]]
        good = hs["n"] >= 30 and hs["mean"] > 0
        L.append("✅ اجتاز الفترة المحجوزة (لا يعني ضمان الربح، نتابع بمراقبة حية)" if good
                 else "❌ لم يجتز الفترة المحجوزة → لا نعتمده")
    return "\n".join(L), runs


def telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    chunk = ""
    for line in text.split("\n") + [None]:
        if line is None or len(chunk) + len(line) > 3500:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data=dict(chat_id=TG_CHAT, text=chunk), timeout=15)
            chunk = ""
        if line is not None:
            chunk += line + "\n"


if __name__ == "__main__":
    raw = download(YEARS)
    print("شموع:", len(raw), raw.index[0], "->", raw.index[-1])
    txt, runs = analyze(raw)
    print(txt)
    pd.concat([t.assign(strategy=k) for k, t in runs.items()]).to_csv("lab_trades.csv", index=False)
    telegram(txt)
