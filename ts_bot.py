"""
TS Strategy Bot - XAUUSD
Time (NY golden candles) + Sweep + MSS + OB/FVG entry -> Telegram alert.
All times are New York time.

Env vars (same names as the GitHub secrets):
  TWELVEDATA_KEY, TG_TOKEN, TG_CHAT
  ANTHROPIC_API_KEY   (optional: adds a short Claude review to each alert)
  ONE_SHOT=1          (run as a session: exit when no golden window is active/upcoming)

Usage:
  python ts_bot.py            # loop
  python ts_bot.py --once     # single scan
  python ts_bot.py --test     # send a Telegram test message

Not financial advice.
"""
import argparse
import json
import os
import time
from datetime import timedelta

import pandas as pd
import requests

# ============================ SETTINGS ============================
NY = "America/New_York"
SYMBOL = "XAU/USD"
GOLDEN = [(2, 0), (2, 30), (7, 0), (7, 30), (9, 0), (9, 30)]

HTF_POI_REQUIRED = True    # sweep must happen inside a 30m FVG/OB
ZONE_TOL = 2.0             # tolerance (USD) for "inside the 30m zone"
ENTRY_MODE = "proximal"    # "proximal" = near edge of the entry zone | "mid" = middle
MIN_RR_TP1 = 1.0           # minimum reward/risk for TP1
CANCEL_HALF_IF = "sweep"   # cancel the :30 candle if the :00 candle ... "sweep" | "setup"
SWING_K_MSS = 2            # swing strength used for the MSS reference (3m)
SWING_K_TP = 5             # swing strength used for TP liquidity (3m)
WINDOW_AFTER = timedelta(minutes=60)   # how long a golden candle stays active
UPCOMING_MIN = 35          # session start buffer before a golden candle
POLL_SECONDS = 60
SEEN_FILE = "seen.json"

