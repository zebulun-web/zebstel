"""
Zebstel wedding support website - Python (Flask) backend.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv
from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, url_for)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def env(name, default=""):
    return os.getenv(name, default).strip()


def env_int(name, default):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


CONFIG = {
    "wedding_name": env("WEDDING_NAME", "Zebstel"),
    "couple": env("COUPLE_NAMES", "Zebulun & Ellie"),
    "wedding_date": env("WEDDING_DATE"),
    "currency": env("CURRENCY", "NGN").upper(),
    "goal": env_int("GOAL_AMOUNT", 1_000_000),
    "min_amount": env_int("MIN_AMOUNT", 500),
    "max_amount": env_int("MAX_AMOUNT", 10_000_000),
    "presets": [int(x) for x in env("PRESET_AMOUNTS", "5000,10000,20000,50000").split(",") if x.strip().isdigit()],
    "bank_name": env("BANK_NAME"),
    "bank_account_name": env("BANK_ACCOUNT_NAME"),
    "bank_account_number": env("BANK_ACCOUNT_NUMBER"),
}

PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY")
DEMO_MODE = not PAYSTACK_SECRET_KEY
SITE_URL = env("SITE_URL").rstrip("/")
ADMIN_PASSWORD = env("ADMIN_PASSWORD")
DB_PATH = env("DATABASE_PATH", os.path.join(BASE_DIR, "data", "zebstel.db"))

app = Flask(__name__)

if env("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contributions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reference     TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    message       TEXT DEFAULT '',
    amount_minor  INTEGER NOT NULL,
    currency      TEXT NOT NULL,
    hide_name     INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    paid_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_paid ON contributions(status, paid_at);
"""


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_hits = defaultdict(deque)


def rate_limited(key, limit=8, window=60):
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def site_url():
    return SITE_URL or request.url_root.rstrip("/")


def paystack(method, path, **kwargs):
    return requests.request(
        method,
        f"https://api.paystack.co{path}",
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
        timeout=20,
        **kwargs,
    )


def mark_paid(db, reference, amount_minor=None, currency=None):
    row = db.execute("SELECT * FROM contributions WHERE reference = ?", (reference,)).fetchone()
    if row is None:
        return None
    if row["status"] == "paid":
        return row
    if amount_minor is not None and (
        int(amount_minor) != row["amount_minor"] or (currency or "").upper() != row["currency"]
    ):
        db.execute("UPDATE contributions SET status = 'mismatch' WHERE reference = ?", (reference,))
        db.commit()
        return None
    db.execute(
        "UPDATE contributions SET status = 'paid', paid_at = ? WHERE reference = ?",
        (now_iso(), reference),
    )
    db.commit()
    return db.execute("SELECT * FROM contributions WHERE reference = ?", (reference,)).fetchone()


def totals(db):
    row = db.execute(
        "SELECT COALESCE(SUM(amount_minor), 0) AS total, COUNT(*) AS n "
        "FROM contributions WHERE status = 'paid'"
    ).fetchone()
    raised = row["total"] / 100
    goal = CONFIG["goal"]
    percent = min(100, round(raised / goal * 100, 1)) if goal else 0
    return {"raised": raised, "count": row["n"], "goal": goal, "percent": percent}


def csv_safe(value):
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def require_admin(view):
    def wrapper(*args, **kwargs):
        auth = request.authorization
        ok = (
            ADMIN_PASSWORD
            and auth
            and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD)
        )
        if not ok:
            if not ADMIN_PASSWORD:
                abort(404)
            return Response(
                "Login required", 401, {"WWW-Authenticate": 'Basic realm="Zebstel admin"'}
            )
        return view(*args, **kwargs)

    wrapper.__name__ = view.__name__
    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


@app.route("/")
def index():
    public_cfg = {
        k: CONFIG[k]
        for k in ("wedding_name", "couple", "wedding_date", "currency", "goal", "min_amount", "presets")
    }
    public_cfg["demo"] = DEMO_MODE
    bank = None
    if CONFIG["bank_account_number"]:
        bank = {
            "bank": CONFIG["bank_name"],
            "name": CONFIG["bank_account_name"],
            "number": CONFIG["bank_account_number"],
        }
    return render_template(
        "index.html", cfg=public_cfg, bank=bank, demo=DEMO_MODE, site_url=site_url()
    )


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/stats")
def api_stats():
    return jsonify(totals(get_db()))


@app.get("/api/supporters")
def api_supporters():
    rows = get_db().execute(
        "SELECT name, message, hide_name, paid_at FROM contributions "
        "WHERE status = 'paid' ORDER BY paid_at DESC LIMIT 40"
    ).fetchall()
    return jsonify(
        [
            {
                "name": "A kind friend" if r["hide_name"] else r["name"],
                "message": r["message"],
                "when": r["paid_at"],
            }
            for r in rows
        ]
    )


@app.get("/api/gift/<reference>")
def api_gift(reference):
    row = get_db().execute(
        "SELECT name, status FROM contributions WHERE reference = ?", (reference,)
    ).fetchone()
    if row is None:
        return jsonify({"status": "unknown"}), 404
    return jsonify({"status": row["status"], "first_name": row["name"].split()[0]})


