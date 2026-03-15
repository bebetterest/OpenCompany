from __future__ import annotations

from opencompany.models import AgentStatus, SessionStatus

SESSION_COMPLETION_STATES = frozenset({"completed", "partial"})

AGENT_ACTIVE_STATUSES = frozenset(
    {
        AgentStatus.PENDING,
        AgentStatus.RUNNING,
    }
)

AGENT_TERMINAL_STATUSES = frozenset(
    {
        AgentStatus.COMPLETED,
        AgentStatus.FAILED,
        AgentStatus.CANCELLED,
        AgentStatus.TERMINATED,
    }
)

AGENT_NON_SCHEDULABLE_STATUSES = frozenset(AGENT_TERMINAL_STATUSES | {AgentStatus.PAUSED})

KNOWN_AGENT_STATUSES = tuple(
    status.value
    for status in (
        AgentStatus.PENDING,
        AgentStatus.RUNNING,
        AgentStatus.PAUSED,
        AgentStatus.COMPLETED,
        AgentStatus.FAILED,
        AgentStatus.CANCELLED,
        AgentStatus.TERMINATED,
    )
)
KNOWN_AGENT_STATUS_SET = frozenset(KNOWN_AGENT_STATUSES)

_AGENT_STATUS_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.PENDING: frozenset(
        {
            AgentStatus.RUNNING,
            AgentStatus.PAUSED,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.TERMINATED,
        }
    ),
    AgentStatus.RUNNING: frozenset(
        {
            AgentStatus.PAUSED,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.TERMINATED,
        }
    ),
    AgentStatus.PAUSED: frozenset(
        {
            AgentStatus.RUNNING,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.TERMINATED,
        }
    ),
    AgentStatus.COMPLETED: frozenset(),
    AgentStatus.FAILED: frozenset(),
    AgentStatus.CANCELLED: frozenset(),
    AgentStatus.TERMINATED: frozenset(),
}


def normalize_agent_status(raw_status: str | AgentStatus | None) -> AgentStatus:
    if isinstance(raw_status, AgentStatus):
        return raw_status
    normalized = str(raw_status or "").strip().lower()
    if normalized == AgentStatus.CANCELLED.value:
        return AgentStatus.CANCELLED
    if normalized == "waiting":
        return AgentStatus.RUNNING
    try:
        return AgentStatus(normalized)
    except ValueError:
        return AgentStatus.PENDING


def normalize_session_completion_state(
    *,
    session_status: SessionStatus | str,
    completion_state: str | None,
) -> str | None:
    session_value = (
        session_status.value
        if isinstance(session_status, SessionStatus)
        else str(session_status or "").strip().lower()
    )
    if session_value != SessionStatus.COMPLETED.value:
        return None
    normalized = str(completion_state or "").strip().lower()
    if not normalized:
        return None
    if normalized == "interrupted":
        return "partial"
    if normalized in SESSION_COMPLETION_STATES:
        return normalized
    return "partial"


def can_explicitly_reopen(status: AgentStatus) -> bool:
    return status in AGENT_NON_SCHEDULABLE_STATUSES


def validate_agent_status_transition(
    *,
    previous: AgentStatus,
    new: AgentStatus,
    explicit_reopen: bool = False,
) -> bool:
    if previous == new:
        return True
    if new == AgentStatus.RUNNING and explicit_reopen and can_explicitly_reopen(previous):
        return True
    allowed = _AGENT_STATUS_TRANSITIONS.get(previous, frozenset())
    return new in allowed