TD_KEY = os.getenv("TWELVEDATA_KEY", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT", "")
AI_KEY = os.getenv("ANTHROPIC_API_KEY", "")

M1 = pd.Timedelta(minutes=1)
M3 = pd.Timedelta(minutes=3)
M30 = pd.Timedelta(minutes=30)


# ============================== DATA ==============================
def fetch_1m(now=None):
    """Twelve Data has no 3min interval, so we pull 1min and resample."""
    now = now if now is not None else pd.Timestamp.now(tz=NY)
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params=dict(symbol=SYMBOL, interval="1min", outputsize=3000,
                    timezone=NY, apikey=TD_KEY),
        timeout=25,
    )
    j = r.json()
    if "values" not in j:
        raise RuntimeError(j)
    df = pd.DataFrame(j["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    df.index = df.index.tz_localize(NY, ambiguous="NaT", nonexistent="shift_forward")
    df = df[df.index.notna()]
    df = df[["open", "high", "low", "close"]].astype(float)
    return df[df.index + M1 <= now]   # drop the minute that is still forming


def resample(df1, rule):
    r = df1.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    end = r.index + pd.Timedelta(rule)
    return r[end <= df1.index[-1] + M1]   # keep only completed candles


def flip(df):
    """Mirror prices so the SELL logic can also detect BUY setups."""
    o = df.copy()
    o["open"], o["close"] = -df["open"], -df["close"]
    o["high"], o["low"] = -df["low"], -df["high"]
    return o


# ============================ STRUCTURE ============================
def swing_lows(low, k):
    return [i for i in range(k, len(low) - k) if low[i] <= min(low[i - k:i + k + 1])]


def bearish_zones(df):
    """Bearish FVG and OB zones: (end_time, low, high, kind)."""
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    zones = []
    for i in range(2, len(df)):
        if l[i - 2] > h[i]:
            zones.append((df.index[i], h[i], l[i - 2], "FVG"))
            for j in range(i - 2, max(i - 7, -1), -1):
                if c[j] > o[j]:
                    zones.append((df.index[i], l[j], h[j], "OB"))
                    break
    return zones


def htf_poi(df30, t0, sweep_high):
    hist = df30[df30.index + M30 <= t0].tail(60)
    for _, zlow, zhigh, kind in reversed(bearish_zones(hist)):
        if zlow <= sweep_high <= zhigh + ZONE_TOL:
            return {"kind": kind, "low": zlow, "high": zhigh}
    return None


# ========================= SETUP DETECTION =========================
def detect_sell(df3, df30, t0):
    prev_t = t0 - M30
    if prev_t not in df30.index:
        return None
    prev_high = df30.loc[prev_t, "high"]
    idx = df3.index
    O, H, L, C = (df3[x].values for x in ("open", "high", "low", "close"))
    n = len(df3)

    win = [i for i in range(n) if t0 <= idx[i] < t0 + M30]
    a = next((i for i in win if H[i] > prev_high), None)              # first break of the high
    if a is None:
        return None
    c = next((i for i in win if i >= a and C[i] < prev_high), None)   # close back inside
    if c is None:
        return None

    # MSS reference: last 3m swing low before the sweep
    lows = [p for p in swing_lows(L[:a], SWING_K_MSS) if p >= a - 60]
    ref = L[lows[-1]] if lows else L[max(0, a - 10):a].min()
    m = next((i for i in range(c, n) if C[i] < ref), None)
    if m is None:
        return None

    h_pos = a + int(H[a:m + 1].argmax())
    sweep = H[h_pos]

    # entry zone: last bearish FVG in the displacement, else the last up-candle (OB)
    zone = None
    for i in range(h_pos + 2, m + 1):
        if L[i - 2] > H[i]:
            zone = (H[i], L[i - 2], "FVG")
    if zone is None:
        for j in range(m - 1, a - 1, -1):
            if C[j] > O[j]:
                zone = (L[j], H[j], "OB")
                break
    if zone is None:
        return None

    entry = zone[0] if ENTRY_MODE == "proximal" else (zone[0] + zone[1]) / 2
    sl = sweep
    if entry >= sl:
        return None
    risk = sl - entry

    # targets: untouched lows on the left (before the sweep)
    cands = []
    for p in swing_lows(L[:a], SWING_K_TP):
        price = L[p]
        if price < entry and L[p + 1:m + 1].min() > price:
            cands.append(price)
    cands.sort(reverse=True)
    tp1 = next((p for p in cands if (entry - p) / risk >= MIN_RR_TP1), None)
    if tp1 is None:
        return None
    tp2 = next((p for p in cands if p < tp1), None)
    if tp2 is None:
        return None

    status = "pending"
    for i in range(m + 1, n):
        if status == "pending":
            if H[i] >= sl:
                status = "invalid"
                break
            if L[i] <= tp1:
                status = "missed"
                break
            if H[i] >= entry:
                status = "active"
        else:
            if H[i] >= sl:
                status = "stopped"
                break
            if L[i] <= tp1:
                status = "tp1"
                break

    return dict(entry=entry, sl=sl, tp1=tp1, tp2=tp2, sweep=sweep, mss=ref,
                zone=zone, status=status, mss_time=idx[m], risk=risk,
                sweep_time=idx[a])


def analyze(df3, df30, t0):
    out = []
    for side in ("sell", "buy"):
        d3, d30 = (df3, df30) if side == "sell" else (flip(df3), flip(df30))
        r = detect_sell(d3, d30, t0)
        if not r:
            continue
        poi = htf_poi(d30, t0, r["sweep"])
        if HTF_POI_REQUIRED and not poi:
            continue
        if side == "buy":
            for k in ("entry", "sl", "tp1", "tp2", "sweep", "mss"):
                r[k] = -r[k]
            z = r["zone"]
            r["zone"] = (-z[1], -z[0], z[2])
            if poi:
                poi = {"kind": poi["kind"], "low": -poi["high"], "high": -poi["low"]}
        r["poi"], r["side"], r["t0"] = poi, side, t0
        out.append(r)
    if len(out) > 1:   # both high and low swept: keep the earlier one
        out = [min(out, key=lambda x: x["sweep_time"])]
    return out


def half_cancelled(df3, df30, t0):
    """The :30 candle is cancelled when the :00 candle before it already took liquidity."""
    if t0.minute != 30:
        return False
    first, prev = t0 - M30, t0 - 2 * M30
    if first not in df30.index or prev not in df30.index:
        return False
    if CANCEL_HALF_IF == "setup":
        return bool(analyze(df3, df30, first))
    f, p = df30.loc[first], df30.loc[prev]
    return bool(f["high"] > p["high"] or f["low"] < p["low"])


# ============================== TIMING ==============================
def active_windows(now):
    if now.weekday() >= 5:
        return []
    out = []
    for h, m in GOLDEN:
        t0 = now.normalize().replace(hour=h, minute=m)
        if t0 <= now < t0 + WINDOW_AFTER:
            out.append(t0)
    return out


def upcoming(now):
    if now.weekday() >= 5:
        return False
    for h, m in GOLDEN:
        t0 = now.normalize().replace(hour=h, minute=m)
        if now < t0 <= now + timedelta(minutes=UPCOMING_MIN):
            return True
    return False


def scan(df1, now):
    windows = active_windows(now)
    if not windows:
        return []
    df3, df30 = resample(df1, "3min"), resample(df1, "30min")
    out = []
    for t0 in windows:
        if half_cancelled(df3, df30, t0):
            continue
        out += analyze(df3, df30, t0)
    return out


# ============================ NOTIFICATIONS ============================
def telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text, flush=True)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data=dict(chat_id=TG_CHAT, text=text), timeout=20)
        if r.status_code != 200:
            print("telegram error:", r.status_code, r.text[:200], flush=True)
    except Exception as e:
        print("telegram exception:", e, flush=True)


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
                "content": "You review XAUUSD setups of a liquidity-sweep + MSS strategy. "
                           "Reply in Arabic, max 3 short lines: strengths, risks, score out of 10.\n"
                           + json.dumps(setup, default=str)}]),
            timeout=30)
        return "\n\nClaude review:\n" + r.json()["content"][0]["text"]
    except Exception as e:
        print("ai_review error:", e, flush=True)
        return ""


