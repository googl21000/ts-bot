
import os
import sys
import json
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

# =========================================================
# TS BOT - XAUUSD
# =========================================================

TD_KEY = os.environ.get("TWELVEDATA_KEY", "")

TG_TOKEN = os.environ.get("TG_TOKEN", "")

TG_CHAT = os.environ.get("TG_CHAT", "")

SYMBOL, TF, BAR = "XAU/USD", "5min", 5
STATE = "state.json"
SPREAD = 0.9
STALE_MIN = 10

C = dict(
    sess=(8 * 60, 13 * 60 + 30),  # UTC = 09:00-14:30 الجزائر
    lookback=36,
    sweep_win=6,
    atr_n=14,
    body_atr=0.5,
    sl=5.0,
    tp=(5.0, 10.0),
    trend=True,
    ema=144,
    max_signals=3,
    max_sl=2,
    day_target=10.0,
    cool_min=15,
    expire_h=3,
)

# =========================================================
# DATA
# =========================================================

def fetch(n=300, end=None):
    if not TD_KEY:
        raise RuntimeError("Missing environment variable: TD_API_KEY")

    params = {
        "symbol": SYMBOL,
        "interval": TF,
        "outputsize": n,
        "timezone": "UTC",
        "apikey": TD_KEY,
    }

    if end:
        params["end_date"] = end

    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")

    df = pd.DataFrame(data["values"])
    df["t"] = pd.to_datetime(df["datetime"], utc=True)

    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[["t", "open", "high", "low", "close"]]
    df = df.dropna()
    return df.sort_values("t").drop_duplicates("t").reset_index(drop=True)


