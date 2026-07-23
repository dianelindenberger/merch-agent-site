from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
import hmac
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = Path(__file__).resolve().parent / "web"
SALES_SYNC_LOCK = threading.Lock()
HOSTED_REFRESH_LOCK = threading.Lock()
HOSTED_REFRESH_STATUS = {
    "running": False,
    "label": "",
    "startedAt": "",
    "finishedAt": "",
    "exitCode": None,
    "error": "",
}

SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from report_utils import file_hash, normalize_columns, read_report  # noqa: E402
from daily_audit import build_daily_audit  # noqa: E402
from database import DATA_DIR, DB_PATH  # noqa: E402

INCOMING_REPORTS = Path(os.getenv("MERCH_AGENT_REPORTS_DIR", DATA_DIR / "incoming_reports")).expanduser().resolve()
AUTH_USERNAME = os.getenv("MERCH_AGENT_USERNAME", "").strip()
AUTH_PASSWORD = os.getenv("MERCH_AGENT_PASSWORD", "")
MAX_SALES_UPLOAD_BYTES = 20 * 1024 * 1024
DAILY_REFRESH_ENABLED = os.getenv("MERCH_AGENT_DAILY_REFRESH_ENABLED", "false").lower() in {"1", "true", "yes"}
DAILY_REFRESH_TIME = os.getenv("MERCH_AGENT_DAILY_REFRESH_TIME", "06:00")
AD_REFRESH_TIMES = os.getenv("MERCH_AGENT_AD_REFRESH_TIMES", "12:00,17:00")
EASTERN_TIME = ZoneInfo("America/New_York")

MARKET_NAMES = {
    ".com": "United States",
    ".co.uk": "United Kingdom",
    ".de": "Germany",
    ".fr": "France",
    ".it": "Italy",
    ".es": "Spain",
    ".co.jp": "Japan",
}

SALES_REPORT_COLUMNS = {"Title", "Purchased", "Royalties", "Revenue", "Date"}
SALES_REPORT_EXTENSIONS = {".csv", ".xlsx", ".xls"}


def authentication_enabled():
    return bool(AUTH_USERNAME and AUTH_PASSWORD)


def authorization_is_valid(header_value):
    if not authentication_enabled():
        return True
    if not (header_value or "").startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value.split(" ", 1)[1], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return hmac.compare_digest(username, AUTH_USERNAME) and hmac.compare_digest(password, AUTH_PASSWORD)


def is_path_within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def sales_sync_status():
    conn = connect()
    row = conn.execute(
        """
        SELECT file_name, imported_at, report_date
        FROM imported_files
        WHERE report_type = 'sales'
        ORDER BY imported_at DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()

    if not row:
        return {"ok": True, "connected": True, "lastSync": None}

    return {
        "ok": True,
        "connected": True,
        "lastSync": {
            "fileName": row["file_name"],
            "importedAt": row["imported_at"],
            "reportDate": row["report_date"] or "",
        },
    }


def sync_downloaded_sales_report(raw_path):
    if not SALES_SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "A sales report is already being synchronized."}

    try:
        source = Path(str(raw_path or "")).expanduser().resolve()
        allowed_roots = [
            (Path.home() / "Downloads").resolve(),
            INCOMING_REPORTS.resolve(),
        ]

        if not source.is_file() or not any(is_path_within(source, root) for root in allowed_roots):
            return {"ok": False, "error": "Choose a report from your Downloads or incoming_reports folder."}
        if source.suffix.lower() not in SALES_REPORT_EXTENSIONS:
            return {"ok": False, "error": "The sales helper supports CSV and Excel reports."}

        try:
            report = normalize_columns(read_report(source))
        except Exception as exc:
            return {"ok": False, "error": f"The downloaded report could not be read: {exc}"}

        missing = sorted(SALES_REPORT_COLUMNS.difference(report.columns))
        if missing:
            return {
                "ok": False,
                "error": "This is not the expected Merch sales report.",
                "missingColumns": missing,
            }

        report_dates = report["Date"]
        parsed_dates = __import__("pandas").to_datetime(report_dates, errors="coerce", format="mixed")
        if parsed_dates.isna().all():
            return {"ok": False, "error": "The sales report does not contain readable dates."}
        report_date = parsed_dates.max().date().isoformat()

        digest = file_hash(source)
        conn = connect()
        existing = conn.execute(
            "SELECT file_name, imported_at, report_date FROM imported_files WHERE file_hash = ? LIMIT 1",
            (digest,),
        ).fetchone()
        conn.close()
        if existing:
            return {
                "ok": True,
                "alreadySynced": True,
                "fileName": existing["file_name"],
                "reportDate": existing["report_date"] or report_date,
                "message": "This report was already synchronized.",
            }

        INCOMING_REPORTS.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = INCOMING_REPORTS / f"SALES_REPORT-auto-{timestamp}{source.suffix.lower()}"
        shutil.copy2(source, destination)

        command = [
            sys.executable,
            str(PROJECT_ROOT / "tools" / "derive_sales_periods.py"),
            str(destination),
        ]
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            return {
                "ok": False,
                "error": "Merch Agent could not prepare the sales periods.",
                "details": (result.stderr or result.stdout)[-1200:],
            }

        return {
            "ok": True,
            "alreadySynced": False,
            "fileName": destination.name,
            "reportDate": report_date,
            "message": "Sales synchronized. Yesterday, 7-day, 30-day, and 60-day views are ready.",
        }
    finally:
        SALES_SYNC_LOCK.release()


def sync_uploaded_sales_report(file_name, encoded_data):
    safe_name = Path(str(file_name or "")).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SALES_REPORT_EXTENSIONS:
        return {"ok": False, "error": "Upload a CSV or Excel Merch sales report."}
    try:
        raw = base64.b64decode(str(encoded_data or ""), validate=True)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": "The uploaded report could not be decoded."}
    if not raw:
        return {"ok": False, "error": "The uploaded report is empty."}
    if len(raw) > MAX_SALES_UPLOAD_BYTES:
        return {"ok": False, "error": "The uploaded report is larger than 20 MB."}

    INCOMING_REPORTS.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    temporary = INCOMING_REPORTS / f".sales-upload-{timestamp}{suffix}"
    temporary.write_bytes(raw)
    try:
        return sync_downloaded_sales_report(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def parse_refresh_time(value, fallback):
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        hour, minute = fallback
    return hour, minute


def ad_refresh_times():
    times = []
    for raw_time in AD_REFRESH_TIMES.split(","):
        raw_time = raw_time.strip()
        if raw_time:
            times.append(parse_refresh_time(raw_time, (12, 0)))
    return times or [(12, 0), (17, 0)]


def scheduled_refresh_jobs(now=None):
    now = now or datetime.now(EASTERN_TIME)
    jobs = []
    full_hour, full_minute = parse_refresh_time(DAILY_REFRESH_TIME, (6, 0))
    jobs.append({
        "label": "full data refresh",
        "script": "refresh_merch_agent.py",
        "scheduled": now.replace(hour=full_hour, minute=full_minute, second=0, microsecond=0),
    })
    for hour, minute in ad_refresh_times():
        jobs.append({
            "label": "Amazon Ads checkpoint refresh",
            "script": "refresh_amazon_ads.py",
            "scheduled": now.replace(hour=hour, minute=minute, second=0, microsecond=0),
        })
    for job in jobs:
        if job["scheduled"] <= now:
            job["scheduled"] += timedelta(days=1)
    return sorted(jobs, key=lambda job: job["scheduled"])


def next_daily_refresh(now=None):
    now = now or datetime.now(EASTERN_TIME)
    hour, minute = parse_refresh_time(DAILY_REFRESH_TIME, (6, 0))
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled <= now:
        scheduled += timedelta(days=1)
    return scheduled


def daily_refresh_scheduler():
    while True:
        job = scheduled_refresh_jobs()[0]
        scheduled = job["scheduled"]
        delay = max(1, (scheduled - datetime.now(EASTERN_TIME)).total_seconds())
        print(f"Next hosted {job['label']}: {scheduled.isoformat()}", flush=True)
        threading.Event().wait(delay)
        run_hosted_refresh(job["label"], job["script"])


def refresh_status_payload():
    with HOSTED_REFRESH_LOCK:
        return dict(HOSTED_REFRESH_STATUS)


def run_hosted_refresh(label, script):
    if not HOSTED_REFRESH_LOCK.acquire(blocking=False):
        print(f"Hosted {label} skipped because another refresh is running.", flush=True)
        return False
    try:
        HOSTED_REFRESH_STATUS.update({
            "running": True,
            "label": label,
            "startedAt": datetime.now(EASTERN_TIME).isoformat(),
            "finishedAt": "",
            "exitCode": None,
            "error": "",
        })
        try:
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "tools" / script)],
                cwd=PROJECT_ROOT,
                text=True,
                timeout=3600,
                check=False,
            )
            HOSTED_REFRESH_STATUS.update({
                "running": False,
                "finishedAt": datetime.now(EASTERN_TIME).isoformat(),
                "exitCode": result.returncode,
            })
            print(f"Hosted {label} finished with exit code {result.returncode}.", flush=True)
        except Exception as exc:
            HOSTED_REFRESH_STATUS.update({
                "running": False,
                "finishedAt": datetime.now(EASTERN_TIME).isoformat(),
                "exitCode": -1,
                "error": str(exc),
            })
            print(f"Hosted {label} failed: {exc}", file=sys.stderr, flush=True)
    finally:
        HOSTED_REFRESH_LOCK.release()
    return True


def start_hosted_ads_refresh_now():
    with HOSTED_REFRESH_LOCK:
        if HOSTED_REFRESH_STATUS.get("running"):
            return {"ok": False, "running": True, "status": dict(HOSTED_REFRESH_STATUS)}
    thread = threading.Thread(
        target=run_hosted_refresh,
        args=("manual Amazon Ads checkpoint refresh", "refresh_amazon_ads.py"),
        daemon=True,
    )
    thread.start()
    return {"ok": True, "started": True, "status": refresh_status_payload()}


def money(value):
    return round(float(value or 0), 2)


def currency_amount_text(amount, currency="USD"):
    currency = (currency or "USD").upper()
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
    decimals = 0 if currency == "JPY" else 2
    return f"{symbols.get(currency, currency + ' ')}{money(amount):,.{decimals}f}"


def design_royalties_text(item):
    breakdown = item.get("royaltiesByCurrency") or []
    if breakdown:
        return " + ".join(
            currency_amount_text(entry.get("amount"), entry.get("currency"))
            for entry in breakdown
        )
    return currency_amount_text(item.get("royalties"), item.get("currency", "USD"))


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_campaign_change_schema(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_name TEXT,
            target_name TEXT,
            change_type TEXT,
            details TEXT,
            previous_value TEXT,
            new_value TEXT,
            effective_date TEXT,
            summary TEXT,
            logged_at TEXT
        )
        """
    )
    columns = {row["name"] for row in cur.execute("PRAGMA table_info(campaign_change_log)").fetchall()}
    for name in ("target_name", "effective_date", "summary"):
        if name not in columns:
            cur.execute(f"ALTER TABLE campaign_change_log ADD COLUMN {name} TEXT")