def format_msg(r):
    side = "SELL \U0001F534" if r["side"] == "sell" else "BUY \U0001F7E2"
    rr1 = abs(r["entry"] - r["tp1"]) / r["risk"]
    rr2 = abs(r["entry"] - r["tp2"]) / r["risk"]
    z, poi = r["zone"], r["poi"]
    if poi:
        poi_txt = "%s 30m [%.2f - %.2f]" % (poi["kind"], poi["low"], poi["high"])
    else:
        poi_txt = "none"
    state = ("Limit order pending (price has not returned yet)" if r["status"] == "pending"
             else "Price already touched the entry zone")
    return (
        f"{side} XAUUSD\n"
        f"Golden candle: {r['t0']:%H:%M} NY | Sweep {r['sweep_time']:%H:%M} | MSS {r['mss_time']:%H:%M}\n"
        f"HTF zone: {poi_txt}\n"
        f"Entry zone (3m): {z[2]} [{z[0]:.2f} - {z[1]:.2f}]\n\n"
        f"Entry: {r['entry']:.2f}\n"
        f"SL: {r['sl']:.2f}\n"
        f"TP1: {r['tp1']:.2f} (RR {rr1:.1f})\n"
        f"TP2: {r['tp2']:.2f} (RR {rr2:.1f})\n\n"
        f"Status: {state}\n"
        f"Not financial advice."
    )


def load_seen():
    try:
        return set(json.load(open(SEEN_FILE)))
    except Exception:
        return set()


def save_seen(s):
    try:
        json.dump(sorted(s), open(SEEN_FILE, "w"))
    except Exception:
        pass


# =============================== MAIN ===============================
def run_scan(now, seen):
    df1 = fetch_1m(now)
    found = scan(df1, now)
    sent = 0
    for r in found:
        if r["status"] not in ("pending", "active"):
            continue
        key = f"{r['t0'].isoformat()}_{r['side']}"
        if key in seen:
            continue
        seen.add(key)
        save_seen(seen)
        telegram(format_msg(r) + ai_review(r))
        sent += 1
    return len(found), sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        telegram("TS Bot: Telegram connection OK")
        return

    one_shot = bool(os.getenv("ONE_SHOT"))
    seen = load_seen()
    print("TS bot started", flush=True)
    while True:
        try:
            now = pd.Timestamp.now(tz=NY)
            busy = bool(active_windows(now))
            if one_shot and not busy and not upcoming(now):
                print("no golden window now, exiting", flush=True)
                return
            if busy:
                found, sent = run_scan(now, seen)
                print(f"[{now:%H:%M} NY] scanned: setups={found} sent={sent}", flush=True)
            else:
                print(f"[{now:%H:%M} NY] waiting for the next golden candle", flush=True)
        except Exception as e:
            print("error:", e, flush=True)
        if args.once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
