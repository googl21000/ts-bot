
import os
import time
import requests
import pandas as pd

from datetime import datetime, timedelta, timezone
import ts_bot as bot


# ==================================================
# TS BOT BACKTEST CONFIG
# ==================================================

BT_DAYS = int(os.environ.get("BT_DAYS", "90"))

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

SPREAD = bot.SPREAD
BAR = bot.BAR

CSV_FILE = "backtest_trades.csv"


# ==================================================
# TELEGRAM
# ==================================================

def telegram_message(message):
    if not TG_TOKEN or not TG_CHAT:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": message
            },
            timeout=30
        ).raise_for_status()

    except Exception as error:
        print("Telegram message error:", error)


def telegram_file(filename):
    if not TG_TOKEN or not TG_CHAT:
        return

    try:
        with open(filename, "rb") as file:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT},
                files={"document": file},
                timeout=60
            ).raise_for_status()

    except Exception as error:
        print("Telegram file error:", error)


# ==================================================
# DOWNLOAD HISTORICAL DATA
# ==================================================

def download_history(days):

    if days <= 0:
        raise ValueError("BT_DAYS must be greater than zero")

    end = None
    parts = []

    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=days)

    # حوالي 288 شمعة في اليوم على إطار 5 دقائق
    max_pages = max(2, int(days * 288 / 4500) + 3)

    print(f"Downloading last {days} days...")

    previous_oldest = None

    for page in range(max_pages):

        print(f"Downloading page {page + 1}/{max_pages}")

        data = bot.fetch(5000, end)

        if data is None or data.empty:
            print("No more candles available.")
            break

        oldest = data["t"].min()

        if previous_oldest is not None and oldest >= previous_oldest:
            print("Pagination stopped: no older candles returned.")
            break

        parts.append(data)
        previous_oldest = oldest

        if oldest <= pd.Timestamp(start_date):
            break

        end = (
            oldest - timedelta(minutes=BAR)
        ).strftime("%Y-%m-%d %H:%M:%S")

        time.sleep(9)

    if not parts:
        raise RuntimeError("No historical candles downloaded")

    df = (
        pd.concat(parts)
        .drop_duplicates(subset=["t"])
        .sort_values("t")
        .reset_index(drop=True)
    )

    df = df[
        (df["t"] >= pd.Timestamp(start_date))
        & (df["t"] <= pd.Timestamp(now))
    ].reset_index(drop=True)

    if df.empty:
        raise RuntimeError("No candles inside requested date range")

    print(
        f"History: {df.t.iloc[0]} -> {df.t.iloc[-1]}"
    )
    print(f"Total candles: {len(df)}")

    return df


# ==================================================
# SIMULATION
# ==================================================

def simulate(df):

    trades = []

    active = None

    current_day = None
    daily_signals = 0
    daily_sl = 0
    daily_pnl = 0.0

    cooldown = None

    for i in range(len(df)):

        candle = df.iloc[i]

        # Reset daily limits using UTC date
        if candle.t.date() != current_day:

            current_day = candle.t.date()

            daily_signals = 0
            daily_sl = 0
            daily_pnl = 0.0

        # ------------------------------------------
        # Manage existing trade
        # ------------------------------------------

        if active is not None:

            bot.step(active, candle)

            # Trade closed by strategy
            if active["closed"]:

                gross = active["pnl"]
                net = gross - SPREAD

                if active["stage"] == 0:
                    exit_reason = "SL"
                    daily_sl += 1

                elif active["stage"] >= len(active["tps"]):
                    exit_reason = "TP2"

                else:
                    exit_reason = "BE"

                daily_pnl += net

                trades.append({
                    "entry_time": active["t"],
                    "exit_time": candle.t.isoformat(),
                    "side": active["side"],
                    "entry": active["entry"],
                    "sl": active["sl"],
                    "tp1": active["tps"][0],
                    "tp2": active["tps"][1],
                    "stage": active["stage"],
                    "exit_reason": exit_reason,
                    "gross_pnl": round(gross, 3),
                    "spread": SPREAD,
                    "net_pnl": round(net, 3),
                    "net_pips": round(net * 10, 1)
                })

                cooldown = candle.t
                active = None

            # --------------------------------------
            # Expiration: same 3-hour rule
            # --------------------------------------

            elif active["stage"] == 0:

                entry_time = pd.to_datetime(active["t"])

                if candle.t - entry_time > timedelta(
                    hours=bot.C["expire_h"]
                ):

                    # Approximate manual close at candle close
                    direction = (
                        1 if active["side"] == "buy" else -1
                    )

                    gross = (
                        direction
                        * (candle.close - active["entry"])
                    )

                    net = gross - SPREAD

                    daily_pnl += net

                    trades.append({
                        "entry_time": active["t"],
                        "exit_time": candle.t.isoformat(),
                        "side": active["side"],
                        "entry": active["entry"],
                        "sl": active["sl"],
                        "tp1": active["tps"][0],
                        "tp2": active["tps"][1],
                        "stage": active["stage"],
                        "exit_reason": "EXPIRE",
                        "gross_pnl": round(gross, 3),
                        "spread": SPREAD,
                        "net_pnl": round(net, 3),
                        "net_pips": round(net * 10, 1)
                    })

                    cooldown = candle.t
                    active = None

        # ------------------------------------------
        # Find new signal
        # ------------------------------------------

        if active is None:

            blocked = (
                daily_signals >= bot.C["max_signals"]
                or daily_sl >= bot.C["max_sl"]
                or daily_pnl >= bot.C["day_target"]
            )

            if blocked:
                continue

            if cooldown is not None:

                if candle.t - cooldown < timedelta(
                    minutes=bot.C["cool_min"]
                ):
                    continue

            # Use history up to current candle
            history = df.iloc[max(0, i - 300):i + 1]

            signal = bot.signal(history)

            if signal:

                active = dict(
                    signal,
                    cur_sl=signal["sl"],
                    stage=0,
                    pnl=0.0,
                    closed=False
                )

                daily_signals += 1

    return trades, active


