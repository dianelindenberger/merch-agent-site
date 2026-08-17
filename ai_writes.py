"""Confirmed, idempotent internal recommendation writes.

This module cannot call Amazon Ads. It owns transactions for Merch Agent's
recommendation interactions, campaign change log, and internal audit tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import sqlite3
from typing import Any, Mapping


CONFIRMATION_TTL_MINUTES = 10
IDENTIFIER_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,240}$")
CONFIRM_PHRASES = frozenset({
    "confirm",
    "confirm it",
    "yes confirm",
    "yes, confirm",
    "confirm this",
    "confirm this action",
    "go ahead and confirm",
})
CANCEL_PHRASES = frozenset({"cancel", "cancel it", "never mind", "nevermind", "do not confirm"})


class WriteValidationError(ValueError):
    pass


class ConfirmationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingWrite:
    confirmation_token: str
    tool_name: str
    arguments: dict[str, Any]
    expires_at: str
    duplicate: bool = False


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def explicit_confirmation(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().strip().split()).strip(".!?")
    return normalized in CONFIRM_PHRASES


def explicit_cancellation(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().strip().split()).strip(".!?")
    return normalized in CANCEL_PHRASES


def validate_identifier(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(clean):
        raise WriteValidationError(f"{field} is invalid.")
    return clean


def _canonical_arguments(arguments: Mapping[str, Any]) -> tuple[str, str]:
    canonical = json.dumps(dict(arguments), separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RecommendationWriteCoordinator:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _after_primary_write(self, conn: sqlite3.Connection) -> None:
        """Test hook invoked before commit."""

    def prepare(self, conversation_id: str, tool_name: str, arguments: Mapping[str, Any]) -> PendingWrite:
        conversation_id = validate_identifier(conversation_id, "conversation_id")
        validated = self._validate_action(tool_name, arguments)
        arguments_json, arguments_hash = _canonical_arguments(validated)
        idempotency_key = hashlib.sha256(
            f"{conversation_id}|{tool_name}|{arguments_hash}".encode("utf-8")
        ).hexdigest()
        now = _now_dt()
        expires_at = (now + timedelta(minutes=CONFIRMATION_TTL_MINUTES)).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_pending(conn, now.isoformat())
            existing = conn.execute(
                """SELECT confirmation_token, tool_name, arguments_json, expires_at, state
                   FROM ai_write_confirmations WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if existing and existing["state"] in {"pending", "confirmed", "used"}:
                conn.commit()
                return PendingWrite(
                    existing["confirmation_token"],
                    existing["tool_name"],
                    json.loads(existing["arguments_json"]),
                    existing["expires_at"],
                    duplicate=True,
                )
            if existing:
                idempotency_key = hashlib.sha256(
                    f"{idempotency_key}|retry|{now.isoformat()}".encode("utf-8")
                ).hexdigest()
            token = secrets.token_urlsafe(24)
            conn.execute(
                """INSERT INTO ai_write_confirmations
                   (confirmation_token, conversation_id, tool_name, arguments_json, arguments_hash,
                    idempotency_key, state, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (token, conversation_id, tool_name, arguments_json, arguments_hash, idempotency_key, now.isoformat(), expires_at),
            )
            self._audit(conn, token, conversation_id, tool_name, idempotency_key, "prepared", "pending", {})
            conn.commit()
            return PendingWrite(token, tool_name, validated, expires_at)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def confirm_latest(self, conversation_id: str) -> dict[str, Any]:
        conversation_id = validate_identifier(conversation_id, "conversation_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            self._expire_pending(conn, now)
            row = conn.execute(
                """SELECT * FROM ai_write_confirmations
                   WHERE conversation_id = ? AND state = 'pending'
                   ORDER BY created_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if not row:
                used = conn.execute(
                    """SELECT result_json FROM ai_write_confirmations
                       WHERE conversation_id = ? AND state = 'used'
                       ORDER BY used_at DESC LIMIT 1""",
                    (conversation_id,),
                ).fetchone()
                conn.commit()
                if used:
                    return {"ok": True, "duplicate": True, "result": json.loads(used["result_json"] or "{}")}
                raise ConfirmationRequired("There is no pending action to confirm.")
            conn.execute(
                "UPDATE ai_write_confirmations SET state = 'confirmed', confirmed_at = ? WHERE confirmation_token = ? AND state = 'pending'",
                (now, row["confirmation_token"]),
            )
            result = self._apply(conn, row["tool_name"], json.loads(row["arguments_json"]), now)
            self._after_primary_write(conn)
            result_json = json.dumps(result, separators=(",", ":"), sort_keys=True)
            conn.execute(
                """UPDATE ai_write_confirmations
                   SET state = 'used', used_at = ?, result_json = ?
                   WHERE confirmation_token = ? AND state = 'confirmed'""",
                (now, result_json, row["confirmation_token"]),
            )
            self._audit(
                conn, row["confirmation_token"], conversation_id, row["tool_name"],
                row["idempotency_key"], "confirmed_and_applied", "success", result,
            )
            conn.commit()
            return {"ok": True, "duplicate": False, "result": result}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel_latest(self, conversation_id: str) -> dict[str, Any]:
        conversation_id = validate_identifier(conversation_id, "conversation_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM ai_write_confirmations
                   WHERE conversation_id = ? AND state = 'pending'
                   ORDER BY created_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if not row:
                conn.commit()
                return {"ok": False, "message": "There is no pending action to cancel."}
            conn.execute(
                "UPDATE ai_write_confirmations SET state = 'cancelled' WHERE confirmation_token = ? AND state = 'pending'",
                (row["confirmation_token"],),
            )
            self._audit(conn, row["confirmation_token"], conversation_id, row["tool_name"], row["idempotency_key"], "cancelled", "success", {})
            conn.commit()
            return {"ok": True, "message": "The pending action was cancelled."}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def pending_for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        conversation_id = validate_identifier(conversation_id, "conversation_id")
        conn = self._connect()
        now = _now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_pending(conn, now)
            row = conn.execute(
                """SELECT confirmation_token, tool_name, arguments_json, expires_at
                   FROM ai_write_confirmations
                   WHERE conversation_id = ? AND state = 'pending'
                   ORDER BY created_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if not row:
            return None
        return {
            "confirmationToken": row["confirmation_token"],
            "tool": row["tool_name"],
            "arguments": json.loads(row["arguments_json"]),
            "expiresAt": row["expires_at"],
        }

    def _expire_pending(self, conn: sqlite3.Connection, now: str) -> None:
        rows = conn.execute(
            "SELECT * FROM ai_write_confirmations WHERE state = 'pending' AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE ai_write_confirmations SET state = 'expired' WHERE confirmation_token = ? AND state = 'pending'",
                (row["confirmation_token"],),
            )
            self._audit(conn, row["confirmation_token"], row["conversation_id"], row["tool_name"], row["idempotency_key"], "expired", "expired", {})

    def _validate_action(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        args = dict(arguments)
        recommendation_id = validate_identifier(args.get("recommendation_id"), "recommendation_id")
        if tool_name == "record_user_action":
            action = str(args.get("action") or "").strip()
            if action not in {"completed", "explain_why"}:
                raise WriteValidationError("action is invalid.")
            entity_type = str(args.get("entity_type") or "").strip()
            if entity_type and entity_type not in {"campaign", "ad_group", "target", "recommendation"}:
                raise WriteValidationError("entity_type is invalid.")
            entity_name = str(args.get("entity_name") or "").strip()
            if entity_name:
                validate_identifier(entity_name, "entity_name")
            return {
                "recommendation_id": recommendation_id,
                "action": action,
                "note": str(args.get("note") or "").strip()[:2000],
                "previous_value": str(args.get("previous_value") or "").strip()[:120],
                "new_value": str(args.get("new_value") or "").strip()[:120],
                "entity_type": entity_type,
                "entity_name": entity_name,
            }
        if tool_name == "defer_recommendation":
            duration = str(args.get("duration") or "").strip()
            if duration not in {"tomorrow", "3_days", "1_week"}:
                raise WriteValidationError("duration is invalid.")
            return {"recommendation_id": recommendation_id, "duration": duration}
        if tool_name == "dismiss_recommendation":
            reason = str(args.get("reason") or "").strip()
            if not reason or len(reason) > 2000:
                raise WriteValidationError("reason is required.")
            return {"recommendation_id": recommendation_id, "reason": reason}
        if tool_name == "add_user_note":
            note = str(args.get("note") or "").strip()
            if not note or len(note) > 2000:
                raise WriteValidationError("note is required.")
            return {"recommendation_id": recommendation_id, "note": note}
        raise WriteValidationError("Write tool is not permitted.")

    def _apply(self, conn: sqlite3.Connection, tool_name: str, args: dict[str, Any], now: str) -> dict[str, Any]:
        recommendation_id = args["recommendation_id"]
        existing = conn.execute(
            "SELECT * FROM recommendation_interactions WHERE recommendation_id = ?",
            (recommendation_id,),
        ).fetchone()
        created_at = (existing["date_created"] if existing and existing["date_created"] else now)
        if tool_name == "record_user_action":
            status = "Completed" if args["action"] == "completed" else "Proposed"
            self._upsert_interaction(
                conn, recommendation_id, status, args["action"], args["note"], "", now, created_at,
            )
            if args["previous_value"] or args["new_value"]:
                conn.execute(
                    """INSERT INTO campaign_change_log
                       (campaign_name, target_name, change_type, details, previous_value, new_value,
                        effective_date, summary, logged_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        args["entity_name"] if args["entity_type"] == "campaign" else "",
                        args["entity_name"] if args["entity_type"] in {"target", "ad_group"} else "",
                        "ai_recorded_user_change",
                        args["note"],
                        args["previous_value"],
                        args["new_value"],
                        now[:10],
                        f"User confirmed {args['action']} for {recommendation_id}",
                        now,
                    ),
                )
            return {"recommendationId": recommendation_id, "status": status, "action": args["action"], "recordedAt": now}
        if tool_name == "defer_recommendation":
            days = {"tomorrow": 1, "3_days": 3, "1_week": 7}[args["duration"]]
            reminder_at = (_now_dt() + timedelta(days=days)).isoformat()
            self._upsert_interaction(conn, recommendation_id, "Deferred", "remind_later", "", reminder_at, now, created_at)
            return {"recommendationId": recommendation_id, "status": "Deferred", "reminderAt": reminder_at}
        if tool_name == "dismiss_recommendation":
            self._upsert_interaction(conn, recommendation_id, "Ignored", "ignore", args["reason"], "", now, created_at)
            return {"recommendationId": recommendation_id, "status": "Ignored", "reason": args["reason"]}
        if tool_name == "add_user_note":
            prior_note = (existing["user_notes"] if existing and existing["user_notes"] else "")
            combined = "\n".join(part for part in (prior_note, args["note"]) if part).strip()[:4000]
            status = existing["status"] if existing else "Proposed"
            self._upsert_interaction(conn, recommendation_id, status, "add_note", combined, "", now, created_at)
            return {"recommendationId": recommendation_id, "status": status, "note": args["note"]}
        raise WriteValidationError("Write tool is not permitted.")

    def _upsert_interaction(
        self, conn: sqlite3.Connection, recommendation_id: str, status: str, action: str,
        note: str, reminder_at: str, now: str, created_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO recommendation_interactions
               (recommendation_id, recommendation_type, recommendation_context, status,
                last_action, reason, reminder_at, acted_at, updated_at, user_action,
                user_notes, date_created, date_completed)
               VALUES (?, 'ai', '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(recommendation_id) DO UPDATE SET
                 status = excluded.status, last_action = excluded.last_action,
                 reason = excluded.reason, reminder_at = excluded.reminder_at,
                 acted_at = excluded.acted_at, updated_at = excluded.updated_at,
                 user_action = excluded.user_action, user_notes = excluded.user_notes,
                 date_completed = excluded.date_completed""",
            (
                recommendation_id, status, action, note, reminder_at, now, now, action,
                note, created_at, now if status == "Completed" else None,
            ),
        )

    def _audit(
        self, conn: sqlite3.Connection, token: str, conversation_id: str, tool_name: str,
        idempotency_key: str, event: str, status: str, details: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO ai_write_audit
               (confirmation_token, conversation_id, tool_name, idempotency_key,
                event, status, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token, conversation_id, tool_name, idempotency_key, event, status,
                json.dumps(dict(details), separators=(",", ":"), sort_keys=True), _now(),
            ),
        )
