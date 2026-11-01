"""
Backtest لبوت TS V2 على بيانات XAU/USD التاريخية (TwelveData)
"""
import os, time
import pandas as pd
import requests
import ts_bot as bot

DAYS = int(os.getenv("BT_DAYS") or "90")
SPREAD = float(os.getenv("BT_SPREAD") or "0.4")
HOLD_HOURS = 8


def download(days):
    end = pd.Timestamp.now(tz=bot.NY).floor("min")
    start = end - pd.Timedelta(days=days)
    frames, cur = [], end
    while cur > start:
        params = dict(symbol=bot.SYMBOL, interval="1min", outputsize=5000,
                      timezone="America/New_York",
                      end_date=cur.strftime("%Y-%m-%d %H:%M:%S"), apikey=bot.TD_KEY)
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
            bot.NY, ambiguous="NaT", nonexistent="NaT")
        df = df.dropna(subset=["datetime"]).set_index("datetime").sort_index()
        frames.append(df)
        print("حُمّل حتى", df.index[0])
        if df.index[0] >= cur:
            break
        cur = df.index[0] - pd.Timedelta(minutes=1)
        time.sleep(8)
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated()]
    return out[["open", "high", "low", "close"]].astype(float).loc[start:]


def finish_bt(df3, j, e, entry, ext, lows, kind):
    sl = ext + bot.SL_BUFFER
    risk = sl - entry
    if risk <= 0:
        return None
    tp1, tp2 = bot.pick_targets(entry, risk, lows)
    if tp1 is None:
        return None
    return dict(kind=kind, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                rr1=round((entry - tp1) / risk, 2),
                sweep_time=str(df3.index[e]), confirm_time=str(df3.index[j]),
                status="bt", t_confirm=df3.index[j])


bot.finish = finish_bt


def sim_sell(entry, sl, tp1, tp2, bars):
    risk = sl - entry
    hi, lo, cl = (bars[c].to_numpy() for c in ("high", "low", "close"))
    n = len(hi)
    i = 0
    while i < n and hi[i] < entry:
        if lo[i] <= tp1:
            return "MISSED", 0.0, 0.0
        i += 1
    if i >= n:
        return "NOFILL", 0.0, 0.0
    if hi[i] >= sl:
        return "LOSS", -1.0, -1.0
    end = min(n, i + 1 + HOLD_HOURS * 60)
    rr1 = (entry - tp1) / risk
    rr2 = (entry - tp2) / risk if tp2 is not None else None
    k = i + 1
    while k < end:
        if hi[k] >= sl:
            return "LOSS", -1.0, -1.0
        if lo[k] <= tp1:
            if tp2 is None:
                return "WIN", rr1, rr1
            m = k
            while m < end:
                if m > k and hi[m] >= sl:
                    return "WIN", rr1, 0.5 * rr1 - 0.5
                if lo[m] <= tp2:
                    return "WIN", rr1, 0.5 * rr1 + 0.5 * rr2
                m += 1
            last = (entry - cl[end - 1]) / risk
            return "WIN", rr1, 0.5 * rr1 + 0.5 * last
        k += 1
    last = (entry - cl[end - 1]) / risk
    return "TIMEOUT", last, last


def run(df1):
    mdf = bot.mirror(df1)
    days = pd.bdate_range(df1.index[0].date(), df1.index[-1].date())
    trades, seen = [], set()
    for d in days:
        for h, m in bot.GOLD_TIMES:
            now = pd.Timestamp(d.year, d.month, d.day, h, m, tz=bot.NY) + pd.Timedelta(minutes=59)
            pos = df1.index.searchsorted(now)
            if pos < 3000:
                continue
            sub = df1.iloc[max(0, pos - 5000):pos]
            if sub.index[-1] < now - pd.Timedelta(minutes=10):
                continue
            for s in bot.scan(sub, now):
                key = (s["dir"], s["kind"], s["confirm_time"])
                if key in seen:
                    continue
                seen.add(key)
                trades.append(s)

    for t in trades:
        sell = t["dir"] == "sell"
        tc = t["t_confirm"] + pd.Timedelta(minutes=3)
        src = df1 if sell else mdf
        bars = src.loc[tc: tc + pd.Timedelta(hours=HOLD_HOURS + 24)]
        sg = 1 if sell else -1
        entry, sl, tp1 = sg * t["entry"], sg * t["sl"], sg * t["tp1"]
        tp2 = sg * t["tp2"] if t["tp2"] is not None else None
        res, ra, rb = sim_sell(entry, sl, tp1, tp2, bars)
        if res in ("WIN", "LOSS", "TIMEOUT"):
            cost = SPREAD / (sl - entry)
            ra, rb = ra - cost, rb - cost
        t.update(res=res, ra=ra, rb=rb)
    return trades


def stats(ts, label):
    f = [t for t in ts if t["res"] in ("WIN", "LOSS", "TIMEOUT")]
    if not f:
        return f"{label}: لا صفقات"
    w = sum(t["res"] == "WIN" for t in f)
    l = sum(t["res"] == "LOSS" for t in f)
    to = sum(t["res"] == "TIMEOUT" for t in f)
    out = [f"{label}: صفقات منفذة {len(f)} | ربح(TP1) {w} | خسارة {l} | مهلة {to} | نسبة النجاح {100*w/len(f):.0f}%"]
    for name, key in (("خروج كامل عند TP1", "ra"), ("50% TP1 + 50% TP2", "rb")):
        r = [t[key] for t in sorted(f, key=lambda x: x["t_confirm"])]
        gp = sum(x for x in r if x > 0)
        gl = -sum(x for x in r if x < 0)
        cum, peak, dd = 0.0, 0.0, 0.0
        for x in r:
            cum += x
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        pf = gp / gl if gl else float("inf")
        out.append(f"  {name}: مجموع {sum(r):+.1f}R | متوسط {sum(r)/len(r):+.2f}R | PF {pf:.2f} | أقصى تراجع {dd:.1f}R")
    return "\n".join(out)


def report(trades):
    miss = sum(t["res"] in ("MISSED", "NOFILL") for t in trades)
    lines = [f"📊 Backtest آخر {DAYS} يوماً (spread {SPREAD}$)",
             f"إشارات: {len(trades)} | لم تُنفَّذ (وصل TP1 قبل الدخول أو لم يلمس السعر): {miss}", ""]
    lines.append(stats(trades, "الكل"))
    for k in sorted({t["kind"] for t in trades}):
        lines.append(stats([t for t in trades if t["kind"] == k], k))
    for d in ("sell", "buy"):
        lines.append(stats([t for t in trades if t["dir"] == d], d))
    lines.append("")
    lines.append("تنبيه: النتائج تعتمد على تعريفاتي للـ CISD/MSS/FVG وعلى بيانات TwelveData، وهي ليست ضماناً.")
    return "\n".join(lines)


if __name__ == "__main__":
    data = download(DAYS)
    print("شموع:", len(data), data.index[0], "->", data.index[-1])
    tr = run(data)
    txt = report(tr)
    print(txt)
    pd.DataFrame([{k: v for k, v in t.items() if k != "t_confirm"} for t in tr]).to_csv("trades.csv", index=False)
    if bot.TG_TOKEN and bot.TG_CHAT:
        bot.telegram(txt)
