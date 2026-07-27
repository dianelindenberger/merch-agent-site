"""Restricted, validated tool boundary for the Merch Agent AI assistant.

The model never receives a database connection or SQL capability. It can only
request one of the fixed operations below. Each operation validates arguments,
calls a host-owned data provider, and returns a compact JSON-safe result.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping


class ToolValidationError(ValueError):
    """Raised when a model requests an invalid or unauthorized tool action."""


READ_ONLY_TOOL_NAMES = frozenset(
    {
        "query_sales",
        "query_designs",
        "query_campaigns",
        "query_ad_groups",
        "query_targets",
        "query_search_terms",
        "query_placements",
        "query_recommendations",
        "compare_periods",
    }
)

LOCAL_WRITE_TOOL_NAMES = frozenset(
    {
        "record_user_action",
        "defer_recommendation",
        "dismiss_recommendation",
        "add_user_note",
    }
)

# This denylist is deliberately broader than the exposed tool names. A model
# cannot create a new tool by naming an Amazon operation in a function call.
AMAZON_MUTATION_TERMS = (
    "amazon",
    "bid",
    "campaign_setting",
    "pause_campaign",
    "enable_campaign",
    "update_target",
    "create_campaign",
    "delete_campaign",
)

ALLOWED_PERIODS = frozenset({"today", "yesterday", "last7", "last14", "last30", "last60"})
MAX_TOOL_ROWS = 50
MAX_TEXT_LENGTH = 240


def _text(value: Any, field: str, *, required: bool = False, maximum: int = MAX_TEXT_LENGTH) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ToolValidationError(f"{field} is required.")
    if len(result) > maximum:
        raise ToolValidationError(f"{field} is too long.")
    return result


def _period(value: Any, *, default: str = "last7") -> str:
    result = _text(value or default, "period", maximum=20).lower()
    if result not in ALLOWED_PERIODS:
        raise ToolValidationError(f"period must be one of: {', '.join(sorted(ALLOWED_PERIODS))}.")
    return result


def _limit(value: Any, *, default: int = 20) -> int:
    try:
        result = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ToolValidationError("limit must be an integer.") from exc
    if result < 1 or result > MAX_TOOL_ROWS:
        raise ToolValidationError(f"limit must be between 1 and {MAX_TOOL_ROWS}.")
    return result


def _reject_unknown(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ToolValidationError(f"Unknown argument(s): {', '.join(unknown)}.")


def _compact(value: Any, *, row_limit: int = MAX_TOOL_ROWS, depth: int = 0) -> Any:
    """Bound model context without returning raw exports or unbounded structures."""
    if depth > 5:
        return "[nested data omitted]"
    if isinstance(value, Mapping):
        return {str(key): _compact(item, row_limit=row_limit, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(item, row_limit=row_limit, depth=depth + 1) for item in value[:row_limit]]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


@dataclass(frozen=True)
class ToolResult:
    name: str
    arguments: dict[str, Any]
    data: dict[str, Any]

    def model_json(self) -> str:
        return json.dumps({"tool": self.name, "data": self.data}, separators=(",", ":"), ensure_ascii=False)


class RestrictedAIToolLayer:
    """Validated dispatcher over fixed host-provided data functions."""

    def __init__(
        self,
        providers: Mapping[str, Callable[..., Mapping[str, Any]]],
        *,
        allow_local_writes: bool = False,
    ):
        self._providers = dict(providers)
        self.allow_local_writes = bool(allow_local_writes)

    @property
    def exposed_names(self) -> frozenset[str]:
        if self.allow_local_writes:
            return READ_ONLY_TOOL_NAMES | LOCAL_WRITE_TOOL_NAMES
        return READ_ONLY_TOOL_NAMES

    def schemas(self) -> list[dict[str, Any]]:
        schemas = [
            _schema("query_sales", "Get aggregate Merch sales and royalties for one bounded period and optional marketplace.", {
                "period": _period_property(),
                "market": {"type": "string", "maxLength": 40},
                "limit": _limit_property(),
            }),
            _schema("query_designs", "Get ranked design sales by title or ASIN, with units and royalty currency breakdown.", {
                "period": _period_property(),
                "market": {"type": "string", "maxLength": 40},
                "search": {"type": "string", "maxLength": 160},
                "limit": _limit_property(),
            }),
            _schema("query_campaigns", "Get aggregate Amazon Ads campaign metrics from imported reports.", {
                "period": _period_property(),
                "search": {"type": "string", "maxLength": 160},
                "limit": _limit_property(),
            }),
            _schema("query_ad_groups", "Get ad groups belonging to one named campaign and their aggregate metrics.", {
                "campaign": {"type": "string", "maxLength": 240},
                "period": _period_property(),
                "limit": _limit_property(),
            }, required=["campaign"]),
            _schema("query_targets", "Get bounded target metrics for one campaign.", {
                "campaign": {"type": "string", "maxLength": 240},
                "period": _period_property(),
                "limit": _limit_property(),
            }, required=["campaign"]),
            _schema("query_search_terms", "Get bounded imported search-term performance, optionally filtered by campaign or text.", {
                "campaign": {"type": "string", "maxLength": 240},
                "search": {"type": "string", "maxLength": 160},
                "period": _period_property(),
                "limit": _limit_property(),
            }),
            _schema("query_placements", "Get bounded placement performance, optionally filtered by campaign.", {
                "campaign": {"type": "string", "maxLength": 240},
                "period": _period_property(),
                "limit": _limit_property(),
            }),
            _schema("query_recommendations", "Get current recommendations and their evidence without generating new recommendations.", {
                "period": _period_property(),
                "status": {"type": "string", "enum": ["Proposed", "Completed", "Deferred", "Ignored", "Action logged", "Monitoring", "Dismissed", "Superseded", "Resolved", "Expired", "all"]},
                "limit": _limit_property(),
            }),
            _schema("compare_periods", "Compare the same bounded metric set across two supported periods.", {
                "entity": {"type": "string", "enum": ["sales", "designs", "campaigns"]},
                "period_a": _period_property(),
                "period_b": _period_property(),
                "search": {"type": "string", "maxLength": 160},
                "market": {"type": "string", "maxLength": 40},
                "limit": _limit_property(),
            }, required=["entity", "period_a", "period_b"]),
        ]
        if self.allow_local_writes:
            schemas.extend(_local_write_schemas())
        return schemas

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        name = _text(name, "tool name", required=True, maximum=80)
        args = dict(arguments or {})
        lowered = name.lower()
        if any(term in lowered for term in AMAZON_MUTATION_TERMS):
            raise ToolValidationError("Amazon Ads mutation tools are not permitted.")
        if name not in self.exposed_names:
            raise ToolValidationError(f"Tool is not permitted: {name}.")
        provider = self._providers.get(name)
        if provider is None:
            raise ToolValidationError(f"Tool is not configured: {name}.")
        validated = self._validate(name, args)
        request_context = dict(context or {})
        # A daily audit is always about the last completed reporting day.
        # Models may interpret the phrase "today's audit" as a request for a
        # partial current-day snapshot, so enforce the completed-day boundary
        # at the restricted tool layer instead of relying on prompt wording.
        if request_context.get("completed_day_only"):
            if validated.get("period") == "today":
                validated["period"] = "yesterday"
            if validated.get("period_a") == "today":
                validated["period_a"] = "yesterday"
            if validated.get("period_b") == "today":
                validated["period_b"] = "yesterday"
        if name in LOCAL_WRITE_TOOL_NAMES:
            result = provider(_context=request_context, **validated)
        else:
            result = provider(**validated)
        if not isinstance(result, Mapping):
            raise RuntimeError(f"{name} returned an invalid result.")
        return ToolResult(name=name, arguments=validated, data=_compact(dict(result), row_limit=validated.get("limit", MAX_TOOL_ROWS)))

    def _validate(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in {"query_sales", "query_designs"}:
            allowed = {"period", "market", "limit"}
            if name == "query_designs":
                allowed.add("search")
            _reject_unknown(args, allowed)
            result = {
                "period": _period(args.get("period")),
                "market": _text(args.get("market"), "market", maximum=40),
                "limit": _limit(args.get("limit")),
            }
            if name == "query_designs":
                result["search"] = _text(args.get("search"), "search", maximum=160)
            return result
        if name == "query_campaigns":
            _reject_unknown(args, {"period", "search", "limit"})
            return {
                "period": _period(args.get("period")),
                "search": _text(args.get("search"), "search", maximum=160),
                "limit": _limit(args.get("limit")),
            }
        if name in {"query_ad_groups", "query_targets"}:
            _reject_unknown(args, {"campaign", "period", "limit"})
            return {
                "campaign": _text(args.get("campaign"), "campaign", required=True),
                "period": _period(args.get("period")),
                "limit": _limit(args.get("limit")),
            }
        if name in {"query_search_terms", "query_placements"}:
            allowed = {"campaign", "period", "limit"}
            if name == "query_search_terms":
                allowed.add("search")
            _reject_unknown(args, allowed)
            result = {
                "campaign": _text(args.get("campaign"), "campaign"),
                "period": _period(args.get("period")),
                "limit": _limit(args.get("limit")),
            }
            if name == "query_search_terms":
                result["search"] = _text(args.get("search"), "search", maximum=160)
            return result
        if name == "query_recommendations":
            _reject_unknown(args, {"period", "status", "limit"})
            status = _text(args.get("status") or "all", "status", maximum=20)
            if status not in {"Proposed", "Completed", "Deferred", "Ignored", "Action logged", "Monitoring", "Dismissed", "Superseded", "Resolved", "Expired", "all"}:
                raise ToolValidationError("Invalid recommendation status.")
            return {"period": _period(args.get("period")), "status": status, "limit": _limit(args.get("limit"))}
        if name == "compare_periods":
            _reject_unknown(args, {"entity", "period_a", "period_b", "search", "market", "limit"})
            entity = _text(args.get("entity"), "entity", required=True, maximum=20)
            if entity not in {"sales", "designs", "campaigns"}:
                raise ToolValidationError("entity must be sales, designs, or campaigns.")
            return {
                "entity": entity,
                "period_a": _period(args.get("period_a")),
                "period_b": _period(args.get("period_b")),
                "search": _text(args.get("search"), "search", maximum=160),
                "market": _text(args.get("market"), "market", maximum=40),
                "limit": _limit(args.get("limit")),
            }
        if name in LOCAL_WRITE_TOOL_NAMES:
            return self._validate_local_write(name, args)
        raise ToolValidationError(f"Tool is not permitted: {name}.")

    def _validate_local_write(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_local_writes:
            raise ToolValidationError("Local write tools are disabled.")
        if name == "record_user_action":
            _reject_unknown(args, {"recommendation_id", "action", "note", "previous_value", "new_value", "entity_type", "entity_name"})
            action = _text(args.get("action"), "action", required=True, maximum=40)
            if action not in {"completed", "explain_why"}:
                raise ToolValidationError("Invalid user action.")
            return {
                "recommendation_id": _text(args.get("recommendation_id"), "recommendation_id", required=True),
                "action": action,
                "note": _text(args.get("note"), "note", maximum=2000),
                "previous_value": _text(args.get("previous_value"), "previous_value", maximum=120),
                "new_value": _text(args.get("new_value"), "new_value", maximum=120),
                "entity_type": _text(args.get("entity_type"), "entity_type", maximum=40),
                "entity_name": _text(args.get("entity_name"), "entity_name", maximum=240),
            }
        if name == "defer_recommendation":
            _reject_unknown(args, {"recommendation_id", "duration"})
            duration = _text(args.get("duration"), "duration", required=True, maximum=20)
            if duration not in {"tomorrow", "3_days", "1_week"}:
                raise ToolValidationError("Invalid defer duration.")
            return {"recommendation_id": _text(args.get("recommendation_id"), "recommendation_id", required=True), "duration": duration}
        if name == "dismiss_recommendation":
            _reject_unknown(args, {"recommendation_id", "reason"})
            return {
                "recommendation_id": _text(args.get("recommendation_id"), "recommendation_id", required=True),
                "reason": _text(args.get("reason"), "reason", required=True, maximum=2000),
            }
        if name == "add_user_note":
            _reject_unknown(args, {"recommendation_id", "note"})
            return {
                "recommendation_id": _text(args.get("recommendation_id"), "recommendation_id", required=True),
                "note": _text(args.get("note"), "note", required=True, maximum=2000),
            }
        raise ToolValidationError(f"Tool is not permitted: {name}.")


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            # Strict function schemas require every property to be present. The
            # dispatcher still applies defaults for defensive direct calls.
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def _period_property() -> dict[str, Any]:
    return {"type": "string", "enum": sorted(ALLOWED_PERIODS)}


def _limit_property() -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_ROWS}


def _local_write_schemas() -> list[dict[str, Any]]:
    return [
        _schema("record_user_action", "Record a user-confirmed recommendation action locally; never mutate Amazon Ads.", {
            "recommendation_id": {"type": "string", "maxLength": 240},
            "action": {"type": "string", "enum": ["completed", "explain_why"]},
            "note": {"type": "string", "maxLength": 2000},
            "previous_value": {"type": "string", "maxLength": 120},
            "new_value": {"type": "string", "maxLength": 120},
            "entity_type": {"type": "string", "maxLength": 40},
            "entity_name": {"type": "string", "maxLength": 240},
        }, required=["recommendation_id", "action"]),
        _schema("defer_recommendation", "Defer a recommendation locally.", {
            "recommendation_id": {"type": "string", "maxLength": 240},
            "duration": {"type": "string", "enum": ["tomorrow", "3_days", "1_week"]},
        }, required=["recommendation_id", "duration"]),
        _schema("dismiss_recommendation", "Dismiss a recommendation locally with the user's reason.", {
            "recommendation_id": {"type": "string", "maxLength": 240},
            "reason": {"type": "string", "maxLength": 2000},
        }, required=["recommendation_id", "reason"]),
        _schema("add_user_note", "Attach a user note to a recommendation locally.", {
            "recommendation_id": {"type": "string", "maxLength": 240},
            "note": {"type": "string", "maxLength": 2000},
        }, required=["recommendation_id", "note"]),
    ]