def latest_sales_import(cur, period):
    params = []
    where = ""

    if period and period != "all":
        where = "WHERE COALESCE(report_period, 'unspecified') = ?"
        params.append(period)

    row = cur.execute(
        f"""
        SELECT import_date
        FROM sales
        {where}
        GROUP BY import_date
        ORDER BY import_date DESC
        LIMIT 1
        """,
        params,
    ).fetchone()

    return row["import_date"] if row else None


def latest_table_import(cur, table_name, period):
    row = cur.execute(
        f"""
        SELECT import_date
        FROM {table_name}
        WHERE COALESCE(report_period, 'unspecified') = ?
        GROUP BY import_date
        ORDER BY import_date DESC
        LIMIT 1
        """,
        (period,),
    ).fetchone()

    return row["import_date"] if row else None


def sales_units_for_period(cur, period):
    latest_import = latest_table_import(cur, "sales", period)

    if not latest_import:
        return None

    row = cur.execute(
        """
        SELECT
            SUM(purchased) AS units,
            MAX(COALESCE(report_date, '')) AS report_date
        FROM sales
        WHERE import_date = ?
          AND COALESCE(report_period, 'unspecified') = ?
        """,
        (latest_import, period),
    ).fetchone()

    return {
        "units": int(row["units"] or 0),
        "reportDate": row["report_date"] or "",
        "importDate": latest_import,
    }


def ad_orders_for_period(cur, period):
    latest_import = latest_table_import(cur, "campaigns", period)

    if not latest_import:
        return None

    row = cur.execute(
        """
        SELECT
            SUM(orders) AS orders,
            SUM(spend) AS spend,
            SUM(sales) AS sales,
            MAX(COALESCE(report_date, '')) AS report_date
        FROM campaigns
        WHERE import_date = ?
          AND COALESCE(report_period, 'unspecified') = ?
        """,
        (latest_import, period),
    ).fetchone()

    return {
        "orders": int(row["orders"] or 0),
        "spend": money(row["spend"]),
        "sales": money(row["sales"]),
        "reportDate": row["report_date"] or "",
        "importDate": latest_import,
    }


def royalty_tier_for_ratio(non_organic_ratio):
    if non_organic_ratio >= 0.35:
        return {"name": "Premium", "multiplier": "2.16x", "target": 0.35}
    if non_organic_ratio >= 0.15:
        return {"name": "Plus", "multiplier": "2x", "target": 0.15}
    return {"name": "Creator", "multiplier": "1x", "target": 0}


def royalty_tier_payload():
    if not DB_PATH.exists():
        return {"source": "missing_database", "cards": [], "summary": "No database found."}

    period_labels = {
        "last60": "Trailing 60 Days",
        "last30": "Last 30 Days",
        "last7": "Last 7 Days",
        "yesterday": "Yesterday",
        "unspecified": "Latest Ads Import",
    }

    conn = connect()
    cur = conn.cursor()
    cards = []

    for period in ("last60", "last30", "last7", "yesterday", "unspecified"):
        sales = sales_units_for_period(cur, period)
        ads = ad_orders_for_period(cur, period)

        if not sales or not ads or not sales["units"]:
            continue

        raw_ad_orders = ads["orders"]
        ad_orders = min(raw_ad_orders, sales["units"])
        organic_units = max(sales["units"] - ad_orders, 0)
        non_organic_ratio = ad_orders / sales["units"] if sales["units"] else 0
        organic_ratio = organic_units / sales["units"] if sales["units"] else 0
        tier = royalty_tier_for_ratio(non_organic_ratio)

        cards.append({
            "period": period,
            "label": period_labels.get(period, period),
            "reportDate": sales["reportDate"] or ads["reportDate"],
            "units": sales["units"],
            "adOrders": ad_orders,
            "rawAdOrders": raw_ad_orders,
            "organicUnits": organic_units,
            "nonOrganicRatio": round(non_organic_ratio, 4),
            "organicRatio": round(organic_ratio, 4),
            "tier": tier,
            "adSpend": ads["spend"],
            "adSales": ads["sales"],
            "isCapped": raw_ad_orders > sales["units"],
        })

    primary = next((card for card in cards if card["period"] == "last60"), None)
    has_60_sales = sales_units_for_period(cur, "last60") is not None
    has_60_ads = ad_orders_for_period(cur, "last60") is not None

    conn.close()

    if not primary:
        return {
            "source": "sqlite",
            "cards": [],
            "window": "Trailing 60 Days",
            "requirements": {"sales": has_60_sales, "ads": has_60_ads},
            "summary": "The 60-day tier estimate is incomplete. Import matching trailing-60-day Merch sales and Amazon Ads campaign data.",
        }

    premium_gap = max(0, 0.35 - primary["nonOrganicRatio"])
    plus_gap = max(0, 0.15 - primary["nonOrganicRatio"])

    return {
        "source": "sqlite",
        "window": "Trailing 60 Days",
        "primary": primary,
        "cards": cards,
        "thresholds": {
            "creatorMax": 0.15,
            "plusMin": 0.15,
            "premiumMin": 0.35,
        },
        "progress": {
            "plusGap": round(plus_gap, 4),
            "premiumGap": round(premium_gap, 4),
        },
        "summary": (
            f"{primary['label']} is at {primary['nonOrganicRatio'] * 100:.1f}% non-organic. "
            f"Current tier estimate: {primary['tier']['name']} ({primary['tier']['multiplier']} royalty)."
        ),
    }


def ads_payload(period, custom_start="", custom_end=""):
    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "metrics": {}, "topProducts": []}

    today = date.today()
    ranges = {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "last7": (today - timedelta(days=6), today),
        "last30": (today - timedelta(days=29), today),
        "last60": (today - timedelta(days=59), today),
    }
    if period == "custom":
        range_start = parse_date(custom_start)
        range_end = parse_date(custom_end)
        if not range_start or not range_end or range_start > range_end:
            return {"source": "invalid_range", "period": period, "metrics": {}, "topProducts": []}
    else:
        range_start, range_end = ranges.get(period, ranges["today"])

    conn = connect()
    cur = conn.cursor()
    source_period = "last60"
    latest_import = latest_table_import(cur, "advertised_products", source_period)
    fallback = False

    if not latest_import:
        source_period = period
        latest_import = latest_table_import(cur, "advertised_products", source_period)
    if not latest_import:
        source_period = "last30"
        latest_import = latest_table_import(cur, "advertised_products", source_period)
        fallback = bool(latest_import)
    if not latest_import:
        conn.close()
        return {"source": "empty_database", "period": period, "metrics": {}, "topProducts": []}

    date_filter = ""
    query_params = [latest_import, source_period]
    if source_period == "last60":
        date_filter = "AND report_date BETWEEN ? AND ?"
        query_params.extend([range_start.isoformat(), range_end.isoformat()])

    totals = cur.execute(
        f"""
        SELECT
            COUNT(*) AS rows,
            COUNT(DISTINCT advertised_asin) AS asins,
            SUM(impressions) AS impressions,
            SUM(clicks) AS clicks,
            SUM(spend) AS spend,
            SUM(sales) AS sales,
            SUM(orders) AS orders,
            SUM(units) AS units,
            MAX(COALESCE(report_date, '')) AS report_date
        FROM advertised_products
        WHERE import_date = ?
          AND COALESCE(report_period, 'unspecified') = ?
          {date_filter}
        """,
        query_params,
    ).fetchone()

    spend = money(totals["spend"])
    sales = money(totals["sales"])
    clicks = int(totals["clicks"] or 0)
    impressions = int(totals["impressions"] or 0)
    orders = int(totals["orders"] or 0)
    rows = cur.execute(
        f"""
        SELECT
            advertised_asin,
            campaign_name,
            ad_group_name,
            country,
            COALESCE(currency, 'USD') AS currency,
            SUM(impressions) AS impressions,
            SUM(clicks) AS clicks,
            SUM(spend) AS spend,
            SUM(sales) AS sales,
            SUM(orders) AS orders,
            SUM(units) AS units
        FROM advertised_products
        WHERE import_date = ?
          AND COALESCE(report_period, 'unspecified') = ?
          {date_filter}
        GROUP BY advertised_asin, campaign_name, ad_group_name, country, COALESCE(currency, 'USD')
        ORDER BY SUM(sales) DESC, SUM(orders) DESC
        LIMIT 12
        """,
        query_params,
    ).fetchall()

    top_products = []
    for row in rows:
        row_spend = money(row["spend"])
        row_sales = money(row["sales"])
        top_products.append({
            "asin": row["advertised_asin"],
            "campaignName": row["campaign_name"],
            "adGroupName": row["ad_group_name"],
            "country": row["country"],
            "currency": row["currency"],
            "impressions": int(row["impressions"] or 0),
            "clicks": int(row["clicks"] or 0),
            "spend": row_spend,
            "sales": row_sales,
            "orders": int(row["orders"] or 0),
            "units": int(row["units"] or 0),
            "roas": round(row_sales / row_spend, 2) if row_spend else 0,
        })

    daily_rows = cur.execute(
        f"""
        SELECT
            report_date,
            SUM(impressions) AS impressions,
            SUM(clicks) AS clicks,
            SUM(spend) AS spend,
            SUM(sales) AS sales,
            SUM(orders) AS orders
        FROM advertised_products
        WHERE import_date = ?
          AND COALESCE(report_period, 'unspecified') = ?
          {date_filter}
        GROUP BY report_date
        ORDER BY report_date
        """,
        query_params,
    ).fetchall()
    daily = []
    for row in daily_rows:
        day_spend = money(row["spend"])
        day_sales = money(row["sales"])
        daily.append({
            "date": row["report_date"],
            "impressions": int(row["impressions"] or 0),
            "clicks": int(row["clicks"] or 0),
            "spend": day_spend,
            "sales": day_sales,
            "orders": int(row["orders"] or 0),
            "acos": round(day_spend / day_sales * 100, 1) if day_sales else 0,
        })

    conn.close()
    acos = spend / sales * 100 if sales else 0
    roas = sales / spend if spend else 0
    ctr = clicks / impressions * 100 if impressions else 0
    cpc = spend / clicks if clicks else 0
    return {
        "source": "sqlite",
        "period": period,
        "sourcePeriod": source_period,
        "rangeStart": range_start.isoformat(),
        "rangeEnd": range_end.isoformat(),
        "partial": range_end >= today,
        "fallback": fallback,
        "reportDate": totals["report_date"] or "",
        "latestImport": latest_import,
        "metrics": {
            "rows": int(totals["rows"] or 0),
            "asins": int(totals["asins"] or 0),
            "impressions": impressions,
            "clicks": clicks,
            "spend": spend,
            "sales": sales,
            "orders": orders,
            "units": int(totals["units"] or 0),
            "acos": round(acos, 1),
            "roas": round(roas, 2),
            "ctr": round(ctr, 2),
            "cpc": round(cpc, 2),
        },
        "topProducts": top_products,
        "daily": daily,
    }


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def analytics_start_date(period, end_date):
    if period == "7D":
        return end_date - timedelta(days=6)
    if period == "30D":
        return end_date - timedelta(days=29)
    if period == "90D":
        return end_date - timedelta(days=89)
    if period == "1Y":
        return end_date - timedelta(days=364)
    return end_date - timedelta(days=29)


