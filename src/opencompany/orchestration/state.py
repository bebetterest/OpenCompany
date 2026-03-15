from __future__ import annotations

from pathlib import Path
from typing import Any

from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    RunSession,
    SessionStatus,
    normalize_workspace_mode,
)
from opencompany.status_machine import normalize_agent_status, normalize_session_completion_state


def session_state(session: RunSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "project_dir": str(session.project_dir),
        "task": session.task,
        "locale": session.locale,
        "root_agent_id": session.root_agent_id,
        "workspace_mode": session.workspace_mode.value,
        "status": session.status.value,
        "status_reason": session.status_reason,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "loop_index": session.loop_index,
        "final_summary": session.final_summary,
        "completion_state": normalize_session_completion_state(
            session_status=session.status,
            completion_state=session.completion_state,
        ),
        "follow_up_needed": session.follow_up_needed,
        "config_snapshot": session.config_snapshot,
    }


def agent_state(agent: AgentNode) -> dict[str, Any]:
    return {
        "id": agent.id,
        "session_id": agent.session_id,
        "name": agent.name,
        "role": agent.role.value,
        "instruction": agent.instruction,
        "workspace_id": agent.workspace_id,
        "parent_agent_id": agent.parent_agent_id,
        "status": agent.status.value,
        "status_reason": agent.status_reason,
        "children": list(agent.children),
        "summary": agent.summary,
        "next_recommendation": agent.next_recommendation,
        "diff_artifact": agent.diff_artifact,
        "completion_status": agent.completion_status,
        "step_count": agent.step_count,
        "metadata": agent.metadata,
        "conversation": list(agent.conversation),
    }


def session_from_state(payload: dict[str, Any]) -> RunSession:
    status = SessionStatus(payload["status"])
    return RunSession(
        id=payload["id"],
        project_dir=Path(payload["project_dir"]),
        task=payload["task"],
        locale=payload["locale"],
        root_agent_id=payload["root_agent_id"],
        workspace_mode=normalize_workspace_mode(payload.get("workspace_mode")),
        status=status,
        status_reason=payload.get("status_reason"),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
        loop_index=int(payload["loop_index"]),
        final_summary=payload.get("final_summary"),
        completion_state=normalize_session_completion_state(
            session_status=status,
            completion_state=payload.get("completion_state"),
        ),
        follow_up_needed=bool(payload.get("follow_up_needed", False)),
        config_snapshot=payload.get("config_snapshot", {}),
    )


def agent_from_state(payload: dict[str, Any]) -> AgentNode:
    return AgentNode(
        id=payload["id"],
        session_id=payload["session_id"],
        name=payload["name"],
        role=AgentRole(payload["role"]),
        instruction=payload["instruction"],
        workspace_id=payload["workspace_id"],
        parent_agent_id=payload.get("parent_agent_id"),
        status=normalize_agent_status(payload.get("status")),
        status_reason=payload.get("status_reason"),
        children=list(payload.get("children", [])),
        summary=payload.get("summary"),
        next_recommendation=payload.get("next_recommendation"),
        diff_artifact=payload.get("diff_artifact"),
        completion_status=payload.get("completion_status"),
        step_count=int(payload.get("step_count", 0)),
        metadata=payload.get("metadata", {}),
        conversation=list(payload.get("conversation", [])),
    )
