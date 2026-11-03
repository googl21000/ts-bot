"""
Backtest for the TS strategy (XAUUSD) - uses the SAME detection code as ts_bot.py.

What it does
  1) downloads 1-minute XAU/USD from Twelve Data for the last N days (paged)
  2) replays every golden candle (NY time) exactly like the live bot would:
     the signal is known when the 3m MSS candle closes, no look-ahead
  3) simulates each trade on 1-minute candles with 3 management styles
  4) prints a summary, saves CSV + equity chart, sends the summary to Telegram

Env vars: TWELVEDATA_KEY, TG_TOKEN, TG_CHAT (same as the bot)
Optional: DAYS (default 90), HTF_REQUIRED (true/false), CANCEL_HALF_IF (sweep/setup),
          ENTRY_MODE (proximal/mid)

Simulation assumptions (conservative)
  - limit order at Entry, filled when price touches it (no slippage beyond COST_USD)
  - if SL and TP are both inside one candle, SL is counted first
  - the candle that fills the order is only checked for SL
  - COST_USD (spread + slippage) is deducted from every filled trade
  - unfilled orders are cancelled after ORDER_EXPIRY_MIN or if TP1 is touched first
Not financial advice. Past results do not guarantee future results.
"""
import os
import time

import numpy as np
import pandas as pd
import requests

import ts_bot as b

# ============================ SETTINGS ============================
DAYS = int(os.getenv("DAYS") or 90)
COST_USD = 0.30            # spread + slippage per trade, in USD per ounce
ORDER_EXPIRY_MIN = 180     # cancel an unfilled limit order after this many minutes
HORIZON_MIN = 720          # max minutes to follow a trade after the signal
MGMTS = {
    "tp1_only": "100% at TP1",
    "split_be": "50% TP1 + 50% TP2, SL to BE after TP1",
    "split_nobe": "50% TP1 + 50% TP2, SL stays",
}
URL = "https://api.twelvedata.com/time_series"
M1, M3, M30 = b.M1, b.M3, b.M30

if os.getenv("HTF_REQUIRED"):
    b.HTF_POI_REQUIRED = os.getenv("HTF_REQUIRED").strip().lower() == "true"
if os.getenv("CANCEL_HALF_IF") in ("sweep", "setup"):
    b.CANCEL_HALF_IF = os.getenv("CANCEL_HALF_IF")
if os.getenv("ENTRY_MODE") in ("mss", "proximal", "mid"):
    b.ENTRY_MODE = os.getenv("ENTRY_MODE")
if os.getenv("SL_MODE") in ("swept", "sweep"):
    b.SL_MODE = os.getenv("SL_MODE")


# ============================== DATA ==============================
def fetch_history(days):
    """Page backwards through Twelve Data (5000 bars per call). UTC is used for paging
    so there is no daylight-saving ambiguity; the result is converted to New York time."""
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("min")
    start = end - pd.Timedelta(days=days + 6)      # +6 days of history for left liquidity
    frames, cur_end, retries = [], end, 0
    while True:
        params = dict(symbol=b.SYMBOL, interval="1min", outputsize=5000, timezone="UTC",
                      end_date=cur_end.strftime("%Y-%m-%d %H:%M:%S"), apikey=b.TD_KEY)
        j = requests.get(URL, params=params, timeout=40).json()
        if "values" not in j:
            msg = str(j.get("message", j))
            if ("credits" in msg or j.get("code") == 429) and retries < 6:
                retries += 1
                print("rate limit reached, waiting 40s...", flush=True)
                time.sleep(40)
                continue
            print("stop fetching:", msg[:200], flush=True)
            break
        retries = 0
        d = pd.DataFrame(j["values"])
        d["datetime"] = pd.to_datetime(d["datetime"])
        frames.append(d)
        oldest = d["datetime"].min()
        print(f"  fetched {len(d)} bars back to {oldest} UTC", flush=True)
        if oldest <= start or len(d) < 5000:
            break
        cur_end = oldest - pd.Timedelta(minutes=1)
        time.sleep(10)                              # free plan: 8 requests / minute
    if not frames:
        raise RuntimeError("no data returned by Twelve Data")
    df = pd.concat(frames).drop_duplicates("datetime").set_index("datetime").sort_index()
    df = df[["open", "high", "low", "close"]].astype(float)
    df.index = df.index.tz_localize("UTC").tz_convert(b.NY)
    return df[df.index >= start.tz_localize("UTC").tz_convert(b.NY)]