def analytics_payload(period, custom_start="", custom_end=""):
    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "points": []}

    conn = connect()
    cur = conn.cursor()
    try:
        source = cur.execute(
            """
            SELECT source_file, MAX(import_date) AS import_date,
                   COUNT(DISTINCT sale_date) AS date_count,
                   MIN(sale_date) AS min_date, MAX(sale_date) AS max_date
            FROM sales_daily
            GROUP BY source_file
            ORDER BY MAX(sale_date) DESC, COUNT(DISTINCT sale_date) DESC, MAX(import_date) DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        source = None

    if source:
        rows = cur.execute(
            """
            SELECT sale_date, currency, MAX(import_date) AS import_date,
                   SUM(purchased) AS sales, SUM(royalties) AS royalties, SUM(revenue) AS revenue
            FROM sales_daily
            WHERE source_file = ?
            GROUP BY sale_date, currency
            ORDER BY sale_date ASC, currency ASC
            """,
            (source["source_file"],),
        ).fetchall()
        points_by_date = {}
        royalty_by_currency = {}
        for row in rows:
            currency = (row["currency"] or "USD").upper()
            point = points_by_date.setdefault(
                row["sale_date"],
                {
                    "date": row["sale_date"],
                    "importDate": row["import_date"],
                    "sales": 0,
                    "royalties": 0,
                    "revenue": 0,
                },
            )
            point["sales"] += int(row["sales"] or 0)
            # The chart has one monetary axis, so it displays USD royalties only.
            if currency == "USD":
                point["royalties"] = money(point["royalties"] + (row["royalties"] or 0))
                point["revenue"] = money(point["revenue"] + (row["revenue"] or 0))
            royalty_by_currency[currency] = money(
                royalty_by_currency.get(currency, 0) + (row["royalties"] or 0)
            )
        points = list(points_by_date.values())
    else:
        points = []
        royalty_by_currency = {}

    available_end = parse_date(points[-1]["date"]) if points else None
    if period == "Custom":
        start_date = parse_date(custom_start)
        last_date = parse_date(custom_end)
        if not start_date or not last_date or start_date > last_date:
            conn.close()
            return {
                "source": "invalid_range",
                "period": period,
                "points": [],
                "error": "Choose a valid Analytics start and end date.",
            }
    elif points:
        last_date = available_end or date.today()
        start_date = analytics_start_date(period, last_date)
    else:
        last_date = date.today()
        start_date = analytics_start_date(period, last_date)

    if points:
        filtered_points = [
            point
            for point in points
            if parse_date(point["date"])
            and start_date <= parse_date(point["date"]) <= last_date
        ]
    else:
        filtered_points = []

    totals = {
        "sales": sum(point["sales"] for point in filtered_points),
        "royalties": money(sum(point["royalties"] for point in filtered_points)),
        "revenue": money(sum(point["revenue"] for point in filtered_points)),
    }

    if source:
        filtered_currency_rows = cur.execute(
            """
            SELECT currency, SUM(royalties) AS royalties
            FROM sales_daily
            WHERE source_file = ? AND sale_date >= ? AND sale_date <= ?
            GROUP BY currency
            ORDER BY CASE WHEN UPPER(currency) = 'USD' THEN 0 ELSE 1 END, UPPER(currency)
            """,
            (source["source_file"], start_date.isoformat(), last_date.isoformat()),
        ).fetchall()
        royalty_breakdown = [
            {
                "currency": (row["currency"] or "USD").upper(),
                "amount": money(row["royalties"]),
            }
            for row in filtered_currency_rows
        ]
    else:
        royalty_breakdown = []

    conn.close()

    return {
        "source": "sqlite",
        "period": period,
        "startDate": start_date.isoformat(),
        "endDate": last_date.isoformat(),
        "dayCount": (last_date - start_date).days + 1,
        "availableDayCount": len(filtered_points),
        "dataThrough": available_end.isoformat() if available_end else "",
        "totals": totals,
        "points": filtered_points,
        "royaltyByCurrency": royalty_breakdown,
        "royaltyChartCurrency": "USD",
        "note": "Daily chart uses every dated row from the newest complete Merch sales report. Monetary values are kept in their reported currencies.",
    }


def weekly_business_briefing():
    analytics = analytics_payload("30D")
    points = analytics.get("points", [])
    if len(points) < 7:
        return None

    current_points = points[-7:]
    previous_points = points[-14:-7]
    period_start = current_points[0]["date"]
    period_end = current_points[-1]["date"]
    current_units = sum(int(point.get("sales", 0)) for point in current_points)
    previous_units = sum(int(point.get("sales", 0)) for point in previous_points)
    sales_change = round((current_units - previous_units) / previous_units * 100, 1) if previous_units else 0
    sales_direction = "up" if sales_change > 2 else "down" if sales_change < -2 else "steady"

    ads = ads_payload("custom", period_start, period_end)
    current_start = parse_date(period_start)
    previous_ads = {"metrics": {}}
    if current_start:
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=6)
        previous_ads = ads_payload("custom", previous_start.isoformat(), previous_end.isoformat())

    ad_metrics = ads.get("metrics", {})
    previous_ad_metrics = previous_ads.get("metrics", {})
    ad_spend = money(ad_metrics.get("spend", 0))
    previous_spend = money(previous_ad_metrics.get("spend", 0))
    spend_change = round((ad_spend - previous_spend) / previous_spend * 100, 1) if previous_spend else 0
    spend_direction = "up" if spend_change > 2 else "down" if spend_change < -2 else "steady"
    attributed_units = int(ad_metrics.get("units", 0) or 0)
    attributed_share = round(attributed_units / current_units * 100, 1) if current_units else 0

    sales_sentence = (
        f"Sales were {sales_direction}: {current_units} units from {period_start} through {period_end}, "
        f"{abs(sales_change):.1f}% {'above' if sales_direction == 'up' else 'below'} the prior seven days."
    )
    if sales_direction == "steady":
        sales_sentence = (
            f"Sales were steady at {current_units} units from {period_start} through {period_end}, "
            f"within {abs(sales_change):.1f}% of the prior seven days."
        )

    spend_sentence = (
        f"Ad spend was {spend_direction} at ${ad_spend:.2f}, "
        f"{abs(spend_change):.1f}% {'above' if spend_direction == 'up' else 'below'} the prior week."
    )
    if spend_direction == "steady":
        spend_sentence = f"Ad spend was steady at ${ad_spend:.2f}, within {abs(spend_change):.1f}% of the prior week."

    opportunities = []
    if sales_direction == "down" and spend_direction == "up":
        opportunities.append("Prioritize campaigns where spend increased without matching sales growth; trim weak targets before adding budget.")
    elif sales_direction == "up" and spend_direction != "up":
        opportunities.append("Sales grew without a matching jump in spend. Protect the strongest campaigns and test increases cautiously.")
    else:
        opportunities.append("Review the highest-spend targets with no orders before making broader bid changes.")

    if float(ad_metrics.get("acos", 0) or 0) > 20:
        opportunities.append(f"Seven-day ACOS is {float(ad_metrics.get('acos', 0)):.1f}%. Look for bids or search terms that can move it toward the 20% target.")
    else:
        opportunities.append(f"Seven-day ACOS is {float(ad_metrics.get('acos', 0)):.1f}%. Keep efficient targets stable while gathering another day of data.")

    campaign_reviews = [item for item in campaigns_payload("last30").get("campaigns", []) if item.get("status") == "review"]
    if campaign_reviews:
        names = ", ".join(item["name"] for item in campaign_reviews[:2])
        opportunities.append(f"Start the next campaign review with {names}.")
    else:
        opportunities.append("Use the next complete day to compare top campaigns and document any bid or status changes in the change log.")

    return {
        "periodStart": period_start,
        "periodEnd": period_end,
        "sales": {
            "units": current_units,
            "previousUnits": previous_units,
            "changePct": sales_change,
            "direction": sales_direction,
            "summary": sales_sentence,
        },
        "ads": {
            "spend": ad_spend,
            "previousSpend": previous_spend,
            "spendChangePct": spend_change,
            "spendDirection": spend_direction,
            "attributedUnits": attributed_units,
            "attributedShare": attributed_share,
            "orders": int(ad_metrics.get("orders", 0) or 0),
            "acos": float(ad_metrics.get("acos", 0) or 0),
            "roas": float(ad_metrics.get("roas", 0) or 0),
            "summary": spend_sentence,
        },
        "impactSummary": (
            f"Amazon Ads attributed {attributed_units} advertised units, equivalent to {attributed_share:.1f}% "
            "of total Merch units in the same period. This indicates contribution, not proof that every sale was caused by an ad."
        ),
        "opportunities": opportunities,
    }


def home_payload(period):
    if not DB_PATH.exists():
        return {
            "source": "missing_database",
            "period": period,
            "sales": 0,
            "royalties": 0,
            "revenue": 0,
            "returns": 0,
            "products": [],
            "markets": [],
            "summary": ["No local database was found yet."],
        }

    conn = connect()
    cur = conn.cursor()
    latest_import = latest_sales_import(cur, period)

    if not latest_import and period != "all":
        period = "all"
        latest_import = latest_sales_import(cur, period)

    if not latest_import:
        conn.close()
        return {
            "source": "empty_database",
            "period": period,
            "sales": 0,
            "royalties": 0,
            "revenue": 0,
            "returns": 0,
            "products": [],
            "markets": [],
            "summary": ["No sales imports are available yet."],
        }

    if period == "all":
        all_rows = cur.execute(
            """
            SELECT title, purchased, royalties, revenue, report_period, report_date, import_date
            FROM sales
            WHERE import_date = ?
              AND purchased > 0
            ORDER BY purchased DESC, royalties DESC
            """,
            (latest_import,),
        ).fetchall()
    else:
        all_rows = cur.execute(
            """
            SELECT title, purchased, royalties, revenue, report_period, report_date, import_date
            FROM sales
            WHERE import_date = ?
              AND COALESCE(report_period, 'unspecified') = ?
              AND purchased > 0
            ORDER BY purchased DESC, royalties DESC
            """,
            (latest_import, period),
        ).fetchall()

    rows = all_rows[:20]

    totals = {
        "sales": sum(int(row["purchased"] or 0) for row in all_rows),
        "royalties": money(sum(row["royalties"] or 0 for row in all_rows)),
        "revenue": money(sum(row["revenue"] or 0 for row in all_rows)),
    }
    report_date = rows[0]["report_date"] if rows else ""

    products = [
        {
            "title": row["title"],
            "units": int(row["purchased"] or 0),
            "royalty": money(row["royalties"]),
            "revenue": money(row["revenue"]),
            "market": "Marketplace pending",
            "time": row["report_date"] or row["import_date"],
        }
        for row in rows
    ]

    markets = []
    market_returns = 0

    try:
        market_import = cur.execute(
            """
            SELECT import_date
            FROM sales_market_breakdown
            WHERE COALESCE(report_period, 'unspecified') = ?
            ORDER BY import_date DESC
            LIMIT 1
            """,
            (period,),
        ).fetchone()

        if market_import:
            market_rows = cur.execute(
                """
                SELECT
                    market,
                    currency,
                    SUM(purchased) AS units,
                    SUM(cancelled) AS cancelled,
                    SUM(returned) AS returned,
                    SUM(royalties) AS royalties,
                    SUM(revenue) AS revenue
                FROM sales_market_breakdown
                WHERE import_date = ?
                  AND COALESCE(report_period, 'unspecified') = ?
                GROUP BY market, currency
                ORDER BY SUM(purchased) DESC, SUM(royalties) DESC
                """,
                (market_import["import_date"], period),
            ).fetchall()
            markets = [
                {
                    "name": MARKET_NAMES.get(row["market"], row["market"] or "Unknown"),
                    "code": row["market"],
                    "currency": row["currency"],
                    "units": int(row["units"] or 0),
                    "cancelled": int(row["cancelled"] or 0),
                    "returned": int(row["returned"] or 0),
                    "royalties": money(row["royalties"]),
                    "revenue": money(row["revenue"]),
                }
                for row in market_rows
            ]
            market_returns = sum(item["returned"] for item in markets)

            product_rows = cur.execute(
                """
                SELECT
                    title,
                    market,
                    currency,
                    SUM(purchased) AS units,
                    SUM(royalties) AS royalties,
                    SUM(revenue) AS revenue
                FROM sales_market_breakdown
                WHERE import_date = ?
                  AND COALESCE(report_period, 'unspecified') = ?
                  AND purchased > 0
                GROUP BY title, market, currency
                ORDER BY SUM(purchased) DESC, SUM(royalties) DESC
                LIMIT 20
                """,
                (market_import["import_date"], period),
            ).fetchall()
            products = [
                {
                    "title": row["title"],
                    "units": int(row["units"] or 0),
                    "royalty": money(row["royalties"]),
                    "revenue": money(row["revenue"]),
                    "market": MARKET_NAMES.get(row["market"], row["market"] or "Unknown"),
                    "currency": row["currency"],
                    "time": report_date or latest_import,
                }
                for row in product_rows
            ]
    except sqlite3.OperationalError:
        markets = []

    royalties_by_currency = {}
    for item in markets:
        currency = (item.get("currency") or "USD").upper()
        royalties_by_currency[currency] = money(
            royalties_by_currency.get(currency, 0) + (item.get("royalties") or 0)
        )
    royalty_breakdown = [
        {"currency": currency, "amount": amount}
        for currency, amount in sorted(
            royalties_by_currency.items(), key=lambda pair: (pair[0] != "USD", pair[0])
        )
    ]
    top_product = (
        {"title": all_rows[0]["title"], "units": int(all_rows[0]["purchased"] or 0)}
        if all_rows else None
    )
    summary = []
    period_labels = {"yesterday": "Yesterday", "last7": "Last 7 days", "last30": "Last 30 days"}
    period_days = {"yesterday": 1, "last7": 7, "last30": 30}
    period_label = period_labels.get(period, "Latest period")
    days = period_days.get(period, 1)

    if top_product:
        summary.append(f"{top_product['title']} led {period_label.lower()} with {top_product['units']} units.")

    daily_average = totals["sales"] / days if days else 0
    currency_count = len({item.get("currency") for item in markets if item.get("currency")})
    royalty_values = " + ".join(
        f"{item['currency']} {item['amount']:.2f}" for item in royalty_breakdown
    )
    royalty_phrase = (
        f"reported royalties of {royalty_values}"
        if royalty_values
        else "no reported royalties"
    )
    summary.append(f"{period_label} produced {totals['sales']} units, averaging {daily_average:.1f} per day, with {royalty_phrase}.")
    if markets:
        top_market = markets[0]
        market_share = top_market["units"] / totals["sales"] * 100 if totals["sales"] else 0
        summary.append(f"{top_market['name']} was the largest marketplace at {market_share:.1f}% of units; {market_returns} returns were reported across all marketplaces.")
    else:
        summary.append("Marketplace detail is not available for this sales import.")

    conn.close()
    business_briefing = weekly_business_briefing()

    return {
        "source": "sqlite",
        "period": period,
        "reportDate": report_date,
        "latestImport": latest_import,
        "sales": totals["sales"],
        "royalties": totals["royalties"],
        "royaltyByCurrency": royalty_breakdown,
        "revenue": totals["revenue"],
        "returns": market_returns,
        "products": products,
        "markets": markets or [
            {"name": "All Markets", "units": totals["sales"], "revenue": totals["revenue"], "currency": "USD"},
        ],
        "summary": summary,
        "summaryPeriod": period_label,
        "businessBriefing": business_briefing,
    }


def designs_payload(period):
    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "designs": []}

    conn = connect()
    cur = conn.cursor()
    latest_import = latest_table_import(cur, "sales_market_breakdown", period)

    if not latest_import:
        latest_import = latest_table_import(cur, "sales", period)
        if not latest_import:
            conn.close()
            return {"source": "empty_database", "period": period, "designs": []}

        rows = cur.execute(
            """
            SELECT title, SUM(purchased) AS units, SUM(royalties) AS royalties,
                   SUM(revenue) AS revenue, MAX(COALESCE(report_date, '')) AS report_date
            FROM sales
            WHERE import_date = ? AND COALESCE(report_period, 'unspecified') = ?
            GROUP BY title
            ORDER BY SUM(purchased) DESC, title ASC
            """,
            (latest_import, period),
        ).fetchall()
        conn.close()
        return {
            "source": "sqlite",
            "period": period,
            "latestImport": latest_import,
            "reportDate": max((row["report_date"] or "" for row in rows), default=""),
            "designs": [
                {
                    "title": row["title"],
                    "units": int(row["units"] or 0),
                    "royaltiesByCurrency": [{"currency": "USD", "amount": money(row["royalties"])}],
                    "revenueByCurrency": [{"currency": "USD", "amount": money(row["revenue"])}],
                }
                for row in rows
            ],
        }

    rows = cur.execute(
        """
        SELECT title, COALESCE(currency, 'USD') AS currency,
               SUM(purchased) AS units, SUM(royalties) AS royalties,
               SUM(revenue) AS revenue, MAX(COALESCE(report_date, '')) AS report_date
        FROM sales_market_breakdown
        WHERE import_date = ? AND COALESCE(report_period, 'unspecified') = ?
        GROUP BY title, COALESCE(currency, 'USD')
        ORDER BY title ASC, currency ASC
        """,
        (latest_import, period),
    ).fetchall()
    designs_by_title = {}
    report_date = ""
    for row in rows:
        report_date = max(report_date, row["report_date"] or "")
        design = designs_by_title.setdefault(
            row["title"],
            {"title": row["title"], "units": 0, "royaltiesByCurrency": [], "revenueByCurrency": []},
        )
        design["units"] += int(row["units"] or 0)
        design["royaltiesByCurrency"].append({
            "currency": row["currency"], "amount": money(row["royalties"])
        })
        design["revenueByCurrency"].append({
            "currency": row["currency"], "amount": money(row["revenue"])
        })
    designs = sorted(designs_by_title.values(), key=lambda item: (-item["units"], item["title"]))
    conn.close()

    return {
        "source": "sqlite",
        "period": period,
        "latestImport": latest_import,
        "reportDate": report_date,
        "designs": designs,
    }


def campaigns_payload(period="last30", search=""):
    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "campaigns": []}

    conn = connect()
    cur = conn.cursor()
    latest_import = latest_table_import(cur, "campaigns", period)

    if not latest_import:
        conn.close()
        return {"source": "empty_database", "period": period, "campaigns": []}

    params = [latest_import, period]
    search_filter = ""

    if search.strip():
        search_filter = "AND LOWER(campaign_name) LIKE ?"
        params.append(f"%{search.strip().lower()}%")

    rows = cur.execute(
        f"""
        SELECT
            campaign_name,
            COALESCE(country, '') AS country,
            COALESCE(currency, '') AS currency,
            SUM(spend) AS spend,
            SUM(clicks) AS clicks,
            SUM(orders) AS orders,
            SUM(sales) AS sales,
            MAX(COALESCE(report_date, '')) AS report_date
        FROM campaigns
        WHERE import_date = ?
          AND COALESCE(report_period, 'unspecified') = ?
          {search_filter}
        GROUP BY campaign_name, COALESCE(country, ''), COALESCE(currency, '')
        ORDER BY SUM(sales) DESC, SUM(orders) DESC, SUM(spend) DESC
        """,
        params,
    ).fetchall()
    conn.close()

    campaigns = []

    for row in rows:
        spend = money(row["spend"])
        sales = money(row["sales"])
        orders = int(row["orders"] or 0)
        clicks = int(row["clicks"] or 0)
        campaigns.append({
            "name": row["campaign_name"],
            "country": row["country"],
            "currency": row["currency"],
            "spend": spend,
            "clicks": clicks,
            "orders": orders,
            "sales": sales,
            "roas": round(sales / spend, 2) if spend else 0,
            "acos": round(spend / sales * 100, 1) if sales else 0,
            "reportDate": row["report_date"],
            "status": "performing" if orders and sales / max(spend, 0.01) >= 5 else "review" if spend >= 10 and not orders else "watch",
        })

    return {
        "source": "sqlite",
        "period": period,
        "latestImport": latest_import,
        "reportDate": max((item["reportDate"] for item in campaigns), default=""),
        "count": len(campaigns),
        "campaigns": campaigns,
    }


def campaign_detail_payload(campaign_name, period="last30"):
    campaigns = campaigns_payload(period).get("campaigns", [])
    campaign = next((item for item in campaigns if item["name"].lower() == campaign_name.lower()), None)

    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "campaign": campaign, "targets": []}

    conn = connect()
    cur = conn.cursor()
    latest_import = latest_table_import(cur, "targets", period)
    targets = []
    target_report_date = ""
    changes = []
    ensure_campaign_change_schema(cur)

    if latest_import:
        rows = cur.execute(
            """
            SELECT
                COALESCE(ad_group_name, '') AS ad_group_name,
                target,
                COALESCE(match_type, '') AS match_type,
                MAX(COALESCE(bid, 0)) AS bid,
                SUM(clicks) AS clicks,
                SUM(spend) AS spend,
                SUM(orders) AS orders,
                SUM(sales) AS sales,
                MAX(COALESCE(top_of_search_impression_share, 0)) AS top_share,
                MAX(COALESCE(report_date, '')) AS report_date
            FROM targets
            WHERE import_date = ?
              AND COALESCE(report_period, 'unspecified') = ?
              AND LOWER(campaign_name) = LOWER(?)
            GROUP BY COALESCE(ad_group_name, ''), target, COALESCE(match_type, '')
            ORDER BY SUM(sales) DESC, SUM(orders) DESC, SUM(spend) DESC
            """,
            (latest_import, period, campaign_name),
        ).fetchall()

        for row in rows:
            spend = money(row["spend"])
            sales = money(row["sales"])
            targets.append({
                "adGroupName": row["ad_group_name"] or "Unassigned ad group",
                "target": row["target"] or "Unnamed target",
                "matchType": row["match_type"],
                "bid": money(row["bid"]),
                "clicks": int(row["clicks"] or 0),
                "spend": spend,
                "orders": int(row["orders"] or 0),
                "sales": sales,
                "roas": round(sales / spend, 2) if spend else 0,
                "acos": round(spend / sales * 100, 1) if sales else 0,
                "topOfSearchShare": round(float(row["top_share"] or 0), 2),
            })
            target_report_date = max(target_report_date, row["report_date"] or "")

    ad_groups_by_name = {}
    for target in targets:
        group = ad_groups_by_name.setdefault(
            target["adGroupName"],
            {
                "name": target["adGroupName"],
                "targetCount": 0,
                "clicks": 0,
                "spend": 0.0,
                "orders": 0,
                "sales": 0.0,
            },
        )
        group["targetCount"] += 1
        group["clicks"] += target["clicks"]
        group["spend"] = money(group["spend"] + target["spend"])
        group["orders"] += target["orders"]
        group["sales"] = money(group["sales"] + target["sales"])

    ad_groups = []
    for group in ad_groups_by_name.values():
        spend = group["spend"]
        sales = group["sales"]
        group["roas"] = round(sales / spend, 2) if spend else 0
        group["acos"] = round(spend / sales * 100, 1) if sales else 0
        ad_groups.append(group)
    ad_groups.sort(key=lambda item: (-item["sales"], -item["orders"], -item["spend"], item["name"].lower()))

    try:
        change_rows = cur.execute(
            """
            SELECT id, target_name, change_type, details, previous_value, new_value,
                   effective_date, summary, logged_at
            FROM campaign_change_log
            WHERE LOWER(campaign_name) = LOWER(?)
            ORDER BY logged_at DESC, id DESC
            LIMIT 30
            """,
            (campaign_name,),
        ).fetchall()
        changes = [
            {
                "id": row["id"],
                "changeType": row["change_type"],
                "targetName": row["target_name"],
                "details": row["details"],
                "previousValue": row["previous_value"],
                "newValue": row["new_value"],
                "effectiveDate": row["effective_date"],
                "summary": row["summary"],
                "loggedAt": row["logged_at"],
            }
            for row in change_rows
        ]
    except sqlite3.OperationalError:
        changes = []

    conn.close()
    return {
        "source": "sqlite",
        "period": period,
        "campaign": campaign or {"name": campaign_name},
        "adGroups": ad_groups,
        "adGroupCount": len(ad_groups),
        "targets": targets,
        "targetCount": len(targets),
        "targetReportDate": target_report_date,
        "latestTargetImport": latest_import or "",
        "changes": changes,
    }


def search_payload(query, period="last30"):
    clean_query = query.strip().lower()
    designs = designs_payload(period).get("designs", [])
    campaigns = campaigns_payload(period, query).get("campaigns", [])

    if clean_query:
        designs = [item for item in designs if clean_query in item["title"].lower()]

    return {
        "query": query.strip(),
        "period": period,
        "designs": designs[:30],
        "campaigns": campaigns[:30],
        "total": len(designs) + len(campaigns),
    }


def campaign_catalog():
    """Return current and historical campaigns so paused campaigns remain addressable."""
    if not DB_PATH.exists():
        return []

    conn = connect()
    rows = conn.execute(
        """
        SELECT
            campaign_name,
            COALESCE(country, '') AS country,
            MAX(COALESCE(report_date, '')) AS report_date,
            MAX(import_date) AS latest_import
        FROM campaigns
        WHERE TRIM(COALESCE(campaign_name, '')) <> ''
        GROUP BY campaign_name, COALESCE(country, '')
        ORDER BY MAX(import_date) DESC, campaign_name ASC
        """
    ).fetchall()
    conn.close()
    return [
        {
            "name": row["campaign_name"],
            "country": row["country"],
            "reportDate": row["report_date"],
            "latestImport": row["latest_import"],
        }
        for row in rows
    ]


def match_campaign_reference(question, campaigns):
    normalized = question.lower()
    exact = next(
        (item for item in sorted(campaigns, key=lambda item: len(item["name"]), reverse=True) if item["name"].lower() in normalized),
        None,
    )
    if exact:
        return exact

    ignored = {
        "a", "ad", "ads", "amazon", "campaign", "campaigns", "change", "changed",
        "for", "from", "log", "me", "my", "on", "please", "the", "to", "was",
    }
    question_terms = {
        token for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) > 1 and token not in ignored
    }
    ranked = []
    for item in campaigns:
        name_terms = set(re.findall(r"[a-z0-9]+", item["name"].lower()))
        country_terms = set(re.findall(r"[a-z0-9]+", str(item.get("country", "")).lower()))
        overlap = question_terms & (name_terms | country_terms)
        score = len(overlap) * 10
        if question_terms & name_terms:
            score += 3
        if question_terms & country_terms:
            score += 2
        if score:
            ranked.append((score, item))

    if not ranked:
        return None
    ranked.sort(key=lambda pair: (pair[0], pair[1].get("latestImport", "")), reverse=True)
    top_score = ranked[0][0]
    top_names = {item["name"].lower() for score, item in ranked if score == top_score}
    return ranked[0][1] if len(top_names) == 1 else None


def parse_effective_date(question):
    match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", question)
    if not match:
        return ""
    month, day, year = (int(value) for value in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def detect_campaign_change(question, campaigns):
    normalized = question.lower()
    change_words = (
        "changed", "raised", "lowered", "increased", "decreased", "paused", "unpaused",
        "turned off", "turn off", "shut off", "stopped", "disabled", "turned on", "turn on",
        "started", "enabled", "set the bid", "set bid", "budget", "remember", "note that",
        "negated", "negative keyword", "negative phrase", "excluded", "blocked search",
    )

    if not any(word in normalized for word in change_words):
        return None

    campaign = match_campaign_reference(question, campaigns)

    if not campaign:
        return {"needsCampaign": True}

    target_aliases = {
        "close match": "close-match",
        "close-match": "close-match",
        "loose match": "loose-match",
        "loose-match": "loose-match",
        "substitute": "substitutes",
        "substitutes": "substitutes",
        "complement": "complements",
        "complements": "complements",
    }
    target_name = next((value for phrase, value in target_aliases.items() if phrase in normalized), "")
    keyword_terms = [term.strip() for term in re.findall(r'["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]', question) if term.strip()]

    if any(word in normalized for word in ("negated", "negative keyword", "negative phrase", "excluded", "blocked search")):
        change_type = "Negative keyword change"
    elif target_name and any(word in normalized for word in ("changed", "raised", "lowered", "increased", "decreased", "set")):
        change_type = "Target bid change"
    elif "bid" in normalized:
        change_type = "Bid change"
    elif "budget" in normalized:
        change_type = "Budget change"
    elif any(word in normalized for word in ("paused", "unpaused", "turned off", "turn off", "shut off", "stopped", "disabled", "turned on", "turn on", "started", "enabled")):
        change_type = "Status change"
    else:
        change_type = "Campaign change"

    question_without_dates = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", question)
    raw_values = re.findall(r"\$?((?:\d+(?:\.\d+)?)|(?:\.\d+))", question_without_dates)
    values = [f"{float(value):g}" if value.startswith(".") else value for value in raw_values]
    previous_value = values[-2] if len(values) >= 2 else ""
    new_value = values[-1] if values else ""
    if change_type == "Status change":
        if any(word in normalized for word in ("unpaused", "turned on", "turn on", "started", "enabled")):
            new_value = "Enabled"
        elif any(word in normalized for word in ("paused", "turned off", "turn off", "shut off", "stopped", "disabled")):
            new_value = "Paused"

    return {
        "campaignName": campaign["name"],
        "changeType": change_type,
        "targetName": target_name,
        "keywordTerms": keyword_terms,
        "details": question.strip(),
        "previousValue": previous_value,
        "newValue": new_value,
        "effectiveDate": parse_effective_date(question),
    }


def short_log_date(value):
    try:
        parsed = date.fromisoformat(str(value or ""))
        return f"{parsed.month}/{parsed.day}/{str(parsed.year)[2:]}"
    except ValueError:
        return str(value or "").strip()


def describe_campaign_change(payload):
    campaign = str(payload.get("campaignName", "Campaign")).strip()
    change_type = str(payload.get("changeType", "Campaign change")).strip()
    previous_value = str(payload.get("previousValue", "")).strip()
    new_value = str(payload.get("newValue", "")).strip()
    target_name = str(payload.get("targetName", "")).strip()
    keyword_terms = [str(term).strip() for term in payload.get("keywordTerms", []) if str(term).strip()]
    effective_date = short_log_date(payload.get("effectiveDate", ""))

    if change_type == "Negative keyword change" and keyword_terms:
        quoted_terms = " and ".join(f'"{term}"' for term in keyword_terms)
        description = f"{campaign} negative keywords added: {quoted_terms}"
    elif change_type == "Target bid change" and target_name and previous_value and new_value:
        direction = "lowered" if float(new_value) < float(previous_value) else "raised" if float(new_value) > float(previous_value) else "changed"
        description = f"{campaign} {target_name} bid {direction} from ${previous_value} to ${new_value}"
    elif change_type == "Status change" and new_value:
        description = f"{campaign} {new_value.lower()}"
    elif change_type == "Bid change" and previous_value and new_value:
        description = f"{campaign} bid changed from ${previous_value} to ${new_value}"
    elif change_type == "Bid change" and new_value:
        description = f"{campaign} bid set to ${new_value}"
    elif change_type == "Budget change" and previous_value and new_value:
        description = f"{campaign} budget changed from ${previous_value} to ${new_value}"
    elif change_type == "Budget change" and new_value:
        description = f"{campaign} budget set to ${new_value}"
    else:
        description = f"{campaign}: {change_type.lower()}"

    return f"{description} on {effective_date}" if effective_date else description


def save_campaign_change(payload):
    campaign_name = str(payload.get("campaignName", "")).strip()
    details = str(payload.get("details", "")).strip()

    if not campaign_name or not details:
        return {"ok": False, "error": "Campaign name and change details are required."}

    known_campaigns = campaign_catalog()
    matched_campaign = next((item for item in known_campaigns if item["name"].lower() == campaign_name.lower()), None)

    if not matched_campaign:
        return {"ok": False, "error": "That campaign was not found in the latest campaign data."}

    conn = connect()
    cur = conn.cursor()
    ensure_campaign_change_schema(cur)
    logged_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = describe_campaign_change({**payload, "campaignName": matched_campaign["name"]})
    cur.execute(
        """
        INSERT INTO campaign_change_log
        (campaign_name, target_name, change_type, details, previous_value, new_value,
         effective_date, summary, logged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            matched_campaign["name"],
            str(payload.get("targetName", "")).strip(),
            str(payload.get("changeType", "Campaign change")).strip(),
            details,
            str(payload.get("previousValue", "")).strip(),
            str(payload.get("newValue", "")).strip(),
            str(payload.get("effectiveDate", "")).strip(),
            summary,
            logged_at,
        ),
    )
    change_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "id": change_id,
        "campaignName": matched_campaign["name"],
        "changeType": str(payload.get("changeType", "Campaign change")).strip(),
        "newValue": str(payload.get("newValue", "")).strip(),
        "effectiveDate": str(payload.get("effectiveDate", "")).strip(),
        "summary": summary,
        "loggedAt": logged_at,
    }


