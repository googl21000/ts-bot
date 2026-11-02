
import os
import time
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta

import ts_bot as bot


# ==================================================
# CONFIGURATION
# ==================================================

API_KEY = os.environ.get("TWELVEDATA_KEY", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

DAYS = int(os.environ.get("BT_DAYS", "90"))

OUTPUT_FILE = "backtest_trades.csv"

PAGE_SIZE = 5000
REQUEST_DELAY = 0.4

SPREAD = bot.SPREAD
BAR_MINUTES = bot.BAR


# ==================================================
# TELEGRAM
# ==================================================

def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError(
            "Missing TG_TOKEN or TG_CHAT GitHub Secrets"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TG_TOKEN}/sendMessage"
    )

    # Telegram message limit is 4096 characters.
    chunks = [
        message[i:i + 3900]
        for i in range(0, len(message), 3900)
    ]

    for chunk in chunks:
        response = requests.post(
            url,
            json={
                "chat_id": TG_CHAT,
                "text": chunk
            },
            timeout=30
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram sendMessage failed: {result}"
            )

    print("Telegram report sent successfully")


def send_csv_to_telegram():
    if not os.path.exists(OUTPUT_FILE):
        raise RuntimeError("CSV file was not created")

    url = (
        f"https://api.telegram.org/"
        f"bot{TG_TOKEN}/sendDocument"
    )

    with open(OUTPUT_FILE, "rb") as file:
        response = requests.post(
            url,
            data={"chat_id": TG_CHAT},
            files={"document": (OUTPUT_FILE, file)},
            timeout=60
        )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram sendDocument failed: {result}"
        )

    print("CSV sent to Telegram successfully")


# ==================================================
# HISTORICAL DATA
# ==================================================

def fetch_history(days):
    if not API_KEY:
        raise RuntimeError("TWELVEDATA_KEY is missing")

    end_time = datetime.now(timezone.utc)

    start_time = end_time - timedelta(days=days)

    cursor = end_time
    pages = []

    page_number = 0

    print(
        f"Downloading {days} days "
        f"of {bot.SYMBOL} data..."
    )

    while cursor > start_time:

        params = {
            "symbol": bot.SYMBOL,
            "interval": bot.TF,
            "outputsize": PAGE_SIZE,
            "timezone": "UTC",
            "end_date": cursor.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "apikey": API_KEY
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
            f"{len(page)} candles | "
            f"oldest: {oldest}"
        )

        if oldest <= start_time:
            break

        next_cursor = (
            oldest.to_pydatetime()
            - timedelta(seconds=1)
        )

        if next_cursor >= cursor:
            break

        cursor = next_cursor

        time.sleep(REQUEST_DELAY)

    if not pages:
        raise RuntimeError(
            "No historical candles downloaded"
        )

    df = pd.concat(
        pages,
        ignore_index=True
    )

    df = (
        df.drop_duplicates(subset=["t"])
        .sort_values("t")
        .reset_index(drop=True)
    )

    # Keep requested period only.
    df = df[
        (df["t"] >= start_time)
        & (df["t"] <= end_time)
    ].reset_index(drop=True)

    # Remove candles that have not closed yet.
    df = df[
        df["t"] + timedelta(minutes=BAR_MINUTES)
        <= end_time
    ].reset_index(drop=True)

    if len(df) < 100:
        raise RuntimeError(
            f"Not enough candles: {len(df)}"
        )

    print("\nHistorical data downloaded")
    print(f"Total candles: {len(df)}")
    print(f"From: {df.t.iloc[0]}")
    print(f"To:   {df.t.iloc[-1]}")

    return df


# ==================================================
# TRADE CREATION
# ==================================================

def new_trade(signal):
    return {
        **signal,
        "cur_sl": signal["sl"],
        "stage": 0,
        "pnl": 0.0,
        "closed": False,
        "seen": None
    }


# ==================================================
# TRADE SIMULATION
# ==================================================

def simulate_trade(trade, candle):

    # Use the exact step function from ts_bot.py.
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
        "reason": reason
    }


def expire_trade(trade, candle):

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
        "reason": "EXPIRED"
    }


# ==================================================
# TRADE RECORD
# ==================================================

def make_record(trade, result, close_time):

    gross = float(trade["pnl"])

    net = gross - SPREAD

    risk = abs(
        trade["entry"] - trade["sl"]
    )

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

        "risk": round(risk, 3)
    }


# ==================================================
# REPORT
# ==================================================

