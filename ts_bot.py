import os, sys, json, time, requests
import pandas as pd
from datetime import datetime, timezone, timedelta

TD_KEY = os.environ.get("TD_API_KEY", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
SYMBOL, TF, BAR = "XAU/USD", "5min", 5
STATE = "state.json"
SPREAD = 0.9      # دولار
STALE_MIN = 10    # لا ترسل إشارة متأخرة أكثر من هذا

C = dict(
    sess=(8 * 60, 13 * 60 + 30),   # UTC = 09:00-14:30 الجزائر
    lookback=36, sweep_win=6, atr_n=14, body_atr=0.5,
    buffer=0.5, min_risk=2.0, max_risk=6.0,
    tp=(5.0, 10.0, 18.0),
    max_signals=3, max_sl=2, day_target=10.0,
    cool_min=15, expire_h=3,
)

# ---------------- data ----------------
def fetch(n=300, end=None):
    p = dict(symbol=SYMBOL, interval=TF, outputsize=n, timezone="UTC", apikey=TD_KEY)
    if end:
        p["end_date"] = end
    r = requests.get("https://api.twelvedata.com/time_series", params=p, timeout=30).json()
    if "values" not in r:
        raise RuntimeError(r)
    df = pd.DataFrame(r["values"])
    df["t"] = pd.to_datetime(df["datetime"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df[["t", "open", "high", "low", "close"]].sort_values("t").reset_index(drop=True)

def atr(df, n):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def in_session(t):
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and C["sess"][0] <= m < C["sess"][1]

# ---------------- strategy ----------------
def signal(df):
    n = C["lookback"] + C["sweep_win"]
    if len(df) < n + 2:
        return None
    last = df.iloc[-1]
    t_in = last.t + timedelta(minutes=BAR)
    if not in_session(t_in):
        return None
    a = atr(df, C["atr_n"]).iloc[-1]
    if not a > 0 or abs(last.close - last.open) < C["body_atr"] * a:
        return None
    w = df.iloc[-C["sweep_win"]:]
    ref = df.iloc[-n:-C["sweep_win"]]
    for buy in (True, False):
        ex = w.low.min() if buy else w.high.max()
        lvl = ref.low.min() if buy else ref.high.max()
        if buy and not (ex < lvl and last.close > lvl and last.close > last.open):
            continue
        if not buy and not (ex > lvl and last.close < lvl and last.close < last.open):
            continue
        pos = list(w.index).index(w.low.idxmin() if buy else w.high.idxmax())
        mid = w.iloc[pos:-1]
        if len(mid) and ((buy and last.close <= mid.high.max()) or (not buy and last.close >= mid.low.min())):
            continue
        e = last.close
        sl = ex - C["buffer"] if buy else ex + C["buffer"]
        risk = abs(e - sl)
        if risk < C["min_risk"]:
            risk = C["min_risk"]
            sl = e - risk if buy else e + risk
        if risk > C["max_risk"]:
            continue
        d = 1 if buy else -1
        return dict(side="buy" if buy else "sell", entry=round(e, 2), sl=round(sl, 2),
                    tps=[round(e + d * x, 2) for x in C["tp"]], t=t_in.isoformat())
    return None

def step(s, b):
‎    """يحدّث الصفقة بشمعة واحدة. الوقف أولاً عند التعارض داخل الشمعة."""
    ev = []
    buy = s["side"] == "buy"
    d = 1 if buy else -1
    if (b.low <= s["cur_sl"]) if buy else (b.high >= s["cur_sl"]):
        s["pnl"] += (3 - s["stage"]) / 3 * d * (s["cur_sl"] - s["entry"])
        s["closed"] = True
        ev.append("SL" if s["stage"] == 0 else "BE")
        return ev
    while s["stage"] < 3:
        tp = s["tps"][s["stage"]]
        if (b.high >= tp) if buy else (b.low <= tp):
            s["pnl"] += d * (tp - s["entry"]) / 3
            s["stage"] += 1
            ev.append(f"TP{s['stage']}")
            if s["stage"] == 1:
                s["cur_sl"] = s["entry"]
        else:
            break
    if s["stage"] == 3:
        s["closed"] = True
    return ev

# ---------------- telegram ----------------
def tg(msg):
    print(msg)
    if TG_TOKEN:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg}, timeout=30)

def pips(a, b):
    return round(abs(a - b) * 10)

def fmt(s):
    t = pd.to_datetime(s["t"]) + timedelta(hours=1)
    buy = s["side"] == "buy"
    head = "🟢 BUY" if buy else "🔴 SELL"
    tps = "\n".join(f"✅ TP{i+1}: {x} (+{pips(x, s['entry'])})" for i, x in enumerate(s["tps"]))
    return (f"{head} XAUUSD (الآن)\n⏰ {t:%H:%M} (الجزائر)\n"
            f"➡️ الدخول: {s['entry']}\n🛑 الوقف: {s['sl']} ({pips(s['entry'], s['sl'])} pip)\n{tps}\n"
            f"📌 عند TP1: أغلق جزءاً وانقل الوقف إلى الدخول\n"
            f"⚠️ لا تدخل إذا ابتعد السعر أكثر من 10 pip عن الدخول")

MSG = {"TP1": "✅ TP1 تحقق — انقل الوقف إلى الدخول", "TP2": "✅ TP2 تحقق",
       "TP3": "🏁 TP3 تحقق — اكتملت الصفقة", "SL": "❌ ضُرب الوقف",
       "BE": "⚪ أُغلقت على نقطة الدخول"}

# ---------------- live ----------------
def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return dict(open=None, day=None, n=0, sl=0, pnl=0.0, last_sig=None, cool=None)

def live():
    st = load()
    now = datetime.now(timezone.utc)
    df = fetch(300)
    df = df[df.t + timedelta(minutes=BAR) <= now].reset_index(drop=True)
    day = now.date().isoformat()
    if st["day"] != day:
        st.update(day=day, n=0, sl=0, pnl=0.0)

    s = st["open"]
    if s:
        since = pd.to_datetime(s["t"])
        seen = pd.to_datetime(s["seen"]) if s.get("seen") else None
        for _, b in df[df.t >= since].iterrows():
            if seen is not None and b.t <= seen:
                continue
            s["seen"] = b.t.isoformat()
            for e in step(s, b):
                tg(f"{MSG[e]}\n({s['side'].upper()} @ {s['entry']})")
            if s["closed"]:
                break
        if s["closed"]:
            st["pnl"] += s["pnl"] - SPREAD
            if s["stage"] == 0:
                st["sl"] += 1
            st["open"], st["cool"] = None, now.isoformat()
        elif now - since > timedelta(hours=C["expire_h"]) and s["stage"] == 0:
            tg(f"⏱ انتهت صلاحية الصفقة ({s['side'].upper()} @ {s['entry']}) — أغلقها يدوياً")
            st["open"], st["cool"] = None, now.isoformat()

    if not st["open"] and len(df):
        last_t = df.t.iloc[-1] + timedelta(minutes=BAR)
        fresh = now - last_t <= timedelta(minutes=STALE_MIN)
        cool = st["cool"] and now - pd.to_datetime(st["cool"]) < timedelta(minutes=C["cool_min"])
        blocked = (st["n"] >= C["max_signals"] or st["sl"] >= C["max_sl"]
                   or st["pnl"] >= C["day_target"] or cool)
        if fresh and not blocked:
            sg = signal(df)
            if sg and sg["t"] != st["last_sig"]:
                tg(fmt(sg))
                st["last_sig"] = sg["t"]
                st["open"] = dict(sg, cur_sl=sg["sl"], stage=0, pnl=0.0, closed=False, seen=None)
                st["n"] += 1
    json.dump(st, open(STATE, "w"), indent=1)

# ---------------- backtest ----------------
def backtest(pages=6):
    parts, end = [], None
    for _ in range(pages):
        d = fetch(5000, end)
        parts.append(d)
        end = d.t.iloc[0].strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(9)
    df = pd.concat(parts).drop_duplicates("t").sort_values("t").reset_index(drop=True)
    tr, s, day, n, sl, pnl, cool = [], None, None, 0, 0, 0.0, None
    for i in range(len(df)):
        b = df.iloc[i]
        if b.t.date() != day:
            day, n, sl, pnl = b.t.date(), 0, 0, 0.0
        if s:
            step(s, b)
            if s["closed"]:
                net = s["pnl"] - SPREAD
                pnl += net
                sl += s["stage"] == 0
                tr.append((s["t"], s["side"], round(net * 10)))
                cool, s = b.t, None
        if s is None:
            if n >= C["max_signals"] or sl >= C["max_sl"] or pnl >= C["day_target"]:
                continue
            if cool is not None and b.t - cool < timedelta(minutes=C["cool_min"]):
                continue
            sg = signal(df.iloc[max(0, i - 120): i + 1])
            if sg:
                s = dict(sg, cur_sl=sg["sl"], stage=0, pnl=0.0, closed=False)
                n += 1
    if not tr:
        return print("no trades")
    p = [x[2] for x in tr]
    w = [x for x in p if x > 0]
    l = [x for x in p if x <= 0]
    pf = sum(w) / abs(sum(l)) if l and sum(l) else float("inf")
    eq = pd.Series(p).cumsum()
    print(f"{df.t.iloc[0]:%Y-%m-%d} -> {df.t.iloc[-1]:%Y-%m-%d} | bars {len(df)}")
    print(f"trades {len(p)} | win {len(w)/len(p):.0%} | total {sum(p)} pips | avg {sum(p)/len(p):.1f} | PF {pf:.2f}")
    print(f"max drawdown {int((eq.cummax() - eq).max())} pips")
    by = pd.Series(p, index=[pd.to_datetime(x[0]).hour for x in tr]).groupby(level=0).agg(["count", "sum"])
    print("by UTC hour:\n", by)

if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else "live"
    if m == "live":
        live()
    elif m == "test":
        tg("✅ البوت متصل")
    elif m == "backtest":
        backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 6)
name: Gold Pattern Bot
on:
  schedule:
    - cron: "*/5 8-16 * * 1-5"
  workflow_dispatch:
    inputs:
      mode:
        description: "live | test | backtest"
        default: "live"
permissions:
  contents: write
concurrency:
  group: gold-pattern
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install pandas requests
      - run: python gold_pattern_bot.py ${{ github.event.inputs.mode || 'live' }}
        env:
          TD_API_KEY: ${{ secrets.TD_API_KEY }}
          TG_TOKEN: ${{ secrets.TG_TOKEN }}
          TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
      - run: |
          git config user.name "bot"
          git config user.email "bot@users.noreply.github.com"
          git add state.json || true
          git commit -m "state" || exit 0
          git push