# ==================================================
# REPORT
# ==================================================

def make_report(trades, days, trend):

    if not trades:
        return f"TS BOT BACKTEST\nDays: {days}\nNo completed trades."

    df = pd.DataFrame(trades)

    pnl = df["net_pips"]

    wins = df[pnl > 0]
    losses = df[pnl < 0]

    gross_profit = wins["net_pips"].sum()
    gross_loss = abs(losses["net_pips"].sum())

    pf = (
        gross_profit / gross_loss
        if gross_loss > 0
        else float("inf")
    )

    equity = pnl.cumsum()
    drawdown = equity.cummax() - equity

    win_rate = len(wins) / len(df) * 100

    lines = [
        "📊 TS BOT BACKTEST",
        f"Period: {days} days",
        f"Trend EMA 144: {'ON' if trend else 'OFF'}",
        "",
        f"Total trades: {len(df)}",
        f"Wins: {len(wins)}",
        f"Losses: {len(losses)}",
        f"Win rate: {win_rate:.2f}%",
        "",
        f"Net PnL: {pnl.sum():.1f} pips",
        f"Gross profit: {gross_profit:.1f} pips",
        f"Gross loss: {gross_loss:.1f} pips",
        f"Average trade: {pnl.mean():.2f} pips",
        f"Profit Factor: {pf:.3f}",
        f"Max Drawdown: {drawdown.max():.1f} pips",
        "",
        "EXIT REASONS:"
    ]

    for reason, count in df["exit_reason"].value_counts().items():
        lines.append(f"{reason}: {count}")

    lines.append("")
    lines.append("DIRECTION:")

    for side in ("buy", "sell"):

        subset = df[df["side"] == side]

        if len(subset):

            side_wins = (subset["net_pips"] > 0).sum()

            lines.append(
                f"{side.upper()}: {len(subset)} trades | "
                f"PnL {subset['net_pips'].sum():.1f} pips | "
                f"Win rate {side_wins / len(subset) * 100:.2f}%"
            )

    return "\n".join(lines)


# ==================================================
# MAIN BACKTEST
# ==================================================

def backtest():

    original_trend = bot.C["trend"]

    try:

        df = download_history(BT_DAYS)

        df.to_csv("candles_5m.csv", index=False)

        all_results = []

        for trend in (False, True):

            bot.C["trend"] = trend

            print("\n" + "=" * 45)
            print("TREND FILTER:", trend)
            print("=" * 45)

            trades, unfinished = simulate(df)

            result = make_report(
                trades,
                BT_DAYS,
                trend
            )

            print("\n" + result)

            suffix = "trend" if trend else "no_trend"

            filename = f"backtest_{suffix}.csv"

            pd.DataFrame(trades).to_csv(
                filename,
                index=False
            )

            if unfinished is not None:
                print(
                    f"Open trade at end of test: "
                    f"{unfinished['side'].upper()} "
                    f"@ {unfinished['entry']}"
                )

            all_results.append(result)

        # Combined CSV from the final EMA-enabled run
        # Each configuration also has its own CSV.

        print("\nBACKTEST COMPLETED SUCCESSFULLY")

        telegram_message(
            "\n\n".join(all_results)
        )

        telegram_file("backtest_trend.csv")

        telegram_file("backtest_no_trend.csv")

    finally:
        bot.C["trend"] = original_trend


# ==================================================
# ERROR HANDLING
# ==================================================

if __name__ == "__main__":

    try:
        backtest()

    except Exception as error:

        import traceback

        details = traceback.format_exc()

        print(details)

        telegram_message(
            "❌ TS BOT BACKTEST FAILED\n\n"
            f"Error:\n{error}\n\n"
            f"{details[-2500:]}"
        )

        raise