def report(records, candles, unfinished):

    columns = [
        "entry_time",
        "exit_time",
        "side",
        "entry",
        "initial_sl",
        "tp1",
        "tp2",
        "tp3",
        "exit_price",
        "exit_reason",
        "stage",
        "gross_pnl",
        "spread",
        "net_pnl",
        "risk"
    ]

    df = pd.DataFrame(
        records,
        columns=columns
    )

    # Always create CSV, even with zero trades.
    df.to_csv(
        OUTPUT_FILE,
        index=False
    )

    total = len(df)

    wins = 0
    losses = 0
    breakeven = 0

    net_profit = 0.0
    gross_profit = 0.0
    gross_loss = 0.0

    profit_factor = 0.0
    win_rate = 0.0
    avg_trade = 0.0
    avg_r = 0.0
    max_drawdown = 0.0

    if total > 0:

        wins = int(
            (df["net_pnl"] > 0).sum()
        )

        losses = int(
            (df["net_pnl"] < 0).sum()
        )

        breakeven = total - wins - losses

        net_profit = float(
            df["net_pnl"].sum()
        )

        gross_profit = float(
            df.loc[
                df["net_pnl"] > 0,
                "net_pnl"
            ].sum()
        )

        gross_loss = abs(
            float(
                df.loc[
                    df["net_pnl"] < 0,
                    "net_pnl"
                ].sum()
            )
        )

        if gross_loss > 0:
            profit_factor = (
                gross_profit / gross_loss
            )

        elif gross_profit > 0:
            profit_factor = float("inf")

        win_rate = wins / total * 100

        avg_trade = net_profit / total

        risk_values = df["risk"].replace(
            0,
            pd.NA
        )

        r_values = (
            df["net_pnl"] / risk_values
        ).dropna()

        if len(r_values):
            avg_r = float(r_values.mean())

        equity = df["net_pnl"].cumsum()

        peak = equity.cummax()

        drawdown = equity - peak

        max_drawdown = abs(
            float(drawdown.min())
        )

    # Build Telegram report.
    lines = [
        "📊 TS BOT BACKTEST RESULTS",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📅 Period: {DAYS} days",
        f"🕯 Candles: {len(candles)}",
        "",
        f"📌 Total completed trades: {total}",
        f"🟢 Wins: {wins}",
        f"🔴 Losses: {losses}",
        f"⚪ Breakeven: {breakeven}",
        f"⏳ Open at end: {unfinished}",
        "",
        f"🎯 Win Rate: {win_rate:.2f}%",
        f"💰 Net PnL: {net_profit:.2f}$",
        f"📈 Gross Profit: {gross_profit:.2f}$",
        f"📉 Gross Loss: {gross_loss:.2f}$",
        f"⚖️ Profit Factor: {profit_factor:.3f}",
        f"📊 Average Trade: {avg_trade:.2f}$",
        f"📐 Average R: {avg_r:.3f}",
        f"🔻 Max Drawdown: {max_drawdown:.2f}$",
        "",
        "📍 EXIT BREAKDOWN"
    ]

    if total:

        for reason, count in (
            df["exit_reason"].value_counts().items()
        ):
            lines.append(
                f"{reason}: {count}"
            )

        lines.extend([
            "",
            "📍 DIRECTION BREAKDOWN"
        ])

        for side, group in df.groupby("side"):

            side_win_rate = (
                group["net_pnl"] > 0
            ).mean() * 100

            lines.append(
                f"{side}: {len(group)} trades | "
                f"PnL: {group['net_pnl'].sum():.2f}$ | "
                f"Win Rate: {side_win_rate:.2f}%"
            )

    else:

        lines.extend([
            "No completed trades were found.",
            "Check the downloaded market data and strategy conditions."
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "TS bot | Backtest finished",
        "CSV file attached below."
    ])

    telegram_message = "\n".join(lines)

    print("\n" + telegram_message)

    # Send report and CSV to Telegram.
    send_telegram(telegram_message)

    send_csv_to_telegram()

    print(f"\nCSV saved: {OUTPUT_FILE}")


# ==================================================
# BACKTEST ENGINE
# ==================================================

