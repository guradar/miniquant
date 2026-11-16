#!/usr/bin/env python3
"""After-close updater for the stock watchlist.

Intended to be run by cron after the stock market closes (e.g. US/Eastern
16:30 on weekdays).  For every stock in ``watchlist.json`` it:

* fetches 1 year of daily bars via ``yfinance``,
* sets  price  = last close,
* sets  ma5    = average of the last 5 closes,
* sets  ma20   = average of the last 20 closes,
* sets  renko  = Renko sequence label of the last 3 segments,
                 e.g. "4D6U8D"  (U = up brick, D = down brick,
                 brick size = ATR(14), see yfrk_db.py),
* sets  trend  = "Up" / "Down" / "Flat"  (MA5 vs MA20),
* sets  signal = "Buy" / "Sell" / "Hold" (price vs MA5, MA5 vs MA20).

The file is written atomically under an exclusive lock, so this script is
safe to run while the Flask app (``app.py``) is serving.

Usage:
    yf_env/bin/python update_prices.py            # normal run
    yf_env/bin/python update_prices.py --force    # skip market-hours check
    yf_env/bin/python update_prices.py --file /path/to/watchlist.json

Example crontab line (Mon-Fri 16:30 US/Eastern, i.e. after the close):
    30 16 * * 1-5  cd /path/to/watchlist && /path/to/yf_env/bin/python /path/to/watchlist/update_prices.py >> /path/to/watchlist/update_prices.log 2>&1
"""

import argparse
import datetime
import os
import sys
import threading
import time

from storage import Store, price_decimals

SLEEP_BETWEEN_TICKERS = 1.0  # be polite to the yfinance API
FETCH_TIMEOUT = 60.0         # wall-clock cap for a single ticker fetch


def fetch_history(code, period="1y", interval="1d"):
    """Fetch daily bars with a hard wall-clock timeout.

    yfinance can hang on flaky Yahoo connections, so the request runs in
    a daemon thread; if it does not return within ``FETCH_TIMEOUT`` the
    ticker is reported as failed and the script moves on.

    Returns (df_or_None, error_or_None).
    """
    import yfinance as yf

    box = {}

    def _work():
        try:
            box["df"] = yf.Ticker(code).history(
                period=period, interval=interval, timeout=30
            )
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(FETCH_TIMEOUT)
    if t.is_alive():
        return None, "fetch timed out"
    if "err" in box:
        return None, str(box["err"])
    return box.get("df"), None


def market_open_now():
    """Rough check: US market hours are 09:30-16:00 ET, Mon-Fri.

    If we are inside those hours (on a weekday) the latest daily bar is
    still forming, so an "after-close" update should wait.  Holidays are
    not checked on purpose — they are a harmless edge case.
    """
    try:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        now = datetime.datetime.now(et)
    except Exception:
        return False
    return now.weekday() < 5 and datetime.time(9, 30) <= now.time() < datetime.time(16, 0)


def build_trend_label(directions, max_segments=3):
    """Convert a Renko direction sequence into a trend label.

    directions: list / numpy array, 1 = up brick, -1 = down brick.
    Returns a label such as "5U3D2U": run-length encoded, keeping only
    the last ``max_segments`` segments.  (Ported from yfrk_db.py.)
    """
    if directions is None or len(directions) == 0:
        return ""

    # duck-typed: numpy arrays / pandas Series expose .tolist()
    if hasattr(directions, "tolist"):
        dirs = directions.tolist()
    else:
        dirs = list(directions)

    # run-length encoding
    groups = []
    current_dir = dirs[0]
    current_count = 1
    for d in dirs[1:]:
        if d == current_dir:
            current_count += 1
        else:
            groups.append((current_dir, current_count))
            current_dir = d
            current_count = 1
    groups.append((current_dir, current_count))

    # keep the last N segments
    if len(groups) > max_segments:
        groups = groups[-max_segments:]

    parts = []
    for direction, count in groups:
        letter = "U" if direction == 1 else "D"
        parts.append(f"{count}{letter}")
    return "".join(parts)


def renko_directions(bricks):
    """Brick base prices -> direction sequence (1 = up, -1 = down).

    A brick is "up" when its base is higher than the previous brick's
    base, "down" when lower (ported from yfrk_db.py).
    """
    if bricks is None or len(bricks) == 0:
        return []
    trends = [1]
    for i in range(1, len(bricks)):
        if bricks[i] > bricks[i - 1]:
            trends.append(1)
        elif bricks[i] < bricks[i - 1]:
            trends.append(-1)
        else:  # numerically identical bases: carry the previous direction
            trends.append(trends[-1])
    return trends


