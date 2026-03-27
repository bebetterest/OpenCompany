from __future__ import annotations

import base64
import json
import math
from datetime import UTC, datetime
from typing import Any, Sequence

from opencompany.models import AgentRole, SteerRunStatus, ToolRunStatus
from opencompany.utils import utc_now

TERMINAL_TOOL_RUN_STATUSES = frozenset(
    {
        ToolRunStatus.COMPLETED.value,
        ToolRunStatus.FAILED.value,
        ToolRunStatus.CANCELLED.value,
        ToolRunStatus.ABANDONED.value,
    }
)

PENDING_TOOL_RUN_STATUSES = frozenset(
    {
        ToolRunStatus.QUEUED.value,
        ToolRunStatus.RUNNING.value,
    }
)

KNOWN_TOOL_RUN_STATUSES = (
    ToolRunStatus.QUEUED.value,
    ToolRunStatus.RUNNING.value,
    ToolRunStatus.COMPLETED.value,
    ToolRunStatus.FAILED.value,
    ToolRunStatus.CANCELLED.value,
    ToolRunStatus.ABANDONED.value,
)
KNOWN_TOOL_RUN_STATUS_SET = frozenset(KNOWN_TOOL_RUN_STATUSES)

KNOWN_STEER_RUN_STATUSES = (
    SteerRunStatus.WAITING.value,
    SteerRunStatus.COMPLETED.value,
    SteerRunStatus.CANCELLED.value,
)
KNOWN_STEER_RUN_STATUS_SET = frozenset(KNOWN_STEER_RUN_STATUSES)

_DURATION_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("<100ms", 100),
    ("100ms-500ms", 500),
    ("500ms-1s", 1_000),
    ("1s-5s", 5_000),
    ("5s-15s", 15_000),
    ("15s-60s", 60_000),
    (">=60s", None),
)

WAIT_TIME_MIN_SECONDS = 10.0
WAIT_TIME_MAX_SECONDS = 60.0