def run_backtest():

    if DAYS < 1:
        raise ValueError("BT_DAYS must be at least 1")

    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError(
            "Telegram credentials are missing. "
            "Add TG_TOKEN and TG_CHAT to GitHub Secrets."
        )

    candles = fetch_history(DAYS)

    warmup = (
        bot.C["lookback"]
        + bot.C["sweep_win"]
        + bot.C["atr_n"]
        + 5
    )

    if len(candles) <= warmup:
        raise RuntimeError(
            "Not enough candles for strategy warmup"
        )

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
                "pnl": 0.0
            }

        stats = daily[day]

        # ------------------------------------------
        # MANAGE ACTIVE TRADE
        # ------------------------------------------

        if active is not None:

            entry_time = (
                pd.to_datetime(active["t"])
                .to_pydatetime()
            )

            if candle_time >= entry_time:

                events, result = simulate_trade(
                    active,
                    candle
                )

                for event in events:
                    print(
                        f"[{event}] "
                        f"{active['side'].upper()} "
                        f"@ {active['entry']} "
                        f"| {candle_time}"
                    )

                # Trade closed by TP or SL.
                if result is not None:

                    record = make_record(
                        active,
                        result,
                        candle_time
                    )

                    records.append(record)

                    stats["pnl"] += (
                        record["net_pnl"]
                    )

                    if result["reason"] == "SL":
                        stats["sl"] += 1

                    cooldown_until = (
                        candle_time
                        + timedelta(
                            minutes=bot.C["cool_min"]
                        )
                    )

                    print(
                        f"[CLOSED] {result['reason']} "
                        f"| Net PnL: "
                        f"{record['net_pnl']:.2f}$"
                    )

                    active = None

            # --------------------------------------
            # EXPIRATION AFTER 3 HOURS
            # --------------------------------------

            if active is not None:

                elapsed = (
                    candle_time - entry_time
                )

                if (
                    elapsed >= timedelta(
                        hours=bot.C["expire_h"]
                    )
                    and active["stage"] == 0
                ):

                    result = expire_trade(
                        active,
                        candle
                    )

                    record = make_record(
                        active,
                        result,
                        candle_time
                    )

                    records.append(record)

                    stats["pnl"] += (
                        record["net_pnl"]
                    )

                    cooldown_until = (
                        candle_time
                        + timedelta(
                            minutes=bot.C["cool_min"]
                        )
                    )

                    print(
                        f"[EXPIRED] "
                        f"Net PnL: "
                        f"{record['net_pnl']:.2f}$"
                    )

                    active = None

        # ------------------------------------------
        # DO NOT OPEN A SECOND TRADE
        # ------------------------------------------

        if active is not None:
            continue

        # ------------------------------------------
        # DAILY RESTRICTIONS
        # ------------------------------------------

        signal_time = (
            candle_time
            + timedelta(minutes=BAR_MINUTES)
        )

        signal_day = signal_time.date().isoformat()

        if signal_day not in daily:

            daily[signal_day] = {
                "signals": 0,
                "sl": 0,
                "pnl": 0.0
            }

        stats = daily[signal_day]

        if not bot.in_session(signal_time):
            continue

        if stats["signals"] >= bot.C["max_signals"]:
            continue

        if stats["sl"] >= bot.C["max_sl"]:
            continue

        if stats["pnl"] >= bot.C["day_target"]:
            continue

        if (
            cooldown_until is not None
            and signal_time < cooldown_until
        ):
            continue

        # ------------------------------------------
        # CHECK STRATEGY SIGNAL
        # ------------------------------------------

        window = candles.iloc[:i + 1].copy()

        signal = bot.signal(window)

        if signal is None:
            continue

        active = new_trade(signal)

        stats["signals"] += 1

        print(
            f"[SIGNAL] "
            f"{signal['side'].upper()} "
            f"@ {signal['entry']} "
            f"| {signal['t']}"
        )

    # ----------------------------------------------
    # OPEN TRADE AT END OF DATA
    # ----------------------------------------------

    unfinished = 0

    if active is not None:

        unfinished = 1

        print(
            "[OPEN AT END] "
            f"{active['side'].upper()} "
            f"@ {active['entry']} "
            "| Not counted as a completed trade"
        )

    # ----------------------------------------------
    # FINAL REPORT
    # ----------------------------------------------

    report(
        records,
        candles,
        unfinished
    )


# ==================================================
# MAIN
# ==================================================

if __name__ == "__main__":

    print("=" * 45)
    print("TS BOT BACKTEST STARTED")
    print("=" * 45)

    try:
        run_backtest()

    except Exception as error:

        print("\nBACKTEST ERROR:", error)

        # Try to notify Telegram about failure.
        if TG_TOKEN and TG_CHAT:

            try:
                send_telegram(
                    "❌ TS BOT BACKTEST FAILED\n\n"
                    f"Error:\n{str(error)[:3000]}"
                )

            except Exception as telegram_error:
                print(
                    "Telegram error:",
                    telegram_error
                )

        raise
