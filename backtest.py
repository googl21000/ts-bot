
import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

import ts_bot as bot


# ================= CONFIG =================

API_KEY = os.environ.get("TWELVEDATA_KEY", "")
DAYS = int(os.environ.get("BT_DAYS", "90"))

OUTPUT_FILE = "backtest_trades.csv"

PAGE_SIZE = 5000
REQUEST_DELAY = 0.3

SPREAD = bot.SPREAD
BAR_MINUTES = bot.BAR


# ================= DATA DOWNLOAD =================

def fetch_history(days):
    if not API_KEY:
        raise RuntimeError("TWELVEDATA_KEY is missing")

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)

    cursor = end_time
    pages = []
    page_number = 0

    print(f"Downloading {days} days of {bot.SYMBOL} data...")

    while cursor > start_time:
        params = {
            "symbol": bot.SYMBOL,
            "interval": bot.TF,
            "outputsize": PAGE_SIZE,
            "timezone": "UTC",
            "end_date": cursor.strftime("%Y-%m-%d %H:%M:%S"),
            "apikey": API_KEY,
        }

        response = requests.get(
            "https://api.twelvedata.com/time_series",
            params=params,
            timeout=60
        )

        response.raise_for_status()
        result = response.json()

        if "values" not in result:
            raise RuntimeError(
                f"TwelveData error: {result}"
            )

        page = pd.DataFrame(result["values"])

        if page.empty:
            break

        page["t"] = pd.to_datetime(
            page["datetime"],
            utc=True
        )

        for col in ("open", "high", "low", "close"):
            page[col] = pd.to_numeric(
                page[col],
                errors="coerce"
            )

        page = page[
            ["t", "open", "high", "low", "close"]
        ].dropna()

        if page.empty:
            break

        pages.append(page)

        oldest = page["t"].min()
        page_number += 1

        print(
            f"Page {page_number}: "
            f"{len(page)} candles | oldest: {oldest}"
        )

        if oldest <= start_time:
            break

        next_cursor = oldest.to_pydatetime() - timedelta(seconds=1)

        if next_cursor >= cursor:
            break

        cursor = next_cursor
        time.sleep(REQUEST_DELAY)

    if not pages:
        raise RuntimeError("No historical candles downloaded")

    df = pd.concat(pages, ignore_index=True)

    df = (
        df.drop_duplicates(subset=["t"])
        .sort_values("t")
        .reset_index(drop=True)
    )

    df = df[
        (df["t"] >= start_time)
        & (df["t"] <= end_time)
    ].reset_index(drop=True)

    if len(df) < 100:
        raise RuntimeError(
            f"Not enough candles: {len(df)}"
        )

    print(f"\nTotal candles: {len(df)}")
    print(f"From: {df.t.iloc[0]}")
    print(f"To:   {df.t.iloc[-1]}")

    return df


# ================= TRADE SIMULATION =================

def new_trade(signal):
    return {
        **signal,
        "cur_sl": signal["sl"],
        "stage": 0,
        "pnl": 0.0,
        "closed": False,
        "seen": None,
    }


def simulate_trade(trade, candle):
    events = bot.step(trade, candle)

    if not trade["closed"]:
        return events, None

    if trade["stage"] == 3:
        exit_price = trade["tps"][2]
        reason = "TP3"

    elif trade["stage"] == 0:
        exit_price = trade["sl"]
        reason = "SL"

    else:
        exit_price = trade["entry"]
        reason = "BE"

    return events, {
        "exit_price": exit_price,
        "reason": reason,
    }


def expire_trade(trade, candle):
    """
    انتهاء 3 ساعات:
    إغلاق الجزء المتبقي عند إغلاق آخر شمعة.
    """

    buy = trade["side"] == "buy"
    direction = 1 if buy else -1

    remaining = (3 - trade["stage"]) / 3

    trade["pnl"] += (
        remaining
        * direction
        * (float(candle.close) - trade["entry"])
    )

    trade["closed"] = True

    return {
        "exit_price": float(candle.close),
        "reason": "EXPIRED",
    }


# ================= RESULTS =================

def make_record(trade, result, close_time):
    gross = float(trade["pnl"])
    net = gross - SPREAD

    return {
        "entry_time": trade["t"],
        "exit_time": close_time.isoformat(),
        "side": trade["side"].upper(),
        "entry": trade["entry"],
        "initial_sl": trade["sl"],
        "tp1": trade["tps"][0],
        "tp2": trade["tps"][1],
        "tp3": trade["tps"][2],
        "exit_price": result["exit_price"],
        "exit_reason": result["reason"],
        "stage": trade["stage"],
        "gross_pnl": round(gross, 3),
        "spread": SPREAD,
        "net_pnl": round(net, 3),
        "risk": round(
            abs(trade["entry"] - trade["sl"]), 3
        ),
    }