def atr(df, n):
    previous_close = df.close.shift()

    tr = pd.concat(
        [
            df.high - df.low,
            (df.high - previous_close).abs(),
            (df.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.rolling(n).mean()


def in_session(t):
    minutes = t.hour * 60 + t.minute
    return (
        t.weekday() < 5
        and C["sess"][0] <= minutes < C["sess"][1]
    )


# =========================================================
# STRATEGY
# =========================================================

def signal(df):
    required = max(
        C["lookback"] + C["sweep_win"] + 2,
        C["ema"] + 10,
    )

    if len(df) < required:
        return None

    last = df.iloc[-1]
    signal_time = last.t + timedelta(minutes=BAR)

    if not in_session(signal_time):
        return None

    current_atr = atr(df, C["atr_n"]).iloc[-1]

    if pd.isna(current_atr) or current_atr <= 0:
        return None

    candle_body = abs(last.close - last.open)

    if candle_body < C["body_atr"] * current_atr:
        return None

    ema_value = (
        df.close
        .ewm(span=C["ema"], adjust=False)
        .mean()
        .iloc[-1]
    )

    window = df.iloc[-C["sweep_win"]:]
    reference = df.iloc[
        -(C["lookback"] + C["sweep_win"]):-C["sweep_win"]
    ]

    for buy in (True, False):

        if C["trend"]:
            if buy and last.close < ema_value:
                continue
            if not buy and last.close > ema_value:
                continue

        extreme = window.low.min() if buy else window.high.max()
        level = reference.low.min() if buy else reference.high.max()

        if buy:
            valid = (
                extreme < level
                and last.close > level
                and last.close > last.open
            )
        else:
            valid = (
                extreme > level
                and last.close < level
                and last.close < last.open
            )

        if not valid:
            continue

        extreme_index = (
            window.low.idxmin()
            if buy
            else window.high.idxmax()
        )

        position = list(window.index).index(extreme_index)
        middle = window.iloc[position:-1]

        if len(middle):
            if buy and last.close <= middle.high.max():
                continue
            if not buy and last.close >= middle.low.min():
                continue

        entry = last.close
        direction = 1 if buy else -1

        return {
            "side": "buy" if buy else "sell",
            "entry": round(entry, 2),
            "sl": round(entry - direction * C["sl"], 2),
            "tps": [
                round(entry + direction * target, 2)
                for target in C["tp"]
            ],
            "t": signal_time.isoformat(),
        }

    return None


# =========================================================
# TRADE MANAGEMENT
# =========================================================

def step(s, candle):
    """
    إدارة الصفقة على شمعة واحدة.
    عند تعارض الوقف والهدف داخل الشمعة، نعتمد التنفيذ المحافظ:
    الوقف أولاً.
    """

    events = []
    buy = s["side"] == "buy"
    direction = 1 if buy else -1
    total_targets = len(s["tps"])

    def stop_hit(stop_price):
        if buy:
            return candle.low <= stop_price
        return candle.high >= stop_price

    # فحص الوقف الحالي أولاً
    if stop_hit(s["cur_sl"]):
        remaining = (total_targets - s["stage"]) / total_targets

        s["pnl"] += (
            remaining
            * direction
            * (s["cur_sl"] - s["entry"])
        )

        s["closed"] = True
        events.append("SL" if s["stage"] == 0 else "BE")
        return events

    # فحص الأهداف
    while s["stage"] < total_targets:

        target = s["tps"][s["stage"]]

        target_hit = (
            candle.high >= target
            if buy
            else candle.low <= target
        )

        if not target_hit:
            break

        s["pnl"] += (
            direction
            * (target - s["entry"])
            / total_targets
        )

        s["stage"] += 1
        events.append(f"TP{s['stage']}")

        # بعد TP1 ننقل الوقف إلى الدخول
        if s["stage"] == 1:
            s["cur_sl"] = s["entry"]

            # إذا لمست الشمعة نقطة الدخول أيضاً،
            # نفترض أن الوقف تحقق قبل الهدف التالي.
            if stop_hit(s["cur_sl]):
                remaining = (
                    total_targets - s["stage"]
                ) / total_targets

                s["pnl"] += (
                    remaining
                    * direction
                    * (s["cur_sl"] - s["entry"])
                )

                s["closed"] = True
                events.append("BE")
                return events

    if s["stage"] == total_targets:
        s["closed"] = True

    return events


# =========================================================
# TELEGRAM
# =========================================================

def tg(message):
    print(message)

    if not TG_TOKEN or not TG_CHAT:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": message,
            },
            timeout=30,
        ).raise_for_status()

    except Exception as error:
        print(f"Telegram error: {error}")


def pips(a, b):
    return round(abs(a - b) * 10)


def fmt(s):
    signal_time = (
        pd.to_datetime(s["t"]) + timedelta(hours=1)
    )

    buy = s["side"] == "buy"
    heading = "🟢 BUY" if buy else "🔴 SELL"

    targets = "\n".join(
        f"✅ TP{i + 1}: {price} (+{pips(price, s['entry'])} pip)"
        for i, price in enumerate(s["tps"])
    )

    return (
        f"{heading} XAUUSD\n"
        f"⏰ {signal_time:%H:%M} الجزائر\n"
        f"➡️ الدخول: {s['entry']}\n"
        f"🛑 الوقف: {s['sl']} "
        f"({pips(s['entry'], s['sl'])} pip)\n"
        f"{targets}\n"
        f"📌 عند TP1: انقل الوقف إلى الدخول\n"
        f"⚠️ لا تدخل إذا ابتعد السعر أكثر من 10 pip عن الدخول"
    )


MSG = {
    "TP1": "✅ TP1 تحقق — انقل الوقف إلى الدخول",
    "TP2": "🏁 TP2 تحقق — اكتملت الصفقة",
    "SL": "❌ ضُرب وقف الخسارة",
    "BE": "⚪ أُغلقت الصفقة على نقطة الدخول",
}


# =========================================================
# LIVE BOT
# =========================================================

def load():
    try:
        with open(STATE, "r") as file:
            return json.load(file)

    except Exception:
        return {
            "open": None,
            "day": None,
            "n": 0,
            "sl": 0,
            "pnl": 0.0,
            "last_sig": None,
            "cool": None,
        }


def save_state(state):
    with open(STATE, "w") as file:
        json.dump(state, file, indent=2)


def live():
    state = load()
    now = datetime.now(timezone.utc)

    df = fetch(300)

    # الشموع المغلقة فقط
    df = df[
        df.t + timedelta(minutes=BAR) <= now
    ].reset_index(drop=True)

    if df.empty:
        print("No closed candles available.")
        return

    today = now.date().isoformat()

    if state["day"] != today:
        state.update(
            day=today,
            n=0,
            sl=0,
            pnl=0.0,
        )

    trade = state["open"]

    if trade:
        since = pd.to_datetime(trade["t"])
        seen = (
            pd.to_datetime(trade["seen"])
            if trade.get("seen")
            else None
        )

        candles = df[df.t >= since]

        for _, candle in candles.iterrows():

            if seen is not None and candle.t <= seen:
                continue

            trade["seen"] = candle.t.isoformat()

            for event in step(trade, candle):
                tg(
                    f"{MSG[event]}\n"
                    f"({trade['side'].upper()} @ {trade['entry']})"
                )

            if trade["closed"]:
                break

        if trade["closed"]:
            state["pnl"] += trade["pnl"] - SPREAD

            if trade["stage"] == 0:
                state["sl"] += 1

            state["open"] = None
            state["cool"] = now.isoformat()

        elif (
            now - since > timedelta(hours=C["expire_h"])
            and trade["stage"] == 0
        ):
            tg(
                f"⏱ انتهت صلاحية الصفقة "
                f"({trade['side'].upper()} @ {trade['entry']}) "
                f"— أغلقها يدوياً"
            )

            state["open"] = None
            state["cool"] = now.isoformat()

    if state["open"] is None:

        last_candle_time = (
            df.t.iloc[-1] + timedelta(minutes=BAR)
        )

        fresh = (
            now - last_candle_time
            <= timedelta(minutes=STALE_MIN)
        )

        cooling = False

        if state["cool"]:
            cooling = (
                now - pd.to_datetime(state["cool"])
                < timedelta(minutes=C["cool_min"])
            )

        blocked = (
            state["n"] >= C["max_signals"]
            or state["sl"] >= C["max_sl"]
            or state["pnl"] >= C["day_target"]
            or cooling
        )

        if fresh and not blocked:
            new_signal = signal(df)

            if (
                new_signal
                and new_signal["t"] != state["last_sig"]
            ):
                tg(fmt(new_signal))

                state["last_sig"] = new_signal["t"]

                state["open"] = dict(
                    new_signal,
                    cur_sl=new_signal["sl"],
                    stage=0,
                    pnl=0.0,
                    closed=False,
                    seen=None,
                )

                state["n"] += 1

    save_state(state)


# =========================================================
# BACKTEST
# =========================================================

def simulate(df):
    trades = []

    trade = None
    current_day = None
    daily_signals = 0
    daily_sl = 0
    daily_pnl = 0.0
    cooldown = None

    for i in range(len(df)):

        candle = df.iloc[i]

        if candle.t.date() != current_day:
            current_day = candle.t.date()
            daily_signals = 0
            daily_sl = 0
            daily_pnl = 0.0

        # إدارة الصفقة المفتوحة
        if trade is not None:

            events = step(trade, candle)

            if trade["closed"]:
                net = trade["pnl"] - SPREAD
                daily_pnl += net

                if trade["stage"] == 0:
                    daily_sl += 1

                trades.append({
                    "entry_time": trade["t"],
                    "exit_time": candle.t.isoformat(),
                    "side": trade["side"],
                    "entry": trade["entry"],
                    "sl": trade["sl"],
                    "tp1": trade["tps"][0],
                    "tp2": trade["tps"][1],
                    "stage": trade["stage"],
                    "exit_reason": (
                        "SL" if trade["stage"] == 0
                        else "TP2" if trade["stage"] == 2
                        else "BE"
                    ),
                    "gross_pnl": round(trade["pnl"], 3),
                    "spread": SPREAD,
                    "net_pnl": round(net, 3),
                    "net_pips": round(net * 10, 1),
                })

                cooldown = candle.t
                trade = None

        # البحث عن إشارة جديدة
        if trade is None:

            if (
                daily_signals >= C["max_signals"]
                or daily_sl >= C["max_sl"]
                or daily_pnl >= C["day_target"]
            ):
                continue

            if (
                cooldown is not None
                and candle.t - cooldown
                < timedelta(minutes=C["cool_min"])
            ):
                continue

            history = df.iloc[max(0, i - 300):i + 1]

            new_signal = signal(history)

            if new_signal:
                trade = dict(
                    new_signal,
                    cur_sl=new_signal["sl"],
                    stage=0,
                    pnl=0.0,
                    closed=False,
                )

                daily_signals += 1

    return trades


def report(trades, name):
    if not trades:
        print(f"\n{name}: no trades")
        return

    pnl = [x["net_pips"] for x in trades]

    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else float("inf")
    )

    equity = pd.Series(pnl).cumsum()
    drawdown = equity.cummax() - equity

    print(f"\n========== {name} ==========")
    print(f"Trades: {len(pnl)}")
    print(f"Wins: {len(wins)}")
    print(f"Losses: {len(losses)}")
    print(f"Win rate: {len(wins) / len(pnl):.2%}")
    print(f"Net PnL: {sum(pnl):.1f} pips")
    print(f"Average trade: {sum(pnl) / len(pnl):.2f} pips")
    print(f"Gross profit: {gross_profit:.1f} pips")
    print(f"Gross loss: {gross_loss:.1f} pips")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Max Drawdown: {drawdown.max():.1f} pips")

    print("\nExit reasons:")
    print(
        pd.Series(
            [x["exit_reason"] for x in trades]
        ).value_counts().to_string()
    )

    print("\nDirection:")
    for side in ("buy", "sell"):
        subset = [
            x for x in trades
            if x["side"] == side
        ]

        if subset:
            side_pnl = sum(x["net_pips"] for x in subset)
            side_wins = sum(
                x["net_pips"] > 0 for x in subset
            )

            print(
                f"{side.upper()}: {len(subset)} trades | "
                f"PnL {side_pnl:.1f} pips | "
                f"Win rate {side_wins / len(subset):.2%}"
            )


def backtest(pages=6):
    parts = []
    end = None

    print("Downloading historical candles...")

    for page in range(pages):
        print(f"Downloading page {page + 1}/{pages}")

        data = fetch(5000, end)
        parts.append(data)

        oldest = data.t.min()

        # الانتقال إلى ما قبل أقدم شمعة في الصفحة السابقة
        end = (
            oldest - timedelta(minutes=BAR)
        ).strftime("%Y-%m-%d %H:%M:%S")

        time.sleep(9)

    df = (
        pd.concat(parts)
        .drop_duplicates("t")
        .sort_values("t")
        .reset_index(drop=True)
    )

    if df.empty:
        raise RuntimeError("No historical candles downloaded.")

    df.to_csv("candles_5m.csv", index=False)

    print(
        f"\nHistory: {df.t.iloc[0]} -> {df.t.iloc[-1]}"
    )
    print(f"Total candles: {len(df)}")

    original_trend = C["trend"]

    try:
        for trend_filter in (False, True):

            C["trend"] = trend_filter

            name = (
                "WITH EMA 144 FILTER"
                if trend_filter
                else "WITHOUT EMA FILTER"
            )

            trades = simulate(df)

            report(trades, name)

            october_trades = [
                x for x in trades
                if pd.to_datetime(x["entry_time"]).date()
                >= datetime(2026, 10, 1).date()
            ]

            report(
                october_trades,
                name + " - OCTOBER",
            )

            filename = (
                "backtest_trend.csv"
                if trend_filter
                else "backtest_no_trend.csv"
            )

            pd.DataFrame(trades).to_csv(
                filename,
                index=False,
            )

    finally:
        C["trend"] = original_trend

    print("\nBacktest completed.")
    print("Files saved:")
    print("- candles_5m.csv")
    print("- backtest_trend.csv")
    print("- backtest_no_trend.csv")


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    mode = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "live"
    )

    if mode == "live":
        live()

    elif mode == "test":
        tg("✅ TS bot متصل بنجاح")

    elif mode == "backtest":
        pages = (
            int(sys.argv[2])
            if len(sys.argv) > 2
            else 6
        )

        backtest(pages)

    else:
        print("Usage:")
        print("python ts_bot.py live")
        print("python ts_bot.py test")
        print("python ts_bot.py backtest 6")