def data_report(df1):
    gaps = df1.index.to_series().diff().dropna()
    odd = gaps[(gaps > pd.Timedelta(minutes=70)) & (gaps < pd.Timedelta(hours=30))]
    print(f"Data: {df1.index[0]:%Y-%m-%d %H:%M} -> {df1.index[-1]:%Y-%m-%d %H:%M} NY | "
          f"{len(df1)} one-minute bars | suspicious gaps: {len(odd)}", flush=True)


# ============================ SIMULATION ============================
def simulate(H, L, C, T, entry, sl, tp1, tp2, mgmt, expiry):
    """Sell-oriented simulation (buys are mirrored by the caller).
    Returns (status, gross R). status: missed / expired / SL / BE / SL_after_TP1 / TP1 / TP2 / TIMEOUT"""
    risk = sl - entry
    r1, r2 = (entry - tp1) / risk, (entry - tp2) / risk
    filled, tp1_hit, sl_cur, realized = False, False, sl, 0.0
    for i in range(len(H)):
        if not filled:
            if H[i] >= entry:
                filled = True
                if H[i] >= sl:
                    return "SL", -1.0
                continue                            # fill candle: only SL is checked
            if L[i] <= tp1:
                return "missed", 0.0
            if T[i] > expiry:
                return "expired", 0.0
            continue
        if H[i] >= sl_cur:                          # SL first when ambiguous
            if mgmt == "tp1_only" or not tp1_hit:
                return "SL", -1.0
            if mgmt == "split_be":
                return "BE", realized
            return "SL_after_TP1", realized - 0.5
        if mgmt == "tp1_only":
            if L[i] <= tp1:
                return "TP1", r1
            continue
        if not tp1_hit and L[i] <= tp1:
            tp1_hit = True
            realized += 0.5 * r1
            if mgmt == "split_be":
                sl_cur = entry
        if tp1_hit and L[i] <= tp2:
            return "TP2", realized + 0.5 * r2
    if not filled:
        return "expired", 0.0
    remaining = 0.5 if tp1_hit else 1.0
    return "TIMEOUT", realized + remaining * (entry - C[-1]) / risk


def run_trade(r, post):
    """Simulate one setup on 1m candles that start right after the signal."""
    sell = r["side"] == "sell"
    sgn = 1.0 if sell else -1.0
    H = post["high"].values if sell else -post["low"].values
    L = post["low"].values if sell else -post["high"].values
    C = sgn * post["close"].values
    T = post.index
    entry, sl, tp1, tp2 = (sgn * r[k] for k in ("entry", "sl", "tp1", "tp2"))
    expiry = T[0] + pd.Timedelta(minutes=ORDER_EXPIRY_MIN) if len(T) else None
    risk = abs(r["entry"] - r["sl"])
    out = {}
    for mg in MGMTS:
        status, gross = simulate(H, L, C, T, entry, sl, tp1, tp2, mg, expiry)
        filled = status not in ("missed", "expired")
        out[mg] = (status, gross - COST_USD / risk if filled else 0.0, filled)
    return out


# ============================== REPORT ==============================
def metrics(R):
    arr = np.asarray(R, dtype=float)
    if len(arr) == 0:
        return None
    wins, losses = arr[arr > 0.1], arr[arr < -0.1]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
    eq = np.cumsum(arr)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    decided = len(wins) + len(losses)
    return dict(n=len(arr), wins=len(wins), losses=len(losses), be=len(arr) - decided,
                wr=(len(wins) / decided * 100) if decided else 0.0,
                avg=float(arr.mean()), total=float(arr.sum()), pf=pf, dd=dd)


def fmt_m(m):
    if not m:
        return "no filled trades"
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    return (f"trades {m['n']} | W {m['wins']} / L {m['losses']} / BE {m['be']} | "
            f"win rate {m['wr']:.0f}% | avg {m['avg']:+.2f}R | total {m['total']:+.1f}R | "
            f"PF {pf} | max DD {m['dd']:.1f}R")