def normalize_tool_run_limit(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_tool_run_statuses(status: Any) -> list[str] | None:
    if isinstance(status, str):
        normalized = status.strip().lower()
        return [normalized] if normalized else None
    if isinstance(status, (list, tuple, set)):
        normalized = [
            str(item).strip().lower() for item in status if str(item).strip()
        ]
        return normalized or None
    return None


def parse_tool_run_status_filters(status: Any) -> tuple[list[str] | None, list[str]]:
    normalized = normalize_tool_run_statuses(status)
    if not normalized:
        return None, []
    valid: list[str] = []
    invalid: list[str] = []
    for item in normalized:
        if item in KNOWN_TOOL_RUN_STATUS_SET:
            valid.append(item)
        else:
            invalid.append(item)
    return (valid or None), invalid


def normalize_steer_run_statuses(status: Any) -> list[str] | None:
    if isinstance(status, str):
        normalized = status.strip().lower()
        return [normalized] if normalized else None
    if isinstance(status, (list, tuple, set)):
        normalized = [
            str(item).strip().lower() for item in status if str(item).strip()
        ]
        return normalized or None
    return None


def parse_steer_run_status_filters(status: Any) -> tuple[list[str] | None, list[str]]:
    normalized = normalize_steer_run_statuses(status)
    if not normalized:
        return None, []
    valid: list[str] = []
    invalid: list[str] = []
    for item in normalized:
        if item in KNOWN_STEER_RUN_STATUS_SET:
            valid.append(item)
        else:
            invalid.append(item)
    return (valid or None), invalid


def encode_tool_run_cursor(created_at: str, tool_run_id: str) -> str:
    payload = json.dumps(
        {"created_at": created_at, "id": tool_run_id},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_tool_run_cursor(cursor: str | None) -> tuple[str, str] | None:
    if cursor is None:
        return None
    normalized = str(cursor).strip()
    if not normalized:
        return None
    try:
        payload = base64.urlsafe_b64decode(normalized.encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    created_at = str(data.get("created_at", "")).strip()
    tool_run_id = str(data.get("id", "")).strip()
    if not created_at or not tool_run_id:
        return None
    return created_at, tool_run_id


def next_tool_run_cursor(
    runs: Sequence[dict[str, Any]],
    *,
    limit: int,
) -> str | None:
    if len(runs) < limit:
        return None
    tail = runs[-1] if runs else {}
    created_at = str(tail.get("created_at", "")).strip()
    tool_run_id = str(tail.get("id", "")).strip()
    if not created_at or not tool_run_id:
        return None
    return encode_tool_run_cursor(created_at, tool_run_id)


def encode_steer_run_cursor(created_at: str, steer_run_id: str) -> str:
    return encode_tool_run_cursor(created_at, steer_run_id)


def decode_steer_run_cursor(cursor: str | None) -> tuple[str, str] | None:
    return decode_tool_run_cursor(cursor)


def next_steer_run_cursor(
    steer_runs: Sequence[dict[str, Any]],
    *,
    limit: int,
) -> str | None:
    return next_tool_run_cursor(steer_runs, limit=limit)


def encode_offset_cursor(offset: int) -> str:
    normalized = max(0, int(offset))
    payload = json.dumps(
        {"offset": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_offset_cursor(cursor: str | None, *, default: int = 0) -> int | None:
    if cursor is None:
        return max(0, int(default))
    normalized = str(cursor).strip()
    if not normalized:
        return max(0, int(default))
    try:
        payload = base64.urlsafe_b64decode(normalized.encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        offset = int(data.get("offset", default))
    except (TypeError, ValueError):
        return None
    if offset < 0:
        return None
    return offset


def validate_finish_action(role: AgentRole, action: dict[str, Any]) -> str | None:
    common_keys = {
        "type",
        "_tool_call_id",
        "status",
        "summary",
    }
    worker_only_keys = {"next_recommendation"}
    allowed_keys = (
        common_keys if role == AgentRole.ROOT else common_keys | worker_only_keys
    )
    unknown_keys = sorted(key for key in action.keys() if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(f"'{key}'" for key in unknown_keys)
        return f"finish received unsupported field(s): {joined}."

    status = str(action.get("status", "")).strip().lower()
    if not status:
        return "finish requires a non-empty 'status'."
    if role == AgentRole.ROOT:
        allowed_status = {"completed", "partial"}
    else:
        allowed_status = {"completed", "partial", "failed"}
    if status not in allowed_status:
        joined_status = ", ".join(sorted(allowed_status))
        return (
            f"finish status '{status}' is invalid for role '{role.value}'. "
            f"Allowed: {joined_status}."
        )

    summary = str(action.get("summary", "")).strip()
    if not summary:
        return "finish requires a non-empty 'summary'."

    next_recommendation = str(action.get("next_recommendation", "")).strip()
    if role == AgentRole.ROOT and next_recommendation:
        return "root finish must not include 'next_recommendation'."
    if role == AgentRole.WORKER and status in {"partial", "failed"} and not next_recommendation:
        return (
            "worker finish with status 'partial' or 'failed' requires a non-empty "
            "'next_recommendation'."
        )
    return None


def validate_wait_time_action(action: dict[str, Any]) -> str | None:
    allowed_keys = {"type", "_tool_call_id", "seconds"}
    unknown_keys = sorted(key for key in action.keys() if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(f"'{key}'" for key in unknown_keys)
        return f"wait_time received unsupported field(s): {joined}."
    raw_seconds = action.get("seconds")
    if raw_seconds is None:
        return "wait_time requires 'seconds'."
    try:
        seconds = float(raw_seconds)
    except (TypeError, ValueError):
        return "wait_time field 'seconds' must be a number."
    if not math.isfinite(seconds):
        return "wait_time field 'seconds' must be finite."
    if seconds < WAIT_TIME_MIN_SECONDS:
        return f"wait_time field 'seconds' must be >= {WAIT_TIME_MIN_SECONDS:g}."
    if seconds > WAIT_TIME_MAX_SECONDS:
        return f"wait_time field 'seconds' must be <= {WAIT_TIME_MAX_SECONDS:g}."
    return None


def validate_wait_run_action(action: dict[str, Any]) -> str | None:
    allowed_keys = {"type", "_tool_call_id", "tool_run_id", "agent_id"}
    unknown_keys = sorted(key for key in action.keys() if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(f"'{key}'" for key in unknown_keys)
        return f"wait_run received unsupported field(s): {joined}."
    has_tool_run_id = bool(str(action.get("tool_run_id", "")).strip())
    has_agent_id = bool(str(action.get("agent_id", "")).strip())
    if has_tool_run_id == has_agent_id:
        return "wait_run requires exactly one of 'tool_run_id' or 'agent_id'."
    return None


def validate_compress_context_action(action: dict[str, Any]) -> str | None:
    allowed_keys = {"type", "_tool_call_id"}
    unknown_keys = sorted(key for key in action.keys() if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(f"'{key}'" for key in unknown_keys)
        return f"compress_context received unsupported field(s): {joined}."
    return None


def tool_run_duration_ms(
    run: dict[str, Any],
    *,
    now_timestamp: str | None = None,
) -> int | None:
    status = str(run.get("status", "")).strip().lower()
    created_at = _parse_iso8601(run.get("created_at"))
    started_at = _parse_iso8601(run.get("started_at")) or created_at
    if started_at is None:
        return None

    completed_at = _parse_iso8601(run.get("completed_at"))
    if completed_at is None:
        if status in TERMINAL_TOOL_RUN_STATUSES:
            return None
        completed_at = _parse_iso8601(now_timestamp) or datetime.now(UTC)

    duration_ms = int((completed_at - started_at).total_seconds() * 1000)
    if duration_ms < 0:
        return None
    return duration_ms


def tool_run_metrics(
    tool_runs: Sequence[dict[str, Any]],
    *,
    session_id: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = generated_at or utc_now()
    status_counts = _empty_status_counts()
    durations_ms: list[int] = []
    by_tool: dict[str, dict[str, Any]] = {}
    by_agent: dict[str, dict[str, Any]] = {}
    terminal_runs = 0
    failed_runs = 0
    cancelled_runs = 0
    failed_or_cancelled_runs = 0

    for run in tool_runs:
        status = _normalized_status(run.get("status"))
        status_counts[status] = int(status_counts.get(status, 0)) + 1
        duration_ms = tool_run_duration_ms(run, now_timestamp=timestamp)
        if duration_ms is not None:
            durations_ms.append(duration_ms)
        if status in TERMINAL_TOOL_RUN_STATUSES:
            terminal_runs += 1
            if status == ToolRunStatus.FAILED.value:
                failed_runs += 1
                failed_or_cancelled_runs += 1
            elif status in {ToolRunStatus.CANCELLED.value, ToolRunStatus.ABANDONED.value}:
                cancelled_runs += 1
                failed_or_cancelled_runs += 1

        tool_name = str(run.get("tool_name", "")).strip() or "-"
        agent_id = str(run.get("agent_id", "")).strip() or "-"
        _update_group_metrics(
            by_tool,
            key=tool_name,
            status=status,
            duration_ms=duration_ms,
        )
        _update_group_metrics(
            by_agent,
            key=agent_id,
            status=status,
            duration_ms=duration_ms,
        )

    total_runs = len(tool_runs)
    return {
        "session_id": session_id,
        "generated_at": timestamp,
        "total_runs": total_runs,
        "terminal_runs": terminal_runs,
        "failed_runs": failed_runs,
        "cancelled_runs": cancelled_runs,
        "status_counts": status_counts,
        "failure_rate": _safe_ratio(failed_runs, terminal_runs),
        "failure_or_cancel_rate": _safe_ratio(failed_or_cancelled_runs, terminal_runs),
        "duration_ms": _duration_summary(durations_ms),
        "by_tool": _finalize_group_metrics(by_tool, key_name="tool_name"),
        "by_agent": _finalize_group_metrics(by_agent, key_name="agent_id"),
    }


def steer_run_metrics(
    steer_runs: Sequence[dict[str, Any]],
    *,
    session_id: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = generated_at or utc_now()
    status_counts = _empty_steer_status_counts()
    by_agent: dict[str, dict[str, Any]] = {}

    for run in steer_runs:
        status = _normalized_steer_status(run.get("status"))
        status_counts[status] = int(status_counts.get(status, 0)) + 1
        agent_id = str(run.get("agent_id", "")).strip() or "-"
        entry = by_agent.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "total_runs": 0,
                "status_counts": _empty_steer_status_counts(),
            },
        )
        entry["total_runs"] = int(entry.get("total_runs", 0)) + 1
        entry_status_counts = entry.get("status_counts")
        if isinstance(entry_status_counts, dict):
            entry_status_counts[status] = int(entry_status_counts.get(status, 0)) + 1

    total_runs = len(steer_runs)
    return {
        "session_id": session_id,
        "generated_at": timestamp,
        "total_runs": total_runs,
        "waiting_runs": int(status_counts.get(SteerRunStatus.WAITING.value, 0)),
        "completed_runs": int(status_counts.get(SteerRunStatus.COMPLETED.value, 0)),
        "cancelled_runs": int(status_counts.get(SteerRunStatus.CANCELLED.value, 0)),
        "status_counts": status_counts,
        "by_agent": sorted(by_agent.values(), key=lambda item: str(item.get("agent_id", ""))),
    }


def _parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _normalized_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text or "unknown"


def _empty_status_counts() -> dict[str, int]:
    return {status: 0 for status in KNOWN_TOOL_RUN_STATUSES}


def _empty_steer_status_counts() -> dict[str, int]:
    return {status: 0 for status in KNOWN_STEER_RUN_STATUSES}


def _normalized_steer_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in KNOWN_STEER_RUN_STATUS_SET:
        return text
    return SteerRunStatus.WAITING.value


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _duration_summary(durations_ms: list[int]) -> dict[str, Any]:
    if not durations_ms:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "histogram": _duration_histogram([]),
        }

    ordered = sorted(durations_ms)
    mean = round(sum(ordered) / len(ordered), 3)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "mean": mean,
        "p50": _quantile_ms(ordered, 0.50),
        "p90": _quantile_ms(ordered, 0.90),
        "p95": _quantile_ms(ordered, 0.95),
        "p99": _quantile_ms(ordered, 0.99),
        "histogram": _duration_histogram(ordered),
    }


def _quantile_ms(sorted_durations_ms: list[int], quantile: float) -> int | None:
    if not sorted_durations_ms:
        return None
    bounded = max(0.0, min(1.0, quantile))
    rank = max(1, math.ceil(bounded * len(sorted_durations_ms)))
    index = min(len(sorted_durations_ms) - 1, rank - 1)
    return sorted_durations_ms[index]


def _duration_histogram(sorted_durations_ms: list[int]) -> list[dict[str, Any]]:
    counts = [0 for _ in _DURATION_BUCKETS]
    for duration in sorted_durations_ms:
        for index, (_label, max_ms) in enumerate(_DURATION_BUCKETS):
            if max_ms is None or duration < max_ms:
                counts[index] += 1
                break
    histogram: list[dict[str, Any]] = []
    for index, (label, max_ms) in enumerate(_DURATION_BUCKETS):
        histogram.append(
            {
                "bucket": label,
                "upper_bound_ms": max_ms,
                "count": counts[index],
            }
        )
    return histogram


def _update_group_metrics(
    grouped: dict[str, dict[str, Any]],
    *,
    key: str,
    status: str,
    duration_ms: int | None,
) -> None:
    row = grouped.get(key)
    if row is None:
        row = {
            "total_runs": 0,
            "terminal_runs": 0,
            "failed_runs": 0,
            "cancelled_runs": 0,
            "status_counts": _empty_status_counts(),
            "durations_ms": [],
        }
        grouped[key] = row
    row["total_runs"] = int(row["total_runs"]) + 1
    status_counts = row["status_counts"]
    status_counts[status] = int(status_counts.get(status, 0)) + 1
    if status in TERMINAL_TOOL_RUN_STATUSES:
        row["terminal_runs"] = int(row["terminal_runs"]) + 1
        if status == ToolRunStatus.FAILED.value:
            row["failed_runs"] = int(row["failed_runs"]) + 1
        elif status in {ToolRunStatus.CANCELLED.value, ToolRunStatus.ABANDONED.value}:
            row["cancelled_runs"] = int(row["cancelled_runs"]) + 1
    if duration_ms is not None:
        row["durations_ms"].append(duration_ms)


def _finalize_group_metrics(
    grouped: dict[str, dict[str, Any]],
    *,
    key_name: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(
        grouped.keys(),
        key=lambda item: (-int(grouped[item]["total_runs"]), item),
    ):
        row = grouped[key]
        terminal_runs = int(row["terminal_runs"])
        failed_runs = int(row["failed_runs"])
        cancelled_runs = int(row["cancelled_runs"])
        rows.append(
            {
                key_name: key,
                "total_runs": int(row["total_runs"]),
                "terminal_runs": terminal_runs,
                "failed_runs": failed_runs,
                "cancelled_runs": cancelled_runs,
                "status_counts": row["status_counts"],
                "failure_rate": _safe_ratio(failed_runs, terminal_runs),
                "failure_or_cancel_rate": _safe_ratio(
                    failed_runs + cancelled_runs,
                    terminal_runs,
                ),
                "duration_ms": _duration_summary(list(row["durations_ms"])),
            }
        )
    return rows
