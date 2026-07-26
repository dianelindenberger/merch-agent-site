from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
import hashlib
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
HOSTED_REFRESH_STATUS_LOCK = threading.Lock()
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
from database import DATA_DIR, DB_PATH, setup_database  # noqa: E402
from reporting_day import amazon_reporting_date  # noqa: E402
from ai_assistant import (  # noqa: E402
    AssistantConfig,
    AssistantUnavailable,
    ConversationStore,
    ResponsesAssistant,
    UngroundedAnswer,
    owner_hash,
)
from ai_tools import RestrictedAIToolLayer  # noqa: E402
from ai_usage import UsageControls, UsageLimitReached  # noqa: E402
from ai_writes import RecommendationWriteCoordinator  # noqa: E402

INCOMING_REPORTS = Path(os.getenv("MERCH_AGENT_REPORTS_DIR", DATA_DIR / "incoming_reports")).expanduser().resolve()
AUTH_USERNAME = os.getenv("MERCH_AGENT_USERNAME", "").strip()
AUTH_PASSWORD = os.getenv("MERCH_AGENT_PASSWORD", "")
MAX_SALES_UPLOAD_BYTES = 20 * 1024 * 1024
MERCH_AGENT_IMPORT_TOKEN = os.getenv("MERCH_AGENT_IMPORT_TOKEN", "").strip()
DAILY_REFRESH_ENABLED = os.getenv("MERCH_AGENT_DAILY_REFRESH_ENABLED", "false").lower() in {"1", "true", "yes"}
DAILY_REFRESH_TIME = os.getenv("MERCH_AGENT_DAILY_REFRESH_TIME", "06:00")
AD_REFRESH_TIMES = os.getenv("MERCH_AGENT_AD_REFRESH_TIMES", "05:30")
EASTERN_TIME = ZoneInfo("America/New_York")
RECOMMENDATION_COOLDOWN_DAYS = max(1, int(os.getenv("MERCH_AGENT_RECOMMENDATION_COOLDOWN_DAYS", "7")))
RECOMMENDATION_MIN_POST_CHANGE_CLICKS = max(1, int(os.getenv("MERCH_AGENT_RECOMMENDATION_MIN_POST_CHANGE_CLICKS", "20")))
HOSTED_CURRENT_REFRESH_TIMEOUT_SECONDS = max(
    1800, int(os.getenv("MERCH_AGENT_CURRENT_REFRESH_TIMEOUT_SECONDS", "7200"))
)
HOSTED_FULL_REFRESH_TIMEOUT_SECONDS = max(
    7200, int(os.getenv("MERCH_AGENT_FULL_REFRESH_TIMEOUT_SECONDS", "28800"))
)

MARKET_NAMES = {
    ".com": "United States",
    ".co.uk": "United Kingdom",
    ".de": "Germany",
    ".fr": "France",
    ".it": "Italy",
    ".es": "Spain",
    ".co.jp": "Japan",
}

MARKET_QUERY_TERMS = {
    "united states": ".com", "us": ".com", "usa": ".com", "america": ".com",
    "germany": ".de", "german": ".de", "de": ".de",
    "united kingdom": ".co.uk", "uk": ".co.uk", "britain": ".co.uk", "gb": ".co.uk",
    "france": ".fr", "french": ".fr", "fr": ".fr",
    "italy": ".it", "italian": ".it", "it": ".it",
    "spain": ".es", "spanish": ".es", "es": ".es",
    "japan": ".co.jp", "japanese": ".co.jp", "jp": ".co.jp",
}


def market_from_text(text):
    """Return the last explicit marketplace mentioned in a question."""
    normalized = str(text or "").lower()
    matches = []
    for term, code in MARKET_QUERY_TERMS.items():
        for match in re.finditer(rf"(?<![a-z]){re.escape(term)}(?![a-z])", normalized):
            matches.append((match.start(), code))
    return max(matches, default=(0, ""))[1]

SALES_REPORT_COLUMNS = {"Title", "Purchased", "Royalties", "Revenue", "Date"}
SALES_REPORT_EXTENSIONS = {".csv", ".xlsx", ".xls"}
RECOMMENDATION_ACTIONS = {
    "made_change", "log_change", "ignore", "dismiss", "remind_later", "keep_monitoring", "discuss",
}
RECOMMENDATION_STATUSES = {
    "Proposed", "Completed", "Deferred", "Ignored", "Action logged", "Monitoring", "Dismissed",
    "Superseded", "Resolved", "Expired",
}


def ensure_recommendation_interactions_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_interactions (
            recommendation_id TEXT PRIMARY KEY,
            recommendation_type TEXT NOT NULL,
            recommendation_context TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Proposed',
            last_action TEXT NOT NULL,
            reason TEXT,
            reminder_at TEXT,
            acted_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            campaign TEXT,
            confidence TEXT,
            supporting_metrics TEXT,
            user_action TEXT,
            user_notes TEXT,
            date_created TEXT,
            date_completed TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recommendation_interactions)").fetchall()}
    additions = {
        "campaign": "TEXT",
        "confidence": "TEXT",
        "supporting_metrics": "TEXT",
        "user_action": "TEXT",
        "user_notes": "TEXT",
        "date_created": "TEXT",
        "date_completed": "TEXT",
        "campaign_id": "TEXT",
        "ad_group_id": "TEXT",
        "target_id": "TEXT",
        "original_recommended_action": "TEXT",
        "actual_action_taken": "TEXT",
        "previous_value": "TEXT",
        "actual_new_value": "TEXT",
        "change_category": "TEXT",
        "effective_at": "TEXT",
        "logged_at": "TEXT",
        "user_id": "TEXT",
        "idempotency_key": "TEXT",
        "subject_key": "TEXT",
        "superseded_by_recommendation_id": "TEXT",
        "supersedes_recommendation_id": "TEXT",
        "related_change_log_id": "INTEGER",
        "reason_for_status": "TEXT",
        "reconciliation_result": "TEXT",
        "effective_change_date": "TEXT",
        "monitoring_until": "TEXT",
        "minimum_post_change_clicks": "INTEGER NOT NULL DEFAULT 20",
        "post_change_metrics": "TEXT",
        "post_change_data_through": "TEXT",
    }
    for name, column_type in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE recommendation_interactions ADD COLUMN {name} {column_type}")
    conn.execute("UPDATE recommendation_interactions SET date_created = COALESCE(date_created, acted_at, updated_at) WHERE date_created IS NULL")
    conn.execute("UPDATE recommendation_interactions SET date_completed = COALESCE(date_completed, acted_at) WHERE status = 'Completed' AND date_completed IS NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_recommendation_interactions_idempotency ON recommendation_interactions(idempotency_key) WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendation_interactions_status ON recommendation_interactions(status, updated_at)")
    conn.commit()


def recommendation_interactions_payload():
    conn = connect()
    ensure_recommendation_interactions_table(conn)
    rows = conn.execute(
        "SELECT * FROM recommendation_interactions ORDER BY CASE status WHEN 'Proposed' THEN 0 WHEN 'Deferred' THEN 1 WHEN 'Monitoring' THEN 2 WHEN 'Action logged' THEN 3 WHEN 'Ignored' THEN 4 WHEN 'Dismissed' THEN 4 WHEN 'Superseded' THEN 5 ELSE 6 END, COALESCE(updated_at, date_created) DESC"
    ).fetchall()
    conn.close()
    return {
        "interactions": {
            row["recommendation_id"]: {
                "recommendationId": row["recommendation_id"],
                "recommendationType": row["recommendation_type"],
                "context": json.loads(row["recommendation_context"] or "{}"),
                "status": row["status"],
                "lastAction": row["last_action"],
                "reason": row["reason"] or "",
                "reminderAt": row["reminder_at"] or "",
                "actedAt": row["acted_at"],
                "updatedAt": row["updated_at"],
                "campaign": row["campaign"] or "",
                "confidence": row["confidence"] or "",
                "supportingMetrics": json.loads(row["supporting_metrics"] or "{}"),
                "userAction": row["user_action"] or row["last_action"],
                "userNotes": row["user_notes"] or row["reason"] or "",
                "dateCreated": row["date_created"] or row["acted_at"],
                "dateCompleted": row["date_completed"] or "",
                "campaignId": row["campaign_id"] or "",
                "adGroupId": row["ad_group_id"] or "",
                "targetId": row["target_id"] or "",
                "originalRecommendedAction": row["original_recommended_action"] or "",
                "actualActionTaken": row["actual_action_taken"] or "",
                "previousValue": row["previous_value"] or "",
                "actualNewValue": row["actual_new_value"] or "",
                "changeCategory": row["change_category"] or "",
                "effectiveAt": row["effective_at"] or "",
                "loggedAt": row["logged_at"] or row["acted_at"],
                "userId": row["user_id"] or "",
                "idempotencyKey": row["idempotency_key"] or "",
                "subjectKey": row["subject_key"] or "",
                "relatedChangeLogId": row["related_change_log_id"],
                "reasonForStatus": row["reason_for_status"] or "",
                "reconciliationResult": row["reconciliation_result"] or "",
                "effectiveChangeDate": row["effective_change_date"] or row["effective_at"] or "",
                "monitoringUntil": row["monitoring_until"] or "",
                "minimumPostChangeClicks": int(row["minimum_post_change_clicks"] or 20),
                "postChangeMetrics": json.loads(row["post_change_metrics"] or "{}"),
                "postChangeDataThrough": row["post_change_data_through"] or "",
            }
            for row in rows
        }
    }