def build_summary(tr, df1, windows_n):
    lines = [
        f"TS Strategy Backtest - XAUUSD",
        f"Period: {df1.index[0]:%Y-%m-%d} to {df1.index[-1]:%Y-%m-%d} (NY) | golden candles checked: {windows_n}",
        f"HTF zone required: {b.HTF_POI_REQUIRED} | cancel :30 if :00 {b.CANCEL_HALF_IF} | entry: {b.ENTRY_MODE} | SL: {b.SL_MODE}",
        f"Costs: ${COST_USD:.2f} per trade | SL counted first when ambiguous",
        "",
        f"Signals: {len(tr)} | filled: {int(tr['filled'].sum()) if len(tr) else 0} | "
        f"not filled (missed/expired): {int((~tr['filled']).sum()) if len(tr) else 0}",
        "",
    ]
    if not len(tr):
        lines.append("No signals in this period with the current settings.")
        return "\n".join(lines)
    for mg, label in MGMTS.items():
        lines.append(f"[{label}]")
        lines.append(fmt_m(metrics(tr.loc[tr["filled"], f"R_{mg}"])))
        lines.append("")
    f = tr[tr["filled"]]
    if len(f):
        lines.append("By golden candle (50/50 + BE):")
        for g, sub in f.groupby("golden"):
            m = metrics(sub["R_split_be"])
            lines.append(f"  {g}: {m['n']} trades, {m['total']:+.1f}R, win {m['wr']:.0f}%")
        lines.append("By side (50/50 + BE):")
        for s, sub in f.groupby("side"):
            m = metrics(sub["R_split_be"])
            lines.append(f"  {s}: {m['n']} trades, {m['total']:+.1f}R, win {m['wr']:.0f}%")
    return "\n".join(lines)


