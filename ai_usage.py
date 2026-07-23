"""Token-based AI usage accounting for Merch Agent."""

from __future__ import annotations

from dataclasses import dataclass
import calendar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import sqlite3
from typing import Any, Mapping


MICRODOLLARS_PER_DOLLAR = Decimal("1000000")
TOKENS_PER_MILLION = Decimal("1000000")
PRICING_VERSION = "2026-07-config-v1"


@dataclass(frozen=True)
class ModelRates:
    input_per_million: Decimal
    cached_read_per_million: Decimal
    output_per_million: Decimal
    cache_write_multiplier: Decimal = Decimal("1.25")


DEFAULT_RATES = {
    "gpt-5.6-luna": ModelRates(Decimal("1.00"), Decimal("0.10"), Decimal("6.00")),
    "gpt-5.6-terra": ModelRates(Decimal("2.50"), Decimal("0.25"), Decimal("15.00")),
}


@dataclass(frozen=True)
class UsageRecord:
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    cost_microusd: int
    raw: dict[str, Any]

    @property
    def cost_usd(self) -> Decimal:
        return Decimal(self.cost_microusd) / MICRODOLLARS_PER_DOLLAR


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def usage_from_response(response: Any, model: str, rates: Mapping[str, ModelRates] | None = None) -> UsageRecord:
    usage = _get(response, "usage", {}) or {}
    details = _get(usage, "input_tokens_details", {}) or {}
    input_tokens = _nonnegative_int(_get(usage, "input_tokens"))
    cached_tokens = _nonnegative_int(_get(details, "cached_tokens"))
    cache_write_tokens = _nonnegative_int(
        _get(details, "cache_write_tokens", _get(details, "cache_creation_tokens", 0))
    )
    output_tokens = _nonnegative_int(_get(usage, "output_tokens"))
    raw = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
    }
    cost = calculate_cost_microusd(
        model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        rates=rates,
    )
    return UsageRecord(input_tokens, cached_tokens, cache_write_tokens, output_tokens, cost, raw)