def save_recommendation_interaction(payload):
    recommendation_id = str(payload.get("recommendationId", "")).strip()[:240]
    recommendation_type = str(payload.get("recommendationType", "")).strip()[:80]
    action = str(payload.get("action", "")).strip()
    if not recommendation_id or not recommendation_type or action not in RECOMMENDATION_ACTIONS:
        return {"ok": False, "error": "A valid recommendation and action are required."}
    if action != "discuss" and payload.get("confirmed") is not True:
        return {"ok": False, "error": "Explicit confirmation is required before saving this action."}

    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    now = datetime.now(EASTERN_TIME).isoformat()
    reason = str(payload.get("reason", "") or payload.get("note", "")).strip()[:2000]
    reminder_at = str(payload.get("reminderAt", "")).strip()[:80]
    campaign = str(payload.get("campaign") or context.get("campaignName") or "").strip()[:240]
    confidence = str(payload.get("confidence") or context.get("confidence") or "").strip()[:40]
    supporting_metrics = payload.get("supportingMetrics") if isinstance(payload.get("supportingMetrics"), dict) else context.get("supportingMetrics", {})
    campaign_id = str(payload.get("campaignId") or context.get("campaignId") or "").strip()[:240]
    ad_group_id = str(payload.get("adGroupId") or context.get("adGroupId") or "").strip()[:240]
    target_id = str(payload.get("targetId") or context.get("targetId") or "").strip()[:240]
    original_action = str(payload.get("originalRecommendedAction") or context.get("action") or "").strip()[:120]
    actual_action = str(payload.get("actualActionTaken") or payload.get("changeWhat") or action).strip()[:240]
    previous_value = str(payload.get("previousValue", "") or "").strip()[:120]
    actual_new_value = str(payload.get("newValue", "") or payload.get("actualNewValue", "") or "").strip()[:120]
    change_category = str(payload.get("changeCategory", "") or "").strip()[:120]
    effective_at = str(payload.get("effectiveAt", "") or now).strip()[:80]
    try:
        effective_date = datetime.fromisoformat(effective_at.replace("Z", "+00:00")).date()
    except ValueError:
        return {"ok": False, "error": "The effective change date and time is invalid."}
    monitoring_until = (effective_date + timedelta(days=RECOMMENDATION_COOLDOWN_DAYS)).isoformat()
    user_id = str(payload.get("userId", "web-user") or "web-user").strip()[:240]
    subject_key = "|".join(str(value or "") for value in (
        context.get("marketplace") or context.get("country") or "",
        campaign_id or campaign,
        ad_group_id,
        target_id or context.get("target") or "",
        original_action,
        context.get("metric") or "",
    ))[:1000]
    idempotency_key = str(payload.get("idempotencyKey", "") or "").strip()[:240]
    if not idempotency_key:
        idempotency_key = hashlib.sha256(json.dumps({
            "recommendationId": recommendation_id, "action": action, "effectiveAt": effective_at,
            "previousValue": previous_value, "newValue": actual_new_value, "note": reason,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    status = {
        "made_change": "Action logged", "log_change": "Action logged", "keep_monitoring": "Monitoring",
        "ignore": "Ignored", "dismiss": "Dismissed", "remind_later": "Deferred",
    }.get(action, "Proposed")
    review_only = actual_action.lower().startswith("no actual settings change") or change_category.startswith("no_actual_settings_change")
    if action in {"made_change", "log_change"} and review_only:
        status = "Monitoring"
    if action == "discuss":
        return {"ok": True, "recommendationId": recommendation_id, "status": "Proposed", "lastAction": "discuss"}

    conn = connect()
    try:
        ensure_recommendation_interactions_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT * FROM recommendation_interactions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if duplicate:
            conn.commit()
            return {
                "ok": True, "duplicate": True, "recommendationId": duplicate["recommendation_id"],
                "status": duplicate["status"], "lastAction": duplicate["last_action"],
                "effectiveAt": duplicate["effective_at"] or "", "loggedAt": duplicate["logged_at"] or duplicate["acted_at"],
            }
        existing = conn.execute(
            "SELECT date_created, acted_at, user_notes FROM recommendation_interactions WHERE recommendation_id = ?",
            (recommendation_id,),
        ).fetchone()
        date_created = (existing["date_created"] or existing["acted_at"]) if existing else now
        prior_notes = existing["user_notes"] if existing and existing["user_notes"] else ""
        combined_notes = "\n".join(part for part in (prior_notes, reason) if part).strip()[:4000]
        date_completed = now if status in {"Completed", "Resolved"} else ""
        change_log_id = None
        if action in {"made_change", "log_change"} and not review_only:
            ensure_campaign_change_schema(conn.cursor())
            change = conn.execute(
                """INSERT INTO campaign_change_log
                   (campaign_name, change_type, details, previous_value, new_value, logged_at,
                    campaign_id, ad_group_id, target_id, recommendation_id, change_category,
                    effective_at, user_id, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (campaign, "ai_recorded_user_change", reason, previous_value, actual_new_value, now,
                 campaign_id, ad_group_id, target_id, recommendation_id, change_category, effective_at, user_id, idempotency_key),
            )
            change_log_id = change.lastrowid
        conn.execute(
            """INSERT INTO recommendation_interactions
               (recommendation_id, recommendation_type, recommendation_context, status, last_action,
                reason, reminder_at, acted_at, updated_at, campaign, confidence, supporting_metrics,
                user_action, user_notes, date_created, date_completed, campaign_id, ad_group_id,
                target_id, original_recommended_action, actual_action_taken, previous_value,
                actual_new_value, change_category, effective_at, logged_at, user_id, idempotency_key,
                subject_key, related_change_log_id, reason_for_status, effective_change_date,
                monitoring_until, minimum_post_change_clicks)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(recommendation_id) DO UPDATE SET
                 recommendation_type=excluded.recommendation_type, recommendation_context=excluded.recommendation_context,
                 status=excluded.status, last_action=excluded.last_action, reason=excluded.reason,
                 reminder_at=excluded.reminder_at, acted_at=excluded.acted_at, updated_at=excluded.updated_at,
                 campaign=excluded.campaign, confidence=excluded.confidence, supporting_metrics=excluded.supporting_metrics,
                 user_action=excluded.user_action, user_notes=excluded.user_notes, date_completed=excluded.date_completed,
                 campaign_id=excluded.campaign_id, ad_group_id=excluded.ad_group_id, target_id=excluded.target_id,
                 original_recommended_action=excluded.original_recommended_action, actual_action_taken=excluded.actual_action_taken,
                 previous_value=excluded.previous_value, actual_new_value=excluded.actual_new_value,
                 change_category=excluded.change_category, effective_at=excluded.effective_at, logged_at=excluded.logged_at,
                 user_id=excluded.user_id, idempotency_key=excluded.idempotency_key, subject_key=excluded.subject_key,
                 related_change_log_id=excluded.related_change_log_id, reason_for_status=excluded.reason_for_status,
                 effective_change_date=excluded.effective_change_date, monitoring_until=excluded.monitoring_until,
                 minimum_post_change_clicks=excluded.minimum_post_change_clicks""",
            (recommendation_id, recommendation_type, json.dumps(context), status, action, reason, reminder_at, now, now,
             campaign, confidence, json.dumps(supporting_metrics), actual_action, combined_notes, date_created, date_completed,
             campaign_id, ad_group_id, target_id, original_action, actual_action, previous_value, actual_new_value,
             change_category, effective_at, now, user_id, idempotency_key, subject_key, change_log_id,
             reason or ("Change logged" if status == "Action logged" else ""), effective_at,
             monitoring_until, RECOMMENDATION_MIN_POST_CHANGE_CLICKS),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "ok": True, "duplicate": False, "recommendationId": recommendation_id, "status": status,
        "lastAction": action, "actedAt": now, "effectiveAt": effective_at, "loggedAt": now,
        "reminderAt": reminder_at, "reason": reason, "dateCreated": date_created, "dateCompleted": date_completed,
        "campaign": campaign, "confidence": confidence, "supportingMetrics": supporting_metrics,
        "userAction": actual_action, "userNotes": combined_notes, "changeCategory": change_category,
        "previousValue": previous_value, "actualNewValue": actual_new_value, "relatedChangeLogId": change_log_id,
        "monitoringUntil": monitoring_until, "minimumPostChangeClicks": RECOMMENDATION_MIN_POST_CHANGE_CLICKS,
    }


def recommendation_history_context(limit=30):
    conn = connect()
    ensure_recommendation_interactions_table(conn)
    rows = conn.execute(
        "SELECT * FROM recommendation_interactions ORDER BY COALESCE(updated_at, date_created) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    history = []
    for row in rows:
        try:
            metrics = json.loads(row["supporting_metrics"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metrics = {}
        history.append({
            "recommendationId": row["recommendation_id"],
            "type": row["recommendation_type"],
            "campaign": row["campaign"] or "",
            "confidence": row["confidence"] or "",
            "supportingMetrics": metrics,
            "status": row["status"],
            "userAction": row["user_action"] or "",
            "userNotes": row["user_notes"] or "",
            "dateCreated": row["date_created"] or "",
            "dateCompleted": row["date_completed"] or "",
            "actualActionTaken": row["actual_action_taken"] or row["user_action"] or "",
            "previousValue": row["previous_value"] or "",
            "actualNewValue": row["actual_new_value"] or "",
            "effectiveChangeDate": row["effective_change_date"] or row["effective_at"] or "",
            "loggedAt": row["logged_at"] or row["acted_at"] or "",
            "subjectKey": row["subject_key"] or "",
            "supersededByRecommendationId": row["superseded_by_recommendation_id"] or "",
            "supersedesRecommendationId": row["supersedes_recommendation_id"] or "",
            "relatedChangeLogId": row["related_change_log_id"],
            "reasonForStatus": row["reason_for_status"] or "",
            "reconciliationResult": row["reconciliation_result"] or "",
        })
    return history


def ensure_sales_import_runs_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imported_at TEXT NOT NULL,
            file_name TEXT,
            file_hash TEXT,
            rows_processed INTEGER DEFAULT 0,
            new_rows INTEGER DEFAULT 0,
            updated_rows INTEGER DEFAULT 0,
            latest_sales_date TEXT,
            source TEXT,
            status TEXT NOT NULL,
            error TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sales_import_runs)").fetchall()}
    if "audit_updated" not in columns:
        conn.execute("ALTER TABLE sales_import_runs ADD COLUMN audit_updated INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_downloader_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            status TEXT NOT NULL,
            expected_sales_date TEXT,
            observed_sales_date TEXT,
            message TEXT,
            details TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_refresh_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            script TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            exit_code INTEGER,
            error TEXT
        )
        """
    )
    conn.commit()


SALES_DOWNLOADER_STATUSES = {
    "current",
    "refreshing",
    "authentication_required",
    "download_failed",
    "import_failed",
    "waiting_for_computer",
    "stale",
}


def expected_completed_date(now=None):
    """Return the most recent completed Amazon 3 AM-to-3 AM reporting date."""
    return amazon_reporting_date(now or datetime.now(EASTERN_TIME)) - timedelta(days=1)


def record_sales_downloader_event(payload):
    status = str(payload.get("status", "")).strip().lower()
    if status not in SALES_DOWNLOADER_STATUSES:
        return {"ok": False, "error": "A valid downloader status is required."}
    expected = str(payload.get("expectedSalesDate", "")).strip()[:10]
    observed = str(payload.get("observedSalesDate", "")).strip()[:10]
    for value in (expected, observed):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                return {"ok": False, "error": "Sales status dates must use YYYY-MM-DD."}
    message = str(payload.get("message", "")).strip()[:500]
    details = payload.get("details")
    conn = connect()
    ensure_sales_import_runs_table(conn)
    conn.execute(
        """
        INSERT INTO sales_downloader_events
        (recorded_at, status, expected_sales_date, observed_sales_date, message, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(EASTERN_TIME).isoformat(),
            status,
            expected,
            observed,
            message,
            json.dumps(details, ensure_ascii=False)[:4000] if details is not None else "",
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "status": sales_status_payload()}


def sales_status_payload(now=None):
    now = now or datetime.now(EASTERN_TIME)
    conn = connect()
    ensure_sales_import_runs_table(conn)
    row = conn.execute(
        "SELECT * FROM sales_import_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    successful = conn.execute(
        "SELECT * FROM sales_import_runs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    event = conn.execute(
        "SELECT * FROM sales_downloader_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    through = conn.execute("SELECT MAX(sale_date) FROM sales_daily").fetchone()[0]
    conn.close()
    expected = expected_completed_date(now).isoformat()
    age_days = None
    if through:
        try:
            age_days = (now.date() - date.fromisoformat(through)).days
        except ValueError:
            age_days = None
    is_current = bool(through and through >= expected)
    if is_current:
        status = "current"
    elif event and event["status"] in SALES_DOWNLOADER_STATUSES:
        status = event["status"]
    elif row and row["status"] == "failed":
        status = "import_failed"
    else:
        status = "waiting_for_computer"
    last_error = ""
    if event and event["status"] in {"authentication_required", "download_failed", "import_failed"}:
        last_error = event["message"] or ""
    elif row and row["status"] == "failed":
        last_error = row["error"] or ""
    return {
        "status": status,
        "latestSalesDate": through or (successful["latest_sales_date"] if successful else "") or "",
        "expectedSalesDate": expected,
        "lastAttemptAt": event["recorded_at"] if event else (row["imported_at"] if row else ""),
        "lastSuccessfulImport": successful["imported_at"] if successful else "",
        "rowsProcessed": int(successful["rows_processed"] or 0) if successful else 0,
        "newRows": int(successful["new_rows"] or 0) if successful else 0,
        "updatedRows": int(successful["updated_rows"] or 0) if successful else 0,
        "source": (successful["source"] or "unknown") if successful else "none",
        "lastError": last_error,
        "message": event["message"] if event else "",
        "authenticationRequired": status == "authentication_required",
        "auditUpdated": bool(successful["audit_updated"]) if successful else False,
        "ageDays": age_days,
    }


def import_token_is_valid(header_value):
    if not MERCH_AGENT_IMPORT_TOKEN:
        return False
    if not (header_value or "").startswith("Bearer "):
        return False
    return hmac.compare_digest(header_value[7:].strip(), MERCH_AGENT_IMPORT_TOKEN)


def import_merch_sales_payload(payload):
    file_name = Path(str(payload.get("fileName", "merch-sales.csv"))).name
    encoded = str(payload.get("data", ""))
    suffix = Path(file_name).suffix.lower()
    if suffix not in SALES_REPORT_EXTENSIONS:
        return {"ok": False, "error": "Upload a CSV or Excel Merch sales report."}
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": "The sales report could not be decoded."}
    if not raw or len(raw) > MAX_SALES_UPLOAD_BYTES:
        return {"ok": False, "error": "The sales report is empty or larger than 20 MB."}

    source = "windows_helper"
    if str(payload.get("source", "")).strip():
        source = str(payload["source"]).strip()[:80]
    digest = hashlib.sha256(raw).hexdigest()
    reprocess = bool(payload.get("reprocess"))
    conn = connect()
    ensure_sales_import_runs_table(conn)
    prior = conn.execute("SELECT id FROM sales_import_runs WHERE file_hash = ? AND status = 'success' LIMIT 1", (digest,)).fetchone()
    conn.close()
    if prior and not reprocess:
        return {"ok": True, "alreadyImported": True, "fileHash": digest, "message": "This sales report was already imported.", "status": sales_status_payload()}

    rows_processed = 0
    latest_sales_date = ""
    try:
        import pandas as pd
        from io import BytesIO
        frame = pd.read_csv(BytesIO(raw)) if suffix == ".csv" else pd.read_excel(BytesIO(raw))
        normalized = normalize_columns(frame)
        missing = sorted(SALES_REPORT_COLUMNS.difference(normalized.columns))
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(missing)}")
        rows_processed = int(len(normalized))
        parsed = pd.to_datetime(normalized["Date"], errors="coerce", format="mixed")
        if parsed.isna().all():
            raise ValueError("The report does not contain readable sales dates.")
        latest_sales_date = parsed.max().date().isoformat()
        result = sync_uploaded_sales_report(file_name, encoded)
        if not result.get("ok"):
            raise ValueError(result.get("error") or "The existing sales importer rejected the report.")
    except Exception as exc:
        conn = connect()
        ensure_sales_import_runs_table(conn)
        conn.execute(
            "INSERT INTO sales_import_runs (imported_at,file_name,file_hash,rows_processed,latest_sales_date,source,status,error) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now(EASTERN_TIME).isoformat(), file_name, digest, rows_processed, latest_sales_date, source, "failed", str(exc)),
        )
        conn.commit(); conn.close()
        return {"ok": False, "error": str(exc), "fileHash": digest}

    conn = connect()
    ensure_sales_import_runs_table(conn)
    audit_updated = False
    try:
        audit_result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "run_daily_audit.py")],
            cwd=PROJECT_ROOT,
            text=True,
            timeout=180,
            check=False,
        )
        audit_updated = audit_result.returncode == 0
    except Exception:
        audit_updated = False
    conn = connect()
    ensure_sales_import_runs_table(conn)
    conn.execute(
        """
        INSERT INTO sales_import_runs
        (imported_at,file_name,file_hash,rows_processed,new_rows,updated_rows,
         latest_sales_date,source,status,error,audit_updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            datetime.now(EASTERN_TIME).isoformat(), file_name, digest, rows_processed,
            rows_processed, 0, latest_sales_date, source, "success", "", int(audit_updated),
        ),
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "alreadyImported": False,
        "fileName": file_name,
        "fileHash": digest,
        "rowsProcessed": rows_processed,
        "newRows": rows_processed,
        "updatedRows": 0,
        "latestSalesDate": latest_sales_date,
        "auditUpdated": audit_updated,
        "message": "Merch sales report imported successfully.",
        "status": sales_status_payload(),
    }


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
    return times or [(5, 30)]


def scheduled_refresh_jobs(now=None):
    now = now or datetime.now(EASTERN_TIME)
    jobs = []
    full_hour, full_minute = parse_refresh_time(DAILY_REFRESH_TIME, (6, 0))
    jobs.append({
        "label": "daily audit",
        "script": "run_daily_audit.py",
        "scheduled": now.replace(hour=full_hour, minute=full_minute, second=0, microsecond=0),
    })
    ad_times = ad_refresh_times()
    full_ads_time = min(ad_times)
    for hour, minute in ad_times:
        is_full_refresh = (hour, minute) == full_ads_time
        jobs.append({
            "label": (
                "full Amazon Ads refresh"
                if is_full_refresh
                else "current Amazon Ads checkpoint refresh"
            ),
            "script": (
                "refresh_amazon_ads.py"
                if is_full_refresh
                else "refresh_amazon_ads_current.py"
            ),
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


def latest_ads_dataset_dates(conn):
    datasets = {}
    queries = {
        "campaigns": "SELECT MAX(report_date) FROM campaigns",
        "advertisedProducts": "SELECT MAX(report_date) FROM advertised_products",
        "targets": "SELECT MAX(report_date) FROM targets",
        "searchTerms": "SELECT MAX(report_date) FROM search_terms",
    }
    for label, sql in queries.items():
        try:
            datasets[label] = conn.execute(sql).fetchone()[0] or ""
        except sqlite3.OperationalError:
            datasets[label] = ""
    for period in ("last7", "last14", "last30"):
        try:
            datasets[f"placements{period[4:]}"] = conn.execute(
                "SELECT MAX(report_date) FROM placements WHERE report_period = ?",
                (period,),
            ).fetchone()[0] or ""
        except sqlite3.OperationalError:
            datasets[f"placements{period[4:]}"] = ""
    return datasets


def refresh_status_payload(now=None):
    now = now or datetime.now(EASTERN_TIME)
    with HOSTED_REFRESH_STATUS_LOCK:
        live_status = dict(HOSTED_REFRESH_STATUS)
    conn = connect()
    ensure_sales_import_runs_table(conn)
    latest = conn.execute("SELECT * FROM ads_refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
    successful = conn.execute(
        "SELECT * FROM ads_refresh_runs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    datasets = latest_ads_dataset_dates(conn)
    conn.close()
    expected = expected_completed_date(now).isoformat()
    required = ("campaigns", "advertisedProducts", "targets", "searchTerms", "placements7", "placements14", "placements30")
    data_current = all(datasets.get(name, "") >= expected for name in required)
    running = bool(live_status.get("running"))
    if running:
        status = "refreshing"
    elif latest and latest["status"] == "failed" and (not successful or latest["id"] > successful["id"]):
        status = "failed"
    elif data_current:
        status = "current"
    else:
        status = "stale"
    return {
        "status": status,
        "running": running,
        "label": live_status.get("label") or (latest["label"] if latest else ""),
        "startedAt": live_status.get("startedAt") or (latest["started_at"] if latest else ""),
        "finishedAt": live_status.get("finishedAt") or (latest["finished_at"] if latest else ""),
        "lastSuccessfulRefresh": successful["finished_at"] if successful else "",
        "exitCode": live_status.get("exitCode") if running else (latest["exit_code"] if latest else None),
        "error": live_status.get("error") or (latest["error"] if latest else ""),
        "expectedReportDate": expected,
        "dataThrough": min((datasets.get(name, "") for name in required), default=""),
        "datasets": datasets,
    }


def data_freshness_payload(now=None):
    sales = sales_status_payload(now)
    ads = refresh_status_payload(now)
    recommendations_current = (
        sales["status"] == "current"
        and ads["status"] == "current"
        and bool(sales.get("auditUpdated"))
    )
    dates = [value for value in (sales.get("latestSalesDate"), ads.get("dataThrough")) if value]
    return {
        "sales": sales,
        "ads": ads,
        "recommendations": {
            "status": "current" if recommendations_current else "stale",
            "dataThrough": min(dates) if dates else "",
            "message": (
                "Recommendations include current sales and Amazon Ads data."
                if recommendations_current
                else "Recommendations are waiting for both current sales and Amazon Ads data."
            ),
        },
    }


def run_hosted_refresh(label, script):
    if not HOSTED_REFRESH_LOCK.acquire(blocking=False):
        print(f"Hosted {label} skipped because another refresh is running.", flush=True)
        return False
    run_id = None
    try:
        conn = connect()
        ensure_sales_import_runs_table(conn)
        cursor = conn.execute(
            "INSERT INTO ads_refresh_runs (label,script,started_at,status) VALUES (?,?,?,?)",
            (label, script, datetime.now(EASTERN_TIME).isoformat(), "running"),
        )
        run_id = cursor.lastrowid
        conn.commit()
        conn.close()
        with HOSTED_REFRESH_STATUS_LOCK:
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
                timeout=(
                    HOSTED_FULL_REFRESH_TIMEOUT_SECONDS
                    if script == "refresh_amazon_ads.py"
                    else HOSTED_CURRENT_REFRESH_TIMEOUT_SECONDS
                ),
                check=False,
            )
            with HOSTED_REFRESH_STATUS_LOCK:
                HOSTED_REFRESH_STATUS.update({
                    "running": False,
                    "finishedAt": datetime.now(EASTERN_TIME).isoformat(),
                    "exitCode": result.returncode,
                })
            conn = connect()
            ensure_sales_import_runs_table(conn)
            conn.execute(
                "UPDATE ads_refresh_runs SET finished_at=?, status=?, exit_code=? WHERE id=?",
                (
                    datetime.now(EASTERN_TIME).isoformat(),
                    "success" if result.returncode == 0 else "failed",
                    result.returncode,
                    run_id,
                ),
            )
            conn.commit()
            conn.close()
            print(f"Hosted {label} finished with exit code {result.returncode}.", flush=True)
        except Exception as exc:
            with HOSTED_REFRESH_STATUS_LOCK:
                HOSTED_REFRESH_STATUS.update({
                    "running": False,
                    "finishedAt": datetime.now(EASTERN_TIME).isoformat(),
                    "exitCode": -1,
                    "error": str(exc),
                })
            if run_id is not None:
                conn = connect()
                ensure_sales_import_runs_table(conn)
                conn.execute(
                    "UPDATE ads_refresh_runs SET finished_at=?, status='failed', exit_code=-1, error=? WHERE id=?",
                    (datetime.now(EASTERN_TIME).isoformat(), str(exc)[:1000], run_id),
                )
                conn.commit()
                conn.close()
            print(f"Hosted {label} failed: {exc}", file=sys.stderr, flush=True)
    finally:
        HOSTED_REFRESH_LOCK.release()
    return True


def start_hosted_ads_refresh_now(mode="current"):
    if mode not in {"current", "full"}:
        return {"ok": False, "error": "Refresh mode must be current or full."}
    with HOSTED_REFRESH_STATUS_LOCK:
        if HOSTED_REFRESH_STATUS.get("running"):
            return {"ok": False, "running": True, "status": dict(HOSTED_REFRESH_STATUS)}
    is_full = mode == "full"
    thread = threading.Thread(
        target=run_hosted_refresh,
        args=(
            "manual full Amazon Ads refresh" if is_full else "manual current Amazon Ads checkpoint refresh",
            "refresh_amazon_ads.py" if is_full else "refresh_amazon_ads_current.py",
        ),
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
    for name in (
        "target_name",
        "effective_date",
        "summary",
        "campaign_id",
        "ad_group_id",
        "target_id",
        "recommendation_id",
        "change_category",
        "effective_at",
        "user_id",
        "idempotency_key",
    ):
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


def dated_sales_home_payload(cur, period):
    """Build a Home window from one dated source report, never mixed snapshots."""
    window_days = {"yesterday": 1, "last7": 7, "last14": 14, "last30": 30}.get(period)
    if not window_days:
        return None
    completed_through = (amazon_reporting_date() - timedelta(days=1)).isoformat()
    candidates = cur.execute(
        """
        SELECT source_file, MAX(sale_date) AS period_end, MIN(sale_date) AS available_start,
               MAX(import_date) AS imported_at
        FROM sales_daily
        WHERE sale_date <= ?
        GROUP BY source_file
        ORDER BY
          CASE WHEN MIN(sale_date) <= date(MAX(sale_date), ?) THEN 1 ELSE 0 END DESC,
          MAX(sale_date) DESC, MAX(import_date) DESC
        """,
        (completed_through, f"-{window_days - 1} days"),
    ).fetchall()
    if not candidates:
        return None
    selected = candidates[0]
    period_end = selected["period_end"]
    period_start = (parse_date(period_end) - timedelta(days=window_days - 1)).isoformat()
    complete = selected["available_start"] <= period_start
    params = (selected["source_file"], period_start, period_end)
    rows = cur.execute(
        """SELECT title, SUM(purchased) AS purchased, SUM(royalties) AS royalties, SUM(revenue) AS revenue
           FROM sales_daily WHERE source_file = ? AND sale_date BETWEEN ? AND ?
           GROUP BY title HAVING SUM(purchased) > 0 ORDER BY SUM(purchased) DESC, SUM(royalties) DESC""",
        params,
    ).fetchall()
    markets = cur.execute(
        """SELECT market, currency, SUM(purchased) AS units, SUM(cancelled) AS cancelled, SUM(returned) AS returned,
                  SUM(royalties) AS royalties, SUM(revenue) AS revenue
           FROM sales_daily WHERE source_file = ? AND sale_date BETWEEN ? AND ?
           GROUP BY market, currency ORDER BY SUM(purchased) DESC, SUM(royalties) DESC""",
        params,
    ).fetchall()
    products = cur.execute(
        """SELECT title, market, currency, SUM(purchased) AS units, SUM(royalties) AS royalties, SUM(revenue) AS revenue
           FROM sales_daily WHERE source_file = ? AND sale_date BETWEEN ? AND ?
           GROUP BY title, market, currency HAVING SUM(purchased) > 0
           ORDER BY SUM(purchased) DESC, SUM(royalties) DESC LIMIT 20""",
        params,
    ).fetchall()
    return {"rows": rows, "markets": markets, "products": products, "periodStart": period_start,
            "periodEnd": period_end, "latestImport": selected["imported_at"], "complete": complete,
            "availableStart": selected["available_start"]}


def latest_table_import(cur, table_name, period):
    order_by = "import_date DESC"
    if table_name in {"sales", "sales_market_breakdown"}:
        order_by = "MAX(COALESCE(report_date, '')) DESC, import_date DESC"
    row = cur.execute(
        f"""
        SELECT import_date
        FROM {table_name}
        WHERE COALESCE(report_period, 'unspecified') = ?
        GROUP BY import_date
        ORDER BY {order_by}
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
        "last14": "Last 14 Days",
        "last7": "Last 7 Days",
        "yesterday": "Yesterday",
        "unspecified": "Latest Ads Import",
    }

    conn = connect()
    cur = conn.cursor()
    cards = []

    for period in ("last60", "last30", "last14", "last7", "yesterday", "unspecified"):
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

    today = amazon_reporting_date()
    ranges = {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "last7": (today - timedelta(days=6), today),
        "last14": (today - timedelta(days=13), today),
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

    if period == "today":
        today_import = latest_table_import(cur, "advertised_products", "today")
        if today_import and (not latest_import or today_import > latest_import):
            source_period = "today"
            latest_import = today_import
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


def choose_analytics_source(candidates, period, custom_start="", custom_end=""):
    """Choose one dated report that can actually support the requested chart."""
    if not candidates:
        return None

    if period == "Custom":
        requested_start = parse_date(custom_start)
        requested_end = parse_date(custom_end)
        covering = [
            candidate
            for candidate in candidates
            if parse_date(candidate["min_date"])
            and parse_date(candidate["max_date"])
            and parse_date(candidate["min_date"]) <= requested_start
            and parse_date(candidate["max_date"]) >= requested_end
        ]
        if covering:
            return max(
                covering,
                key=lambda item: (
                    item["import_date"] or "",
                    -int(item["date_count"] or 0),
                ),
            )

    required_days = {"7D": 7, "30D": 30, "90D": 90, "1Y": 365}.get(period, 30)
    sufficient = [
        candidate
        for candidate in candidates
        if int(candidate["date_count"] or 0) >= required_days
    ]
    if sufficient:
        return max(
            sufficient,
            key=lambda item: (
                item["max_date"] or "",
                -int(item["date_count"] or 0),
                item["import_date"] or "",
            ),
        )

    return max(
        candidates,
        key=lambda item: (
            int(item["date_count"] or 0),
            item["max_date"] or "",
            item["import_date"] or "",
        ),
    )


def analytics_payload(period, custom_start="", custom_end=""):
    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "points": []}

    if period == "Custom":
        start_date = parse_date(custom_start)
        last_date = parse_date(custom_end)
        if not start_date or not last_date or start_date > last_date:
            return {
                "source": "invalid_range",
                "period": period,
                "points": [],
                "error": "Choose a valid Analytics start and end date.",
            }

    conn = connect()
    cur = conn.cursor()
    try:
        candidates = cur.execute(
            """
            SELECT source_file, MAX(import_date) AS import_date,
                   COUNT(DISTINCT sale_date) AS date_count,
                   MIN(sale_date) AS min_date, MAX(sale_date) AS max_date
            FROM sales_daily
            GROUP BY source_file
            """
        ).fetchall()
    except sqlite3.OperationalError:
        candidates = []
    source = choose_analytics_source(
        candidates,
        period,
        custom_start,
        custom_end,
    )

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


def dated_home_response(period, daily):
    all_rows = daily["rows"]
    totals = {
        "sales": sum(int(row["purchased"] or 0) for row in all_rows),
        "royalties": money(sum(row["royalties"] or 0 for row in all_rows)),
        "revenue": money(sum(row["revenue"] or 0 for row in all_rows)),
    }
    markets = [{
        "name": MARKET_NAMES.get(row["market"], row["market"] or "Unknown"), "code": row["market"],
        "currency": row["currency"], "units": int(row["units"] or 0),
        "cancelled": int(row["cancelled"] or 0), "returned": int(row["returned"] or 0),
        "royalties": money(row["royalties"]), "revenue": money(row["revenue"]),
    } for row in daily["markets"]]
    products = [{
        "title": row["title"], "units": int(row["units"] or 0), "royalty": money(row["royalties"]),
        "revenue": money(row["revenue"]), "market": MARKET_NAMES.get(row["market"], row["market"] or "Unknown"),
        "currency": row["currency"], "time": daily["periodEnd"],
    } for row in daily["products"]]
    royalty_totals = {}
    for market in markets:
        currency = (market.get("currency") or "USD").upper()
        royalty_totals[currency] = money(royalty_totals.get(currency, 0) + market["royalties"])
    royalty_breakdown = [{"currency": currency, "amount": amount} for currency, amount in sorted(
        royalty_totals.items(), key=lambda pair: (pair[0] != "USD", pair[0]))]
    labels = {"yesterday": "Yesterday", "last7": "Last 7 days", "last14": "Last 14 days", "last30": "Last 30 days"}
    days = {"yesterday": 1, "last7": 7, "last14": 14, "last30": 30}.get(period, 1)
    label = labels.get(period, "Selected period")
    summary = []
    if all_rows:
        summary.append(f"{all_rows[0]['title']} led {label.lower()} with {int(all_rows[0]['purchased'] or 0)} units.")
    summary.append(f"{label} produced {totals['sales']} units, averaging {totals['sales'] / days:.1f} per day.")
    if not daily["complete"]:
        summary.append(f"This source only contains sales from {daily['availableStart']} forward, so it is not a complete {label.lower()} window.")
    return {
        "source": "dated_sales", "period": period, "reportDate": daily["periodEnd"],
        "periodStart": daily["periodStart"], "periodEnd": daily["periodEnd"], "latestImport": daily["latestImport"],
        "complete": daily["complete"], "availableStart": daily["availableStart"], "sales": totals["sales"],
        "royalties": totals["royalties"], "royaltyByCurrency": royalty_breakdown, "revenue": totals["revenue"],
        "returns": sum(item["returned"] for item in markets), "products": products, "markets": markets,
        "summary": summary, "summaryPeriod": label, "businessBriefing": weekly_business_briefing(),
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
    daily = dated_sales_home_payload(cur, period)
    if daily:
        conn.close()
        return dated_home_response(period, daily)
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
    period_labels = {"yesterday": "Yesterday", "last7": "Last 7 days", "last14": "Last 14 days", "last30": "Last 30 days"}
    period_days = {"yesterday": 1, "last7": 7, "last14": 14, "last30": 30}
    period_label = period_labels.get(period, "Latest period")
    days = period_days.get(period, 1)
    period_end = parse_date(report_date)
    period_start = period_end - timedelta(days=days - 1) if period_end else None

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
        "periodStart": period_start.isoformat() if period_start else "",
        "periodEnd": period_end.isoformat() if period_end else "",
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


def designs_payload(period, market=None, search=""):
    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "designs": []}

    conn = connect()
    cur = conn.cursor()
    latest_import = latest_table_import(cur, "sales_market_breakdown", period)
    asin_search = search.strip().upper() if re.fullmatch(r"[A-Za-z0-9]{10}", search.strip()) else ""
    if asin_search:
        matching_import = cur.execute(
            """
            SELECT import_date
            FROM sales_market_breakdown
            WHERE COALESCE(report_period, 'unspecified') = ?
              AND UPPER(COALESCE(asin, '')) = ?
            GROUP BY import_date
            ORDER BY MAX(COALESCE(report_date, '')) DESC, import_date DESC
            LIMIT 1
            """,
            (period, asin_search),
        ).fetchone()
        if matching_import:
            latest_import = matching_import["import_date"]

    if not latest_import:
        latest_import = latest_table_import(cur, "sales", period)
        if asin_search:
            matching_import = cur.execute(
                """
                SELECT import_date
                FROM sales
                WHERE COALESCE(report_period, 'unspecified') = ?
                  AND UPPER(COALESCE(asin, '')) = ?
                GROUP BY import_date
                ORDER BY MAX(COALESCE(report_date, '')) DESC, import_date DESC
                LIMIT 1
                """,
                (period, asin_search),
            ).fetchone()
            if matching_import:
                latest_import = matching_import["import_date"]
        if not latest_import:
            conn.close()
            return {"source": "empty_database", "period": period, "designs": []}

        search_filter = ""
        search_params = [latest_import, period]
        if search:
            search_filter = " AND (LOWER(title) LIKE ? OR UPPER(COALESCE(asin, '')) = ?)"
            search_params.extend((f"%{search.lower()}%", search.upper()))
        rows = cur.execute(
            f"""
            SELECT title, GROUP_CONCAT(DISTINCT NULLIF(asin, '')) AS asins,
                   SUM(purchased) AS units, SUM(royalties) AS royalties,
                   SUM(revenue) AS revenue, MAX(COALESCE(report_date, '')) AS report_date
            FROM sales
            WHERE import_date = ? AND COALESCE(report_period, 'unspecified') = ?
                  {search_filter}
            GROUP BY title
            ORDER BY SUM(purchased) DESC, title ASC
            """,
            search_params,
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
                    "asins": [value for value in str(row["asins"] or "").split(",") if value],
                    "units": int(row["units"] or 0),
                    "royaltiesByCurrency": [{"currency": "USD", "amount": money(row["royalties"])}],
                    "revenueByCurrency": [{"currency": "USD", "amount": money(row["revenue"])}],
                }
                for row in rows
            ],
        }

    market_filter = ""
    market_params = [latest_import, period]
    if market:
        market_filter = " AND LOWER(COALESCE(market, '')) = ?"
        market_params.append(market.lower())
    search_filter = ""
    if search:
        search_filter = " AND (LOWER(title) LIKE ? OR UPPER(COALESCE(asin, '')) = ?)"
        market_params.extend((f"%{search.lower()}%", search.upper()))
    rows = cur.execute(
        f"""
        SELECT title, COALESCE(currency, 'USD') AS currency,
               GROUP_CONCAT(DISTINCT NULLIF(asin, '')) AS asins,
               SUM(purchased) AS units, SUM(royalties) AS royalties,
               SUM(revenue) AS revenue, MAX(COALESCE(report_date, '')) AS report_date
        FROM sales_market_breakdown
        WHERE import_date = ? AND COALESCE(report_period, 'unspecified') = ?{market_filter}{search_filter}
        GROUP BY title, COALESCE(currency, 'USD')
        ORDER BY SUM(purchased) DESC, SUM(royalties) DESC, title ASC, currency ASC
        """,
        market_params,
    ).fetchall()
    designs_by_title = {}
    report_date = ""
    for row in rows:
        report_date = max(report_date, row["report_date"] or "")
        design = designs_by_title.setdefault(
            row["title"],
            {
                "title": row["title"],
                "asins": [],
                "units": 0,
                "royaltiesByCurrency": [],
                "revenueByCurrency": [],
            },
        )
        for asin in str(row["asins"] or "").split(","):
            if asin and asin not in design["asins"]:
                design["asins"].append(asin)
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
        "market": market or "",
        "latestImport": latest_import,
        "reportDate": report_date,
        "designs": designs,
    }


def campaigns_payload(period="last30", search=""):
    if not DB_PATH.exists():
        return {"source": "missing_database", "period": period, "campaigns": []}

    conn = connect()
    cur = conn.cursor()
    search_text = search.strip().lower()
    latest_import = latest_table_import(cur, "campaigns", period)

    # A campaign with no activity can be omitted from Amazon's newest
    # performance export. When the user names a campaign, use the newest
    # snapshot that actually contains that campaign instead of searching only
    # the newest global batch and incorrectly claiming the campaign is unknown.
    if search_text:
        matching_import = cur.execute(
            """
            SELECT import_date
            FROM campaigns
            WHERE COALESCE(report_period, 'unspecified') = ?
              AND LOWER(campaign_name) LIKE ?
            ORDER BY import_date DESC
            LIMIT 1
            """,
            (period, f"%{search_text}%"),
        ).fetchone()
        if matching_import:
            latest_import = matching_import["import_date"]

    if not latest_import:
        conn.close()
        return {"source": "empty_database", "period": period, "campaigns": []}

    params = [latest_import, period]
    search_filter = ""

    if search_text:
        search_filter = "AND LOWER(campaign_name) LIKE ?"
        params.append(f"%{search_text}%")

    rows = cur.execute(
        f"""
        SELECT
            campaign_name,
            COALESCE(country, '') AS country,
            COALESCE(currency, '') AS currency,
            SUM(COALESCE(impressions, 0)) AS impressions,
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

    campaigns = []

    for row in rows:
        spend = money(row["spend"])
        sales = money(row["sales"])
        orders = int(row["orders"] or 0)
        clicks = int(row["clicks"] or 0)
        item = {
            "name": row["campaign_name"],
            "country": row["country"],
            "currency": row["currency"],
            "impressions": int(row["impressions"] or 0),
            "spend": spend,
            "clicks": clicks,
            "orders": orders,
            "sales": sales,
            "roas": round(sales / spend, 2) if spend else 0,
            "acos": round(spend / sales * 100, 1) if sales else 0,
            "reportDate": row["report_date"],
            "status": "performing" if orders and sales / max(spend, 0.01) >= 5 else "review" if spend >= 10 and not orders else "watch",
        }
        product_groups = advertised_product_campaign_groups(cur, item["name"], period)
        if product_groups:
            item["impressions"] = sum(group["impressions"] for group in product_groups)
            item["impressionsSource"] = "advertised_products"
            item["impressionsReportDate"] = max(
                (group["reportDate"] for group in product_groups),
                default=item["reportDate"],
            )
        campaigns.append(item)

    conn.close()

    return {
        "source": "sqlite",
        "period": period,
        "latestImport": latest_import,
        "reportDate": max((item["reportDate"] for item in campaigns), default=""),
        "count": len(campaigns),
        "campaigns": campaigns,
    }


def advertised_product_campaign_groups(cur, campaign_name, period):
    """Return exact-range ad-group metrics from the daily advertised-product report."""
    days = {
        "today": 1,
        "yesterday": 1,
        "last7": 7,
        "last14": 14,
        "last30": 30,
        "last60": 60,
    }.get(period)
    if not days:
        return []

    source_period = "today" if period == "today" else "last60"
    latest_import = latest_table_import(cur, "advertised_products", source_period)
    if not latest_import and source_period == "today":
        source_period = "last60"
        latest_import = latest_table_import(cur, "advertised_products", source_period)
    if not latest_import:
        return []

    range_end = amazon_reporting_date()
    if period != "today":
        range_end -= timedelta(days=1)
    range_start = range_end - timedelta(days=days - 1)
    rows = cur.execute(
        """
        SELECT
            COALESCE(ad_group_name, '') AS ad_group_name,
            SUM(COALESCE(impressions, 0)) AS impressions,
            SUM(COALESCE(clicks, 0)) AS clicks,
            SUM(COALESCE(spend, 0)) AS spend,
            SUM(COALESCE(orders, 0)) AS orders,
            SUM(COALESCE(sales, 0)) AS sales,
            MAX(COALESCE(report_date, '')) AS report_date
        FROM advertised_products
        WHERE import_date = ?
          AND COALESCE(report_period, 'unspecified') = ?
          AND LOWER(campaign_name) = LOWER(?)
          AND report_date BETWEEN ? AND ?
        GROUP BY COALESCE(ad_group_name, '')
        ORDER BY SUM(COALESCE(sales, 0)) DESC,
                 SUM(COALESCE(orders, 0)) DESC,
                 SUM(COALESCE(spend, 0)) DESC,
                 COALESCE(ad_group_name, '')
        """,
        (
            latest_import,
            source_period,
            campaign_name,
            range_start.isoformat(),
            range_end.isoformat(),
        ),
    ).fetchall()
    groups = []
    for row in rows:
        spend = money(row["spend"])
        sales = money(row["sales"])
        groups.append({
            "name": row["ad_group_name"] or "Unassigned ad group",
            "impressions": int(row["impressions"] or 0),
            "clicks": int(row["clicks"] or 0),
            "spend": spend,
            "orders": int(row["orders"] or 0),
            "sales": sales,
            "roas": round(sales / spend, 2) if spend else 0,
            "acos": round(spend / sales * 100, 1) if sales else 0,
            "reportDate": row["report_date"] or "",
            "metricsSource": "advertised_products",
        })
    return groups


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
                SUM(COALESCE(impressions, 0)) AS impressions,
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
                "impressions": int(row["impressions"] or 0),
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
                "impressions": 0,
                "clicks": 0,
                "spend": 0.0,
                "orders": 0,
                "sales": 0.0,
            },
        )
        group["targetCount"] += 1
        group["impressions"] += target["impressions"]
        group["clicks"] += target["clicks"]
        group["spend"] = money(group["spend"] + target["spend"])
        group["orders"] += target["orders"]
        group["sales"] = money(group["sales"] + target["sales"])

    product_groups = advertised_product_campaign_groups(cur, campaign_name, period)
    for product_group in product_groups:
        group = ad_groups_by_name.setdefault(
            product_group["name"],
            {
                "name": product_group["name"],
                "targetCount": 0,
                "impressions": 0,
                "clicks": 0,
                "spend": 0.0,
                "orders": 0,
                "sales": 0.0,
            },
        )
        group.update({
            "impressions": product_group["impressions"],
            "clicks": product_group["clicks"],
            "spend": product_group["spend"],
            "orders": product_group["orders"],
            "sales": product_group["sales"],
            "reportDate": product_group["reportDate"],
            "metricsSource": product_group["metricsSource"],
        })

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
    change_category = "campaign"
    # When the user says “paused DESIGN in CAMPAIGN”, the campaign is the
    # container and the design is the object that changed.  The logger adds
    # the selected campaign to free-form text during preview, so preserve the
    # design name instead of incorrectly recording a campaign pause.
    status_words = r"paused|unpaused|turned\s+off|turn\s+off|shut\s+off|stopped|disabled|turned\s+on|turn\s+on|started|enabled"
    scoped_target = re.search(
        rf"\b(?:{status_words})\s+(?:the\s+)?(.+?)\s+\b(?:in|inside)\s+(.+)$",
        question,
        re.IGNORECASE,
    )
    if scoped_target and any(word in normalized for word in ("paused", "unpaused", "turned off", "turn off", "shut off", "stopped", "disabled", "turned on", "turn on", "started", "enabled")):
        candidate = scoped_target.group(1).strip(" .,:;\"")
        candidate = re.sub(r"^(?:design|ad group|target)\s*[:\-]?\s*", "", candidate, flags=re.IGNORECASE)
        if candidate:
            target_name = candidate
            change_category = "design"
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
        "changeCategory": change_category,
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
    elif change_type == "Status change" and new_value and target_name:
        category = str(payload.get("changeCategory", "")).strip().lower() or "design"
        noun = "design" if category == "design" else "target"
        description = f'{campaign} {noun} "{target_name}" {new_value.lower()}'
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
         effective_date, summary, logged_at, change_category)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            str(payload.get("changeCategory", "")).strip(),
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
                   new_value, effective_date, summary, logged_at,
                   campaign_id, ad_group_id, target_id, recommendation_id,
                   change_category, effective_at, user_id
            FROM campaign_change_log
            ORDER BY logged_at DESC, id DESC
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
                "campaignId": row["campaign_id"],
                "adGroupId": row["ad_group_id"],
                "targetId": row["target_id"],
                "recommendationId": row["recommendation_id"],
                "changeCategory": row["change_category"],
                "effectiveAt": row["effective_at"],
                "userId": row["user_id"],
            }
            for row in rows
        ]

    return {
        "campaigns": sorted(unique_campaigns.values(), key=lambda item: item["name"].lower()),
        "recentChanges": recent_changes,
    }


def build_owner_audit_briefing(daily_audit, sales_yesterday, sales_last7, campaigns_last7,
                               audit_bid_actions, audit_search_terms, audit_sales_opportunities):
    """Turn the structured audit into a concise business-owner briefing."""
    active = [item for item in (daily_audit.get("activeBidRecommendations") or audit_bid_actions)
              if item.get("action") != "Hold"]
    active.sort(key=lambda item: (str(item.get("priority", "medium")).lower() != "high",
                                  -float(item.get("spend", 0) or 0)))
    active = active[:4]
    monitoring = (daily_audit.get("monitoringRecommendations") or [])[:3]
    yesterday_units = int(sales_yesterday.get("sales") or 0)
    recent_units = int(sales_last7.get("sales") or 0)
    average_units = recent_units / 7 if recent_units else 0
    if average_units and yesterday_units > average_units * 1.15:
        opening = "Yesterday was stronger than the recent norm, with sales momentum worth protecting."
    elif average_units and yesterday_units < average_units * 0.85:
        opening = "Yesterday was softer than the recent norm, so the priority is separating a real decline from normal daily volatility."
    else:
        opening = "Yesterday was generally stable, with no evidence of a broad business deterioration."

    yesterday_royalties = float(sales_yesterday.get("royalties") or 0)
    parts = [opening, f"Total sales were {yesterday_units} units and approximately ${yesterday_royalties:.2f} in royalties."]
    winners = (sales_yesterday.get("products") or sales_last7.get("products") or [])[:3]
    if winners:
        names = ", ".join(f"{item.get('title', 'Untitled')} ({item.get('units', 0)} units)" for item in winners)
        parts.append(f"The strongest recent sellers were {names}; protect those winners while reviewing weaker advertising signals.")

    evidence = []
    if active:
        lines = []
        for item in active:
            lines.append(
                f"- **{str(item.get('priority', 'medium')).title()} Priority** — {item.get('action', 'Review')} "
                f"{item.get('campaignName', 'campaign')} / {item.get('target', 'target')} "
                f"from ${float(item.get('currentBid', 0) or 0):.2f} to ${float(item.get('suggestedBid', 0) or 0):.2f}. "
                f"{item.get('reason', 'Recent performance supports this action.')} "
                f"Confidence: {str(item.get('confidence', 'medium')).title()}."
            )
            evidence.append(
                f"{item.get('campaignName')} | {item.get('target')} | {item.get('recommendationPeriod', '14-day')} | "
                f"{item.get('clicks', 0)} clicks | ${float(item.get('spend', 0) or 0):.2f} spend | "
                f"{item.get('orders', 0)} orders | {float(item.get('roas', 0) or 0):.2f} ROAS"
            )
        parts.append("**Highest-priority actions**\n" + "\n".join(lines))
    else:
        parts.append("**Highest-priority actions**\nNo additional bid change is recommended today; the evidence threshold for a new action was not met.")

    search_terms = (daily_audit.get("searchTermRecommendations") or audit_search_terms)[:2]
    if search_terms:
        parts.append("**Advertising trends**\n" + "\n".join(
            f"- Review **{item.get('searchTerm', 'term')}** in **{item.get('campaignName', 'unknown campaign')}**: "
            f"{item.get('action', 'monitor')} ({float(item.get('roas', 0) or 0):.2f} ROAS)."
            for item in search_terms
        ))

    weak = [item for item in campaigns_last7 if item.get("spend", 0) >= 5 and item.get("orders", 0) == 0][:2]
    improving = [item for item in campaigns_last7 if item.get("orders", 0) and item.get("roas", 0) >= 7][:2]
    trend_lines = []
    if improving:
        trend_lines.append("- Improving: " + ", ".join(item["name"] for item in improving) + ".")
    if weak:
        trend_lines.append("- Wasted-spend candidates: " + ", ".join(item["name"] for item in weak) + "; review targets and search terms before pausing a whole campaign.")
    if trend_lines:
        parts.append("**Campaign trends**\n" + "\n".join(trend_lines))

    if monitoring:
        parts.append("**Recent changes to monitor**\n" + "\n".join(
            f"- {item.get('campaignName', 'Campaign')} / {item.get('target', 'target')} is still gathering post-change data; do not change it again yet."
            for item in monitoring
        ))
    elif not active:
        parts.append("Several decisions are better treated as wait-and-see until the next completed data window.")

    if daily_audit.get("delayedSalesData"):
        parts.append("Merch sales are delayed, so confidence is based primarily on the available advertising data.")
    parts.append("**Bottom line:** " + ("Focus on the highest-priority action above." if active else "No major changes are recommended today; continue monitoring recent adjustments."))
    return "\n\n".join(parts), evidence


def assistant_payload(question, history=None, requested_period="last30", recommendation_context=None):
    question = (question or "").strip()
    contextual_question = question
    if isinstance(recommendation_context, dict) and recommendation_context:
        contextual_question = (
            "Recommendation context (use this as the subject of the conversation): "
            + json.dumps(recommendation_context, ensure_ascii=False)
            + "\nUser question: "
            + question
        )
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
        "last14": "last14",
        "last30": "last30",
        "last60": "last60",
        "7 days": "last7",
        "7 day": "last7",
        "last week": "last7",
        "this week": "last7",
        "14 days": "last14",
        "14 day": "last14",
        "two weeks": "last14",
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
        "last14": "the last 14 days",
        "last30": "the last 30 days",
        "last60": "the trailing 60 days",
    }[analysis_period]
    market_filter = market_from_text(question)
    if not market_filter:
        for item in reversed(history):
            market_filter = market_from_text(item.get("text", ""))
            if market_filter:
                break
    market_label = MARKET_NAMES.get(market_filter, "")
    sales_yesterday = home_payload("yesterday")
    sales_last7 = home_payload("last7")
    selected_sales = home_payload(analysis_period)
    campaigns_last7 = campaigns_payload(analysis_period).get("campaigns", [])
    designs_last7 = designs_payload(analysis_period, market_filter).get("designs", [])
    daily_audit = build_daily_audit()
    previous_decisions = recommendation_history_context()
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

    if previous_decisions:
        contextual_question += (
            "\nPrevious recommendation decisions (use as historical context; do not override current evidence): "
            + json.dumps(previous_decisions, ensure_ascii=False)
        )

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
        answer, briefing_evidence = build_owner_audit_briefing(
            daily_audit,
            sales_yesterday,
            sales_last7,
            campaigns_last7,
            audit_bid_actions,
            audit_search_terms,
            audit_sales_opportunities,
        )
        evidence.extend(briefing_evidence)
    elif False:
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
            parts.append("Then review search terms: " + "; ".join(
                f"{item['searchTerm']} in {item['campaignName']}"
                + (f" / {item.get('adGroupName')}" if item.get("adGroupName") else "")
                + f" — {item['action']}"
                for item in top_search
            ) + ".")
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
                f"{item['searchTerm']} in {item['campaignName']}"
                + (f" / {item.get('adGroupName')}" if item.get("adGroupName") else "")
                + f" ({item['action']}, {item['roas']:.2f} ROAS)"
                for item in audit_search_terms[:6]
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
    elif any(phrase in normalized for phrase in ("least performing", "worst performing", "lowest performing", "weakest campaign", "worst campaign")):
        ranked = sorted(
            campaigns_last7,
            key=lambda item: (
                item.get("orders", 0) > 0,
                item.get("roas", 0) if item.get("orders", 0) else -item.get("spend", 0),
                item.get("sales", 0),
            ),
        )
        candidates = [item for item in ranked if item.get("spend", 0) > 0][:5]
        if candidates:
            answer = f"Lowest-performing campaigns for {period_label}:\n" + "\n".join(
                f"• {item['name']} — {currency_amount_text(item['spend'], item.get('currency'))} spend; {currency_amount_text(item['sales'], item.get('currency'))} ad sales; {item['orders']} orders; {item['roas']:.2f} ROAS"
                for item in candidates
            )
            zero_order = [item for item in candidates if item.get("orders", 0) == 0]
            if zero_order:
                answer += f"\n\nPriority: start with {zero_order[0]['name']} because it spent money without producing an order in this period. Review its search terms and targets before pausing the whole campaign."
            evidence.extend(f"Amazon Ads, {period_label}, report ending {item.get('reportDate') or 'latest'}" for item in candidates[:3])
        else:
            answer = f"I do not have campaign spend rows for {period_label}, so I cannot identify the weakest campaign."
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
    elif any(phrase in normalized for phrase in ("best performing campaign", "top performing campaign", "best campaign", "top campaign")):
        ranked = sorted(
            campaigns_last7,
            key=lambda item: (item.get("sales", 0), item.get("roas", 0), item.get("orders", 0)),
            reverse=True,
        )
        if ranked:
            leaders = ranked[:5]
            answer = "Based on ad sales for " + period_label + ", the leading campaigns are: " + "; ".join(
                f"{item['name']} ({currency_amount_text(item['sales'], item.get('currency'))} sales, {item['orders']} orders, {item['roas']:.2f} ROAS, {item['clicks']} clicks)"
                for item in leaders
            ) + ". I rank by ad sales first, then ROAS, so a small high-ROAS campaign does not hide the campaign driving the most revenue."
            evidence.append(f"Amazon Ads, {period_label}, report ending {leaders[0].get('reportDate') or 'latest'}")
        else:
            answer = f"I do not have campaign rows for {period_label} yet, so I cannot rank them."
    elif "design" in normalized and any(phrase in normalized for phrase in ("not advertised", "aren't advertised", "unadvertised", "should be advertised", "advertise")):
        campaign_names = [str(item.get("name", "")).lower() for item in campaigns_last7]
        candidates = []
        for design in designs_last7:
            title = str(design.get("title", ""))
            words = {word for word in title.lower().split() if len(word) >= 4}
            has_campaign_match = any(
                title.lower() in name or len(words & {word for word in name.split() if len(word) >= 4}) >= 2
                for name in campaign_names
            )
            if not has_campaign_match:
                candidates.append(design)
        if candidates:
            answer = "The strongest designs without an obvious campaign-name match are: " + "; ".join(
                f"{item['title']} ({item['units']} units, {design_royalties_text(item)} royalties)"
                for item in candidates[:8]
            ) + ". These are candidates for advertising review, not proof that no ad target exists; confirm by ASIN before creating a campaign."
            evidence.append(f"Merch sales and Amazon campaign names, {period_label}")
        else:
            answer = "Every design in the current sales view has at least one obvious campaign-name match. Review ASIN-level coverage for gaps."
    elif any(phrase in normalized for phrase in ("heating up", "top design", "best design", "selling")) or (
        "what about" in normalized and any("design" in str(item.get("text", "")).lower() for item in history)
    ):
        leaders = designs_last7[:5]
        scope = f" in {market_label}" if market_label else ""
        if leaders:
            answer = f"Strongest designs{scope} for {period_label}:\n" + "\n".join(
                f"• {item['title']} — {item['units']} units; {design_royalties_text(item)} royalties" for item in leaders
            )
        else:
            answer = f"I do not have design sales rows for {period_label}{scope}."
        evidence.append(f"Merch sales{', ' + market_label if market_label else ''}, {period_label} ending {sales_last7.get('reportDate') or 'latest report'}")
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
        all_designs = designs_payload(analysis_period, market_filter).get("designs", [])
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
        if any(phrase in normalized for phrase in ("roas for the campaigns", "campaign roas", "roas of the campaigns")) and not found_campaigns:
            referenced_campaigns = sorted(
                campaigns_last7,
                key=lambda item: (item.get("sales", 0), item.get("orders", 0), -item.get("spend", 0)),
                reverse=True,
            )[:5]
            if referenced_campaigns:
                answer = "For " + period_label + ", the campaigns most recently referenced are: " + "; ".join(
                    f"{item['name']} ({item['roas']:.2f} ROAS, {item['orders']} orders, {currency_amount_text(item['sales'], item.get('currency'))} ad sales)"
                    for item in referenced_campaigns
                ) + "."
                evidence.append(f"Amazon Ads, {period_label}, report ending {referenced_campaigns[0].get('reportDate') or 'latest'}")
            else:
                answer = f"I do not have campaign rows for {period_label} yet, so I cannot calculate ROAS."
        elif found_campaigns and any(phrase in normalized for phrase in ("ad group", "ad groups", "adgroup")):
            campaign = found_campaigns[0]
            detail = campaign_detail_payload(campaign["name"], analysis_period)
            ad_groups = detail.get("adGroups", [])
            if ad_groups:
                wants_counts = any(word in normalized for word in ("unit", "order", "purchase", "count", "how many"))
                if wants_counts and any(phrase in normalized for phrase in ("not dollar", "no dollar", "instead of dollar", "rather than dollar")):
                    group_text = "; ".join(f"{group['name']}: {group['orders']} purchases across {group['targetCount']} targets" for group in ad_groups)
                    answer = f"For {period_label}, {campaign['name']} has these ad-group purchase counts: {group_text}. Amazon Ads reports purchases/orders here, not Merch royalty units."
                else:
                    group_text = "; ".join(
                        f"{group['name']} ({group['targetCount']} targets, {group['orders']} orders, {currency_amount_text(group['sales'], campaign.get('currency'))} ad sales)"
                        for group in ad_groups
                    )
                    answer = f"For {period_label}, the ad groups in {campaign['name']} are: {group_text}."
            else:
                answer = f"I found the {campaign['name']} campaign, but there are no ad-group rows in the imported {period_label} target data yet."
            evidence.append(f"Amazon Ads target report, {period_label}, report ending {campaign.get('reportDate') or 'latest'}")
        elif found_campaigns and any(metric in normalized for metric in ("impression", "click", "spend", "cost", "sales", "order", "roas", "acos", "ctr")):
            campaign = found_campaigns[0]
            impressions = int(campaign.get("impressions") or 0)
            clicks = int(campaign.get("clicks") or 0)
            ctr = clicks / impressions * 100 if impressions else 0
            answer = (
                f"For {period_label}, {campaign['name']} had {impressions:,} impressions and {clicks:,} clicks "
                f"({ctr:.2f}% CTR), spent {currency_amount_text(campaign['spend'], campaign.get('currency'))}, "
                f"generated {currency_amount_text(campaign['sales'], campaign.get('currency'))} in ad sales, "
                f"and produced {campaign['orders']} orders at {campaign['roas']:.2f} ROAS."
            )
            evidence.append(f"Amazon Ads, {period_label}, report ending {campaign.get('reportDate') or 'latest'}")
        elif found_campaigns and any(word in normalized for word in ("why", "weak", "wrong", "happening", "explain")):
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


def ai_search_terms_provider(campaign="", search="", period="last7", limit=20):
    conn = connect()
    cur = conn.cursor()
    period_order = {"yesterday": 0, "last7": 1, "last14": 2, "last30": 3, "last60": 4}
    available_periods = sorted(
        {
            str(row[0] or "")
            for row in cur.execute(
                """SELECT DISTINCT COALESCE(report_period, '')
                   FROM search_terms
                   WHERE COALESCE(report_period, '') != ''"""
            ).fetchall()
        },
        key=lambda value: (period_order.get(value, 99), value),
    )
    latest_import = latest_table_import(cur, "search_terms", period)
    if not latest_import:
        conn.close()
        return {
            "period": period,
            "reportDate": "",
            "availablePeriods": available_periods,
            "searchTerms": [],
        }
    where = [
        "import_date = ?",
        "COALESCE(report_period, 'unspecified') = ?",
    ]
    params = [latest_import, period]
    if campaign:
        where.append("LOWER(campaign_name) LIKE ?")
        params.append(f"%{campaign.lower()}%")
    if search:
        where.append("(LOWER(search_term) LIKE ? OR LOWER(COALESCE(keyword, '')) LIKE ? OR LOWER(COALESCE(targeting, '')) LIKE ?)")
        search_like = f"%{search.lower()}%"
        params.extend([search_like, search_like, search_like])
    params.append(limit)
    rows = cur.execute(
        f"""SELECT campaign_name, COALESCE(ad_group_name, '') AS ad_group_name,
                   COALESCE(country, '') AS country,
                   COALESCE(search_term, '') AS search_term, COALESCE(keyword, '') AS keyword,
                   COALESCE(targeting, '') AS targeting, COALESCE(match_type, '') AS match_type,
                   SUM(COALESCE(impressions, 0)) AS impressions, SUM(COALESCE(clicks, 0)) AS clicks,
                   SUM(COALESCE(spend, 0)) AS spend, SUM(COALESCE(orders, 0)) AS orders,
                   SUM(COALESCE(sales, 0)) AS sales, MAX(COALESCE(report_date, '')) AS report_date
            FROM search_terms
            WHERE {' AND '.join(where)}
            GROUP BY campaign_name, COALESCE(ad_group_name, ''), COALESCE(country, ''), COALESCE(search_term, ''),
                     COALESCE(keyword, ''), COALESCE(targeting, ''), COALESCE(match_type, '')
            ORDER BY SUM(COALESCE(sales, 0)) DESC, SUM(COALESCE(orders, 0)) DESC,
                     SUM(COALESCE(spend, 0)) DESC
            LIMIT ?""",
        params,
    ).fetchall()
    conn.close()
    items = []
    report_date = ""
    for row in rows:
        spend = money(row["spend"])
        sales = money(row["sales"])
        report_date = max(report_date, row["report_date"] or "")
        items.append({
            "campaign": row["campaign_name"],
            "adGroup": row["ad_group_name"],
            "marketplace": row["country"],
            "searchTerm": row["search_term"],
            "keyword": row["keyword"],
            "targeting": row["targeting"],
            "matchType": row["match_type"],
            "impressions": int(row["impressions"] or 0),
            "clicks": int(row["clicks"] or 0),
            "spend": spend,
            "orders": int(row["orders"] or 0),
            "sales": sales,
            "roas": round(sales / spend, 2) if spend else 0,
        })
    return {
        "period": period,
        "reportDate": report_date,
        "availablePeriods": available_periods,
        "searchTerms": items,
    }


def ai_placements_provider(campaign="", period="last7", limit=20):
    conn = connect()
    cur = conn.cursor()
    available_periods = [
        str(row[0] or "")
        for row in cur.execute(
            """SELECT DISTINCT COALESCE(report_period, '')
               FROM placements
               WHERE COALESCE(report_period, '') != ''
               ORDER BY report_period"""
        ).fetchall()
    ]
    latest_import = latest_table_import(cur, "placements", period)
    if not latest_import:
        conn.close()
        return {
            "period": period,
            "reportDate": "",
            "availablePeriods": available_periods,
            "dataAgeDays": None,
            "stale": True,
            "placements": [],
        }
    where = ["import_date = ?", "COALESCE(report_period, 'unspecified') = ?"]
    params = [latest_import, period]
    if campaign:
        where.append("LOWER(campaign_name) LIKE ?")
        params.append(f"%{campaign.lower()}%")
    params.append(limit)
    rows = cur.execute(
        f"""SELECT campaign_name, placement, COALESCE(country, '') AS country,
                   COALESCE(currency, '') AS currency,
                   SUM(COALESCE(impressions, 0)) AS impressions, SUM(COALESCE(clicks, 0)) AS clicks,
                   SUM(COALESCE(spend, 0)) AS spend, SUM(COALESCE(orders, 0)) AS orders,
                   SUM(COALESCE(units, 0)) AS units, SUM(COALESCE(sales, 0)) AS sales,
                   MAX(COALESCE(report_date, '')) AS report_date
            FROM placements
            WHERE {' AND '.join(where)}
            GROUP BY campaign_name, placement, COALESCE(country, ''), COALESCE(currency, '')
            ORDER BY SUM(COALESCE(sales, 0)) DESC, SUM(COALESCE(spend, 0)) DESC
            LIMIT ?""",
        params,
    ).fetchall()
    conn.close()
    items = []
    report_date = ""
    for row in rows:
        spend = money(row["spend"])
        sales = money(row["sales"])
        report_date = max(report_date, row["report_date"] or "")
        items.append({
            "campaign": row["campaign_name"],
            "placement": row["placement"],
            "country": row["country"],
            "currency": row["currency"],
            "impressions": int(row["impressions"] or 0),
            "clicks": int(row["clicks"] or 0),
            "spend": spend,
            "orders": int(row["orders"] or 0),
            "units": int(row["units"] or 0),
            "sales": sales,
            "roas": round(sales / spend, 2) if spend else 0,
        })
    try:
        data_age_days = max(0, (amazon_reporting_date() - date.fromisoformat(report_date)).days)
    except (TypeError, ValueError):
        data_age_days = None
    return {
        "period": period,
        "reportDate": report_date,
        "availablePeriods": available_periods,
        "dataAgeDays": data_age_days,
        "stale": data_age_days is None or data_age_days > 2,
        "placements": items,
    }


def build_ai_tool_layer():
    write_coordinator = RecommendationWriteCoordinator(str(DB_PATH))

    def normalized_market_code(value):
        normalized = str(value or "").strip().lower()
        if not normalized:
            return ""
        if normalized in MARKET_NAMES:
            return normalized
        detected = market_from_text(normalized)
        if detected:
            return detected
        for code, name in MARKET_NAMES.items():
            if normalized == name.lower():
                return code
        return ""

    def query_sales(period="last7", market="", limit=20):
        payload = home_payload(period)
        markets = payload.get("markets", [])
        products = payload.get("products", [])
        units = payload.get("sales", 0)
        returns = payload.get("returns", 0)
        royalties_by_currency = payload.get("royaltyByCurrency", [])
        market_code = normalized_market_code(market)
        if market:
            market_name = MARKET_NAMES.get(market_code, str(market).strip())
            markets = [
                item for item in markets
                if (
                    market_code and str(item.get("code", "")).lower() == market_code
                ) or str(item.get("name", "")).lower() == market_name.lower()
            ]
            products = [
                item for item in products
                if str(item.get("market", "")).lower() == market_name.lower()
            ]
            units = sum(int(item.get("units", 0) or 0) for item in markets)
            returns = sum(int(item.get("returned", 0) or 0) for item in markets)
            royalty_totals = {}
            for item in markets:
                currency = str(item.get("currency") or "USD").upper()
                royalty_totals[currency] = money(
                    royalty_totals.get(currency, 0) + float(item.get("royalties", 0) or 0)
                )
            royalties_by_currency = [
                {"currency": currency, "amount": amount}
                for currency, amount in sorted(royalty_totals.items())
            ]
        return {
            "period": period,
            "periodStart": payload.get("periodStart", ""),
            "periodEnd": payload.get("periodEnd", ""),
            "reportDate": payload.get("reportDate", ""),
            "requestedMarket": market,
            "marketCode": market_code,
            "marketMatched": bool(markets) if market else True,
            "units": units,
            "royaltiesByCurrency": royalties_by_currency,
            "returns": returns,
            "markets": markets[:limit],
            "products": products[:limit],
        }

    def query_designs(period="last7", market="", search="", limit=20):
        market_code = normalized_market_code(market)
        payload = designs_payload(period, market_code or market or None, search)
        designs = payload.get("designs", [])
        return {
            "period": period,
            "market": market,
            "marketCode": market_code,
            "reportDate": payload.get("reportDate", ""),
            "designs": designs[:limit],
        }

    def query_campaigns(period="last7", search="", limit=20):
        payload = campaigns_payload(period, search)
        return {
            "period": period,
            "reportDate": payload.get("reportDate", ""),
            "campaigns": payload.get("campaigns", [])[:limit],
        }

    def query_ad_groups(campaign, period="last7", limit=20):
        payload = campaign_detail_payload(campaign, period)
        return {
            "period": period,
            "campaign": campaign,
            "reportDate": payload.get("reportDate") or payload.get("targetReportDate", ""),
            "adGroups": payload.get("adGroups", [])[:limit],
        }

    def query_targets(campaign, period="last7", limit=20):
        payload = campaign_detail_payload(campaign, period)
        return {
            "period": period,
            "campaign": campaign,
            "reportDate": payload.get("reportDate") or payload.get("targetReportDate", ""),
            "targets": payload.get("targets", [])[:limit],
        }

    def query_recommendations(period="last7", status="all", limit=20):
        audit = build_daily_audit()
        history = recommendation_history_context(limit=limit)
        history_by_id = {item.get("recommendationId"): item for item in history}

        def recommendation_with_id(recommendation_type, item):
            recommendation_id = item.get("recommendationId") or "|".join(str(value or "") for value in (
                recommendation_type,
                item.get("campaignName"),
                item.get("target") or item.get("searchTerm") or item.get("title"),
                item.get("action") or item.get("pattern"),
                item.get("currentBid"),
                item.get("suggestedBid"),
                item.get("reportDate") or item.get("periodEnd"),
            ))
            saved = history_by_id.get(recommendation_id, {})
            return {
                **item,
                "recommendationId": recommendation_id,
                "recommendationType": recommendation_type,
                "status": saved.get("status") or item.get("status", "Proposed"),
                "userNotes": saved.get("userNotes") or item.get("changeNote", ""),
            }

        recommendations = []
        for key, recommendation_type in (
            ("bidRecommendations14Day", "bid_14"),
            ("bidRecommendations", "bid_30"),
            ("searchTermFindings14Day", "search_14"),
            ("searchTermFindings", "search_30"),
            ("salesPatternOpportunities", "sales_opportunity"),
        ):
            recommendations.extend(
                recommendation_with_id(recommendation_type, item)
                for item in (audit.get(key) or [])
            )
        if status != "all":
            recommendations = [item for item in recommendations if item.get("status") == status]
        return {
            "period": period,
            "reportDate": audit.get("targetReportDate") or audit.get("salesDataThrough", ""),
            "recommendations": recommendations[:limit],
            "decisionHistory": history[:limit],
        }

    def compare_periods(entity, period_a, period_b, search="", market="", limit=20):
        if entity == "sales":
            first = query_sales(period_a, market, limit)
            second = query_sales(period_b, market, limit)
        elif entity == "designs":
            first = query_designs(period_a, market, search, limit)
            second = query_designs(period_b, market, search, limit)
        else:
            first = query_campaigns(period_a, search, limit)
            second = query_campaigns(period_b, search, limit)
        return {"entity": entity, "periodA": first, "periodB": second}

    def prepare_write(tool_name, _context, **arguments):
        conversation_id = str((_context or {}).get("conversation_id") or "")
        pending = write_coordinator.prepare(conversation_id, tool_name, arguments)
        return {
            "requiresConfirmation": True,
            "confirmationToken": pending.confirmation_token,
            "tool": pending.tool_name,
            "arguments": pending.arguments,
            "expiresAt": pending.expires_at,
            "duplicatePending": pending.duplicate,
            "instruction": 'No write has occurred. Ask the user to reply "confirm" to apply this internal Merch Agent action.',
        }

    return RestrictedAIToolLayer({
        "query_sales": query_sales,
        "query_designs": query_designs,
        "query_campaigns": query_campaigns,
        "query_ad_groups": query_ad_groups,
        "query_targets": query_targets,
        "query_search_terms": ai_search_terms_provider,
        "query_placements": ai_placements_provider,
        "query_recommendations": query_recommendations,
        "compare_periods": compare_periods,
        "record_user_action": lambda _context, **kwargs: prepare_write("record_user_action", _context, **kwargs),
        "defer_recommendation": lambda _context, **kwargs: prepare_write("defer_recommendation", _context, **kwargs),
        "dismiss_recommendation": lambda _context, **kwargs: prepare_write("dismiss_recommendation", _context, **kwargs),
        "add_user_note": lambda _context, **kwargs: prepare_write("add_user_note", _context, **kwargs),
    }, allow_local_writes=True)


_AI_ASSISTANT = None


def openai_assistant():
    global _AI_ASSISTANT
    if _AI_ASSISTANT is None:
        _AI_ASSISTANT = ResponsesAssistant(
            tools=build_ai_tool_layer(),
            store=ConversationStore(str(DB_PATH)),
            config=AssistantConfig.from_env(),
            write_coordinator=RecommendationWriteCoordinator(str(DB_PATH)),
        )
    return _AI_ASSISTANT


def ai_usage_payload():
    return UsageControls(str(DB_PATH)).stats()


def update_ai_settings(payload):
    updates = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
    if not isinstance(updates, dict):
        raise ValueError("AI settings must be an object.")
    return {"ok": True, "settings": UsageControls(str(DB_PATH)).update_settings(updates)}


def hybrid_assistant_payload(
    question,
    conversation_id,
    identity,
    requested_period="last30",
    history=None,
    recommendation_context=None,
):
    config = AssistantConfig.from_env()
    if config.mode == "rules":
        result = assistant_payload(question, history or [], requested_period, recommendation_context)
        result.update({"source": "deterministic_rules", "fallback": False})
        return result
    try:
        return openai_assistant().answer(
            question,
            conversation_id=conversation_id,
            owner_hash=owner_hash(identity),
            authenticated=authentication_enabled(),
            default_period=requested_period,
            recommendation_context=recommendation_context,
        )
    except (AssistantUnavailable, UngroundedAnswer, UsageLimitReached, ValueError) as exc:
        if config.mode == "hybrid":
            result = assistant_payload(question, history or [], requested_period, recommendation_context)
            reason = (
                "usage_limit"
                if isinstance(exc, UsageLimitReached)
                else "grounding_guard"
                if isinstance(exc, UngroundedAnswer)
                else "openai_unavailable"
            )
            result.update({
                "source": "rules_fallback",
                "fallback": True,
                "fallbackReason": reason,
                "fallbackLabel": "Rules fallback",
            })
            return result
        raise


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

        if parsed.path == "/api/sales-status":
            self.send_json(sales_status_payload())
            return

        if parsed.path == "/api/data-freshness":
            self.send_json(data_freshness_payload())
            return

        if parsed.path == "/api/recommendation-interactions":
            self.send_json(recommendation_interactions_payload())
            return

        if parsed.path == "/api/ai-usage":
            self.send_json(ai_usage_payload())
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

        allowed_paths = {"/api/assistant", "/api/ai-settings", "/api/campaign-change", "/api/change-preview", "/api/refresh-ads-now", "/api/sales-sync", "/api/sales-upload", "/api/imports/merch-sales", "/api/imports/merch-sales-status", "/api/recommendation-interaction"}
        if parsed.path not in allowed_paths:
            self.send_json({"error": "Not found"}, 404)
            return

        if parsed.path in {"/api/imports/merch-sales", "/api/imports/merch-sales-status"}:
            if not import_token_is_valid(self.headers.get("Authorization", "")):
                self.send_json({"error": "A valid Merch sales import token is required."}, 401)
                return
        elif self.require_authentication():
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
            request_limit = (MAX_SALES_UPLOAD_BYTES * 2) if parsed.path in {"/api/sales-upload", "/api/imports/merch-sales"} else 50000
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
            elif parsed.path == "/api/imports/merch-sales":
                result = import_merch_sales_payload(payload)
                self.send_json(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/imports/merch-sales-status":
                if str(payload.get("action", "check")).lower() == "check":
                    result = {"ok": True, "status": sales_status_payload()}
                else:
                    result = record_sales_downloader_event(payload)
                self.send_json(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/recommendation-interaction":
                result = save_recommendation_interaction(payload)
                self.send_json(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/ai-settings":
                result = update_ai_settings(payload)
                self.send_json(result)
            elif parsed.path == "/api/refresh-ads-now":
                result = start_hosted_ads_refresh_now(str(payload.get("mode", "current")).lower())
                self.send_json(result, 202 if result.get("ok") else 409)
            else:
                question = payload.get("question", "")
                ai_result = hybrid_assistant_payload(
                    question,
                    payload.get("conversationId", ""),
                    f"{AUTH_USERNAME}:{self.headers.get('Authorization', '')}",
                    payload.get("period", "last30"),
                    payload.get("history", []),
                    payload.get("recommendationContext"),
                )
                self.send_json(ai_result)
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid request"}, 400)
        except (AssistantUnavailable, UngroundedAnswer):
            self.send_json({"error": "The AI assistant is temporarily unavailable."}, 503)


if __name__ == "__main__":
    setup_database()
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