def save_chart(tr, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    f = tr[tr["filled"]]
    if not len(f):
        return False
    fig, ax = plt.subplots(figsize=(9, 5))
    for mg, label in MGMTS.items():
        ax.plot(range(1, len(f) + 1), f[f"R_{mg}"].cumsum().values, label=label)
    ax.axhline(0, color="gray", lw=0.8)
    ax.set_title("Equity curve (cumulative R, net of costs)")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("R")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def tg_file(path, kind):
    if not b.TG_TOKEN or not b.TG_CHAT or not os.path.exists(path):
        return
    method, field = ("sendPhoto", "photo") if kind == "photo" else ("sendDocument", "document")
    try:
        with open(path, "rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{b.TG_TOKEN}/{method}",
                              data={"chat_id": b.TG_CHAT}, files={field: fh}, timeout=60)
        if r.status_code != 200:
            print("telegram file error:", r.status_code, flush=True)
    except Exception as e:
        print("telegram file exception:", e, flush=True)


# =============================== MAIN ===============================
def golden_windows(df1, days):
    last = df1.index[-1]
    first_day = (pd.Timestamp.now(tz=b.NY) - pd.Timedelta(days=days)).normalize()
    out = []
    for day in pd.date_range(start=first_day, end=last.normalize(), freq="D"):
        if day.weekday() >= 5:
            continue
        for h, m in b.GOLDEN:
            t0 = day.replace(hour=h, minute=m)
            if t0 + b.WINDOW_AFTER <= last + M1:
                out.append(t0)
    return out


def debug_day(df1, date_str):
    """Explain every golden candle of one day step by step, and dump the bot's own candles
    so they can be compared with TradingView (feeds differ by a few tenths of a dollar)."""
    day = pd.Timestamp(date_str, tz=b.NY).normalize()
    df3, df30 = b.resample(df1, "3min"), b.resample(df1, "30min")
    out = [f"DEBUG {date_str} (New York time) - the bot's own data feed",
           f"settings: HTF required={b.HTF_POI_REQUIRED} | cancel :30 if :00 {b.CANCEL_HALF_IF} | "
           f"entry={b.ENTRY_MODE} | SL={b.SL_MODE} | MSS swing k={b.SWING_K_MSS}", ""]
    if day.weekday() >= 5:
        out.append("this day is a weekend - the bot does not trade it")
    for h, m in b.GOLDEN:
        t0 = day.replace(hour=h, minute=m)
        end = t0 + b.WINDOW_AFTER
        d3 = df3[df3.index + M3 <= end].tail(1000)
        d30 = df30[df30.index + M30 <= end].tail(100)
        if len(d3) < 200 or d3.index[-1] < t0:
            out.append(f"== {t0:%H:%M}: no data ==")
            continue
        out.append(f"== golden candle {t0:%H:%M} ==")
        if b.half_cancelled(d3, d30, t0):
            out.append("  CANCELLED: the :00 candle before it already took liquidity")
        out += b.explain(d3, d30, t0)
        out.append("")
    for h, m in ((2, 0), (7, 0), (9, 0)):
        t0 = day.replace(hour=h, minute=m)
        seg = df3[(df3.index >= t0 - pd.Timedelta(minutes=30)) & (df3.index < t0 + pd.Timedelta(minutes=60))]
        out.append(f"-- 3m candles {t0 - pd.Timedelta(minutes=30):%H:%M} to {t0 + pd.Timedelta(minutes=60):%H:%M} --")
        for t, r in seg.iterrows():
            out.append(f"{t:%H:%M}  O {r.open:.2f}  H {r.high:.2f}  L {r.low:.2f}  C {r.close:.2f}")
        out.append("")
    text = "\n".join(out)
    print(text, flush=True)
    open("backtest_debug.txt", "w").write(text)
    b.telegram(f"Debug for {date_str} is ready (see the attached file).")
    tg_file("backtest_debug.txt", "document")


def main():
    debug_date = (os.getenv("DEBUG_DATE") or "").strip()
    if debug_date:
        back = (pd.Timestamp.now(tz=b.NY).normalize() - pd.Timestamp(debug_date, tz=b.NY)).days + 4
        print(f"Debug mode for {debug_date}", flush=True)
        debug_day(fetch_history(max(back, 8)), debug_date)
        return
    print(f"Backtest: last {DAYS} days | HTF required={b.HTF_POI_REQUIRED} "
          f"cancel={b.CANCEL_HALF_IF} entry={b.ENTRY_MODE} SL={b.SL_MODE}", flush=True)
    df1 = fetch_history(DAYS)
    data_report(df1)
    df3_all, df30_all = b.resample(df1, "3min"), b.resample(df1, "30min")
    windows = golden_windows(df1, DAYS)
    rows = []
    for t0 in windows:
        end = t0 + b.WINDOW_AFTER
        d3 = df3_all[df3_all.index + M3 <= end].tail(1000)       # same history size as the live bot
        d30 = df30_all[df30_all.index + M30 <= end].tail(100)
        if len(d3) < 200:
            continue
        if b.half_cancelled(d3, d30, t0):
            continue
        for r in b.analyze(d3, d30, t0):
             signal_time = r["mss_time"] + M3 + pd.Timedelta(minutes=2)
                 # candle close = when the bot alerts
            post = df1[(df1.index >= signal_time) &
                       (df1.index < signal_time + pd.Timedelta(minutes=HORIZON_MIN))]
            if post.empty:
                continue
            res = run_trade(r, post)
            row = dict(date=f"{t0:%Y-%m-%d}", golden=f"{t0:%H:%M}", side=r["side"],
                       htf=(r["poi"]["kind"] if r["poi"] else ""), zone=r["zone"][2],
                       signal_time=f"{signal_time:%Y-%m-%d %H:%M}",
                       sweep_time=f"{r['sweep_time']:%H:%M}", mss_time=f"{r['mss_time']:%H:%M}",
                       mss_level=round(r["mss"], 2),
                       zone_lo=round(r["zone"][0], 2), zone_hi=round(r["zone"][1], 2),
                       entry=round(r["entry"], 2), sl=round(r["sl"], 2),
                       tp1=round(r["tp1"], 2), tp2=round(r["tp2"], 2),
                       rr1=round(abs(r["entry"] - r["tp1"]) / r["risk"], 2),
                       rr2=round(abs(r["entry"] - r["tp2"]) / r["risk"], 2),
                       filled=res["split_be"][2])
            for mg in MGMTS:
                row[f"out_{mg}"] = res[mg][0]
                row[f"R_{mg}"] = round(res[mg][1], 3)
            rows.append(row)

    tr = pd.DataFrame(rows)
    summary = build_summary(tr, df1, len(windows))
    print("\n" + summary, flush=True)
    open("backtest_summary.txt", "w").write(summary)
    tr.to_csv("backtest_trades.csv", index=False)
    has_chart = save_chart(tr, "backtest_equity.png") if len(tr) else False

    b.telegram(summary[:3900])
    if has_chart:
        tg_file("backtest_equity.png", "photo")
    if len(tr):
        tg_file("backtest_trades.csv", "document")


if __name__ == "__main__":
    main()