def calculate_cost_microusd(
    model: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    rates: Mapping[str, ModelRates] | None = None,
) -> int:
    rate_map = rates or DEFAULT_RATES
    rate = rate_map.get(model)
    if rate is None:
        raise ValueError(f"No configured pricing for model: {model}")
    total_input = _nonnegative_int(input_tokens)
    cached = min(_nonnegative_int(cached_input_tokens), total_input)
    cache_write = min(_nonnegative_int(cache_write_tokens), max(total_input - cached, 0))
    uncached = max(total_input - cached - cache_write, 0)
    cost_dollars = (
        Decimal(uncached) / TOKENS_PER_MILLION * rate.input_per_million
        + Decimal(cached) / TOKENS_PER_MILLION * rate.cached_read_per_million
        + Decimal(cache_write) / TOKENS_PER_MILLION * rate.input_per_million * rate.cache_write_multiplier
        + Decimal(_nonnegative_int(output_tokens)) / TOKENS_PER_MILLION * rate.output_per_million
    )
    return int((cost_dollars * MICRODOLLARS_PER_DOLLAR).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def configured_limits() -> dict[str, Decimal]:
    return {
        "warning1": Decimal(os.getenv("MERCH_AGENT_AI_WARNING_1_USD", "5")),
        "warning2": Decimal(os.getenv("MERCH_AGENT_AI_WARNING_2_USD", "8")),
        "monthlyLimit": Decimal(os.getenv("MERCH_AGENT_AI_MONTHLY_LIMIT_USD", "10")),
    }


SETTING_DEFAULTS = {
    "warning_threshold_usd": "5",
    "warning_high_threshold_usd": "8",
    "monthly_limit_usd": "10",
    "daily_request_limit": "100",
    "max_input_tokens": "12000",
    "max_output_tokens": "1200",
    "terra_enabled": "false",
    "default_model": "gpt-5.6-luna",
    "complex_model": "gpt-5.6-terra",
}


class UsageLimitReached(RuntimeError):
    pass


class UsageControls:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def settings(self) -> dict[str, Any]:
        conn = self._connect()
        rows = conn.execute("SELECT setting_key, setting_value FROM ai_settings").fetchall()
        conn.close()
        values = dict(SETTING_DEFAULTS)
        values.update({row["setting_key"]: row["setting_value"] for row in rows})
        values["default_model"] = os.getenv("MERCH_AGENT_AI_MODEL_LUNA", values["default_model"])
        values["complex_model"] = os.getenv("MERCH_AGENT_AI_MODEL_TERRA", values["complex_model"])
        return {
            "warningThresholdUsd": float(values["warning_threshold_usd"]),
            "warningHighThresholdUsd": float(values["warning_high_threshold_usd"]),
            "monthlyLimitUsd": float(values["monthly_limit_usd"]),
            "dailyRequestLimit": int(values["daily_request_limit"]),
            "maxInputTokens": int(values["max_input_tokens"]),
            "maxOutputTokens": int(values["max_output_tokens"]),
            "terraEnabled": str(values["terra_enabled"]).lower() in {"1", "true", "yes"},
            "defaultModel": values["default_model"],
            "complexModel": values["complex_model"],
        }

    def update_settings(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "warningThresholdUsd": ("warning_threshold_usd", float, 0.01, 1000),
            "warningHighThresholdUsd": ("warning_high_threshold_usd", float, 0.01, 1000),
            "monthlyLimitUsd": ("monthly_limit_usd", float, 0.01, 1000),
            "dailyRequestLimit": ("daily_request_limit", int, 1, 10000),
            "maxInputTokens": ("max_input_tokens", int, 500, 200000),
            "maxOutputTokens": ("max_output_tokens", int, 100, 10000),
        }
        unknown = sorted(set(updates) - (set(allowed) | {"terraEnabled"}))
        if unknown:
            raise ValueError(f"Unknown AI setting(s): {', '.join(unknown)}")
        normalized: dict[str, str] = {}
        for public_name, value in updates.items():
            if public_name == "terraEnabled":
                if not isinstance(value, bool):
                    raise ValueError("terraEnabled must be true or false.")
                normalized["terra_enabled"] = "true" if value else "false"
                continue
            key, caster, minimum, maximum = allowed[public_name]
            try:
                cast_value = caster(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{public_name} is invalid.") from exc
            if cast_value < minimum or cast_value > maximum:
                raise ValueError(f"{public_name} must be between {minimum} and {maximum}.")
            normalized[key] = str(cast_value)
        conn = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for key, value in normalized.items():
                conn.execute(
                    """INSERT INTO ai_settings (setting_key, setting_value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(setting_key) DO UPDATE SET
                         setting_value = excluded.setting_value, updated_at = excluded.updated_at""",
                    (key, value, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.settings()

    def enforce_admission(self, estimated_input_tokens: int) -> dict[str, Any]:
        settings = self.settings()
        if estimated_input_tokens > settings["maxInputTokens"]:
            raise UsageLimitReached(
                f"This request is too large for the configured {settings['maxInputTokens']:,}-token input limit."
            )
        stats = self.stats()
        model = settings["defaultModel"]
        rates = DEFAULT_RATES.get(model)
        if rates is None:
            raise UsageLimitReached("The configured model does not have an approved cost profile.")
        worst_case_per_call = (
            Decimal(settings["maxInputTokens"]) / TOKENS_PER_MILLION * rates.input_per_million
            + Decimal(settings["maxOutputTokens"]) / TOKENS_PER_MILLION * rates.output_per_million
        )
        worst_case_request = float(worst_case_per_call * 4)
        if stats["actualCostUsd"] + worst_case_request > settings["monthlyLimitUsd"]:
            raise UsageLimitReached("The monthly AI usage limit has been reached.")
        if stats["todayRequestCount"] >= settings["dailyRequestLimit"]:
            raise UsageLimitReached("The daily AI request limit has been reached.")
        return settings

    def stats(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        month_prefix = now.strftime("%Y-%m")
        day_prefix = now.strftime("%Y-%m-%d")
        conn = self._connect()
        totals = conn.execute(
            """SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(estimated_cost_microusd), 0) AS cost,
                      COUNT(DISTINCT CASE WHEN root_request_id != '' THEN root_request_id ELSE request_id END) AS requests,
                      COALESCE(SUM(tool_calls), 0) AS tool_calls,
                      COALESCE(AVG(NULLIF(duration_ms, 0)), 0) AS average_ms
               FROM ai_usage_events WHERE substr(created_at, 1, 7) = ?""",
            (month_prefix,),
        ).fetchone()
        today_count = conn.execute(
            """SELECT COUNT(DISTINCT CASE WHEN root_request_id != '' THEN root_request_id ELSE request_id END)
               FROM ai_usage_events WHERE substr(created_at, 1, 10) = ?""",
            (day_prefix,),
        ).fetchone()[0]
        by_model = conn.execute(
            "SELECT model, COUNT(*) AS calls FROM ai_usage_events WHERE substr(created_at, 1, 7) = ? GROUP BY model ORDER BY calls DESC",
            (month_prefix,),
        ).fetchall()
        by_status = conn.execute(
            "SELECT status, COUNT(*) AS calls FROM ai_usage_events WHERE substr(created_at, 1, 7) = ? GROUP BY status ORDER BY calls DESC",
            (month_prefix,),
        ).fetchall()
        errors = conn.execute(
            """SELECT created_at, model, error_code
               FROM ai_usage_events WHERE status = 'error'
               ORDER BY created_at DESC LIMIT 10"""
        ).fetchall()
        recent_tools = conn.execute(
            """SELECT created_at, tool_name, status, result_count
               FROM ai_tool_audit ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()
        recent_writes = conn.execute(
            """SELECT created_at, tool_name, event, status
               FROM ai_write_audit ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()
        conn.close()
        actual = int(totals["cost"] or 0) / 1_000_000
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        projected = actual / max(now.day, 1) * days_in_month
        return {
            "month": month_prefix,
            "actualCostUsd": round(actual, 6),
            "projectedMonthEndCostUsd": round(projected, 6),
            "inputTokens": int(totals["input_tokens"] or 0),
            "cachedInputTokens": int(totals["cached_input_tokens"] or 0),
            "outputTokens": int(totals["output_tokens"] or 0),
            "requestCount": int(totals["requests"] or 0),
            "todayRequestCount": int(today_count or 0),
            "toolCallCount": int(totals["tool_calls"] or 0),
            "averageResponseTimeMs": round(float(totals["average_ms"] or 0), 1),
            "callsByModel": [{"model": row["model"], "calls": int(row["calls"])} for row in by_model],
            "callsByStatus": [{"status": row["status"], "calls": int(row["calls"])} for row in by_status],
            "recentErrors": [
                {"createdAt": row["created_at"], "model": row["model"], "errorCode": row["error_code"]}
                for row in errors
            ],
            "recentToolCalls": [
                {
                    "createdAt": row["created_at"],
                    "tool": row["tool_name"],
                    "status": row["status"],
                    "resultCount": int(row["result_count"] or 0),
                }
                for row in recent_tools
            ],
            "recentWrites": [
                {
                    "createdAt": row["created_at"],
                    "tool": row["tool_name"],
                    "event": row["event"],
                    "status": row["status"],
                }
                for row in recent_writes
            ],
            "settings": self.settings(),
        }


def usage_json(record: UsageRecord) -> str:
    return json.dumps(record.raw, separators=(",", ":"), sort_keys=True)
