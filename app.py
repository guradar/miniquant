"""Stock watchlist manager — simple Flask web app.

* Data:  ``watchlist.json`` (managed by :mod:`storage`)
* Add:   code, name, buy price, position  (4 fields)
* Edit:  name, trend, signal, notes      (4 fields)
* Delete: any stock
* Market data (price / MA5 / MA20) is refreshed out-of-band by
  ``update_prices.py`` (a cron script that uses yfinance).
"""

import os

from flask import Flask, flash, redirect, render_template, request, url_for

from storage import Store, new_stock, price_decimals

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORE = Store(os.path.join(BASE_DIR, "watchlist.json"))

app = Flask(__name__)
app.secret_key = "watchlist-secret-key"


@app.context_processor
def inject_price_helpers():
    """Expose a price formatter to the templates.

    ``fmt_price(value, code)`` renders a numeric price with 3 decimals for
    A-share ETFs (e.g. ``512880.SS``) and 2 decimals otherwise, or ``—``
    when the value is missing.
    """
    def fmt_price(value, code):
        if value is None:
            return "—"
        return f"{value:.{price_decimals(code)}f}"
    return dict(fmt_price=fmt_price)


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _parse_float(value, required=False, label="field"):
    """Parse a form field into a float. Returns (value, error)."""
    value = (value or "").strip()
    if not value:
        if required:
            return None, f"{label} is required."
        return 0.0, None
    try:
        return float(value), None
    except ValueError:
        return None, f"{label} must be a number."


def _check_decimal_digits(raw, max_digits, label):
    """Validate that a numeric string has at most ``max_digits`` decimal
    places. Returns an error string, or None when the input is fine
    (empty / non-numeric input is reported by the float parsing)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        float(raw)
    except ValueError:
        return None
    if "." in raw:
        decimals = len(raw.split(".", 1)[1])
        if decimals > max_digits:
            return f"{label} allows at most {max_digits} decimal places."
    return None


def _find(stocks, code):
    for s in stocks:
        if s["code"].lower() == code.lower():
            return s
    return None


# ---------------------------------------------------------------------- #
# routes
# ---------------------------------------------------------------------- #
@app.route("/")
def index():
    with STORE.lock():
        stocks = STORE.load()
    # newest first (by insertion order: keep JSON order, which is stable)
    return render_template("index.html", stocks=stocks)


@app.route("/add", methods=["POST"])
def add():
    code = (request.form.get("code") or "").strip().upper()
    name = (request.form.get("name") or "").strip()
    buy_price, err1 = _parse_float(request.form.get("buy_price"), required=True, label="Buy price")
    position, err2 = _parse_float(request.form.get("position"), label="Position")
    notes = (request.form.get("notes") or "").strip()
    err3 = _check_decimal_digits(request.form.get("buy_price"), 3, "Buy price")

    errors = [e for e in (err1, err2, err3) if e]
    if not code:
        errors.insert(0, "Code is required.")
    elif not code.replace(".", "").replace("/", "").isalnum():
        errors.insert(0, "Code must contain letters / numbers only.")
    if not name:
        errors.insert(0, "Name is required.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("index"))

    with STORE.lock():
        stocks = STORE.load()
        if _find(stocks, code):
            flash(f"Code {code} already exists.", "error")
            return redirect(url_for("index"))
        stocks.append(new_stock(code, name, buy_price, position, notes=notes))
        STORE.save(stocks)
    flash(f"Added {code}.", "ok")
    return redirect(url_for("index"))


@app.route("/update/<code>", methods=["POST"])
def update(code):
    name = (request.form.get("name") or "").strip()
    trend = (request.form.get("trend") or "Unknown").strip() or "Unknown"
    signal = (request.form.get("signal") or "Unknown").strip() or "Unknown"
    notes = (request.form.get("notes") or "").strip()

    if not name:
        flash("Name is required.", "error")
        return redirect(url_for("index"))

    with STORE.lock():
        stocks = STORE.load()
        stock = _find(stocks, code)
        if stock is None:
            flash(f"Code {code} not found.", "error")
            return redirect(url_for("index"))
        stock["name"] = name
        stock["trend"] = trend
        stock["signal"] = signal
        stock["notes"] = notes
        STORE.save(stocks)
    flash(f"Updated {code}.", "ok")
    return redirect(url_for("index"))


@app.route("/delete/<code>", methods=["POST"])
def delete(code):
    with STORE.lock():
        stocks = STORE.load()
        stock = _find(stocks, code)
        if stock is None:
            flash(f"Code {code} not found.", "error")
        else:
            stocks.remove(stock)
            STORE.save(stocks)
            flash(f"Deleted {code}.", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
