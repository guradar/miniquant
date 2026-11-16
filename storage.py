"""Shared storage helper for the stock watchlist.

Data lives in a single ``watchlist.json`` file (a JSON array of stock
objects).  All reads/writes go through :class:`Store`, which:

* takes an exclusive inter-process lock (``fcntl.flock``) so the Flask
  app and the cron update script never clobber each other, and
* writes atomically (temp file in the same directory + ``os.replace``).
"""

import contextlib
import json
import os
import tempfile

import fcntl


class Store:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock_path = self.path + ".lock"

    # ------------------------------------------------------------------ #
    # locking
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def lock(self):
        """Exclusive cross-process lock for the whole JSON file."""
        with open(self._lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ #
    # load / save
    # ------------------------------------------------------------------ #
    def load(self):
        """Return the list of stock dicts (call inside ``lock()``)."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        # normalise: make sure every record has all known keys
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            rec = {k: item.get(k) for k in FIELDS}
            if not rec.get("code"):
                continue
            out.append(rec)
        return out

    def save(self, stocks):
        """Atomically replace the JSON file (call inside ``lock()``)."""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".watchlist-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(stocks, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


#: canonical field order for every stock record
FIELDS = [
    "code",       # ticker symbol, e.g. "AAPL"
    "name",       # display name
    "price",      # current/latest price (float or None)
    "buy_price",  # price at which the position was bought (float)
    "position",   # number of shares held (int/float)
    "ma5",        # 5-day simple moving average (float or None)
    "ma20",       # 20-day simple moving average (float or None)
    "renko",      # Renko sequence label of the last 3 segments, e.g. "4D6U8D"
    "trend",      # "Up" / "Down" / "Flat" / "Unknown"
    "signal",     # "Buy" / "Sell" / "Hold" / "Unknown"
    "notes",      # free text
]


def new_stock(code, name, buy_price, position, notes=""):
    """Blank record for a newly added stock (market fields are filled
    later by the cron update script)."""
    return {
        "code": code,
        "name": name,
        "price": None,
        "buy_price": buy_price,
        "position": position,
        "ma5": None,
        "ma20": None,
        "renko": "",
        "trend": "Unknown",
        "signal": "Unknown",
        "notes": notes,
    }


def is_a_share_etf(code):
    """True for A-share ETFs that trade at 3-decimal prices.

    Matches codes such as ``512880.SS`` / ``159915.SZ``: a fund/ETF
    prefix (51/15/16/50/52/53/55/56/58) followed by the .SS/.SZ exchange
    suffix (case-insensitive).
    """
    code = (code or "").upper()
    return code[:2] in ("51", "15", "16", "50", "52", "53", "55", "56", "58") \
        and code[7:] in ("SS", "SZ")


def price_decimals(code):
    """Number of decimal places to display/round prices for a code."""
    return 3 if is_a_share_etf(code) else 2