def report(records, candles):
    if not records:
        print("\nNo completed trades.")
        return

    df = pd.DataFrame(records)

    total = len(df)

    wins = int((df.net_pnl > 0).sum())
    losses = int((df.net_pnl < 0).sum())
    breakeven = total - wins - losses

    net_profit = float(df.net_pnl.sum())

    gross_profit = float(
        df.loc[df.net_pnl > 0, "net_pnl"].sum()
    )

    gross_loss = abs(float(
        df.loc[df.net_pnl < 0, "net_pnl"].sum()
    ))

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0 else float("inf")
    )

    win_rate = wins / total * 100

    avg_trade = net_profit / total

    df["cumulative_pnl"] = df.net_pnl.cumsum()

    df["equity_peak"] = (
        df["cumulative_pnl"].cummax()
    )

    df["drawdown"] = (
        df["cumulative_pnl"] - df["equity_peak"]
    )

    max_drawdown = abs(float(df.drawdown.min()))

    risk_values = df["risk"].replace(0, pd.NA)

    df["R"] = df["net_pnl"] / risk_values

    avg_r = float(df["R"].dropna().mean())

    print("\n" + "=" * 45)
    print("TS BOT BACKTEST RESULTS")
    print("=" * 45)

    print(f"Candles:          {len(candles)}")
    print(f"Total trades:     {total}")
    print(f"Wins:             {wins}")
    print(f"Losses:           {losses}")
    print(f"Breakeven:        {breakeven}")
    print(f"Win rate:         {win_rate:.2f}%")
    print(f"Net PnL:          {net_profit:.2f}")
    print(f"Gross profit:     {gross_profit:.2f}")
    print(f"Gross loss:       {gross_loss:.2f}")
    print(f"Profit factor:    {profit_factor:.2f}")
    print(f"Average trade:    {avg_trade:.2f}")
    print(f"Average R:        {avg_r:.3f}")
    print(f"Max drawdown:     {max_drawdown:.2f}")

    print("\nExit reasons:")

    for reason, count in df.exit_reason.value_counts().items():
        print(f"{reason}: {count}")

    print("\nBy direction:")

    for side, group in df.groupby("side"):
        print(
            f"{side}: "
            f"{len(group)} trades | "
            f"PnL: {group.net_pnl.sum():.2f} | "
            f"Win rate: "
            f"{(group.net_pnl > 0).mean() * 100:.2f}%"
        )

    df.to_csv(OUTPUT_FILE, index=False)

    print(f"\nCSV saved: {OUTPUT_FILE}")


# ================= BACKTEST ENGINE =================

def run_backtest():
    candles = fetch_history(DAYS)

    warmup = (
        bot.C["lookback"]
        + bot.C["sweep_win"]
        + bot.C["atr_n"]
        + 5
    )

    if len(candles) <= warmup:
        raise RuntimeError("Not enough candles for strategy warmup")

    records = []

    active = None
    cooldown_until = None

    daily = {}

    print("\nRunning TS bot strategy...")

    for i in range(warmup, len(candles)):
        candle = candles.iloc[i]
        candle_time = candle.t.to_pydatetime()

        day = candle_time.date().isoformat()

        if day not in daily:
            daily[day] = {
                "signals": 0,
                "sl": 0,
                "pnl": 0.0,
            }

        stats = daily[day]

        # -------- Manage open trade --------

        if active is not None:
            entry_time = pd.to_datetime(active["t"]).to_pydatetime()

            if candle_time >= entry_time:
                events, result = simulate_trade(
                    active,
                    candle
                )

                if result is not None:
                    record = make_record(
                        active,
                        result,
                        candle_time
                    )

                    records.append(record)

                    stats["pnl"] += record["net_pnl"]

                    if result["reason"] == "SL":
                        stats["sl"] += 1

                    cooldown_until = (
                        candle_time
                        + timedelta(minutes=bot.C["cool_min"])
                    )

                    active = None

            # -------- Expiration --------

            if active is not None:
                elapsed = candle_time - entry_time

                if (
                    elapsed >= timedelta(hours=bot.C["expire_h"])
                    and active["stage"] == 0
                ):
                    result = expire_trade(active, candle)

                    record = make_record(
                        active,
                        result,
                        candle_time
                    )

                    records.append(record)

                    stats["pnl"] += record["net_pnl"]

                    cooldown_until = (
                        candle_time
                        + timedelta(minutes=bot.C["cool_min"])
                    )

                    active = None

        # -------- New signal --------

        if active is not None:
            continue

        if not bot.in_session(candle_time + timedelta(minutes=BAR_MINUTES)):
            continue

        if stats["signals"] >= bot.C["max_signals"]:
            continue

        if stats["sl"] >= bot.C["max_sl"]:
            continue

        if stats["pnl"] >= bot.C["day_target"]:
            continue

        if (
            cooldown_until is not None
            and candle_time < cooldown_until
        ):
            continue

        window = candles.iloc[:i + 1].copy()

        signal = bot.signal(window)

        if signal is None:
            continue

        active = new_trade(signal)
        stats["signals"] += 1

        print(
            f"[SIGNAL] {signal['side'].upper()} "
            f"@ {signal['entry']} | "
            f"{signal['t']}"
        )

    # -------- Close remaining open trade --------

    if active is not None:
        last_candle = candles.iloc[-1]

        result = expire_trade(
            active,
            last_candle
        )

        result["reason"] = "END_OF_DATA"

        records.append(
            make_record(
                active,
                result,
                last_candle.t.to_pydatetime()
            )
        )

    report(records, candles)


# ================= MAIN =================

if __name__ == "__main__":
    run_backtest()