@app.post("/api/pay")
def api_pay():
    if rate_limited(request.remote_addr or "unknown"):
        return jsonify(error="Too many attempts. Please wait a minute and try again."), 429

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:80]
    email = str(data.get("email", "")).strip()[:120]
    message = str(data.get("message", "")).strip()[:300]
    hide_name = 1 if data.get("hide_name") else 0

    if not name:
        return jsonify(error="Please tell us your name."), 400
    if not EMAIL_RE.match(email):
        return jsonify(error="Please enter a valid email address so we can send your receipt."), 400
    try:
        amount = Decimal(str(data.get("amount")))
    except (InvalidOperation, TypeError):
        return jsonify(error="Please choose or enter an amount."), 400
    if not amount.is_finite() or amount < CONFIG["min_amount"]:
        return jsonify(error=f"The smallest gift is {CONFIG['min_amount']:,} {CONFIG['currency']}."), 400
    if amount > CONFIG["max_amount"]:
        return jsonify(error=f"The largest online gift is {CONFIG['max_amount']:,} {CONFIG['currency']}."), 400

    amount_minor = int((amount * 100).to_integral_value())
    reference = "ZEB-" + secrets.token_hex(8)
    callback = f"{site_url()}{url_for('pay_callback')}"

    db = get_db()
    db.execute(
        "INSERT INTO contributions (reference, name, email, message, amount_minor, currency, hide_name, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (reference, name, email, message, amount_minor, CONFIG["currency"], hide_name, now_iso()),
    )
    db.commit()

    if DEMO_MODE:
        return jsonify(url=f"{callback}?reference={reference}")

    try:
        resp = paystack(
            "POST",
            "/transaction/initialize",
            json={
                "email": email,
                "amount": amount_minor,
                "currency": CONFIG["currency"],
                "reference": reference,
                "callback_url": callback,
                "metadata": {"name": name, "message": message, "wedding": CONFIG["wedding_name"]},
            },
        )
        body = resp.json()
        if not resp.ok or not body.get("status"):
            raise RuntimeError(body.get("message", "Paystack refused the request"))
        return jsonify(url=body["data"]["authorization_url"])
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        app.logger.error("Paystack initialise failed: %s", exc)
        db.execute("UPDATE contributions SET status = 'failed' WHERE reference = ?", (reference,))
        db.commit()
        return jsonify(error="We couldn't reach the payment service. Please try again in a moment."), 502


@app.get("/pay/callback")
def pay_callback():
    reference = request.args.get("reference") or request.args.get("trxref") or ""
    db = get_db()
    row = db.execute("SELECT status FROM contributions WHERE reference = ?", (reference,)).fetchone()
    if row is None:
        return redirect(url_for("index"))

    if DEMO_MODE:
        mark_paid(db, reference)
        return redirect(url_for("index", gift="thanks", ref=reference))

    try:
        body = paystack("GET", f"/transaction/verify/{reference}").json()
        data = body.get("data") or {}
        if body.get("status") and data.get("status") == "success":
            if mark_paid(db, reference, data.get("amount"), data.get("currency")):
                return redirect(url_for("index", gift="thanks", ref=reference))
        elif data.get("status") in ("failed", "abandoned"):
            db.execute(
                "UPDATE contributions SET status = 'failed' WHERE reference = ? AND status = 'pending'",
                (reference,),
            )
            db.commit()
    except (requests.RequestException, ValueError) as exc:
        app.logger.error("Paystack verify failed: %s", exc)
        return redirect(url_for("index", gift="pending", ref=reference))
    return redirect(url_for("index", gift="failed", ref=reference))


@app.post("/api/paystack/webhook")
def paystack_webhook():
    if DEMO_MODE:
        abort(404)
    signature = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), request.get_data(), hashlib.sha512).hexdigest()
    if not hmac.compare_digest(signature, expected):
        abort(401)
    try:
        event = json.loads(request.get_data() or b"{}")
    except ValueError:
        return "", 400
    if event.get("event") == "charge.success":
        data = event.get("data", {})
        mark_paid(get_db(), data.get("reference", ""), data.get("amount"), data.get("currency", ""))
    return "", 200


@app.get("/admin")
@require_admin
def admin():
    db = get_db()
    rows = db.execute("SELECT * FROM contributions ORDER BY created_at DESC LIMIT 500").fetchall()
    return render_template("admin.html", rows=rows, stats=totals(db), currency=CONFIG["currency"], cfg=CONFIG)


@app.get("/admin/export.csv")
@require_admin
def admin_export():
    rows = get_db().execute(
        "SELECT reference, name, email, message, amount_minor, currency, status, created_at, paid_at "
        "FROM contributions ORDER BY created_at DESC"
    ).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["reference", "name", "email", "message", "amount", "currency", "status", "created_at", "paid_at"])
    for r in rows:
        writer.writerow(
            [csv_safe(r["reference"]), csv_safe(r["name"]), csv_safe(r["email"]), csv_safe(r["message"]),
             r["amount_minor"] / 100, r["currency"], r["status"], r["created_at"], r["paid_at"] or ""]
        )
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=zebstel-gifts.csv"},
    )


if __name__ == "__main__":
    print("\n  Zebstel site running in", "DEMO mode (no real payments)" if DEMO_MODE else "LIVE mode")
    app.run(debug=env("FLASK_DEBUG") == "1", host="0.0.0.0", port=env_int("PORT", 5000))