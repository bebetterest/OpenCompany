from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    EventRecord,
    RunSession,
    SessionStatus,
    SteerRun,
    SteerRunStatus,
    ToolRun,
    ToolRunStatus,
)
from opencompany.orchestrator import Orchestrator
from opencompany.webui.server import create_webui_app


class WebUIApiTests(unittest.TestCase):
    def test_bootstrap_locale_uses_config_default_locale(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[project]
default_locale = "zh"
""".strip(),
                encoding="utf-8",
            )
            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                bootstrap = client.get("/api/bootstrap")
                self.assertEqual(bootstrap.status_code, 200)
                self.assertEqual(bootstrap.json()["locale"], "zh")

    def test_bootstrap_runtime_keep_pinned_messages_uses_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[runtime.context]
max_context_tokens = 8192
compression_model = "openai/gpt-4.1-mini"
keep_pinned_messages = 3
""".strip(),
                encoding="utf-8",
            )
            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                bootstrap = client.get("/api/bootstrap")
                self.assertEqual(bootstrap.status_code, 200)
                self.assertEqual(
                    bootstrap.json()["runtime"]["keep_pinned_messages"],
                    3,
                )

    def test_bootstrap_preloads_configured_mcp_catalog_with_oauth_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            project_dir = app_dir / "project"
            project_dir.mkdir()
            (app_dir / "opencompany.toml").write_text(
                """
[mcp.servers.huggingface]
transport = "streamable_http"
enabled = true
url = "https://huggingface.co/mcp?login"
oauth_enabled = true

[mcp.servers.notion]
transport = "streamable_http"
enabled = true
url = "https://mcp.notion.com/mcp"
oauth_enabled = true
""".strip(),
                encoding="utf-8",
            )
            app = create_webui_app(project_dir=project_dir, app_dir=app_dir)
            with TestClient(app) as client:
                bootstrap = client.get("/api/bootstrap")
                self.assertEqual(bootstrap.status_code, 200)
                catalog = {
                    str(item["id"]): item
                    for item in bootstrap.json()["runtime"]["available_mcp_servers"]
                }
                self.assertIn("huggingface", catalog)
                self.assertIn("notion", catalog)
                self.assertTrue(catalog["huggingface"]["oauth_enabled"])
                self.assertTrue(catalog["notion"]["oauth_enabled"])
                self.assertFalse(catalog["huggingface"]["oauth_authorized"])
                self.assertFalse(catalog["notion"]["oauth_authorized"])

    def test_bootstrap_omits_disabled_mcp_servers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            project_dir = app_dir / "project"
            project_dir.mkdir()
            (app_dir / "opencompany.toml").write_text(
                """
[mcp.servers.filesystem]
transport = "stdio"
enabled = true
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

[mcp.servers.docs]
transport = "streamable_http"
enabled = false
url = "https://example.com/mcp"
""".strip(),
                encoding="utf-8",
            )
            app = create_webui_app(project_dir=project_dir, app_dir=app_dir)
            with TestClient(app) as client:
                bootstrap = client.get("/api/bootstrap")
                self.assertEqual(bootstrap.status_code, 200)
                ids = [
                    str(item.get("id", ""))
                    for item in bootstrap.json()["runtime"]["available_mcp_servers"]
                ]
                self.assertIn("filesystem", ids)
                self.assertNotIn("docs", ids)

    def test_index_uses_multiline_task_textarea(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                '[project]\ndefault_locale = "en"\n',
                encoding="utf-8",
            )
            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn('<textarea id="task-input"', response.text)
                self.assertNotIn('<input id="task-input"', response.text)
                self.assertIn('<input id="model-input"', response.text)
                self.assertIn('<input id="root-agent-name-input"', response.text)
                self.assertNotIn('<input id="skills-input"', response.text)
                self.assertIn('id="skills-toggle-button"', response.text)
                self.assertIn('aria-controls="skills-panel-body"', response.text)
                self.assertIn('aria-expanded="false"', response.text)
                self.assertIn('<div id="skills-panel-body" class="skills-control-shell collapsible-body hidden">', response.text)
                self.assertIn('<button id="skills-select-all-button"', response.text)
                self.assertIn('<button id="skills-clear-button"', response.text)
                self.assertIn('<h3 id="skills-overview-title" class="selector-section-title">Overview</h3>', response.text)
                self.assertIn('<div id="skills-overview" class="selector-overview"></div>', response.text)
                self.assertIn('<h3 id="skills-selected-title" class="selector-section-title">Enabled Skills</h3>', response.text)
                self.assertIn('<h3 id="skills-catalog-title" class="selector-section-title">Catalog</h3>', response.text)
                self.assertIn('<h3 id="skills-warnings-title" class="selector-section-title">Skill Warnings</h3>', response.text)
                self.assertIn('<div id="skills-selected"', response.text)
                self.assertIn('<div id="skills-warnings"', response.text)
                self.assertNotIn('id="skills-status"', response.text)
                self.assertNotIn('<input id="mcp-input"', response.text)
                self.assertIn('id="mcp-toggle-button"', response.text)
                self.assertIn('aria-controls="mcp-panel-body"', response.text)
                self.assertIn('<div id="mcp-panel-body" class="skills-control-shell collapsible-body hidden">', response.text)
                self.assertIn('<button id="mcp-discover-button"', response.text)
                self.assertIn('<button id="mcp-use-defaults-button"', response.text)
                self.assertIn('<h3 id="mcp-overview-title" class="selector-section-title">Overview</h3>', response.text)
                self.assertIn('<h3 id="mcp-selected-title" class="selector-section-title">Enabled MCP Servers</h3>', response.text)
                self.assertIn('<h3 id="mcp-catalog-title" class="selector-section-title">Catalog</h3>', response.text)
                self.assertIn('<h3 id="mcp-warnings-title" class="selector-section-title">MCP Warnings</h3>', response.text)
                self.assertIn('<div id="mcp-insight"', response.text)
                self.assertIn('<div id="mcp-selected"', response.text)
                self.assertIn('<div id="mcp-warnings"', response.text)
                self.assertNotIn('id="mcp-status"', response.text)
                self.assertIn('<select id="agents-role-filter">', response.text)
                self.assertIn('<input id="agents-search-input"', response.text)
                self.assertIn('<input id="steer-runs-search-input"', response.text)

    def test_bootstrap_config_and_directory_endpoints(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            project_dir = app_dir / "project"
            project_dir.mkdir()

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                bootstrap = client.get("/api/bootstrap")
                self.assertEqual(bootstrap.status_code, 200)
                payload = bootstrap.json()
                self.assertIn("launch_config", payload)
                self.assertIn("runtime", payload)
                self.assertEqual(payload["launch_config"]["sandbox_backend"], "anthropic")

                directories = client.get("/api/directories")
                self.assertEqual(directories.status_code, 200)
                self.assertIn("entries", directories.json())

                configured = client.post(
                    "/api/launch-config",
                    json={"project_dir": str(project_dir), "session_id": None, "sandbox_backend": "none"},
                )
                self.assertEqual(configured.status_code, 200)
                self.assertEqual(
                    configured.json()["launch_config"]["project_dir"],
                    str(project_dir.resolve()),
                )
                self.assertEqual(configured.json()["launch_config"]["sandbox_backend"], "none")

    def test_run_requires_valid_config_and_non_empty_task(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            project_dir = app_dir / "project"
            project_dir.mkdir()

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                # No configured project yet.
                run_without_project = client.post("/api/run", json={"task": "demo"})
                self.assertEqual(run_without_project.status_code, 400)

                # Configure project then send an empty task.
                configured = client.post(
                    "/api/launch-config",
                    json={"project_dir": str(project_dir), "session_id": None},
                )
                self.assertEqual(configured.status_code, 200)
                run_empty_task = client.post("/api/run", json={"task": "   "})
                self.assertEqual(run_empty_task.status_code, 400)

    def test_run_forwards_model_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, str | None] = {}

            async def _fake_start_run(
                task: str,
                model: str | None = None,
                root_agent_name: str | None = None,
            ) -> dict[str, object]:
                captured["task"] = task
                captured["model"] = model
                captured["root_agent_name"] = root_agent_name
                return {"ok": True}

            runtime_state.start_run = _fake_start_run  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/run",
                    json={
                        "task": "demo",
                        "model": "openai/gpt-4.1-mini",
                        "root_agent_name": "Root Alpha",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(captured["task"], "demo")
            self.assertEqual(captured["model"], "openai/gpt-4.1-mini")
            self.assertEqual(captured["root_agent_name"], "Root Alpha")

    def test_run_forwards_enabled_skill_ids(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, object] = {}

            async def _fake_start_run(
                task: str,
                model: str | None = None,
                root_agent_name: str | None = None,
                enabled_skill_ids: list[str] | None = None,
            ) -> dict[str, object]:
                captured["task"] = task
                captured["model"] = model
                captured["root_agent_name"] = root_agent_name
                captured["enabled_skill_ids"] = enabled_skill_ids
                return {"ok": True}

            runtime_state.start_run = _fake_start_run  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/run",
                    json={
                        "task": "demo",
                        "enabled_skill_ids": ["skill-a", " skill-b ", ""],
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(captured["task"], "demo")
            self.assertEqual(captured["enabled_skill_ids"], ["skill-a", "skill-b"])

    def test_run_forwards_enabled_mcp_server_ids(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, object] = {}

            async def _fake_start_run(
                task: str,
                model: str | None = None,
                root_agent_name: str | None = None,
                enabled_mcp_server_ids: list[str] | None = None,
            ) -> dict[str, object]:
                captured["task"] = task
                captured["model"] = model
                captured["root_agent_name"] = root_agent_name
                captured["enabled_mcp_server_ids"] = enabled_mcp_server_ids
                return {"ok": True}

            runtime_state.start_run = _fake_start_run  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/run",
                    json={
                        "task": "demo",
                        "enabled_mcp_server_ids": ["filesystem", " docs ", ""],
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(captured["task"], "demo")
            self.assertEqual(
                captured["enabled_mcp_server_ids"],
                ["filesystem", "docs"],
            )

    def test_run_rejects_invalid_skill_id_with_400(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)

            with TestClient(app) as client:
                response = client.post(
                    "/api/run",
                    json={
                        "task": "demo",
                        "enabled_skill_ids": ["bad skill"],
                    },
                )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Invalid skill id", response.json()["detail"])

    def test_skills_discover_endpoint_forwards_payload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, object] = {}

            async def _fake_discover_skills(
                *,
                project_dir: str | None = None,
                remote: dict[str, object] | None = None,
                remote_password: str | None = None,
            ) -> dict[str, object]:
                captured["project_dir"] = project_dir
                captured["remote"] = remote
                captured["remote_password"] = remote_password
                return {"skills": [{"id": "skill-a"}], "snapshot": {"ok": True}}

            runtime_state.discover_skills = _fake_discover_skills  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/skills/discover",
                    json={"project_dir": "/tmp/demo"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["skills"], [{"id": "skill-a"}])
            self.assertEqual(captured["project_dir"], "/tmp/demo")

    def test_skills_discover_endpoint_returns_400_on_value_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state

            async def _raise_discover_skills(**kwargs):  # type: ignore[no-untyped-def]
                del kwargs
                raise ValueError("skills config required")

            runtime_state.discover_skills = _raise_discover_skills  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post("/api/skills/discover", json={})

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "skills config required")

    def test_mcp_servers_discover_endpoint_forwards_payload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, object] = {"called": False}

            async def _fake_discover_mcp_servers() -> dict[str, object]:
                captured["called"] = True
                return {"mcp_servers": [{"id": "filesystem"}], "snapshot": {"ok": True}}

            runtime_state.discover_mcp_servers = _fake_discover_mcp_servers  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post("/api/mcp/servers", json={})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mcp_servers"], [{"id": "filesystem"}])
            self.assertTrue(bool(captured["called"]))

    def test_mcp_servers_discover_endpoint_returns_500_on_runtime_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state

            async def _raise_discover_mcp_servers() -> dict[str, object]:
                raise RuntimeError("mcp discover failed")

            runtime_state.discover_mcp_servers = _raise_discover_mcp_servers  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post("/api/mcp/servers", json={})

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json()["detail"], "mcp discover failed")

    def test_mcp_oauth_endpoints_forward_runtime_calls(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state

            async def _fake_start(server_id: str, *, timeout_seconds: float = 300.0) -> dict[str, object]:
                self.assertEqual(server_id, "notion")
                self.assertEqual(timeout_seconds, 45.0)
                return {
                    "flow_id": "flow-1",
                    "server_id": "notion",
                    "status": "pending",
                    "authorization_url": "https://auth.example.com/authorize",
                    "snapshot": {"runtime": {"available_mcp_servers": []}},
                }

            async def _fake_status(flow_id: str) -> dict[str, object]:
                self.assertEqual(flow_id, "flow-1")
                return {
                    "flow_id": "flow-1",
                    "server_id": "notion",
                    "status": "completed",
                    "snapshot": {"runtime": {"available_mcp_servers": []}},
                }

            async def _fake_clear(server_id: str) -> dict[str, object]:
                self.assertEqual(server_id, "notion")
                return {
                    "server_id": "notion",
                    "cleared": True,
                    "snapshot": {"runtime": {"available_mcp_servers": []}},
                }

            runtime_state.start_mcp_oauth_login = _fake_start  # type: ignore[method-assign]
            runtime_state.mcp_oauth_login_status = _fake_status  # type: ignore[method-assign]
            runtime_state.clear_mcp_oauth_login = _fake_clear  # type: ignore[method-assign]

            with TestClient(app) as client:
                started = client.post(
                    "/api/mcp/oauth/start",
                    json={"server_id": "notion", "timeout_seconds": 45},
                )
                self.assertEqual(started.status_code, 200)
                self.assertEqual(started.json()["flow_id"], "flow-1")

                status = client.get("/api/mcp/oauth/flow-1")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["status"], "completed")

                cleared = client.post("/api/mcp/oauth/clear", json={"server_id": "notion"})
                self.assertEqual(cleared.status_code, 200)
                self.assertTrue(cleared.json()["cleared"])

    def test_mcp_env_auth_configure_endpoint_forwards_runtime_calls(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state

            async def _fake_configure(server_id: str, values: dict[str, object]) -> dict[str, object]:
                self.assertEqual(server_id, "github")
                self.assertEqual(
                    values,
                    {"GITHUB_MCP_AUTHORIZATION": "Bearer token-demo"},
                )
                return {
                    "server_id": "github",
                    "updated_keys": ["GITHUB_MCP_AUTHORIZATION"],
                    "snapshot": {"runtime": {"available_mcp_servers": []}},
                }

            runtime_state.configure_mcp_env_auth = _fake_configure  # type: ignore[method-assign]

            with TestClient(app) as client:
                configured = client.post(
                    "/api/mcp/env-auth/configure",
                    json={
                        "server_id": "github",
                        "values": {"GITHUB_MCP_AUTHORIZATION": "Bearer token-demo"},
                    },
                )
                self.assertEqual(configured.status_code, 200)
                self.assertEqual(
                    configured.json()["updated_keys"],
                    ["GITHUB_MCP_AUTHORIZATION"],
                )

    def test_mcp_env_auth_configure_endpoint_rejects_non_object_values(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)

            with TestClient(app) as client:
                response = client.post(
                    "/api/mcp/env-auth/configure",
                    json={"server_id": "github", "values": "invalid"},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "values must be an object.")

    def test_run_while_running_skips_launch_reconfigure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, Any] = {"set_launch_config_calls": 0, "start_run_calls": 0}
            runtime_state.has_running_session = lambda: True  # type: ignore[method-assign]
            runtime_state.current_session_id = "session-live"

            def _unexpected_set_launch_config(*, project_dir: str | None, session_id: str | None) -> dict[str, Any]:
                del project_dir, session_id
                captured["set_launch_config_calls"] = int(captured["set_launch_config_calls"]) + 1
                raise AssertionError("set_launch_config should not be called while session is running")

            async def _fake_start_run(
                task: str,
                model: str | None = None,
                root_agent_name: str | None = None,
            ) -> dict[str, Any]:
                captured["start_run_calls"] = int(captured["start_run_calls"]) + 1
                captured["task"] = task
                captured["model"] = model
                captured["root_agent_name"] = root_agent_name
                return {"ok": True}

            runtime_state.set_launch_config = _unexpected_set_launch_config  # type: ignore[method-assign]
            runtime_state.start_run = _fake_start_run  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/run",
                    json={
                        "task": "append live root",
                        "session_id": "session-live",
                        "model": "openai/gpt-4.1-mini",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(captured["set_launch_config_calls"], 0)
                self.assertEqual(captured["start_run_calls"], 1)
                self.assertEqual(captured["task"], "append live root")
                self.assertEqual(captured["model"], "openai/gpt-4.1-mini")
                self.assertIsNone(captured["root_agent_name"])

    def test_run_while_running_rejects_switching_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, Any] = {"start_run_calls": 0}
            runtime_state.has_running_session = lambda: True  # type: ignore[method-assign]
            runtime_state.current_session_id = "session-live"

            async def _fake_start_run(
                task: str,
                model: str | None = None,
                root_agent_name: str | None = None,
            ) -> dict[str, Any]:
                del task, model, root_agent_name
                captured["start_run_calls"] = int(captured["start_run_calls"]) + 1
                return {"ok": True}

            runtime_state.start_run = _fake_start_run  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/run",
                    json={
                        "task": "append live root",
                        "session_id": "session-other",
                        "model": "openai/gpt-4.1-mini",
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(captured["start_run_calls"], 0)

    def test_terminate_agent_endpoint_forwards_to_runtime_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, str] = {}

            async def _fake_terminate(
                session_id: str,
                *,
                agent_id: str,
                source: str = "webui",
            ) -> dict[str, object]:
                captured["session_id"] = session_id
                captured["agent_id"] = agent_id
                captured["source"] = source
                return {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "source": source,
                    "target_agent_ids": [agent_id],
                    "terminated_agent_ids": [agent_id],
                    "cancelled_tool_run_ids": [],
                }

            runtime_state.terminate_agent_with_subtree = _fake_terminate  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/session/session-1/agents/agent-1/terminate",
                    json={"source": "webui"},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["session_id"], "session-1")
                self.assertEqual(payload["agent_id"], "agent-1")
                self.assertEqual(captured["session_id"], "session-1")
                self.assertEqual(captured["agent_id"], "agent-1")
                self.assertEqual(captured["source"], "webui")

    def test_launch_config_rejects_invalid_session_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                response = client.post(
                    "/api/launch-config",
                    json={"project_dir": None, "session_id": "../escape"},
                )
                self.assertEqual(response.status_code, 400)

    def test_launch_config_rejects_remote_when_mode_is_staged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                response = client.post(
                    "/api/launch-config",
                    json={
                        "project_dir": None,
                        "session_id": None,
                        "session_mode": "staged",
                        "remote": {
                            "kind": "remote_ssh",
                            "ssh_target": "demo@example.com",
                            "remote_dir": "/home/demo/workspace",
                            "auth_mode": "key",
                            "identity_file": "~/.ssh/id_ed25519",
                            "known_hosts_policy": "accept_new",
                            "remote_os": "linux",
                        },
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("direct mode", str(response.json().get("detail", "")))

    def test_remote_validate_endpoint_forwards_payload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, Any] = {}

            async def _fake_validate_remote_workspace(
                *,
                remote: dict[str, Any],
                remote_password: str | None = None,
                session_mode: str | None = None,
                sandbox_backend: str | None = None,
            ) -> dict[str, Any]:
                captured["remote"] = remote
                captured["remote_password"] = remote_password
                captured["session_mode"] = session_mode
                captured["sandbox_backend"] = sandbox_backend
                return {"ok": True, "ssh_target": remote.get("ssh_target")}

            runtime_state.validate_remote_workspace = _fake_validate_remote_workspace  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/remote/validate",
                    json={
                        "session_mode": "direct",
                        "sandbox_backend": "none",
                        "remote_password": "secret-pass",
                        "remote": {
                            "kind": "remote_ssh",
                            "ssh_target": "demo@example.com",
                            "remote_dir": "/home/demo/workspace",
                            "auth_mode": "password",
                            "known_hosts_policy": "accept_new",
                            "remote_os": "linux",
                        },
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["ssh_target"], "demo@example.com")
                self.assertEqual(captured["session_mode"], "direct")
                self.assertEqual(captured["sandbox_backend"], "none")
                self.assertEqual(captured["remote_password"], "secret-pass")
                self.assertEqual(
                    str((captured["remote"] or {}).get("remote_dir", "")),
                    "/home/demo/workspace",
                )

    def test_launch_config_with_session_id_triggers_remote_session_validation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, Any] = {}

            async def _fake_validate_remote_session_load(
                *,
                session_id: str,
                sandbox_backend: str | None = None,
                remote_password: str | None = None,
            ) -> dict[str, Any]:
                captured["session_id"] = session_id
                captured["sandbox_backend"] = sandbox_backend
                captured["remote_password"] = remote_password
                return {"ok": True}

            def _fake_set_launch_config(**kwargs):  # type: ignore[no-untyped-def]
                captured["set_launch_config"] = kwargs
                return {"launch_config": {"session_id": kwargs.get("session_id")}}

            runtime_state.validate_remote_session_load = _fake_validate_remote_session_load  # type: ignore[method-assign]
            runtime_state.set_launch_config = _fake_set_launch_config  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/launch-config",
                    json={
                        "session_id": "session-123",
                        "sandbox_backend": "anthropic",
                        "remote_password": "secret-pass",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(captured.get("session_id"), "session-123")
                self.assertEqual(captured.get("sandbox_backend"), "anthropic")
                self.assertEqual(captured.get("remote_password"), "secret-pass")
                self.assertEqual(
                    str((captured.get("set_launch_config") or {}).get("session_id", "")),
                    "session-123",
                )

    def test_launch_config_returns_400_when_remote_session_validation_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            captured: dict[str, Any] = {"set_called": False}

            async def _fake_validate_remote_session_load(**kwargs):  # type: ignore[no-untyped-def]
                del kwargs
                raise ValueError("Remote validation failed: dependency missing")

            def _fake_set_launch_config(**kwargs):  # type: ignore[no-untyped-def]
                del kwargs
                captured["set_called"] = True
                return {"launch_config": {"session_id": "session-123"}}

            runtime_state.validate_remote_session_load = _fake_validate_remote_session_load  # type: ignore[method-assign]
            runtime_state.set_launch_config = _fake_set_launch_config  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post(
                    "/api/launch-config",
                    json={"session_id": "session-123", "sandbox_backend": "anthropic"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("Remote validation failed", str(response.json().get("detail", "")))
                self.assertFalse(bool(captured["set_called"]))

    def test_terminal_open_endpoint(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            runtime_state.open_terminal = lambda _session_id=None: {  # type: ignore[method-assign]
                "session_id": "session-demo",
                "workspace_root": "/tmp/workspace",
            }

            with TestClient(app) as client:
                response = client.post("/api/terminal/open", json={})
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["session_id"], "session-demo")
                self.assertEqual(payload["workspace_root"], "/tmp/workspace")

    def test_config_save_reload_and_invalid_toml(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text('[project]\nname="OpenCompany"\n', encoding="utf-8")

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                config = client.get("/api/config")
                self.assertEqual(config.status_code, 200)
                self.assertIn("path", config.json())
                self.assertIn("text", config.json())

                invalid = client.post("/api/config/save", json={"text": 'not = "valid" = toml'})
                self.assertEqual(invalid.status_code, 400)

                saved = client.post("/api/config/save", json={"text": '[project]\nname = "After"\n'})
                self.assertEqual(saved.status_code, 200)
                self.assertIn('name = "After"', saved.json()["text"])

                reloaded = client.post("/api/config/reload", json={})
                self.assertEqual(reloaded.status_code, 200)
                self.assertIn('name = "After"', reloaded.json()["text"])

    def test_websocket_batches_and_coalesces_stream_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")

            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state

            with TestClient(app) as client:
                with client.websocket_connect("/api/events") as websocket:
                    first = websocket.receive_json()
                    self.assertEqual(first.get("event_type"), "runtime_state")

                    runtime_state.event_hub.publish(
                        {
                            "event_type": "llm_token",
                            "timestamp": "2026-03-10T00:00:01Z",
                            "session_id": "session-1",
                            "agent_id": "agent-1",
                            "phase": "llm",
                            "payload": {"token": "Hello "},
                        }
                    )
                    runtime_state.event_hub.publish(
                        {
                            "event_type": "llm_token",
                            "timestamp": "2026-03-10T00:00:02Z",
                            "session_id": "session-1",
                            "agent_id": "agent-1",
                            "phase": "llm",
                            "payload": {"token": "World"},
                        }
                    )

                    batch = websocket.receive_json()
                    self.assertEqual(batch.get("event_type"), "event_batch")
                    events = batch.get("payload", {}).get("events", [])
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["payload"]["token"], "Hello World")

    def test_steer_endpoints_publish_websocket_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                '[project]\ndefault_locale = "en"\n',
                encoding="utf-8",
            )
            session_id = "session-steer-websocket-events"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            project_dir = app_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)

            setup_orchestrator = Orchestrator(Path("."), locale="en", app_dir=app_dir)
            setup_orchestrator.storage.upsert_session(
                RunSession(
                    id=session_id,
                    project_dir=project_dir,
                    task="Steer websocket test",
                    locale="en",
                    root_agent_id="agent-root",
                    status=SessionStatus.RUNNING,
                    created_at="2026-03-13T10:00:00Z",
                    updated_at="2026-03-13T10:00:00Z",
                )
            )
            setup_orchestrator.storage.upsert_agent(
                AgentNode(
                    id="agent-root",
                    session_id=session_id,
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="Do work",
                    workspace_id="root",
                    status=AgentStatus.RUNNING,
                    conversation=[{"role": "user", "content": "start"}],
                )
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                with client.websocket_connect("/api/events") as websocket:
                    first = websocket.receive_json()
                    self.assertEqual(first.get("event_type"), "runtime_state")

                    submit = client.post(
                        f"/api/session/{session_id}/steers",
                        json={
                            "agent_id": "agent-root",
                            "content": "please prioritize tests",
                            "source": "webui",
                        },
                    )
                    self.assertEqual(submit.status_code, 200)
                    steer_run_id = str((submit.json().get("steer_run") or {}).get("id", ""))
                    self.assertTrue(steer_run_id)

                    submitted_batch = websocket.receive_json()
                    self.assertEqual(submitted_batch.get("event_type"), "event_batch")
                    submitted_events = submitted_batch.get("payload", {}).get("events", [])
                    self.assertTrue(
                        any(
                            str(event.get("event_type", "")) == "steer_run_submitted"
                            and str((event.get("payload") or {}).get("steer_run_id", ""))
                            == steer_run_id
                            for event in submitted_events
                            if isinstance(event, dict)
                        )
                    )

                    cancel = client.post(
                        f"/api/session/{session_id}/steer-runs/{steer_run_id}/cancel",
                        json={},
                    )
                    self.assertEqual(cancel.status_code, 200)

                    updated_batch = websocket.receive_json()
                    self.assertEqual(updated_batch.get("event_type"), "event_batch")
                    updated_events = updated_batch.get("payload", {}).get("events", [])
                    self.assertTrue(
                        any(
                            str(event.get("event_type", "")) == "steer_run_updated"
                            and str((event.get("payload") or {}).get("steer_run_id", ""))
                            == steer_run_id
                            and str((event.get("payload") or {}).get("status", ""))
                            == SteerRunStatus.CANCELLED.value
                            for event in updated_events
                            if isinstance(event, dict)
                        )
                    )

    def test_system_picker_project_updates_launch_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            project_dir = app_dir / "project"
            project_dir.mkdir()

            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            runtime_state.prompt_for_directory = lambda _title, _initial: project_dir.resolve()

            with TestClient(app) as client:
                response = client.post("/api/picker/project", json={"sandbox_backend": "none"})
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["launch_config"]["project_dir"], str(project_dir.resolve()))
                self.assertTrue(payload["launch_config"]["can_run"])
                self.assertEqual(payload["launch_config"]["sandbox_backend"], "none")

    def test_system_picker_session_updates_launch_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_dir = app_dir / ".opencompany" / "sessions" / "session-123"
            session_dir.mkdir(parents=True, exist_ok=True)
            project_dir = app_dir / "project"
            project_dir.mkdir()

            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            runtime_state.prompt_for_directory = lambda _title, _initial: session_dir.resolve()

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.app_dir = app_dir

                def load_session_context(self, session_id: str) -> RunSession:
                    del session_id
                    return RunSession(
                        id="session-123-copy",
                        project_dir=project_dir,
                        task="loaded task",
                        locale="en",
                        root_agent_id="agent-root",
                        status=SessionStatus.INTERRUPTED,
                    )

            runtime_state._read_orchestrator = lambda _project_dir: _FakeOrchestrator()  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post("/api/picker/session", json={"sandbox_backend": "none"})
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["launch_config"]["session_id"], "session-123-copy")
                self.assertTrue(payload["launch_config"]["can_resume"])
                self.assertEqual(payload["launch_config"]["sandbox_backend"], "none")

    def test_system_picker_session_returns_validation_failure_detail(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_dir = app_dir / ".opencompany" / "sessions" / "session-123"
            session_dir.mkdir(parents=True, exist_ok=True)

            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            runtime_state.prompt_for_directory = lambda _title, _initial: session_dir.resolve()

            async def _fake_validate_remote_session_load(**kwargs):  # type: ignore[no-untyped-def]
                del kwargs
                raise ValueError("Remote validation failed: Node.js >= 18 required")

            runtime_state.validate_remote_session_load = _fake_validate_remote_session_load  # type: ignore[method-assign]

            with TestClient(app) as client:
                response = client.post("/api/picker/session", json={"sandbox_backend": "anthropic"})
                self.assertEqual(response.status_code, 400)
                self.assertIn(
                    "Remote validation failed: Node.js >= 18 required",
                    str(response.json().get("detail", "")),
                )

    def test_system_picker_cancel_returns_bad_request(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")

            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            runtime_state.prompt_for_directory = lambda _title, _initial: None

            with TestClient(app) as client:
                response = client.post("/api/picker/project", json={})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(str(response.json().get("detail", "")).strip())

    def test_session_events_includes_agents_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-events-agents"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            orchestrator = Orchestrator(Path("."), locale="en", app_dir=app_dir)
            now = "2026-03-12T12:00:00Z"
            session = RunSession(
                id=session_id,
                project_dir=Path(".").resolve(),
                task="restore snapshot",
                locale="en",
                root_agent_id="agent-root",
                status=SessionStatus.INTERRUPTED,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            root_agent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root",
                role=AgentRole.ROOT,
                instruction="root",
                workspace_id="root",
                status=AgentStatus.PAUSED,
                children=["agent-child"],
                metadata={
                    "model": "openai/gpt-4.1",
                    "context_summary": "## Current context summary\n- completed setup",
                    "summary_version": 2,
                },
            )
            child_agent = AgentNode(
                id="agent-child",
                session_id=session_id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="child",
                workspace_id="ws-agent-child",
                parent_agent_id="agent-root",
                status=AgentStatus.PAUSED,
                metadata={"model": "openai/gpt-4.1"},
            )
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator.storage.upsert_agent(child_agent)

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                response = client.get(f"/api/session/{session_id}/events")
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertIn("events", payload)
                self.assertIn("agents", payload)
                agents = payload.get("agents", [])
                self.assertEqual(len(agents), 2)
                agents_by_id = {str(item.get("id", "")): item for item in agents}
                self.assertIsNone(agents_by_id["agent-root"].get("parent_agent_id"))
                self.assertEqual(
                    agents_by_id["agent-child"].get("parent_agent_id"),
                    "agent-root",
                )
                self.assertEqual(
                    agents_by_id["agent-root"].get("status"),
                    AgentStatus.PAUSED.value,
                )
                self.assertEqual(
                    agents_by_id["agent-root"].get("model"),
                    "openai/gpt-4.1",
                )
                self.assertEqual(
                    agents_by_id["agent-root"].get("keep_pinned_messages"),
                    1,
                )
                self.assertEqual(
                    agents_by_id["agent-root"].get("summary_version"),
                    2,
                )
                self.assertEqual(
                    agents_by_id["agent-root"].get("context_latest_summary"),
                    "## Current context summary\n- completed setup",
                )
                self.assertEqual(
                    agents_by_id["agent-child"].get("model"),
                    "openai/gpt-4.1",
                )

    def test_resume_endpoint_removed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                openapi = client.get("/openapi.json")
                self.assertEqual(openapi.status_code, 200)
                paths = (openapi.json() or {}).get("paths", {})
                self.assertNotIn("/api/resume", paths)
                response = client.post("/api/resume", json={"session_id": "any", "instruction": "continue"})
                self.assertIn(response.status_code, {404, 405})

    def test_events_endpoint_supports_recent_activity_window_and_before_cursor(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-events-window"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            project_dir = app_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.storage.upsert_session(
                RunSession(
                    id=session_id,
                    project_dir=project_dir,
                    task="events",
                    locale="en",
                    root_agent_id="agent-root",
                    status=SessionStatus.INTERRUPTED,
                )
            )
            orchestrator.storage.upsert_agent(
                AgentNode(
                    id="agent-root",
                    session_id=session_id,
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="events",
                    workspace_id="root",
                    status=AgentStatus.PAUSED,
                )
            )
            for timestamp, event_type, payload in [
                ("2026-03-11T12:00:00Z", "session_started", {"task": "events"}),
                ("2026-03-11T12:00:01Z", "llm_reasoning", {"token": "thinking"}),
                ("2026-03-11T12:00:02Z", "agent_prompt", {"step_count": 1, "agent_name": "Root"}),
                ("2026-03-11T12:00:03Z", "shell_stream", {"stream": "stdout", "chunk": "hi"}),
                ("2026-03-11T12:00:04Z", "agent_completed", {"summary": "done"}),
            ]:
                orchestrator.storage.append_event(
                    EventRecord(
                        timestamp=timestamp,
                        session_id=session_id,
                        agent_id="agent-root",
                        parent_agent_id=None,
                        event_type=event_type,
                        phase="runtime",
                        payload=payload,
                        workspace_id="root",
                        checkpoint_seq=0,
                    )
                )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                first = client.get(
                    f"/api/session/{session_id}/events?limit=2&activity_only=true"
                )
                self.assertEqual(first.status_code, 200)
                first_payload = first.json()
                self.assertEqual(
                    [item["event_type"] for item in first_payload["events"]],
                    ["agent_prompt", "agent_completed"],
                )
                self.assertEqual(
                    [item["id"] for item in first_payload["agents"]],
                    ["agent-root"],
                )
                self.assertTrue(first_payload["has_more_before"])
                self.assertTrue(first_payload["before_cursor"])

                second = client.get(
                    f"/api/session/{session_id}/events?limit=2&activity_only=true&include_agents=false&before={first_payload['before_cursor']}"
                )
                self.assertEqual(second.status_code, 200)
                second_payload = second.json()
                self.assertEqual(
                    [item["event_type"] for item in second_payload["events"]],
                    ["session_started"],
                )
                self.assertEqual(second_payload["agents"], [])
                self.assertFalse(second_payload["has_more_before"])

    def test_tool_run_endpoints_return_rows_and_metrics(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-tool-runs"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id="toolrun-1",
                    session_id=session_id,
                    agent_id="agent-root",
                    tool_name="list_agent_runs",
                    arguments={"type": "list_agent_runs"},
                    status=ToolRunStatus.COMPLETED,
                    blocking=True,
                    created_at="2026-03-11T10:00:00Z",
                    started_at="2026-03-11T10:00:00Z",
                    completed_at="2026-03-11T10:00:01Z",
                )
            )
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id="toolrun-2",
                    session_id=session_id,
                    agent_id="agent-root",
                    tool_name="shell",
                    arguments={"type": "shell", "command": "false"},
                    status=ToolRunStatus.FAILED,
                    blocking=True,
                    created_at="2026-03-11T10:00:02Z",
                    started_at="2026-03-11T10:00:02Z",
                    completed_at="2026-03-11T10:00:03Z",
                )
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                runs = client.get(f"/api/session/{session_id}/tool-runs")
                self.assertEqual(runs.status_code, 200)
                runs_payload = runs.json()
                self.assertEqual(runs_payload["session_id"], session_id)
                self.assertEqual(len(runs_payload["tool_runs"]), 2)
                self.assertIn("has_more", runs_payload)
                self.assertFalse(bool(runs_payload["has_more"]))

                detail = client.get(f"/api/session/{session_id}/tool-runs/toolrun-2")
                self.assertEqual(detail.status_code, 200)
                detail_payload = detail.json()
                self.assertEqual(detail_payload["session_id"], session_id)
                self.assertEqual(detail_payload["tool_run"]["id"], "toolrun-2")
                self.assertEqual(detail_payload["tool_run"]["tool_name"], "shell")
                self.assertEqual(detail_payload["tool_run"]["stdout"], "")
                self.assertEqual(detail_payload["tool_run"]["stderr"], "")

                metrics = client.get(f"/api/session/{session_id}/tool-runs/metrics")
                self.assertEqual(metrics.status_code, 200)
                metrics_payload = metrics.json()
                self.assertEqual(metrics_payload["session_id"], session_id)
                self.assertEqual(metrics_payload["total_runs"], 2)
                self.assertEqual(metrics_payload["failed_runs"], 1)

    def test_tool_run_detail_endpoint_returns_running_shell_output_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-shell-running"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            app = create_webui_app(app_dir=app_dir)
            runtime_state = app.state.runtime_state
            run_record = {
                "id": "toolrun-shell-running",
                "session_id": session_id,
                "agent_id": "agent-root",
                "tool_name": "shell",
                "status": ToolRunStatus.RUNNING.value,
                "arguments": {"type": "shell", "command": "sleep 30"},
                "created_at": "2026-03-14T12:00:00Z",
                "started_at": "2026-03-14T12:00:00Z",
                "completed_at": None,
                "result": None,
                "error": "",
            }

            class _FakeStorage:
                def load_tool_run(self, tool_run_id: str) -> dict[str, Any] | None:
                    if tool_run_id == "toolrun-shell-running":
                        return dict(run_record)
                    return None

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.storage = _FakeStorage()

                @staticmethod
                def _shell_outputs_for_tool_run(_run: dict[str, Any]) -> tuple[str, str]:
                    return ("line-1\nline-2\n", "warn-1\n")

            runtime_state._terminal_orchestrator = lambda: _FakeOrchestrator()  # type: ignore[method-assign]

            with TestClient(app) as client:
                detail = client.get(f"/api/session/{session_id}/tool-runs/toolrun-shell-running")
                self.assertEqual(detail.status_code, 200)
                detail_payload = detail.json()
                self.assertEqual(detail_payload["session_id"], session_id)
                run = detail_payload["tool_run"]
                self.assertEqual(run["id"], "toolrun-shell-running")
                self.assertEqual(run["status"], ToolRunStatus.RUNNING.value)
                self.assertEqual(run["stdout"], "line-1\nline-2\n")
                self.assertEqual(run["stderr"], "warn-1\n")
                self.assertEqual(run["timeline"], [])

    def test_tool_run_detail_endpoint_includes_timeline(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-tool-run-timeline"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id="toolrun-1",
                    session_id=session_id,
                    agent_id="agent-root",
                    tool_name="shell",
                    arguments={"type": "shell", "command": "pwd"},
                    status=ToolRunStatus.COMPLETED,
                    blocking=True,
                    created_at="2026-03-14T12:00:00Z",
                    started_at="2026-03-14T12:00:01Z",
                    completed_at="2026-03-14T12:00:02Z",
                    result={"stdout": "/tmp/demo\n"},
                )
            )
            for timestamp, event_type, payload in [
                (
                    "2026-03-14T12:00:00Z",
                    "tool_call_started",
                    {
                        "action": {
                            "type": "shell",
                            "command": "pwd",
                            "_tool_call_id": "call-1",
                            "tool_run_id": "toolrun-1",
                        }
                    },
                ),
                (
                    "2026-03-14T12:00:01Z",
                    "tool_run_submitted",
                    {
                        "tool_run_id": "toolrun-1",
                        "tool_name": "shell",
                        "action": {"_tool_call_id": "call-1"},
                    },
                ),
                (
                    "2026-03-14T12:00:02Z",
                    "tool_run_updated",
                    {"tool_run_id": "toolrun-1", "status": ToolRunStatus.COMPLETED.value},
                ),
            ]:
                orchestrator.storage.append_event(
                    EventRecord(
                        timestamp=timestamp,
                        session_id=session_id,
                        agent_id="agent-root",
                        parent_agent_id=None,
                        event_type=event_type,
                        phase="runtime",
                        payload=payload,
                        workspace_id="root",
                        checkpoint_seq=0,
                    )
                )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                detail = client.get(f"/api/session/{session_id}/tool-runs/toolrun-1")
                self.assertEqual(detail.status_code, 200)
                timeline = detail.json()["tool_run"]["timeline"]
                self.assertEqual(
                    [item["event_type"] for item in timeline],
                    ["tool_call_started", "tool_run_submitted", "tool_run_updated"],
                )

    def test_steer_run_endpoints_submit_list_metrics_and_cancel(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                '[project]\ndefault_locale = "en"\n',
                encoding="utf-8",
            )
            session_id = "session-steer-runs"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            project_dir = app_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)

            orchestrator = Orchestrator(Path("."), locale="en", app_dir=app_dir)
            orchestrator.storage.upsert_session(
                RunSession(
                    id=session_id,
                    project_dir=project_dir,
                    task="Steer API test",
                    locale="en",
                    root_agent_id="agent-root",
                    status=SessionStatus.RUNNING,
                    created_at="2026-03-13T10:00:00Z",
                    updated_at="2026-03-13T10:00:00Z",
                )
            )
            orchestrator.storage.upsert_agent(
                AgentNode(
                    id="agent-root",
                    session_id=session_id,
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="Do work",
                    workspace_id="root",
                    status=AgentStatus.RUNNING,
                    conversation=[{"role": "user", "content": "start"}],
                )
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                submit = client.post(
                    f"/api/session/{session_id}/steers",
                    json={
                        "agent_id": "agent-root",
                        "content": "Please prioritize tests",
                        "source": "webui",
                    },
                )
                self.assertEqual(submit.status_code, 200)
                submit_payload = submit.json()
                steer_run = submit_payload.get("steer_run", {})
                steer_run_id = str(steer_run.get("id", ""))
                self.assertTrue(steer_run_id)
                self.assertEqual(steer_run.get("status"), SteerRunStatus.WAITING.value)
                self.assertEqual(steer_run.get("source_agent_id"), "user")
                self.assertEqual(steer_run.get("source_agent_name"), "user")
                self.assertEqual(steer_run.get("target_agent_name"), "Root")

                runs = client.get(f"/api/session/{session_id}/steer-runs")
                self.assertEqual(runs.status_code, 200)
                runs_payload = runs.json()
                self.assertEqual(runs_payload["session_id"], session_id)
                self.assertEqual(len(runs_payload["steer_runs"]), 1)
                self.assertEqual(runs_payload["steer_runs"][0].get("target_agent_name"), "Root")
                self.assertFalse(bool(runs_payload["has_more"]))

                metrics = client.get(f"/api/session/{session_id}/steer-runs/metrics")
                self.assertEqual(metrics.status_code, 200)
                metrics_payload = metrics.json()
                self.assertEqual(metrics_payload["session_id"], session_id)
                self.assertEqual(metrics_payload["total_runs"], 1)
                self.assertEqual(metrics_payload["status_counts"]["waiting"], 1)

                cancel = client.post(
                    f"/api/session/{session_id}/steer-runs/{steer_run_id}/cancel",
                    json={},
                )
                self.assertEqual(cancel.status_code, 200)
                cancel_payload = cancel.json()
                self.assertEqual(cancel_payload["final_status"], SteerRunStatus.CANCELLED.value)
                self.assertTrue(bool(cancel_payload["cancelled"]))

                cancel_again = client.post(
                    f"/api/session/{session_id}/steer-runs/{steer_run_id}/cancel",
                    json={},
                )
                self.assertEqual(cancel_again.status_code, 200)
                cancel_again_payload = cancel_again.json()
                self.assertEqual(
                    cancel_again_payload["final_status"],
                    SteerRunStatus.CANCELLED.value,
                )
                self.assertFalse(bool(cancel_again_payload["cancelled"]))

                submit_completed = client.post(
                    f"/api/session/{session_id}/steers",
                    json={
                        "agent_id": "agent-root",
                        "content": "This one will be completed",
                        "source": "webui",
                    },
                )
                self.assertEqual(submit_completed.status_code, 200)
                completed_id = str(submit_completed.json()["steer_run"]["id"])
                orchestrator.storage.complete_waiting_steer_run(
                    session_id=session_id,
                    steer_run_id=completed_id,
                    completed_at="2026-03-13T10:01:00Z",
                    delivered_step=2,
                )
                cancel_completed = client.post(
                    f"/api/session/{session_id}/steer-runs/{completed_id}/cancel",
                    json={},
                )
                self.assertEqual(cancel_completed.status_code, 200)
                cancel_completed_payload = cancel_completed.json()
                self.assertEqual(
                    cancel_completed_payload["final_status"],
                    SteerRunStatus.COMPLETED.value,
                )
                self.assertFalse(bool(cancel_completed_payload["cancelled"]))

    def test_steer_run_list_includes_delivery_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                '[project]\ndefault_locale = "en"\n',
                encoding="utf-8",
            )
            session_id = "session-steer-runs-delivery"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            project_dir = app_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)

            orchestrator = Orchestrator(Path("."), locale="en", app_dir=app_dir)
            orchestrator.storage.upsert_session(
                RunSession(
                    id=session_id,
                    project_dir=project_dir,
                    task="Steer delivery metadata test",
                    locale="en",
                    root_agent_id="agent-root",
                    status=SessionStatus.RUNNING,
                    created_at="2026-03-14T09:00:00Z",
                    updated_at="2026-03-14T09:00:00Z",
                )
            )
            orchestrator.storage.upsert_agent(
                AgentNode(
                    id="agent-root",
                    session_id=session_id,
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="Do work",
                    workspace_id="root",
                    status=AgentStatus.RUNNING,
                    conversation=[{"role": "user", "content": "start"}],
                )
            )
            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id="steerrun-completed",
                    session_id=session_id,
                    agent_id="agent-root",
                    content="Please prioritize tests",
                    source="webui",
                    source_agent_id="agent-parent",
                    source_agent_name="Planner",
                    status=SteerRunStatus.COMPLETED,
                    created_at="2026-03-14T09:00:00Z",
                    completed_at="2026-03-14T09:00:05Z",
                    delivered_step=7,
                )
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                response = client.get(f"/api/session/{session_id}/steer-runs")
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(len(payload["steer_runs"]), 1)
                run = payload["steer_runs"][0]
                self.assertEqual(run["delivered_step"], 7)
                self.assertEqual(run["source_agent_id"], "agent-parent")
                self.assertEqual(run["source_agent_name"], "Planner")
                self.assertEqual(run["target_agent_name"], "Root")

    def test_steer_run_endpoints_return_400_on_invalid_requests(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-steer-runs-errors"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            project_dir = app_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.storage.upsert_session(
                RunSession(
                    id=session_id,
                    project_dir=project_dir,
                    task="Steer API invalid test",
                    locale="en",
                    root_agent_id="agent-root",
                    status=SessionStatus.RUNNING,
                    created_at="2026-03-13T10:00:00Z",
                    updated_at="2026-03-13T10:00:00Z",
                )
            )
            orchestrator.storage.upsert_agent(
                AgentNode(
                    id="agent-root",
                    session_id=session_id,
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="Do work",
                    workspace_id="root",
                    status=AgentStatus.RUNNING,
                    conversation=[{"role": "user", "content": "start"}],
                )
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                missing_agent = client.post(
                    f"/api/session/{session_id}/steers",
                    json={"content": "no agent"},
                )
                self.assertEqual(missing_agent.status_code, 400)

                invalid_status = client.get(
                    f"/api/session/{session_id}/steer-runs?status=bad"
                )
                self.assertEqual(invalid_status.status_code, 400)

                missing_run = client.post(
                    f"/api/session/{session_id}/steer-runs/steerrun-missing/cancel",
                    json={},
                )
                self.assertEqual(missing_run.status_code, 400)

    def test_messages_endpoint_supports_cursor_and_agent_filter(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-messages"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            root_path = session_dir / "agent-root_messages.jsonl"
            worker_path = session_dir / "agent-worker_messages.jsonl"
            root_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-03-11T12:00:00Z",
                                "session_id": session_id,
                                "agent_id": "agent-root",
                                "agent_name": "Root",
                                "agent_role": "root",
                                "message_index": 0,
                                "role": "user",
                                "message": {"role": "user", "content": "task"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-03-11T12:00:01Z",
                                "session_id": session_id,
                                "agent_id": "agent-root",
                                "agent_name": "Root",
                                "agent_role": "root",
                                "message_index": 1,
                                "role": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": '{"actions":[{"type": "list_agent_runs"}]}',
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            worker_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-03-11T12:00:02Z",
                        "session_id": session_id,
                        "agent_id": "agent-worker",
                        "agent_name": "Worker",
                        "agent_role": "worker",
                        "message_index": 0,
                        "role": "user",
                        "message": {"role": "user", "content": "inspect"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                first = client.get(f"/api/session/{session_id}/messages?limit=1")
                self.assertEqual(first.status_code, 200)
                first_payload = first.json()
                self.assertEqual(len(first_payload["messages"]), 1)
                self.assertTrue(first_payload["has_more"])
                self.assertTrue(first_payload["next_cursor"])

                cursor = first_payload["next_cursor"]
                second = client.get(
                    f"/api/session/{session_id}/messages?limit=5&cursor={cursor}"
                )
                self.assertEqual(second.status_code, 200)
                second_payload = second.json()
                self.assertGreaterEqual(len(second_payload["messages"]), 1)

                filtered = client.get(
                    f"/api/session/{session_id}/messages?agent_id=agent-root&limit=10"
                )
                self.assertEqual(filtered.status_code, 200)
                filtered_payload = filtered.json()
                self.assertEqual(
                    sorted({item["agent_id"] for item in filtered_payload["messages"]}),
                    ["agent-root"],
                )

    def test_messages_endpoint_supports_tail_and_before_cursor(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-messages-before"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            root_path = session_dir / "agent-root_messages.jsonl"
            root_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-03-11T12:00:00Z",
                                "session_id": session_id,
                                "agent_id": "agent-root",
                                "agent_name": "Root",
                                "agent_role": "root",
                                "message_index": 0,
                                "role": "user",
                                "message": {"role": "user", "content": "first"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-03-11T12:00:01Z",
                                "session_id": session_id,
                                "agent_id": "agent-root",
                                "agent_name": "Root",
                                "agent_role": "root",
                                "message_index": 1,
                                "role": "assistant",
                                "message": {"role": "assistant", "content": "second"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-03-11T12:00:02Z",
                                "session_id": session_id,
                                "agent_id": "agent-root",
                                "agent_name": "Root",
                                "agent_role": "root",
                                "message_index": 2,
                                "role": "assistant",
                                "message": {"role": "assistant", "content": "third"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                tail = client.get(f"/api/session/{session_id}/messages?limit=2&tail=2")
                self.assertEqual(tail.status_code, 200)
                tail_payload = tail.json()
                self.assertEqual(
                    [item["message"]["content"] for item in tail_payload["messages"]],
                    ["second", "third"],
                )
                self.assertFalse(tail_payload["has_more"])
                self.assertTrue(tail_payload["next_cursor"])
                self.assertTrue(tail_payload["has_more_before"])
                self.assertTrue(tail_payload["before_cursor"])

                previous = client.get(
                    f"/api/session/{session_id}/messages?limit=2&before={tail_payload['before_cursor']}"
                )
                self.assertEqual(previous.status_code, 200)
                previous_payload = previous.json()
                self.assertEqual(
                    [item["message"]["content"] for item in previous_payload["messages"]],
                    ["first"],
                )
                self.assertFalse(previous_payload["has_more_before"])

    def test_messages_endpoint_annotates_prompt_visibility_with_summary_window(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[runtime.context]
max_context_tokens = 8192
keep_pinned_messages = 1
""".strip(),
                encoding="utf-8",
            )
            session_id = "session-message-window"
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.paths.session_dir(session_id)
            orchestrator.storage.upsert_session(
                RunSession(
                    id=session_id,
                    project_dir=app_dir,
                    task="demo",
                    locale="en",
                    root_agent_id="agent-root",
                    status=SessionStatus.RUNNING,
                )
            )
            agent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root",
                role=AgentRole.ROOT,
                instruction="demo",
                workspace_id="root",
                status=AgentStatus.PAUSED,
                metadata={
                    "context_summary": "## Current context summary\n- completed setup",
                    "summary_version": 2,
                    "summarized_until_message_index": 2,
                },
            )
            agent.step_count = 1
            orchestrator._append_agent_message(
                agent,
                {"role": "user", "content": "head pinned"},
            )
            agent.step_count = 1
            orchestrator._append_agent_message(
                agent,
                {"role": "assistant", "content": "same step but summarized"},
            )
            agent.step_count = 2
            orchestrator._append_agent_message(
                agent,
                {"role": "user", "content": "older summarized step"},
            )
            agent.step_count = 3
            orchestrator._append_agent_message(
                agent,
                {"role": "user", "content": "context pressure reminder"},
                None,
                {"exclude_from_context_compression": True},
            )
            agent.step_count = 3
            orchestrator._append_agent_message(
                agent,
                {"role": "assistant", "content": "latest assistant reply"},
            )

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                response = client.get(
                    f"/api/session/{session_id}/messages?agent_id=agent-root&limit=20"
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                records = payload.get("messages", [])
                by_index = {
                    int(record["message_index"]): record
                    for record in records
                    if "message_index" in record
                }
                self.assertEqual(by_index[0]["prompt_bucket"], "pinned")
                self.assertTrue(bool(by_index[0]["prompt_visible"]))
                self.assertEqual(by_index[1]["prompt_bucket"], "hidden_middle")
                self.assertFalse(bool(by_index[1]["prompt_visible"]))
                self.assertEqual(by_index[2]["prompt_bucket"], "hidden_middle")
                self.assertFalse(bool(by_index[2]["prompt_visible"]))
                self.assertEqual(by_index[3]["prompt_bucket"], "tail")
                self.assertTrue(bool(by_index[3]["prompt_visible"]))
                self.assertTrue(bool(by_index[3]["exclude_from_context_compression"]))
                self.assertEqual(by_index[4]["prompt_bucket"], "tail")
                self.assertTrue(bool(by_index[4]["prompt_visible"]))