def preview_campaign_change(payload):
    campaign_name = str(payload.get("campaignName", "")).strip()
    details = str(payload.get("details", "")).strip()
    effective_date = str(payload.get("effectiveDate", "")).strip()
    known_campaigns = campaign_catalog()
    matched = next((item for item in known_campaigns if item["name"].lower() == campaign_name.lower()), None)

    if not matched:
        return {"ok": False, "error": "Select a campaign before previewing the change."}
    if not details:
        return {"ok": False, "error": "Describe what changed before previewing the log."}
    try:
        date.fromisoformat(effective_date)
    except ValueError:
        return {"ok": False, "error": "Select the date when the change was made."}

    contextual_details = f"{details} in {matched['name']} on {short_log_date(effective_date)}"
    pending = detect_campaign_change(contextual_details, known_campaigns)
    if not pending or pending.get("needsCampaign"):
        pending = {
            "campaignName": matched["name"],
            "changeType": "Campaign note",
            "targetName": "",
            "keywordTerms": [],
            "previousValue": "",
            "newValue": "",
        }

    pending.update({
        "campaignName": matched["name"],
        "details": details,
        "effectiveDate": effective_date,
    })
    pending["summary"] = describe_campaign_change(pending)
    return {
        "ok": True,
        "answer": f"I understood: {pending['summary']}.",
        "pendingLog": pending,
    }