def calc_renko_label(hist):
    """Compute the Renko label (last 3 segments) for a daily-bar frame.

    Uses the same settings as yfrk_db.py: mplfinance Renko with
    brick_size='atr', atr_length=14.  Returns "" when it cannot be
    computed (caller keeps the previous value).
    """
    try:
        # headless: force the Agg backend (TkAgg hangs in cron / no GUI)
        import os as _os
        _os.environ.setdefault("MPLBACKEND", "Agg")
        import warnings

        import matplotlib
        matplotlib.use("Agg")
        import mplfinance as mpf
        cvalues = {}
        with warnings.catch_warnings():
            # mplfinance beta tries plt.show() on the canvas; harmless with Agg
            warnings.simplefilter("ignore")
            mpf.plot(
                hist,
                type="renko",
                volume=False,
                renko_params={"brick_size": "atr", "atr_length": 14},
                return_calculated_values=cvalues,
                show_nontrading=False,
                style="yahoo",
            )
            matplotlib.pyplot.close("all")
        bricks = cvalues.get("renko_bricks")
        if bricks is None:
            return ""
        return build_trend_label(renko_directions(bricks))
    except Exception as exc:
        print(f"    renko calc failed: {exc}")
        return ""


def classify(price, ma5, ma20):
    """Return (trend, signal) from the latest price and the two MAs."""
    if ma5 is None or ma20 is None:
        return "Unknown", "Unknown"
    if ma5 > ma20:
        trend = "Up"
    elif ma5 < ma20:
        trend = "Down"
    else:
        trend = "Flat"

    if price is not None:
        if price > ma5 and ma5 >= ma20:
            signal = "Buy"
        elif price < ma5 and ma5 <= ma20:
            signal = "Sell"
        else:
            signal = "Hold"
    else:
        signal = "Hold"
    return trend, signal


def update_stock(stock, sleep):
    """Refresh the market fields of one stock dict in place.

    Returns (ok: bool, message: str).
    """
    code = stock["code"]
    # 1 year of daily bars — same period yfrk_db.py uses for the Renko chart
    hist, err = fetch_history(code)
    if hist is None:
        return False, f"{code}: fetch failed ({err})"
    if len(hist) == 0 or "Close" not in hist.columns:
        return False, f"{code}: no data returned"

    closes = hist["Close"].dropna()
    if len(closes) == 0:
        return False, f"{code}: no close data"

    # A-share ETFs (512880.SS etc.) quote at 3 decimals; everything else at 2
    dec = price_decimals(code)

    def avg_last(n):
        if len(closes) < n:
            return None
        return round(float(closes.tail(n).mean()), dec)

    price = round(float(closes.iloc[-1]), dec)
    ma5 = avg_last(5)
    ma20 = avg_last(20)
    trend, signal = classify(price, ma5, ma20)

    renko_label = calc_renko_label(hist)

    stock["price"] = price
    stock["ma5"] = ma5
    stock["ma20"] = ma20
    if renko_label:
        stock["renko"] = renko_label
    stock["trend"] = trend
    stock["signal"] = signal
    if sleep:
        time.sleep(SLEEP_BETWEEN_TICKERS)
    return True, (f"{code}: price={price} ma5={ma5} ma20={ma20} "
                  f"renko={stock.get('renko', '')} "
                  f"trend={trend} signal={signal}")


def main(argv=None):
    base = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Update watchlist market data after the close.")
    parser.add_argument("--file", default=os.path.join(base, "watchlist.json"),
                        help="path to watchlist.json")
    parser.add_argument("--force", action="store_true",
                        help="update even if the market is still open")
    args = parser.parse_args(argv)

    store = Store(args.file)
    with store.lock():
        stocks = store.load()

    if not stocks:
        print("Nothing to do: watchlist is empty.")
        return 0

    if market_open_now() and not args.force:
        print("Market appears to be open — skipping (use --force to override).")
        return 0

    print(f"Updating {len(stocks)} stock(s) at "
          f"{datetime.datetime.now().isoformat()} ...", flush=True)
    failures = 0
    with store.lock():
        stocks = store.load()
        for stock in stocks:
            ok, msg = update_stock(stock, sleep=True)
            print(("OK  " if ok else "FAIL ") + msg, flush=True)
            if not ok:
                failures += 1
        store.save(stocks)

    print("Done.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