def change_options_payload():
    unique_campaigns = {}
    for item in campaign_catalog():
        unique_campaigns.setdefault(item["name"].lower(), item)

    recent_changes = []
    if DB_PATH.exists():
        conn = connect()
        cur = conn.cursor()
        ensure_campaign_change_schema(cur)
        rows = cur.execute(
            """
            SELECT id, campaign_name, target_name, change_type, details, previous_value,
                   new_value, effective_date, summary, logged_at
            FROM campaign_change_log
            ORDER BY logged_at DESC, id DESC
            LIMIT 20
            """
        ).fetchall()
        conn.commit()
        conn.close()
        recent_changes = [
            {
                "id": row["id"],
                "campaignName": row["campaign_name"],
                "targetName": row["target_name"],
                "changeType": row["change_type"],
                "details": row["details"],
                "previousValue": row["previous_value"],
                "newValue": row["new_value"],
                "effectiveDate": row["effective_date"],
                "summary": row["summary"] or row["details"],
                "loggedAt": row["logged_at"],
            }
            for row in rows
        ]

    return {
        "campaigns": sorted(unique_campaigns.values(), key=lambda item: item["name"].lower()),
        "recentChanges": recent_changes,
    }


def assistant_payload(question, history=None, requested_period="last7"):
    question = (question or "").strip()
    contextual_question = question
    normalized = question.lower()
    history = history if isinstance(history, list) else []
    if len(normalized.split()) <= 5 and history:
        previous_questions = [
            item.get("text", "")
            for item in history
            if item.get("role") == "user"
            and item.get("text", "").strip().lower() != question.lower()
        ]
        if previous_questions:
            contextual_question = f"{previous_questions[-1]} {question}"
            normalized = contextual_question.lower()

    period_aliases = {
        "today": "yesterday",
        "yesterday": "yesterday",
        "last7": "last7",
        "last30": "last30",
        "last60": "last60",
        "7 days": "last7",
        "7 day": "last7",
        "30 days": "last30",
        "30 day": "last30",
        "60 days": "last60",
        "60 day": "last60",
    }
    analysis_period = period_aliases.get(requested_period, "last7")
    for phrase, value in period_aliases.items():
        if phrase in normalized:
            analysis_period = value
    period_label = {
        "yesterday": "yesterday",
        "last7": "the last 7 days",
        "last30": "the last 30 days",
        "last60": "the trailing 60 days",
    }[analysis_period]
    sales_yesterday = home_payload("yesterday")
    sales_last7 = home_payload("last7")
    selected_sales = home_payload(analysis_period)
    campaigns_last7 = campaigns_payload(analysis_period).get("campaigns", [])
    designs_last7 = designs_payload(analysis_period).get("designs", [])
    daily_audit = build_daily_audit()
    audit_bid_actions = [
        item for item in daily_audit.get("bidRecommendations", [])
        if item.get("action") != "Hold"
    ]
    audit_search_terms = daily_audit.get("searchTermFindings", [])
    audit_sales_opportunities = daily_audit.get("salesPatternOpportunities", [])
    evidence = []

    if not question:
        return {"answer": "Ask me about sales, designs, campaigns, ACOS, ROAS, or what deserves attention today.", "evidence": []}

    pending_change = detect_campaign_change(contextual_question, campaign_catalog())

    if pending_change:
        if pending_change.get("needsCampaign"):
            return {
                "answer": "I can log that change, but I could not identify the campaign. Include the campaign name exactly as it appears on the Campaigns screen.",
                "evidence": [],
            }

        pending_change["summary"] = describe_campaign_change(pending_change)
        return {
            "answer": f"I understood: {pending_change['summary']}. Review the details below, then confirm to save it.",
            "evidence": ["Nothing has been saved yet."],
            "pendingLog": pending_change,
            "question": question,
        }

    navigation_commands = (
        ("campaign", "ads", "Campaigns"),
        ("ad group", "ads", "Campaigns"),
        ("keyword", "ads", "Campaigns"),
        ("search term", "ads", "Campaigns"),
        ("royalt", "ads", "Royalty"),
        ("analytic", "analytics", ""),
        ("design", "home", ""),
        ("home", "home", ""),
        ("dashboard", "home", ""),
        ("ads", "ads", "Overview"),
    )
    is_navigation = any(phrase in normalized for phrase in ("open ", "go to ", "take me to ", "show the ", "show me the "))
    if is_navigation:
        destination = next((item for item in navigation_commands if item[0] in normalized), None)
        if destination:
            keyword, page, ads_tab = destination
            label = ads_tab or page.title()
            action = {"type": "navigate", "page": page}
            if ads_tab:
                action["adsTab"] = ads_tab
            return {
                "answer": f"Opening {label}.",
                "evidence": [],
                "action": action,
                "question": question,
            }

    if any(phrase in normalized for phrase in ("what can you do", "help me", "commands", "how do i use")):
        return {
            "answer": (
                "I can explain the daily audit, exact bid suggestions, search-term findings, sales opportunities, campaigns, ACOS, ROAS, and royalty-tier data; "
                "compare 7, 30, or 60 days; open app sections; and prepare a campaign-change log for your approval. "
                "For example: 'Explain the Rain Frogs bid recommendation', 'Which search terms need attention?', or 'I lowered a bid today, please log it.'"
            ),
            "evidence": ["Campaign changes are saved only after you tap Log change."],
            "question": question,
        }

    def audit_match_score(item, fields):
        words = {
            word.strip(".,?!:;'\"") for word in normalized.split()
            if len(word.strip(".,?!:;'\"")) >= 3
        }
        text = " ".join(str(item.get(field, "")) for field in fields).lower()
        return sum(1 for word in words if word in text)

    if any(phrase in normalized for phrase in ("work on today", "do today", "attention today", "daily audit", "audit summary")):
        top_bids = audit_bid_actions[:3]
        top_search = audit_search_terms[:2]
        top_sales = audit_sales_opportunities[:2]
        parts = []
        if top_bids:
            parts.append(
                "Start with these bid reviews: " + "; ".join(
                    f"{item['campaignName']} / {item['target']} from ${item['currentBid']:.2f} to ${item['suggestedBid']:.2f}"
                    for item in top_bids
                ) + "."
            )
            evidence.extend([
                f"{item['campaignName']} | {item['target']} | {item['clicks']} clicks | ${item['spend']:.2f} spend | {item['orders']} orders | {item['roas']:.2f} ROAS"
                for item in top_bids
            ])
        if top_search:
            parts.append("Then review search terms: " + "; ".join(f"{item['searchTerm']} — {item['action']}" for item in top_search) + ".")
        if top_sales:
            parts.append("Sales opportunities to inspect: " + "; ".join(f"{item['title']} — {item['pattern']}" for item in top_sales) + ".")
        if daily_audit.get("targetDataStale"):
            parts.append(f"Do not act on bid values until targets refresh; current target data is only through {daily_audit.get('targetReportDate') or 'an unknown date'}.")
        answer = " ".join(parts) or "The audit does not currently contain an action that meets its evidence thresholds."
    elif any(phrase in normalized for phrase in ("bid recommendation", "bid suggestion", "which bid", "change bid", "lower bid", "raise bid", "increase bid", "explain bid")):
        ranked = sorted(
            ((audit_match_score(item, ("campaignName", "adGroupName", "target", "matchType")), item) for item in audit_bid_actions),
            key=lambda pair: (pair[0], pair[1].get("spend", 0)),
            reverse=True,
        )
        matched = ranked[0][1] if ranked and ranked[0][0] > 0 else None
        if matched:
            answer = (
                f"The audit recommends {matched['action'].lower()} for {matched['campaignName']} / {matched['target']}: "
                f"${matched['currentBid']:.2f} to ${matched['suggestedBid']:.2f} ({matched['changePercent']:+.1f}%). "
                f"The evidence is {matched['clicks']} clicks, ${matched['spend']:.2f} spend, {matched['orders']} orders, and {matched['roas']:.2f} ROAS. "
                f"{matched['reason']} Confidence is {matched['confidence']}."
            )
            evidence.append(f"Target report through {matched.get('reportDate') or daily_audit.get('targetReportDate') or 'latest'}")
        elif audit_bid_actions:
            answer = "The highest-priority bid reviews are: " + "; ".join(
                f"{item['campaignName']} / {item['target']}: ${item['currentBid']:.2f} to ${item['suggestedBid']:.2f}"
                for item in audit_bid_actions[:5]
            ) + ". Name one campaign or target and I will explain its evidence."
        else:
            answer = "No target currently meets the audit's cautious threshold for a bid change."
        if daily_audit.get("targetDataStale"):
            answer += f" The target snapshot is stale through {daily_audit.get('targetReportDate') or 'an unknown date'}, so refresh before acting."
    elif any(phrase in normalized for phrase in ("search term", "customer search", "negative keyword", "exact target")):
        ranked = sorted(
            ((audit_match_score(item, ("searchTerm", "campaignName", "sourceTarget")), item) for item in audit_search_terms),
            key=lambda pair: (pair[0], pair[1].get("spend", 0)),
            reverse=True,
        )
        matched = ranked[0][1] if ranked and ranked[0][0] > 0 else None
        if matched:
            answer = (
                f"For {matched['campaignName']}, the customer search term '{matched['searchTerm']}' has {matched['clicks']} clicks, "
                f"${matched['spend']:.2f} spend, {matched['orders']} orders, and {matched['roas']:.2f} ROAS. "
                f"The audit action is: {matched['action']}. This is a review recommendation, not an automatic negative or keyword change."
            )
            evidence.append(f"Search-term report through {matched.get('reportDate') or 'latest'}")
        elif audit_search_terms:
            answer = "The audit found these search terms for review: " + "; ".join(
                f"{item['searchTerm']} ({item['action']}, {item['roas']:.2f} ROAS)" for item in audit_search_terms[:6]
            ) + "."
            evidence.append(f"{daily_audit.get('searchTermPeriod') or 'latest'} search-term snapshot")
        else:
            answer = "No imported search term currently meets the audit's action thresholds."
    elif any(phrase in normalized for phrase in ("sales opportunity", "sales pattern", "new opportunity", "advertising opportunity", "organic opportunity")):
        ranked = sorted(
            ((audit_match_score(item, ("title", "pattern")), item) for item in audit_sales_opportunities),
            key=lambda pair: (pair[0], pair[1].get("recentUnits", 0)),
            reverse=True,
        )
        matched = ranked[0][1] if ranked and ranked[0][0] > 0 else None
        if matched:
            answer = (
                f"{matched['title']} has {matched['recentUnits']} units in the latest seven days versus {matched['previousUnits']} in the prior seven. "
                f"The pattern is '{matched['pattern']}'. {matched['nextStep']} "
                "This flags a sales pattern; it does not claim the sales were organic or caused by advertising."
            )
            evidence.append(f"Merch sales through {matched.get('periodEnd') or daily_audit.get('salesDataThrough') or 'latest'}")
        elif audit_sales_opportunities:
            answer = "The highest-ranked sales opportunities are: " + "; ".join(
                f"{item['title']} ({item['recentUnits']} vs {item['previousUnits']} units, {item['pattern']})"
                for item in audit_sales_opportunities[:5]
            ) + "."
        else:
            answer = "No recent sales pattern currently meets the opportunity threshold."
    elif any(phrase in normalized for phrase in ("pause", "wasting", "waste", "campaigns need review", "campaign need review", "which campaigns")):
        review = sorted(
            [
                item for item in campaigns_last7
                if (item["orders"] == 0 and item["spend"] >= 5)
                or (item["orders"] > 0 and item["spend"] >= 5 and item["roas"] < 5)
            ],
            key=lambda item: (item["orders"] > 0, item["roas"] if item["orders"] else -item["spend"]),
        )[:5]
        if review:
            answer = "These campaigns deserve target-level review, but the current evidence is not enough to automatically pause the entire campaign: " + "; ".join(
                f"{item['name']} spent ${item['spend']:.2f}, produced {item['orders']} {'order' if item['orders'] == 1 else 'orders'}, and has {item['roas']:.2f} ROAS" for item in review
            ) + ". Check search terms and targets first, then lower or pause only the weakest traffic."
            evidence.extend([f"Amazon Ads, last 7 days ending {item['reportDate'] or 'latest report'}" for item in review])
        else:
            answer = "No campaign currently meets the app's cautious review rule of at least $10 spend and zero orders in the last seven days."
    elif any(phrase in normalized for phrase in ("heating up", "top design", "best design", "selling")):
        leaders = designs_last7[:5]
        answer = "The strongest designs in the latest seven-day sales data are: " + "; ".join(
            f"{item['title']} with {item['units']} units and {design_royalties_text(item)} royalties" for item in leaders
        ) + "."
        evidence.append(f"Merch sales, last 7 days ending {sales_last7.get('reportDate') or 'latest report'}")
    elif any(phrase in normalized for phrase in ("sales down", "sales up", "compare today", "compare yesterday")):
        yesterday_units = int(sales_yesterday.get("sales") or 0)
        seven_units = int(sales_last7.get("sales") or 0)
        daily_average = seven_units / 7 if seven_units else 0
        difference = yesterday_units - daily_average
        direction = "above" if difference >= 0 else "below"
        answer = f"Yesterday had {yesterday_units} units. The latest seven-day report averages {daily_average:.1f} units per day, so yesterday was {abs(difference):.1f} units {direction} that average. This is a short comparison, not yet proof of a trend."
        evidence.extend([
            f"Yesterday ending {sales_yesterday.get('reportDate') or 'latest report'}: {yesterday_units} units",
            f"Last 7 days ending {sales_last7.get('reportDate') or 'latest report'}: {seven_units} units",
        ])
    elif "royalty" in normalized and "tier" in normalized:
        tier = royalty_tier_payload()
        primary = tier.get("primary")
        if primary:
            answer = f"The current estimate is {primary['tier']['name']} at {primary['nonOrganicRatio'] * 100:.1f}% non-organic sales for {primary['label']}. This estimate compares imported ad orders with imported Merch units and may need reconciliation when Amazon attribution differs."
            evidence.append(f"{primary['label']}: {primary['adOrders']} ad orders and {primary['units']} total units")
        else:
            answer = "The app does not yet have matching sales and ad periods needed to estimate a royalty tier."
    else:
        stop_words = {"about", "auto", "doing", "does", "have", "show", "tell", "that", "this", "what", "when", "where", "which", "with", "your"}
        question_terms = {
            word.strip(".,?!:;'\"")
            for word in normalized.split()
            if len(word.strip(".,?!:;'\"")) >= 3 and word.strip(".,?!:;'\"") not in stop_words
        }
        all_designs = designs_payload(analysis_period).get("designs", [])
        all_campaigns = campaigns_payload(analysis_period).get("campaigns", [])

        def mention_score(name):
            name_terms = {
                word.strip(".,?!:;'\"")
                for word in name.lower().split()
                if len(word.strip(".,?!:;'\"")) >= 3 and word.strip(".,?!:;'\"") not in stop_words
            }
            return len(question_terms & name_terms)

        ranked_designs = sorted(((mention_score(item["title"]), item) for item in all_designs), key=lambda pair: pair[0], reverse=True)
        ranked_campaigns = sorted(((mention_score(item["name"]), item) for item in all_campaigns), key=lambda pair: pair[0], reverse=True)
        best_design_score = ranked_designs[0][0] if ranked_designs else 0
        best_campaign_score = ranked_campaigns[0][0] if ranked_campaigns else 0
        found_designs = [item for score, item in ranked_designs if score == best_design_score and score > 0][:4]
        found_campaigns = [item for score, item in ranked_campaigns if score == best_campaign_score and score > 0][:4]
        if not found_designs and not found_campaigns:
            matches = search_payload(question, analysis_period)
            found_designs = matches["designs"][:4]
            found_campaigns = matches["campaigns"][:4]
        if found_campaigns and any(word in normalized for word in ("why", "weak", "wrong", "happening", "explain")):
            campaign = found_campaigns[0]
            detail = campaign_detail_payload(campaign["name"], analysis_period)
            targets = sorted(detail.get("targets", []), key=lambda item: (item.get("orders", 0), item.get("roas", 0), -item.get("spend", 0)))
            answer = (
                f"For {period_label}, {campaign['name']} spent {currency_amount_text(campaign['spend'], campaign.get('currency'))}, generated "
                f"{currency_amount_text(campaign['sales'], campaign.get('currency'))} in ad sales and {campaign['orders']} orders, for {campaign['roas']:.2f} ROAS. "
            )
            if targets:
                target = targets[0]
                answer += (
                    f"The weakest visible target is {target['target']}: {currency_amount_text(target['spend'], campaign.get('currency'))} spend, "
                    f"{target['orders']} orders, and {target['roas']:.2f} ROAS. Review that target before changing the whole campaign."
                )
            else:
                answer += "No matching target rows are available yet, so I would not recommend a bid change from campaign totals alone."
            evidence.append(f"Amazon Ads, {period_label}, report ending {campaign.get('reportDate') or 'latest'}")
        elif found_designs or found_campaigns:
            pieces = []
            if found_designs:
                pieces.append("Design matches: " + "; ".join(f"{item['title']} ({item['units']} units, {design_royalties_text(item)} royalties)" for item in found_designs))
            if found_campaigns:
                pieces.append("Campaign matches: " + "; ".join(f"{item['name']} ({item['orders']} orders, {item['roas']:.2f} ROAS)" for item in found_campaigns))
            answer = " ".join(pieces) + "."
            evidence.append(f"Imported sales and campaign data for {period_label}")
        else:
            top_designs = designs_last7[:3]
            top_campaigns = sorted(campaigns_last7, key=lambda item: item.get("sales", 0), reverse=True)[:3]
            answer = (
                f"For {period_label}, Merch Agent has {int(selected_sales.get('sales') or 0)} units. "
                + ("Top designs are " + ", ".join(f"{item['title']} ({item['units']} units)" for item in top_designs) + ". " if top_designs else "")
                + ("Top ad campaigns by sales are " + ", ".join(f"{item['name']} (${item['sales']:.2f})" for item in top_campaigns) + ". " if top_campaigns else "")
                + "Name a campaign or design and ask me to compare, explain, or review it."
            )
            evidence.append(f"Local Merch Agent data for {period_label}")

    return {
        "answer": answer,
        "evidence": evidence,
        "question": question,
        "suggestions": ["Summarize today's daily audit", "Which bids should I change?", "Which search terms need attention?", "What sales opportunities should I review?"],
    }


class MerchAgentHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def require_authentication(self):
        if authorization_is_valid(self.headers.get("Authorization", "")):
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Merch Agent", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = b'{"error":"Authentication required"}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def end_headers(self):
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Merch-Agent-Helper")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            self.send_json({
                "ok": True,
                "databaseReady": DB_PATH.exists(),
                "authenticationRequired": authentication_enabled(),
                "salesUploadEnabled": authentication_enabled() or self.client_address[0] in {"127.0.0.1", "::1"},
            })
            return

        if self.require_authentication():
            return

        if parsed.path == "/api/sales-sync-status":
            self.send_json(sales_sync_status())
            return

        if parsed.path == "/api/home":
            query = parse_qs(parsed.query)
            period = query.get("period", ["yesterday"])[0]
            self.send_json(home_payload(period))
            return

        if parsed.path == "/api/royalty-tier":
            self.send_json(royalty_tier_payload())
            return

        if parsed.path == "/api/ads":
            query = parse_qs(parsed.query)
            period = query.get("period", ["today"])[0]
            self.send_json(ads_payload(
                period,
                query.get("start", [""])[0],
                query.get("end", [""])[0],
            ))
            return

        if parsed.path == "/api/analytics":
            query = parse_qs(parsed.query)
            period = query.get("period", ["30D"])[0]
            self.send_json(analytics_payload(
                period,
                query.get("start", [""])[0],
                query.get("end", [""])[0],
            ))
            return

        if parsed.path == "/api/designs":
            query = parse_qs(parsed.query)
            period = query.get("period", ["last30"])[0]
            self.send_json(designs_payload(period))
            return

        if parsed.path == "/api/campaigns":
            query = parse_qs(parsed.query)
            period = query.get("period", ["last30"])[0]
            search = query.get("q", [""])[0]
            self.send_json(campaigns_payload(period, search))
            return

        if parsed.path == "/api/daily-audit":
            self.send_json(build_daily_audit())
            return

        if parsed.path == "/api/change-options":
            self.send_json(change_options_payload())
            return

        if parsed.path == "/api/refresh-status":
            self.send_json(refresh_status_payload())
            return

        if parsed.path == "/api/campaign-detail":
            query = parse_qs(parsed.query)
            period = query.get("period", ["last30"])[0]
            campaign_name = query.get("name", [""])[0]
            self.send_json(campaign_detail_payload(campaign_name, period))
            return

        if parsed.path == "/api/search":
            query = parse_qs(parsed.query)
            period = query.get("period", ["last30"])[0]
            search = query.get("q", [""])[0]
            self.send_json(search_payload(search, period))
            return

        return super().do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        allowed_paths = {"/api/assistant", "/api/campaign-change", "/api/change-preview", "/api/refresh-ads-now", "/api/sales-sync", "/api/sales-upload"}
        if parsed.path not in allowed_paths:
            self.send_json({"error": "Not found"}, 404)
            return

        if self.require_authentication():
            return


        if parsed.path == "/api/sales-sync":
            origin = self.headers.get("Origin", "")
            helper_header = self.headers.get("X-Merch-Agent-Helper", "")
            if not origin.startswith("chrome-extension://") or helper_header != "sales-sync-v1":
                self.send_json({"error": "This endpoint is only available to the private Chrome helper."}, 403)
                return

        if parsed.path == "/api/sales-upload" and not (
            authentication_enabled() or self.client_address[0] in {"127.0.0.1", "::1"}
        ):
            self.send_json({"error": "Secure login must be configured before browser uploads are enabled."}, 403)
            return

        try:
            request_limit = (MAX_SALES_UPLOAD_BYTES * 2) if parsed.path == "/api/sales-upload" else 50000
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > request_limit:
                self.send_json({"error": "Request is too large"}, 413)
                return
            length = min(content_length, request_limit)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if parsed.path == "/api/campaign-change":
                result = save_campaign_change(payload)
                self.send_json(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/change-preview":
                result = preview_campaign_change(payload)
                self.send_json(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/sales-sync":
                result = sync_downloaded_sales_report(payload.get("path", ""))
                self.send_json(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/sales-upload":
                result = sync_uploaded_sales_report(payload.get("fileName", ""), payload.get("data", ""))
                self.send_json(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/refresh-ads-now":
                result = start_hosted_ads_refresh_now()
                self.send_json(result, 202 if result.get("ok") else 409)
            else:
                self.send_json(assistant_payload(
                    payload.get("question", ""),
                    payload.get("history", []),
                    payload.get("period", "last7"),
                ))
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid request"}, 400)


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8788"))
    server = ThreadingHTTPServer((host, port), MerchAgentHandler)
    print(f"Merch Agent v2 running at http://{host}:{port}")
    print(f"API health: http://127.0.0.1:{port}/api/health")
    print(f"Password protection: {'enabled' if authentication_enabled() else 'disabled for local use'}")
    if DAILY_REFRESH_ENABLED:
        threading.Thread(target=daily_refresh_scheduler, daemon=True).start()
    else:
        print("Hosted daily refresh: disabled", flush=True)
    server.serve_forever()
