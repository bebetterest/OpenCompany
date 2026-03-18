from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class TuiImportTests(unittest.TestCase):
    def test_app_imports_when_textual_is_available(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp  # noqa: F401
        except ImportError:
            self.skipTest("textual is not installed in the current environment")


class TuiInteractionTests(unittest.IsolatedAsyncioTestCase):
    def test_resize_before_mount_is_safe(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.on_resize(None)

    def test_render_updates_before_mount_are_safe(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app._set_controls_running(False)
        app._update_status("shutting down")
        app._clear_activity_log()
        app._render_all()

    async def test_runtime_updates_before_mount_are_safe(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp, RuntimeUpdate
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        await app.on_runtime_update(
            RuntimeUpdate(
                {
                    "event_type": "session_started",
                    "timestamp": "2026-03-09T12:00:00Z",
                    "payload": {"task": "demo"},
                }
            )
        )

    async def test_context_compacted_event_triggers_full_message_reload(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp, RuntimeUpdate
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.current_session_id = "session-live-1"
        reloaded: list[str] = []
        incremental_syncs: list[str] = []

        def fake_reload(session_id: str) -> None:
            reloaded.append(session_id)

        async def fake_incremental_sync() -> None:
            incremental_syncs.append("incremental")

        app._reload_session_messages = fake_reload  # type: ignore[method-assign]
        app._sync_session_messages_incremental = fake_incremental_sync  # type: ignore[method-assign]

        await app.on_runtime_update(
            RuntimeUpdate(
                {
                    "event_type": "context_compacted",
                    "timestamp": "2026-03-16T21:28:42Z",
                    "session_id": "session-live-1",
                    "agent_id": "agent-ctx-1",
                    "payload": {
                        "step_range": {"start": 1, "end": 7},
                        "message_range": {"start": 0, "end": 14},
                        "summary_version": 1,
                        "context_latest_summary": "## Context Summary\n- done",
                    },
                }
            )
        )

        self.assertEqual(reloaded, ["session-live-1"])
        self.assertEqual(incremental_syncs, [])

    async def test_terminal_button_reenables_when_running_session_id_arrives(self) -> None:
        try:
            from textual.widgets import Button

            from opencompany.tui.app import OpenCompanyApp, RuntimeUpdate
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())

        async with app.run_test() as pilot:
            await pilot.pause()
            terminal_button = app.query_one("#terminal_button", Button)
            app.current_session_id = None
            app.configured_resume_session_id = None
            app.session_task = asyncio.create_task(asyncio.sleep(60))
            app._set_controls_running(True)
            await pilot.pause()
            self.assertTrue(terminal_button.disabled)

            await app.on_runtime_update(
                RuntimeUpdate(
                    {
                        "event_type": "session_started",
                        "timestamp": "2026-03-14T10:00:00Z",
                        "session_id": "session-live-1",
                        "payload": {"session_status": "running"},
                    }
                )
            )
            await pilot.pause()
            self.assertFalse(terminal_button.disabled)

            if app.session_task is not None:
                app.session_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await app.session_task

    async def test_runtime_update_exceptions_are_contained(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp, RuntimeUpdate
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())

        def broken_consume(record: dict[str, object]) -> None:
            del record
            raise ValueError("boom")

        app._consume_runtime_update = broken_consume  # type: ignore[method-assign]
        await app.on_runtime_update(
            RuntimeUpdate(
                {
                    "event_type": "session_started",
                    "timestamp": "2026-03-09T12:00:00Z",
                    "payload": {"task": "demo"},
                }
            )
        )
        self.assertEqual(app.current_session_status, "failed")
        self.assertEqual(app.current_summary, "boom")

    async def test_cancelled_run_before_mount_is_safe(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        class _CancelledOrchestrator:
            async def run_task(
                self,
                task: str,
                model: str | None = None,
                root_agent_name: str | None = None,
            ) -> None:
                del task
                del model
                del root_agent_name
                raise asyncio.CancelledError()

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.orchestrator = _CancelledOrchestrator()
        await app._run_task("demo", "fake/model")
        self.assertEqual(app.current_session_status, "interrupted")

    def test_post_runtime_update_handles_cancelled_error(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())

        def cancelled_post_message(message) -> None:  # type: ignore[no-untyped-def]
            del message
            raise asyncio.CancelledError()

        app.post_message = cancelled_post_message  # type: ignore[method-assign]
        app._post_runtime_update({"event_type": "llm_token", "payload": {"token": "x"}})

    def test_handle_exception_writes_diagnostic_record_and_keeps_app_alive(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path.cwd(), app_dir=Path(temp_dir))
            app._handle_exception(ValueError("boom"))
            self.assertEqual(app.current_session_status, "failed")
            self.assertEqual(app.current_summary, "boom")
            diagnostics_log = Path(temp_dir) / ".opencompany" / "diagnostics.jsonl"
            self.assertTrue(diagnostics_log.exists())
            self.assertIn("ui_exception", diagnostics_log.read_text(encoding="utf-8"))
            self.assertIn("ValueError", diagnostics_log.read_text(encoding="utf-8"))

    async def test_run_task_handles_base_exception_without_crashing(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        class _FatalOrchestrator:
            async def run_task(
                self,
                task: str,
                model: str | None = None,
                root_agent_name: str | None = None,
            ) -> None:
                del task
                del model
                del root_agent_name
                raise SystemExit("fatal")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.orchestrator = _FatalOrchestrator()
        await app._run_task("demo", "fake/model")
        self.assertEqual(app.current_session_status, "failed")
        self.assertEqual(app.current_summary, "fatal")

    async def test_quit_is_blocked_while_session_task_is_running(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.session_task = asyncio.create_task(asyncio.sleep(10))
        try:
            await app.action_quit()
            self.assertEqual(app.status_message, "Session is running. Interrupt it first before quitting.")
        finally:
            app.session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.session_task

    async def test_exit_is_blocked_while_session_task_is_running(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.session_task = asyncio.create_task(asyncio.sleep(10))
        try:
            app.exit()
            self.assertFalse(getattr(app, "_exit", False))
            self.assertEqual(app.status_message, "Session is running. Interrupt it first before quitting.")
        finally:
            app.session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.session_task

    async def test_default_layout_keeps_bottom_panels_visible(self) -> None:
        try:
            from textual.containers import Vertical, VerticalScroll

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertGreater(app.query_one("#monitor_body").size.height, 0)
                self.assertGreater(app.query_one("#overview_tree").size.height, 0)
                self.assertGreater(app.query_one("#activity_log").size.height, 0)
                self.assertIsInstance(app.query_one("#overview_tree"), Vertical)
                self.assertIsInstance(app.query_one("#overview_scroll"), VerticalScroll)
                self.assertIsInstance(app.query_one("#live_scroll"), VerticalScroll)

    async def test_compact_layout_keeps_panels_visible_on_small_terminal(self) -> None:
        try:
            from textual.widgets import TabbedContent

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(92, 20)
                await pilot.pause()
                self.assertLessEqual(app.query_one("#status_panel").size.height, 3)
                self.assertGreaterEqual(app.query_one("#overview_tree").size.height, 3)
                self.assertGreaterEqual(app.query_one("#activity_log").size.height, 2)
                tabs = app.query_one("#main_tabs", TabbedContent)
                tabs.active = "agents_tab"
                await pilot.pause()
                self.assertGreaterEqual(app.query_one("#live_tree").size.height, 4)

    async def test_diff_tab_is_disabled_in_direct_mode_and_reenabled_in_staged_mode(self) -> None:
        try:
            from textual.widgets import TabbedContent

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())

        async with app.run_test() as pilot:
            await pilot.pause()
            tabs = app.query_one("#main_tabs", TabbedContent)
            self.assertTrue(tabs.get_tab("diff_tab").disabled)

            app.session_mode = "staged"
            app._render_all()
            await pilot.pause()
            self.assertFalse(tabs.get_tab("diff_tab").disabled)

    def test_restore_session_history_locks_workspace_mode_from_loaded_session(self) -> None:
        try:
            from opencompany.models import RunSession, SessionStatus, WorkspaceMode
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            app = OpenCompanyApp(project_dir=project_dir)

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.app_dir = project_dir

                def load_session_context(self, session_id: str) -> RunSession:
                    del session_id
                    return RunSession(
                        id="session-live",
                        project_dir=project_dir,
                        task="loaded task",
                        locale="en",
                        root_agent_id="agent-root",
                        workspace_mode=WorkspaceMode.DIRECT,
                        status=SessionStatus.INTERRUPTED,
                    )

                def load_session_events(self, session_id: str) -> list[dict[str, object]]:
                    del session_id
                    return []

                def load_session_agents(self, session_id: str) -> list[dict[str, object]]:
                    del session_id
                    return []

                def list_session_messages(
                    self,
                    session_id: str,
                    *,
                    agent_id: str | None = None,
                    cursor: str | None = None,
                    limit: int = 500,
                    tail: int | None = None,
                ) -> dict[str, object]:
                    del session_id, agent_id, cursor, limit, tail
                    return {"messages": [], "next_cursor": None, "has_more": False}

            app.orchestrator = _FakeOrchestrator()  # type: ignore[assignment]
            app._restore_session_history("session-live")

            self.assertEqual(app.configured_resume_session_id, "session-live")
            self.assertEqual(app.current_session_id, "session-live")
            self.assertEqual(app.session_mode, WorkspaceMode.DIRECT)
            self.assertTrue(app.session_mode_locked)

    async def test_control_layout_uses_three_rows_with_compact_locale_buttons(self) -> None:
        try:
            from textual.containers import Horizontal, Vertical
            from textual.widgets import Button, Input, Static, TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()

                control_row = app.query_one("#control_row", Vertical)
                model_row = app.query_one("#model_row", Horizontal)
                locale_controls = app.query_one("#locale_controls", Horizontal)
                task_row = app.query_one("#task_row", Horizontal)
                task_label = app.query_one("#task_label", Static)
                task_input = app.query_one("#task_input", TextArea)
                root_agent_name_label = app.query_one("#root_agent_name_label", Static)
                root_agent_name_input = app.query_one("#root_agent_name_input", Input)
                buttons = app.query_one("#buttons", Horizontal)
                locale_en_button = app.query_one("#locale_en_button", Button)
                locale_zh_button = app.query_one("#locale_zh_button", Button)

                self.assertIs(model_row.parent, control_row)
                self.assertIs(task_row.parent, control_row)
                self.assertIs(task_input.parent, task_row)
                self.assertIs(buttons.parent, control_row)
                self.assertIs(locale_controls.parent, model_row)
                self.assertEqual(str(task_label.renderable), app.translator.text("task"))
                self.assertEqual(
                    str(root_agent_name_label.renderable),
                    app.translator.text("root_agent_name_label"),
                )
                self.assertEqual(
                    root_agent_name_input.placeholder,
                    app.translator.text("root_agent_name_placeholder"),
                )
                self.assertEqual(str(locale_en_button.label), "EN")
                self.assertEqual(str(locale_zh_button.label), "中文")

    async def test_agent_panels_update_in_place_and_preserve_scroll_position(self) -> None:
        try:
            from textual.containers import Vertical, VerticalScroll
            from textual.widgets import Static, TabbedContent

            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            state = AgentRuntimeView(
                id="agent/root-1",
                name="Root",
                role="root",
                instruction="\n".join(f"instruction {i}" for i in range(60)),
                summary="\n".join(f"summary {i}" for i in range(60)),
                step_count=1,
                step_order=[1],
                step_entries={1: [("thinking", f"entry {i}") for i in range(80)]},
            )
            app.agent_states = {state.id: state}
            app.stream_agent_order = [state.id]

            async with app.run_test() as pilot:
                await pilot.resize_terminal(120, 24)
                await pilot.pause()
                await app._refresh_agent_panels()
                await pilot.pause()

                key = app._widget_safe_id(state.id)
                tabs = app.query_one("#main_tabs", TabbedContent)
                tabs.active = "monitor_tab"
                await pilot.pause()
                summary_before = app.query_one(f"#wf-agent-{key}-summary-body", Static)
                overview_scroll = app.query_one("#overview_scroll", VerticalScroll)
                tabs.active = "agents_tab"
                await pilot.pause()
                step_before = app.query_one(f"#live-agent-{key}-step-1-body", Vertical)
                live_scroll = app.query_one("#live_scroll", VerticalScroll)

                tabs.active = "monitor_tab"
                await pilot.pause()
                overview_scroll.scroll_to(y=6, animate=False, immediate=True)
                tabs.active = "agents_tab"
                await pilot.pause()
                live_scroll.scroll_to(y=6, animate=False, immediate=True)
                await pilot.pause()
                overview_before_y = overview_scroll.scroll_y
                live_before_y = live_scroll.scroll_y

                state.summary = "\n".join(f"updated summary {i}" for i in range(60))
                state.step_entries[1].append(("reply", "new reply"))
                await app._refresh_agent_panels()
                await pilot.pause()

                tabs.active = "monitor_tab"
                await pilot.pause()
                summary_after = app.query_one(f"#wf-agent-{key}-summary-body", Static)
                tabs.active = "agents_tab"
                await pilot.pause()
                step_after = app.query_one(f"#live-agent-{key}-step-1-body", Vertical)
                self.assertIs(summary_before, summary_after)
                self.assertIs(step_before, step_after)
                self.assertEqual(overview_scroll.scroll_y, overview_before_y)
                self.assertEqual(live_scroll.scroll_y, live_before_y)

    async def test_live_in_place_refresh_does_not_override_new_scroll_position(self) -> None:
        try:
            from textual.containers import VerticalScroll
            from textual.widgets import TabbedContent

            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            state = AgentRuntimeView(
                id="agent/root-1",
                name="Root",
                role="root",
                step_count=1,
                step_order=[1],
                step_entries={1: [("reply", f"entry {i}") for i in range(160)]},
            )
            app.agent_states = {state.id: state}
            app.stream_agent_order = [state.id]
            app.live_step_collapsed_overrides[(state.id, 1)] = False

            async with app.run_test() as pilot:
                await pilot.resize_terminal(96, 18)
                await pilot.pause()
                tabs = app.query_one("#main_tabs", TabbedContent)
                tabs.active = "agents_tab"
                await app._refresh_agent_panels()
                await pilot.pause()
                live_scroll = app.query_one("#live_scroll", VerticalScroll)
                live_scroll.scroll_to(y=2, animate=False, immediate=True)
                await pilot.pause()

                original_update = app._update_live_widgets_in_place

                async def fake_update_live_widgets_in_place() -> bool:
                    live_scroll.scroll_to(y=9, animate=False, immediate=True)
                    return True

                app._update_live_widgets_in_place = fake_update_live_widgets_in_place  # type: ignore[method-assign]
                try:
                    await app._refresh_agent_panels()
                    await pilot.pause()
                finally:
                    app._update_live_widgets_in_place = original_update  # type: ignore[method-assign]

                self.assertEqual(live_scroll.scroll_y, 9)

    async def test_live_step_sync_updates_only_changed_entries(self) -> None:
        try:
            from textual.widgets import Static, TabbedContent

            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            state = AgentRuntimeView(
                id="agent/root-1",
                name="Root",
                role="root",
                step_count=1,
                step_order=[1],
                step_entries={1: [("reply", "entry 0"), ("reply", "entry 1"), ("reply", "entry 2")]},
            )
            app.agent_states = {state.id: state}
            app.stream_agent_order = [state.id]
            app.live_step_collapsed_overrides[(state.id, 1)] = False

            async with app.run_test() as pilot:
                await pilot.resize_terminal(120, 24)
                await pilot.pause()
                tabs = app.query_one("#main_tabs", TabbedContent)
                tabs.active = "agents_tab"
                await app._refresh_agent_panels()
                await pilot.pause()

                agent_key = app._widget_safe_id(state.id)
                unchanged_widget = app.query_one(
                    f"#live-agent-{agent_key}-step-1-entry-0-reply",
                    Static,
                )
                original_update = unchanged_widget.update

                def fail_if_called(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                    raise AssertionError("unchanged entry should not be re-rendered")

                unchanged_widget.update = fail_if_called  # type: ignore[method-assign]
                try:
                    state.step_entries[1][-1] = ("reply", "entry 2 updated")
                    await app._refresh_agent_panels()
                    await pilot.pause()
                finally:
                    unchanged_widget.update = original_update  # type: ignore[method-assign]

                updated_widget = app.query_one(
                    f"#live-agent-{agent_key}-step-1-entry-2-reply",
                    Static,
                )
                self.assertIn("entry 2 updated", str(updated_widget.renderable))

    async def test_task_input_expands_for_multiline_instructions(self) -> None:
        try:
            from textual.widgets import TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                task_input = app.query_one("#task_input", TextArea)
                initial_height = task_input.size.height
                task_input.load_text("\n".join(f"line {idx}" for idx in range(8)))
                await pilot.pause()
                self.assertGreater(task_input.size.height, initial_height)

    async def test_task_input_expands_as_soon_as_second_line_is_entered(self) -> None:
        try:
            from textual.widgets import TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                task_input = app.query_one("#task_input", TextArea)
                initial_height = task_input.size.height
                task_input.load_text("line 1\nline 2")
                await pilot.pause()
                self.assertGreater(task_input.size.height, initial_height)

    async def test_model_input_defaults_from_config_model(self) -> None:
        try:
            from textual.widgets import Input

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[llm.openrouter]
model = "openai/gpt-4.1-mini"
""".strip(),
                encoding="utf-8",
            )
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            async with app.run_test() as pilot:
                await pilot.pause()
                model_input = app.query_one("#model_input", Input)
                self.assertEqual(model_input.value, "openai/gpt-4.1-mini")

    async def test_start_run_uses_model_input_override(self) -> None:
        try:
            from textual.widgets import Input, TextArea

            from opencompany.models import RunSession, SessionStatus
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            app = OpenCompanyApp(project_dir=project_dir)

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.app_dir = project_dir
                    self.task = ""
                    self.model = ""
                    self.root_agent_name: str | None = None

                def subscribe(self, callback) -> None:  # type: ignore[no-untyped-def]
                    self.callback = callback

                async def run_task(
                    self,
                    task: str,
                    model: str | None = None,
                    root_agent_name: str | None = None,
                ) -> RunSession:
                    self.task = task
                    self.model = str(model or "")
                    self.root_agent_name = root_agent_name
                    return RunSession(
                        id="session-run-1",
                        project_dir=project_dir.resolve(),
                        task=task,
                        locale="en",
                        root_agent_id="agent-root",
                        status=SessionStatus.COMPLETED,
                    )

            fake = _FakeOrchestrator()
            app._create_orchestrator = lambda _project_dir, **_kwargs: fake  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#task_input", TextArea).load_text("demo task")
                model_input = app.query_one("#model_input", Input)
                model_input.value = "openai/gpt-4.1"
                root_agent_name_input = app.query_one("#root_agent_name_input", Input)
                root_agent_name_input.value = "Root Alpha"
                await app._start_run()
                if app.session_task is not None:
                    await app.session_task
                await pilot.pause()
                self.assertEqual(fake.task, "demo task")
                self.assertEqual(fake.model, "openai/gpt-4.1")
                self.assertEqual(fake.root_agent_name, "Root Alpha")

    async def test_start_run_with_configured_session_uses_run_task_in_session(self) -> None:
        try:
            from textual.widgets import Input, TextArea

            from opencompany.models import RunSession, SessionStatus
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-run-existing"
            (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
            app = OpenCompanyApp(project_dir=app_dir, session_id=session_id, app_dir=app_dir)

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.app_dir = app_dir
                    self.session_id = ""
                    self.task = ""
                    self.model = ""
                    self.root_agent_name: str | None = None
                    self.run_task_called = False

                def subscribe(self, callback) -> None:  # type: ignore[no-untyped-def]
                    self.callback = callback

                async def run_task_in_session(
                    self,
                    session_id: str,
                    task: str,
                    model: str | None = None,
                    root_agent_name: str | None = None,
                ) -> RunSession:
                    self.session_id = session_id
                    self.task = task
                    self.model = str(model or "")
                    self.root_agent_name = root_agent_name
                    return RunSession(
                        id=session_id,
                        project_dir=app_dir.resolve(),
                        task=task,
                        locale="en",
                        root_agent_id="agent-root-new",
                        status=SessionStatus.COMPLETED,
                    )

                async def run_task(
                    self,
                    task: str,
                    model: str | None = None,
                    root_agent_name: str | None = None,
                ) -> RunSession:
                    del task, model, root_agent_name
                    self.run_task_called = True
                    return RunSession(
                        id="unexpected",
                        project_dir=app_dir.resolve(),
                        task="unexpected",
                        locale="en",
                        root_agent_id="agent-root",
                        status=SessionStatus.COMPLETED,
                    )

            fake = _FakeOrchestrator()
            app._create_orchestrator = lambda _project_dir, **_kwargs: fake  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#task_input", TextArea).load_text("new root task")
                model_input = app.query_one("#model_input", Input)
                model_input.value = "openai/gpt-4.1"
                root_agent_name_input = app.query_one("#root_agent_name_input", Input)
                root_agent_name_input.value = "Root Beta"
                await app._start_run()
                if app.session_task is not None:
                    await app.session_task
                await pilot.pause()
                self.assertEqual(fake.session_id, session_id)
                self.assertEqual(fake.task, "new root task")
                self.assertEqual(fake.model, "openai/gpt-4.1")
                self.assertEqual(fake.root_agent_name, "Root Beta")
                self.assertFalse(fake.run_task_called)

    async def test_start_run_while_running_submits_live_root_run(self) -> None:
        try:
            from textual.widgets import Button, Input, TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-run-queue"
            (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
            app = OpenCompanyApp(project_dir=app_dir, session_id=session_id, app_dir=app_dir)

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.app_dir = app_dir
                    self.calls: list[tuple[str, str, str, str]] = []
                    self.root_agent_names: list[str | None] = []

                def subscribe(self, callback) -> None:  # type: ignore[no-untyped-def]
                    self.callback = callback

                def submit_run_in_active_session(
                    self,
                    session_id: str,
                    task: str,
                    *,
                    model: str | None = None,
                    root_agent_name: str | None = None,
                    source: str = "tui",
                ) -> dict[str, str]:
                    self.calls.append((session_id, task, str(model or ""), source))
                    self.root_agent_names.append(root_agent_name)
                    return {
                        "session_id": session_id,
                        "root_agent_id": "agent-root-live",
                        "task": task,
                        "model": str(model or ""),
                        "source": source,
                    }

            fake = _FakeOrchestrator()
            app._create_orchestrator = lambda _project_dir, **_kwargs: fake  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.pause()
                app.orchestrator = fake
                app.current_session_id = session_id
                app.session_task = asyncio.create_task(asyncio.sleep(30))
                app.query_one("#task_input", TextArea).load_text("root task live")
                model_input = app.query_one("#model_input", Input)
                model_input.value = "openai/gpt-4.1-mini"
                root_agent_name_input = app.query_one("#root_agent_name_input", Input)
                root_agent_name_input.value = "Root Live"
                await pilot.pause()
                run_button = app.query_one("#run_button", Button)
                self.assertFalse(run_button.disabled)
                await app._start_run()
                await pilot.pause()
                self.assertEqual(
                    fake.calls,
                    [
                        (
                            session_id,
                            "root task live",
                            "openai/gpt-4.1-mini",
                            "tui",
                        )
                    ],
                )
                self.assertEqual(fake.root_agent_names, ["Root Live"])
                self.assertEqual(app.current_task, "root task live")
                self.assertEqual(app.current_session_status, "running")
                self.assertIsNotNone(app.session_task)
                assert app.session_task is not None
                app.session_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await app.session_task

    async def test_locale_switch_uses_buttons(self) -> None:
        try:
            from textual.widgets import Button, TextArea, Input

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                self.assertEqual(
                    app.query_one("#task_input", TextArea).text.strip(),
                    app.translator.text("task_input_default_value"),
                )
                app.query_one("#locale_zh_button", Button).press()
                await pilot.pause()
                self.assertEqual(app.locale, "zh")
                self.assertEqual(
                    str(app.query_one("#run_button", Button).label),
                    app.translator.text("run"),
                )
                self.assertEqual(
                    str(app.query_one("#apply_button", Button).label),
                    app.translator.text("apply"),
                )
                self.assertEqual(
                    str(app.query_one("#task_input", TextArea).border_title),
                    app.translator.text("task_input"),
                )
                self.assertEqual(
                    app.query_one("#model_input", Input).placeholder,
                    app.translator.text("model_input_placeholder"),
                )
                self.assertEqual(
                    app.query_one("#task_input", TextArea).text.strip(),
                    app.translator.text("task_input_default_value"),
                )
                custom_task = "custom task text"
                app.query_one("#task_input", TextArea).load_text(custom_task)
                await pilot.pause()
                app.query_one("#locale_en_button", Button).press()
                await pilot.pause()
                self.assertEqual(
                    app.query_one("#task_input", TextArea).text.strip(),
                    custom_task,
                )

    def test_initial_locale_uses_config_default_locale(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[project]
default_locale = "zh"
""".strip(),
                encoding="utf-8",
            )
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            self.assertEqual(app.locale, "zh")

    async def test_config_save_updates_locale_in_current_tui_session(self) -> None:
        try:
            from textual.widgets import Button, TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            config_path = app_dir / "opencompany.toml"
            config_path.write_text(
                """
[project]
default_locale = "en"
""".strip(),
                encoding="utf-8",
            )
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            self.assertEqual(app.locale, "en")
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                editor = app.query_one("#config_editor", TextArea)
                editor.load_text('[project]\ndefault_locale = "zh"\n')
                await pilot.pause()
                app.query_one("#config_save_button", Button).press()
                await pilot.pause()
                self.assertEqual(app.locale, "zh")

    async def test_config_save_updates_sessions_root_in_current_tui_process(self) -> None:
        try:
            from textual.widgets import Button, TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[project]
data_dir = ".opencompany"
""".strip(),
                encoding="utf-8",
            )
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                editor = app.query_one("#config_editor", TextArea)
                editor.load_text('[project]\ndata_dir = ".opencompany-next"\n')
                await pilot.pause()
                app.query_one("#config_save_button", Button).press()
                await pilot.pause()
                self.assertEqual(
                    app._sessions_root_dir(),
                    (app_dir / ".opencompany-next" / "sessions").resolve(),
                )

    async def test_config_panel_shows_apply_behavior_notes(self) -> None:
        try:
            from textual.widgets import Button, Static

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                notes = app.query_one("#config_effect_notes", Static)
                rendered = str(notes.renderable)
                self.assertIn(app.translator.text("config_effect_next_session"), rendered)
                self.assertIn(app.translator.text("config_effect_running_session"), rendered)

                app.query_one("#locale_zh_button", Button).press()
                await pilot.pause()
                rendered = str(app.query_one("#config_effect_notes", Static).renderable)
                self.assertIn(app.translator.text("config_effect_next_session"), rendered)
                self.assertIn(app.translator.text("config_effect_running_session"), rendered)

    async def test_tab_titles_are_visible_and_localized(self) -> None:
        try:
            from textual.widgets import Button, TabbedContent

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                tabs = app.query_one("#main_tabs", TabbedContent)
                self.assertEqual(
                    str(tabs.get_tab("monitor_tab").label_text),
                    app.translator.text("monitor_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("agents_tab").label_text),
                    app.translator.text("agents_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("tool_runs_tab").label_text),
                    app.translator.text("tool_runs_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("steer_runs_tab").label_text),
                    app.translator.text("steer_runs_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("diff_tab").label_text),
                    app.translator.text("diff_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("config_tab").label_text),
                    app.translator.text("config_tab_title"),
                )

                app.query_one("#locale_zh_button", Button).press()
                await pilot.pause()
                self.assertEqual(
                    str(tabs.get_tab("monitor_tab").label_text),
                    app.translator.text("monitor_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("agents_tab").label_text),
                    app.translator.text("agents_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("tool_runs_tab").label_text),
                    app.translator.text("tool_runs_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("steer_runs_tab").label_text),
                    app.translator.text("steer_runs_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("diff_tab").label_text),
                    app.translator.text("diff_tab_title"),
                )
                self.assertEqual(
                    str(tabs.get_tab("config_tab").label_text),
                    app.translator.text("config_tab_title"),
                )

    async def test_config_tab_save_updates_opencompany_toml(self) -> None:
        try:
            from textual.widgets import Button, TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            config_path = app_dir / "opencompany.toml"
            config_path.write_text('[project]\nname = "Before"\n', encoding="utf-8")
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                editor = app.query_one("#config_editor", TextArea)
                editor.load_text('[project]\nname = "After"\n')
                await pilot.pause()
                app.query_one("#config_save_button", Button).press()
                await pilot.pause()

            self.assertIn('name = "After"', config_path.read_text(encoding="utf-8"))

    async def test_config_tab_pulls_external_file_updates(self) -> None:
        try:
            from textual.widgets import Static, TextArea

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            config_path = app_dir / "opencompany.toml"
            config_path.write_text('[project]\nname = "Before"\n', encoding="utf-8")
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                config_path.write_text('[project]\nname = "External"\n', encoding="utf-8")
                await asyncio.sleep(0.02)
                app._poll_external_config_changes()
                await pilot.pause()
                editor = app.query_one("#config_editor", TextArea)
                status = app.query_one("#config_sync_status", Static)
                self.assertIn('name = "External"', editor.text)
                self.assertIn(app.translator.text("config_reloaded_external"), str(status.renderable))

    async def test_modal_buttons_do_not_trigger_root_controls(self) -> None:
        try:
            from textual.widgets import Button

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                app.screen.query_one("#cancel_launch_config", Button).press()
                await pilot.pause()
            self.assertEqual(app.status_message, app.translator.text("ready"))

    async def test_launch_config_uses_scroll_container(self) -> None:
        try:
            from textual.containers import VerticalScroll
            from textual.widgets import Button

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(100, 24)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen.query_one("#launch_config_scroll"), VerticalScroll)

    async def test_project_selection_applies_immediately_from_setup(self) -> None:
        try:
            from textual.widgets import Button

            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as original_dir, TemporaryDirectory() as selected_dir:
            app = OpenCompanyApp(project_dir=Path(original_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                self.assertEqual(screen.mode, "project")
                screen._on_project_dir_selected(Path(selected_dir))
                await pilot.pause()
            self.assertEqual(app.project_dir, Path(selected_dir).resolve())
            self.assertIsNone(app.configured_resume_session_id)

    async def test_reconfigure_new_session_allows_workspace_mode_switch_from_locked_resume(
        self,
    ) -> None:
        try:
            from textual.widgets import Button

            from opencompany.models import WorkspaceMode
            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as root_dir, TemporaryDirectory() as selected_dir:
            app = OpenCompanyApp(project_dir=Path(root_dir))
            app.configured_resume_session_id = "session-loaded"
            app.session_mode = WorkspaceMode.DIRECT
            app.session_mode_locked = True
            selected_path = Path(selected_dir).resolve()

            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)

                direct_button = screen.query_one("#launch_workspace_mode_direct_button", Button)
                staged_button = screen.query_one("#launch_workspace_mode_staged_button", Button)
                self.assertTrue(direct_button.disabled)
                self.assertTrue(staged_button.disabled)

                screen.query_one("#launch_mode_project_button", Button).press()
                await pilot.pause()
                self.assertFalse(direct_button.disabled)
                self.assertFalse(staged_button.disabled)

                staged_button.press()
                await pilot.pause()
                self.assertEqual(screen.session_mode, WorkspaceMode.STAGED)

                screen._on_project_dir_selected(selected_path)
                await pilot.pause()

            self.assertEqual(app.project_dir, selected_path)
            self.assertIsNone(app.configured_resume_session_id)
            self.assertEqual(app.session_mode, WorkspaceMode.STAGED)
            self.assertFalse(app.session_mode_locked)

    async def test_new_session_mode_status_shows_selected_mode_description(self) -> None:
        try:
            from textual.widgets import Button, Static

            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)

                mode_status = screen.query_one("#launch_workspace_mode_status", Static)
                self.assertIn(
                    app.translator.text("workspace_mode_direct_desc"),
                    str(mode_status.renderable),
                )

                screen.query_one("#launch_workspace_mode_staged_button", Button).press()
                await pilot.pause()
                self.assertIn(
                    app.translator.text("workspace_mode_staged_desc"),
                    str(mode_status.renderable),
                )

    def test_reconfigure_defaults_sandbox_backend_to_config_each_time(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[sandbox]
backend = "anthropic"
""".strip(),
                encoding="utf-8",
            )
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            app.sandbox_backend = "none"
            captured: dict[str, object] = {}

            def _capture_push_screen(screen, callback):  # type: ignore[no-untyped-def]
                captured["screen"] = screen
                captured["callback"] = callback

            app.push_screen = _capture_push_screen  # type: ignore[method-assign]
            app._open_launch_config()
            screen = captured.get("screen")
            self.assertIsInstance(screen, SessionConfigScreen)
            assert isinstance(screen, SessionConfigScreen)
            self.assertEqual(screen.sandbox_backend, "anthropic")

    async def test_apply_launch_config_status_includes_selected_sandbox_backend(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp, SessionLaunchConfig
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir).resolve()
            app = OpenCompanyApp(project_dir=project_dir, locale="en")
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app._apply_launch_config(
                    SessionLaunchConfig.create(
                        project_dir=project_dir,
                        session_id=None,
                        sandbox_backend="none",
                    )
                )
                await pilot.pause()
            self.assertEqual(app.sandbox_backend, "none")
            self.assertIn(app.translator.text("sandbox_backend_label"), app.status_message)
            self.assertIn("none", app.status_message.lower())

    async def test_remote_setup_validate_and_create_saves_launch_config_on_success(self) -> None:
        try:
            from textual.widgets import Button

            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            captured: dict[str, object] = {}

            async def _fake_validate_remote_workspace_config(
                *,
                remote,
                remote_password=None,
                session_mode=None,
                sandbox_backend=None,
            ):  # type: ignore[no-untyped-def]
                captured["remote"] = remote
                captured["remote_password"] = remote_password
                captured["session_mode"] = session_mode
                captured["sandbox_backend"] = sandbox_backend
                return {"ok": True}

            app.validate_remote_workspace_config = _fake_validate_remote_workspace_config  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_sandbox_backend_none_button", Button).press()
                await pilot.pause()
                screen.query_one("#launch_workspace_source_remote_button", Button).press()
                await pilot.pause()
                screen.remote_target = "demo@example.com:22"
                screen.remote_dir = "/home/demo/workspace"
                screen.remote_auth_mode = "key"
                screen.remote_key_path = "~/.ssh/id_ed25519"
                screen._render_mode()
                save_button = screen.query_one("#save_launch_config", Button)
                self.assertEqual(str(save_button.label), app.translator.text("remote_validate_button"))
                save_button.press()
                await pilot.pause()
                await pilot.pause()

            self.assertIsNotNone(app.remote_config)
            assert app.remote_config is not None
            self.assertEqual(app.remote_config.ssh_target, "demo@example.com:22")
            self.assertEqual(app.remote_config.remote_dir, "/home/demo/workspace")
            self.assertEqual(captured.get("remote_password"), "")
            self.assertEqual(captured.get("session_mode"), "direct")
            self.assertEqual(captured.get("sandbox_backend"), "none")

    async def test_remote_setup_validate_and_create_surfaces_validation_failure(self) -> None:
        try:
            from textual.widgets import Button, Static

            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            captured: dict[str, object] = {}

            async def _fake_validate_remote_workspace_config(
                *,
                remote,
                remote_password=None,
                session_mode=None,
                sandbox_backend=None,
            ):  # type: ignore[no-untyped-def]
                del remote, remote_password, session_mode, sandbox_backend
                captured["called"] = True
                return {"ok": False, "stderr": "ssh handshake timeout"}

            app.validate_remote_workspace_config = _fake_validate_remote_workspace_config  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_workspace_source_remote_button", Button).press()
                await pilot.pause()
                screen.remote_target = "demo@example.com:22"
                screen.remote_dir = "/home/demo/workspace"
                screen.remote_auth_mode = "key"
                screen.remote_key_path = "~/.ssh/id_ed25519"
                screen._render_mode()
                screen.query_one("#save_launch_config", Button).press()
                error_text = ""
                for _ in range(10):
                    await pilot.pause()
                    error_text = str(screen.query_one("#launch_remote_validate_status", Static).renderable)
                    if error_text or captured.get("called"):
                        break
                self.assertIs(app.screen, screen)
                self.assertTrue(captured.get("called", False))
                self.assertIsNone(app.remote_config)
                self.assertIn(app.translator.text("remote_validate_failed"), error_text)
                self.assertIn("ssh handshake timeout", error_text)

    async def test_remote_setup_switch_auth_after_failed_validation_with_large_output(self) -> None:
        try:
            from textual.pilot import WaitForScreenTimeout
            from textual.widgets import Button, Input

            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            long_error = "\n".join(
                f"[opencompany][remote-setup] line {index} {'x' * 120}" for index in range(240)
            )

            async def _fake_validate_remote_workspace_config(
                *,
                remote,
                remote_password=None,
                session_mode=None,
                sandbox_backend=None,
            ):  # type: ignore[no-untyped-def]
                del remote, remote_password, session_mode, sandbox_backend
                return {"ok": False, "stderr": long_error}

            app.validate_remote_workspace_config = _fake_validate_remote_workspace_config  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.resize_terminal(120, 30)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_workspace_source_remote_button", Button).press()
                await pilot.pause()
                screen.query_one("#launch_remote_target_input", Input).value = "demo@example.com:22"
                screen.query_one("#launch_remote_dir_input", Input).value = "/home/demo/workspace"
                screen.query_one("#launch_remote_key_input", Input).value = "~/.ssh/id_ed25519"
                await pilot.pause()
                screen.query_one("#save_launch_config", Button).press()
                await pilot.pause()
                await pilot.pause()
                screen.query_one("#launch_remote_auth_password_button", Button).press()
                try:
                    await pilot.pause()
                except WaitForScreenTimeout as exc:
                    self.fail(f"switch auth after failed validation should not hang: {exc}")

    async def test_resume_setup_clears_project_directory(self) -> None:
        try:
            from textual.widgets import Button

            from opencompany.tui.app import OpenCompanyApp, PathPickerScreen, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as project_dir, TemporaryDirectory() as app_dir:
            sessions_dir = Path(app_dir) / ".opencompany" / "sessions"
            session_dir = sessions_dir / "session-123"
            session_dir.mkdir(parents=True, exist_ok=True)

            app = OpenCompanyApp(project_dir=Path(project_dir), app_dir=Path(app_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_mode_resume_button", Button).press()
                await pilot.pause()
                screen.query_one("#choose_session_dir", Button).press()
                await pilot.pause()

                picker = app.screen
                self.assertIsInstance(picker, PathPickerScreen)
                self.assertEqual(picker.root_path, sessions_dir.resolve())
                picker.selected_path = session_dir.resolve()
                picker.query_one("#path_picker_confirm", Button).press()
                await pilot.pause()
                await pilot.pause()
            self.assertIsNone(app.project_dir)
            self.assertEqual(app.configured_resume_session_id, "session-123")

    async def test_resume_setup_hides_workspace_mode_text(self) -> None:
        try:
            from textual.widgets import Button, Static

            from opencompany.tui.app import OpenCompanyApp, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as project_dir:
            app = OpenCompanyApp(project_dir=Path(project_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_mode_resume_button", Button).press()
                await pilot.pause()
                session_text = str(screen.query_one("#launch_session_value", Static).renderable)
                self.assertIn(app.translator.text("selected_session_dir"), session_text)
                self.assertNotIn(app.translator.text("workspace_mode_label"), session_text)

    async def test_resume_session_picker_sorts_by_recent_time_and_shows_timestamp(self) -> None:
        try:
            from textual.widgets import Button

            from opencompany.tui.app import OpenCompanyApp, PathPickerScreen, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as project_dir, TemporaryDirectory() as app_dir:
            sessions_dir = Path(app_dir) / ".opencompany" / "sessions"
            older_dir = sessions_dir / "session-older"
            newer_dir = sessions_dir / "session-newer"
            older_dir.mkdir(parents=True, exist_ok=True)
            newer_dir.mkdir(parents=True, exist_ok=True)
            older_events = older_dir / "events.jsonl"
            newer_events = newer_dir / "events.jsonl"
            older_events.write_text("{}", encoding="utf-8")
            newer_events.write_text("{}", encoding="utf-8")
            os.utime(older_events, (1_700_000_000, 1_700_000_000))
            os.utime(newer_events, (1_710_000_000, 1_710_000_000))

            app = OpenCompanyApp(project_dir=Path(project_dir), app_dir=Path(app_dir))
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_mode_resume_button", Button).press()
                await pilot.pause()
                screen.query_one("#choose_session_dir", Button).press()
                await pilot.pause()

                picker = app.screen
                self.assertIsInstance(picker, PathPickerScreen)
                self.assertTrue(picker.session_picker)
                ordered_ids = [entry[0].name for entry in picker._session_entries]
                self.assertEqual(ordered_ids[:2], ["session-newer", "session-older"])
                first_label = picker._session_entries[0][1]
                self.assertIn(app.translator.text("session_picker_updated_at"), first_label)
                self.assertRegex(first_label, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    async def test_resume_setup_validates_remote_session_before_loading(self) -> None:
        try:
            from textual.widgets import Button

            from opencompany.models import RemoteSessionConfig
            from opencompany.remote import save_remote_session_config
            from opencompany.tui.app import OpenCompanyApp, PathPickerScreen, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as project_dir, TemporaryDirectory() as app_dir:
            sessions_dir = Path(app_dir) / ".opencompany" / "sessions"
            session_dir = sessions_dir / "session-remote"
            session_dir.mkdir(parents=True, exist_ok=True)
            save_remote_session_config(
                session_dir,
                RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )

            app = OpenCompanyApp(project_dir=Path(project_dir), app_dir=Path(app_dir))
            captured: dict[str, object] = {}

            async def _fake_validate_remote_workspace_config(
                *,
                remote,
                remote_password=None,
                session_mode=None,
                sandbox_backend=None,
            ):  # type: ignore[no-untyped-def]
                captured["remote"] = remote
                captured["remote_password"] = remote_password
                captured["session_mode"] = session_mode
                captured["sandbox_backend"] = sandbox_backend
                return {"ok": True}

            app.validate_remote_workspace_config = _fake_validate_remote_workspace_config  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_mode_resume_button", Button).press()
                await pilot.pause()
                screen.query_one("#choose_session_dir", Button).press()
                await pilot.pause()

                picker = app.screen
                self.assertIsInstance(picker, PathPickerScreen)
                picker.selected_path = session_dir.resolve()
                picker.query_one("#path_picker_confirm", Button).press()
                await pilot.pause()
                await pilot.pause()

            self.assertEqual(app.configured_resume_session_id, "session-remote")
            self.assertEqual(str(captured.get("session_mode", "")), "direct")
            self.assertEqual(str(captured.get("sandbox_backend", "")), "anthropic")
            self.assertEqual(
                str((captured.get("remote") or {}).get("ssh_target", "")),
                "demo@example.com:22",
            )

    async def test_resume_setup_surfaces_remote_validation_failure_status(self) -> None:
        try:
            from textual.widgets import Button, Static

            from opencompany.models import RemoteSessionConfig
            from opencompany.remote import save_remote_session_config
            from opencompany.tui.app import OpenCompanyApp, PathPickerScreen, SessionConfigScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as project_dir, TemporaryDirectory() as app_dir:
            sessions_dir = Path(app_dir) / ".opencompany" / "sessions"
            session_dir = sessions_dir / "session-remote"
            session_dir.mkdir(parents=True, exist_ok=True)
            save_remote_session_config(
                session_dir,
                RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )

            app = OpenCompanyApp(project_dir=Path(project_dir), app_dir=Path(app_dir))

            async def _fake_validate_remote_workspace_config(
                *,
                remote,
                remote_password=None,
                session_mode=None,
                sandbox_backend=None,
            ):  # type: ignore[no-untyped-def]
                del remote, remote_password, session_mode, sandbox_backend
                return {"ok": False, "stderr": "Node.js remains too old after system package install (12)"}

            app.validate_remote_workspace_config = _fake_validate_remote_workspace_config  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.resize_terminal(140, 40)
                app.query_one("#reconfigure_button", Button).press()
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SessionConfigScreen)
                screen.query_one("#launch_mode_resume_button", Button).press()
                await pilot.pause()
                screen.query_one("#choose_session_dir", Button).press()
                await pilot.pause()

                picker = app.screen
                self.assertIsInstance(picker, PathPickerScreen)
                picker.selected_path = session_dir.resolve()
                picker.query_one("#path_picker_confirm", Button).press()
                status_text = ""
                for _ in range(12):
                    await pilot.pause()
                    current = app.screen
                    if not isinstance(current, SessionConfigScreen):
                        continue
                    status_text = str(
                        current.query_one("#launch_resume_validate_status", Static).renderable
                    )
                    if status_text:
                        break

            self.assertIsNone(app.configured_resume_session_id)
            self.assertIn(app.translator.text("remote_validate_failed"), status_text)
            self.assertIn("Node.js remains too old", status_text)

    async def test_mount_with_resume_session_restores_monitor_panels_from_history(self) -> None:
        try:
            import opencompany.tui.app as tui_app_module

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        records = [
            {
                "timestamp": "2026-03-09T12:00:00Z",
                "session_id": "session-123",
                "agent_id": None,
                "parent_agent_id": None,
                "event_type": "session_started",
                "phase": "runtime",
                "payload": {
                    "task": "Resume demo",
                    "session_status": "running",
                    "root_agent_name": "Root",
                    "root_agent_role": "root",
                },
                "workspace_id": None,
                "checkpoint_seq": 1,
            },
            {
                "timestamp": "2026-03-09T12:00:01Z",
                "session_id": "session-123",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "event_type": "agent_spawned",
                "phase": "runtime",
                "payload": {
                    "agent_name": "Root",
                    "agent_role": "root",
                    "instruction": "Inspect repository state",
                    "agent_status": "pending",
                },
                "workspace_id": "workspace-root",
                "checkpoint_seq": 1,
            },
            {
                "timestamp": "2026-03-09T12:00:02Z",
                "session_id": "session-123",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "event_type": "agent_prompt",
                "phase": "runtime",
                "payload": {
                    "agent_name": "Root",
                    "agent_role": "root",
                    "step_count": 1,
                    "agent_status": "running",
                },
                "workspace_id": "workspace-root",
                "checkpoint_seq": 1,
            },
            {
                "timestamp": "2026-03-09T12:00:03Z",
                "session_id": "session-123",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "event_type": "llm_token",
                "phase": "runtime",
                "payload": {
                    "agent_name": "Root",
                    "step_count": 1,
                    "token": "Found the relevant files.",
                },
                "workspace_id": "workspace-root",
                "checkpoint_seq": 1,
            },
        ]

        class _HistoryOrchestrator:
            def __init__(
                self,
                project_dir: Path,
                locale: str | None = None,
                app_dir: Path | None = None,
            ) -> None:
                del project_dir, locale
                self.app_dir = (app_dir or Path.cwd()).resolve()

            def load_session_events(self, session_id: str) -> list[dict[str, object]]:
                self.loaded_session_id = session_id
                return list(records)

            def project_sync_status(self, session_id: str) -> dict[str, object]:
                del session_id
                return {"status": "none"}

            def project_sync_preview(
                self,
                session_id: str,
                *,
                max_files: int = 80,
                max_chars: int = 200_000,
            ) -> dict[str, object]:
                del session_id, max_files, max_chars
                return {
                    "status": "none",
                    "project_dir": str(Path.cwd()),
                    "files": [],
                    "added_count": 0,
                    "modified_count": 0,
                    "deleted_count": 0,
                    "truncated": False,
                }

        original_orchestrator = tui_app_module.Orchestrator
        tui_app_module.Orchestrator = _HistoryOrchestrator
        try:
            app = OpenCompanyApp(project_dir=None, session_id="session-123")
            async with app.run_test() as pilot:
                await pilot.resize_terminal(120, 30)
                await pilot.pause()
                await pilot.pause()

                self.assertEqual(app.current_session_id, "session-123")
                self.assertEqual(app.current_task, "Resume demo")
                self.assertIn("agent-root", app.agent_states)
                self.assertGreater(len(app.query_one("#overview_content").children), 0)
                self.assertGreater(len(app.query_one("#live_content").children), 0)
                self.assertGreater(len(app.query_one("#activity_log").lines), 0)
        finally:
            tui_app_module.Orchestrator = original_orchestrator

    def test_response_stream_entries_extract_human_readable_sections(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        entries = app._response_stream_entries(
            '{"thinking":"inspect repo","actions":[{"type":"shell","command":"rg TODO"},{"type": "finish","summary":"finished","next_recommendation":"verify tests"}]}'
        )
        self.assertEqual(entries[0], ("thinking", "inspect repo"))
        self.assertIn(("tool_call", "shell: rg TODO"), entries)
        self.assertIn(("tool_call", "finish(status=-)"), entries)

    def test_partial_response_entries_stream_before_completion(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        entries = app._streaming_entries_from_partial_response('{"thinking":"inspect rep')
        self.assertEqual(entries, [("thinking", "inspect rep")])

    def test_streaming_llm_entries_include_reasoning_buffer(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        entries = app._streaming_llm_entries('{"summary":"done', reasoning="inspect repo")
        self.assertEqual(entries, [("thinking", "inspect repo"), ("reply", "done")])

    def test_reasoning_stream_survives_tool_response_render(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]
        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {}},
        )
        app._update_stream_for_event(
            state,
            {"event_type": "llm_reasoning", "payload": {"token": "inspect repo"}},
        )
        self.assertEqual(state.stream_entries, [("thinking_preview", "inspect repo")])

        app._update_stream_for_event(
            state,
            {
                "event_type": "agent_response",
                "payload": {
                    "content": "",
                    "reasoning": "inspect repo",
                    "actions": [{"type": "shell", "command": "rg TODO"}],
                },
            },
        )
        self.assertEqual(state.stream_entries, [("thinking_preview", "inspect repo")])

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {
                    "content": '{"thinking":"inspect repo","actions":[{"type":"shell","command":"rg TODO"}]}',
                },
            }
        )
        self.assertEqual(
            state.stream_entries,
            [("thinking", "inspect repo"), ("tool_call", "shell: rg TODO")],
        )

        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call_started",
                "payload": {"action": {"type": "shell", "command": "rg TODO"}},
            },
        )
        self.assertEqual(
            state.stream_entries,
            [
                ("thinking", "inspect repo"),
                ("tool_call", "shell: rg TODO"),
                ("tool_call_extra", "shell: rg TODO"),
            ],
        )

    def test_agent_response_replaces_equivalent_streamed_thinking_entry(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        class _PatchedApp(OpenCompanyApp):
            debug = False

        app = _PatchedApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]
        streamed_reasoning = "I should inspect the workspace first and then spawn two workers.\n"
        final_reasoning = "I should inspect the workspace first and then spawn two workers."

        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {"step_count": 1}},
        )
        app._update_stream_for_event(
            state,
            {"event_type": "llm_reasoning", "payload": {"step_count": 1, "token": streamed_reasoning}},
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "agent_response",
                "payload": {
                    "step_count": 1,
                    "content": "",
                    "reasoning": final_reasoning,
                    "actions": [{"type": "list_tool_runs"}],
                },
            },
        )
        self.assertEqual(
            state.stream_entries,
            [("thinking_preview", streamed_reasoning)],
        )

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {
                    "content": '{"actions":[{"type":"list_tool_runs"}]}',
                    "reasoning": final_reasoning,
                },
            }
        )

        self.assertEqual(
            state.stream_entries,
            [("thinking", final_reasoning), ("tool_call", "list_tool_runs(status=-)")],
        )

    def test_agent_response_does_not_duplicate_near_identical_long_thinking(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        class _PatchedApp(OpenCompanyApp):
            debug = False

        app = _PatchedApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]
        streamed_reasoning = (
            "The workspace is empty and I need to spawn two worker agents to build "
            "C and Python versions of the game."
        )
        final_reasoning = (
            "The workspace is empty and I need to spawn two worker agents to build "
            "C and Python version of the game."
        )

        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {"step_count": 1}},
        )
        app._update_stream_for_event(
            state,
            {"event_type": "llm_reasoning", "payload": {"step_count": 1, "token": streamed_reasoning}},
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "agent_response",
                "payload": {
                    "step_count": 1,
                    "content": "",
                    "reasoning": final_reasoning,
                    "actions": [{"type": "list_tool_runs"}],
                },
            },
        )
        self.assertEqual(
            state.stream_entries,
            [("thinking_preview", streamed_reasoning)],
        )

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:02Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {
                    "content": '{"actions":[{"type":"list_tool_runs"}]}',
                    "reasoning": final_reasoning,
                },
            }
        )

        self.assertEqual(
            [entry for entry in state.stream_entries if entry[0] == "thinking"],
            [("thinking", final_reasoning)],
        )

    def test_tool_call_started_does_not_duplicate_planned_action_entry(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]
        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {"step_count": 1}},
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "agent_response",
                "payload": {
                    "content": "",
                    "actions": [{"type": "shell", "command": "cp demo.py /tmp/demo.py"}],
                },
            },
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:03Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {
                    "content": '{"actions":[{"type":"shell","command":"cp demo.py /tmp/demo.py"}]}',
                },
            }
        )
        self.assertEqual(
            state.stream_entries,
            [("tool_call", "shell: cp demo.py /tmp/demo.py")],
        )

        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call_started",
                "payload": {
                    "step_count": 1,
                    "action": {"type": "shell", "command": "cp demo.py /tmp/demo.py"},
                },
            },
        )
        self.assertEqual(
            [
                text
                for kind, text in state.stream_entries
                if kind == "tool_call_extra" and text == "shell: cp demo.py /tmp/demo.py"
            ],
            ["shell: cp demo.py /tmp/demo.py"],
        )

    def test_tool_call_started_keeps_tool_call_id_and_shell_command(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call_started",
                "payload": {
                    "step_count": 1,
                    "action": {
                        "type": "shell",
                        "_tool_call_id": "call-shell-1",
                        "command": "cp demo.py /tmp/demo.py",
                    },
                },
            },
        )

        self.assertEqual(
            state.stream_entries,
            [("tool_call_extra", "shell (tool_call_id=call-shell-1): cp demo.py /tmp/demo.py")],
        )

    def test_tool_call_started_is_not_duplicated_after_tool_call_trace_entry(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]
        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {"step_count": 1}},
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "agent_response",
                "payload": {
                    "step_count": 1,
                    "content": "",
                    "actions": [{"type": "list_agent_runs", "_tool_call_id": "call-tree-1"}],
                    "tool_calls": [
                        {
                            "id": "call-tree-1",
                            "name": "list_agent_runs",
                            "arguments": {},
                        }
                    ],
                },
            },
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:04Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {
                    "content": "{}",
                    "tool_calls": [
                        {
                            "id": "call-tree-1",
                            "function": {
                                "name": "list_agent_runs",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            }
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call_started",
                "payload": {
                    "step_count": 1,
                    "action": {"type": "list_agent_runs", "_tool_call_id": "call-tree-1"},
                },
            },
        )

        expected = "list_agent_runs (tool_call_id=call-tree-1)()"
        expected_trace = "tool_call_id=call-tree-1\nname=list_agent_runs\narguments: {}"
        self.assertEqual(
            [text for kind, text in state.stream_entries if kind == "tool_call" and text == expected_trace],
            [expected_trace],
        )
        self.assertEqual(
            [text for kind, text in state.stream_entries if kind == "multiagent_call_extra" and text == expected],
            [expected],
        )

    def test_response_stream_entries_keep_reasoning_when_actions_are_supplied(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        entries = app._response_stream_entries(
            '{"thinking":"inspect repo","actions":[{"type": "finish","summary":"finished"}]}',
            actions=[{"type": "finish", "summary": "finished"}],
        )
        self.assertEqual(entries[0], ("thinking", "inspect repo"))
        self.assertIn(("tool_call", "finish(status=-)"), entries)

    def test_tool_call_trace_entries_pretty_print_nested_arguments_without_truncation(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        nested_value = "x" * 256
        entries = app._tool_call_trace_entries(
            [
                {
                    "id": "call-nested",
                    "name": "custom_tool",
                    "arguments": {"path": ".", "meta": {"note": nested_value}},
                }
            ]
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], "tool_call")
        self.assertIn("tool_call_id=call-nested", entries[0][1])
        self.assertIn("name=custom_tool", entries[0][1])
        self.assertIn('"meta": {', entries[0][1])
        self.assertIn(nested_value, entries[0][1])

    def test_tool_call_result_entries_pretty_print_nested_result_without_truncation(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        nested_value = "y" * 256
        entries = app._tool_call_result_entries(
            {
                "action": {"type": "custom_tool"},
                "result": {"tree": {"root": {"leaf": nested_value}}},
            }
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], "tool_return")
        self.assertIn("custom_tool result:", entries[0][1])
        self.assertIn('"tree": {', entries[0][1])
        self.assertIn(nested_value, entries[0][1])

    def test_agent_response_prefers_reasoning_over_reasoning_details(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:05Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {
                    "content": '{"actions":[{"type":"list_agent_runs","_tool_call_id":"call-xyz"}]}',
                    "reasoning": "Need inspect tree first.",
                    "reasoning_details": [{"type": "reasoning.text", "text": "Need inspect tree first."}],
                    "tool_calls": [
                        {
                            "id": "call-xyz",
                            "function": {
                                "name": "list_agent_runs",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            }
        )

        self.assertIn(("thinking", "Need inspect tree first."), state.stream_entries)
        self.assertNotIn(
            ("thinking", "reasoning_details[1]: [reasoning.text] Need inspect tree first."),
            state.stream_entries,
        )
        self.assertIn(("multiagent_call", "list_agent_runs (tool_call_id=call-xyz)()"), state.stream_entries)
        self.assertIn(
            ("tool_call", "tool_call_id=call-xyz\nname=list_agent_runs\narguments: {}"),
            state.stream_entries,
        )

    def test_apply_message_record_assigns_non_assistant_to_previous_step(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]
        state.next_message_step = 3

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "user",
                "message": {
                    "content": "fallback control",
                },
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "role": "tool",
                "message": {
                    "tool_call_id": "call-1",
                    "content": '{"ok": true}',
                },
            }
        )
        self.assertIn(("user_message", "fallback control"), state.step_entries.get(2, []))
        self.assertTrue(
            any(
                kind == "tool_message" and "tool_call_id=call-1" in text
                for kind, text in state.step_entries.get(2, [])
            )
        )
        self.assertEqual(state.next_message_step, 3)

    def test_apply_message_records_group_first_user_assistant_tool_messages_into_same_step(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "user",
                "message": {"content": "Inspect repo"},
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "role": "assistant",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-tree-1",
                            "function": {
                                "name": "list_agent_runs",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:02Z",
                "agent_id": state.id,
                "message_index": 2,
                "role": "tool",
                "message": {
                    "tool_call_id": "call-tree-1",
                    "content": '{"files": 2}',
                },
            }
        )

        step_entries = state.step_entries.get(1, [])
        self.assertIn(("user_message", "Inspect repo"), step_entries)
        self.assertIn(
            ("tool_call", "tool_call_id=call-tree-1\nname=list_agent_runs\narguments: {}"),
            step_entries,
        )
        self.assertTrue(
            any(kind == "tool_message" and "tool_call_id=call-tree-1" in text for kind, text in step_entries)
        )
        self.assertEqual(state.step_order, [1])

    def test_apply_message_record_keeps_tool_call_entries_when_assistant_content_empty(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-shell-1",
                            "function": {
                                "name": "shell",
                                "arguments": '{"command":"pwd"}',
                            },
                        }
                    ],
                },
            }
        )

        self.assertIn(
            ("tool_call", 'tool_call_id=call-shell-1\nname=shell\narguments:\n{\n  "command": "pwd"\n}'),
            state.step_entries.get(1, []),
        )

    def test_apply_message_record_respects_explicit_step_count(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "step_count": 5,
                "role": "user",
                "message": {"content": "forced step"},
            }
        )
        self.assertIn(("user_message", "forced step"), state.step_entries.get(5, []))
        self.assertEqual(state.step_count, 5)
        self.assertEqual(state.next_message_step, 1)

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "step_count": 6,
                "role": "assistant",
                "message": {"content": '{"actions":[{"type":"finish","summary":"done"}]}'},
            }
        )
        self.assertIn(("tool_call", "finish(status=-)"), state.step_entries.get(6, []))
        self.assertEqual(state.step_count, 6)
        self.assertEqual(state.next_message_step, 7)

    def test_apply_message_record_skips_internal_control_messages(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "step_count": 34,
                "role": "assistant",
                "internal": True,
                "message": {
                    "content": "",
                    "reasoning": "internal compress request",
                    "tool_calls": [
                        {
                            "id": "call-compress-1",
                            "function": {
                                "name": "compress_context",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "step_count": 39,
                "role": "assistant",
                "message": {
                    "content": '{"actions":[{"type":"finish","summary":"done"}]}',
                },
            }
        )

        self.assertNotIn(34, state.step_entries)
        self.assertIn(39, state.step_entries)
        self.assertEqual(state.last_message_index, 1)

    def test_apply_message_record_keeps_prompt_visible_context_pressure_reminder(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "step_count": 34,
                "role": "user",
                "prompt_visible": True,
                "prompt_bucket": "tail",
                "exclude_from_context_compression": True,
                "message": {
                    "content": "context pressure reminder",
                },
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "step_count": 39,
                "role": "user",
                "prompt_visible": True,
                "prompt_bucket": "tail",
                "message": {"content": "real user message"},
            }
        )

        self.assertIn(("user_message", "context pressure reminder"), state.step_entries.get(34, []))
        self.assertIn(("user_message", "real user message"), state.step_entries.get(39, []))
        self.assertEqual(state.last_message_index, 1)

    def test_apply_message_record_hides_same_step_hidden_middle_messages(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(
            id="agent-1",
            name="Worker",
            context_latest_summary="## Context Summary\n- done",
        )
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "step_count": 34,
                "role": "user",
                "prompt_visible": True,
                "prompt_bucket": "pinned",
                "message": {"content": "head pinned"},
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "step_count": 34,
                "role": "assistant",
                "prompt_visible": False,
                "prompt_bucket": "hidden_middle",
                "message": {"content": '{"actions":[{"type":"list_agent_runs"}]}'},
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:02Z",
                "agent_id": state.id,
                "message_index": 2,
                "step_count": 35,
                "role": "assistant",
                "prompt_visible": True,
                "prompt_bucket": "tail",
                "message": {"content": '{"actions":[{"type":"finish","summary":"done"}]}'},
            }
        )

        self.assertEqual(state.step_entries.get(34), [("user_message", "head pinned")])
        self.assertEqual(state.pinned_prompt_steps, {34})
        self.assertIn(("tool_call", "finish(status=-)"), state.step_entries.get(35, []))

    def test_apply_message_record_accumulates_output_tokens_total(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app.agent_states = {state.id: state}
        app.stream_agent_order = [state.id]

        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "agent_id": state.id,
                "message_index": 0,
                "role": "assistant",
                "message": {"content": '{"actions":[{"type": "list_agent_runs"}]}'},
                "response": {"usage": {"output_tokens": 7, "input_tokens": 3}},
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "role": "assistant",
                "message": {"content": '{"actions":[{"type": "list_agent_runs"}]}'},
                "usage": {"completion_tokens": 5, "prompt_tokens": 4},
            }
        )
        # Duplicate index should be ignored.
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "agent_id": state.id,
                "message_index": 1,
                "role": "assistant",
                "message": {"content": '{"actions":[{"type": "list_agent_runs"}]}'},
                "response": {"usage": {"output_tokens": 99}},
            }
        )
        app._apply_message_record(
            {
                "timestamp": "2026-03-10T10:00:02Z",
                "agent_id": state.id,
                "message_index": 2,
                "role": "assistant",
                "message": {
                    "content": '{"actions":[{"type": "list_agent_runs"}]}',
                    "usage": {"total_tokens": 30, "prompt_tokens": 18},
                },
            }
        )

        self.assertEqual(state.output_tokens_total, 24)

    def test_control_message_event_is_not_rendered_in_stream(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app._update_stream_for_event(
            state,
            {
                "event_type": "control_message",
                "payload": {
                    "step_count": 1,
                    "kind": "step_limit_summary",
                    "content": "Summarize current state before exiting.",
                },
            },
        )

        self.assertEqual(state.stream_entries, [])

    def test_context_pressure_reminder_event_is_not_rendered_in_stream(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app._update_stream_for_event(
            state,
            {
                "event_type": "control_message",
                "payload": {
                    "step_count": 1,
                    "kind": "context_pressure_reminder",
                    "content": "Context usage warning: prompt tokens=9000/10240.",
                },
            },
        )

        self.assertEqual(state.stream_entries, [])

    def test_llm_retry_event_is_rendered_with_http_status(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app._update_stream_for_event(
            state,
            {
                "event_type": "llm_retry",
                "payload": {
                    "step_count": 1,
                    "attempt": 1,
                    "max_attempts": 3,
                    "status_code": 400,
                    "status_text": "Bad Request",
                    "retry_delay_seconds": 1.5,
                    "retry_reason": "http_status_error",
                },
            },
        )

        self.assertEqual(len(state.stream_entries), 1)
        self.assertEqual(state.stream_entries[0][0], "error_extra")
        self.assertIn("status=400 Bad Request", state.stream_entries[0][1])
        self.assertIn("attempt=1/3", state.stream_entries[0][1])

    def test_format_event_includes_llm_request_error_http_status(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        record = {
            "event_type": "llm_request_error",
            "timestamp": "2026-03-10T10:00:00Z",
            "payload": {
                "agent_name": "Worker",
                "status_code": 400,
                "status_text": "Bad Request",
                "error": "Client error '400 Bad Request' for url 'https://openrouter.ai/api/v1/chat/completions'",
            },
        }

        rendered = app._format_event(record)
        self.assertIn("llm request error", rendered)
        self.assertIn("400 Bad Request", rendered)

    def test_step_entries_keep_full_history_without_hard_cap(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker", step_count=1)
        for index in range(260):
            app._append_step_stream_entry(state, 1, "reply", f"line-{index}")

        entries = state.step_entries.get(1, [])
        self.assertEqual(len(entries), 260)
        self.assertEqual(entries[0], ("reply", "line-0"))
        self.assertEqual(entries[-1], ("reply", "line-259"))

    def test_completed_agent_stream_keeps_history_and_summary(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(
            id="agent-1",
            name="Worker",
            status="completed",
            stream_entries=[("thinking", "inspect repo"), ("tool_call", "shell: rg TODO")],
            summary="finished work",
        )
        self.assertEqual(
            app._entries_for_stream_render(state),
            [("thinking", "inspect repo"), ("tool_call", "shell: rg TODO"), ("summary", "finished work")],
        )

    def test_describe_action_supports_shell_command(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        self.assertEqual(
            app._describe_action({"type": "shell", "command": "rg TODO"}),
            "shell: rg TODO",
        )

    def test_describe_action_includes_multiagent_tool_parameters(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        spawn_text = app._describe_action(
            {
                "type": "spawn_agent",
                "name": "Implementer",
                "instruction": "update tests for worker summaries",
            }
        )
        terminate_text = app._describe_action(
            {"type": "cancel_agent", "agent_id": "agent-child"}
        )

        self.assertIn("spawn_agent(", spawn_text)
        self.assertIn("name=Implementer", spawn_text)
        self.assertIn("instruction=update tests for worker summaries", spawn_text)
        self.assertEqual(terminate_text, "cancel_agent(agent_id=agent-child)")

    def test_overview_and_live_body_keep_multiline_content(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(
            id="agent-1",
            name="Worker",
            status="running",
            instruction="line one\nline two",
            summary="done line one\ndone line two",
            stream_entries=[("thinking", "alpha\nbeta"), ("reply", "gamma\ndelta")],
            last_phase="llm",
            last_detail="generating",
            step_count=2,
        )
        overview_plain = app._overview_agent_body(state).plain
        live_plain = app._live_agent_body(state).plain
        self.assertIn("line one\n  line two", overview_plain)
        self.assertIn("done line one\n  done line two", overview_plain)
        self.assertIn("alpha\n  beta", live_plain)
        self.assertIn("gamma\n  delta", live_plain)

    def test_show_stream_label_for_entry_hides_consecutive_duplicate_reply_entries(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        entries = [
            ("reply", "go home"),
            ("reply", "go home"),
            ("response", "go home"),
            ("reply", "arrived"),
        ]

        self.assertEqual(
            [app._show_stream_label_for_entry(entries, index) for index in range(len(entries))],
            [True, False, False, True],
        )

    def test_live_step_body_omits_repeated_reply_header_for_consecutive_duplicates(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(
            id="agent-1",
            name="Worker",
            step_count=1,
            step_order=[1],
            step_entries={
                1: [("reply", "go home"), ("reply", "go home"), ("response", "go home")]
            },
        )

        body_plain = app._live_step_body(state, 1).plain
        self.assertEqual(body_plain.count(app.translator.text("stream_reply")), 1)
        self.assertEqual(body_plain.count("go home"), 3)

    def test_live_entries_show_message_kinds_and_hide_non_message_kinds(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(
            id="agent-1",
            name="Worker",
            step_count=1,
            step_order=[1],
            step_entries={
                1: [
                    ("thinking_preview", "streaming"),
                    ("tool_call", "shell: rg TODO"),
                    ("tool_message", '{"stdout":"ok"}'),
                    ("tool_return_extra", '{"count":1}'),
                    ("thinking", "final thinking"),
                    ("control_extra", "[budget] step=1"),
                ]
            },
        )

        self.assertEqual(
            app._entries_for_step(state, 1),
            [
                ("tool_call", "shell: rg TODO"),
                ("tool_message", '{"stdout":"ok"}'),
                ("thinking", "final thinking"),
            ],
        )

    def test_thinking_stream_styles_are_distinct_from_stderr(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        self.assertEqual(app._stream_label_style("thinking"), "bold blue")
        self.assertEqual(app._stream_body_style("thinking"), "blue")
        self.assertNotEqual(app._stream_label_style("thinking"), app._stream_label_style("stderr"))
        self.assertNotEqual(app._stream_body_style("thinking"), app._stream_body_style("stderr"))

    def test_multiagent_stream_uses_dedicated_style(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        self.assertEqual(app._stream_label_style("multiagent"), "bold bright_cyan")
        self.assertEqual(app._stream_body_style("multiagent"), "bright_cyan")
        self.assertEqual(
            app._stream_label("tool_call"),
            f"🔧 {app.translator.text('stream_tool_call')}",
        )
        self.assertEqual(
            app._stream_label("tool_return"),
            f"🧰 {app.translator.text('stream_tool_return')}",
        )
        self.assertEqual(
            app._stream_label("multiagent_call"),
            f"🤝 {app.translator.text('stream_multiagent_call')}",
        )
        self.assertEqual(
            app._stream_label("multiagent_return"),
            f"📬 {app.translator.text('stream_multiagent_return')}",
        )
        self.assertEqual(app._stream_label_style("tool_call"), app._stream_label_style("tool_return"))
        self.assertEqual(app._stream_body_style("tool_call"), app._stream_body_style("tool_return"))
        self.assertNotEqual(
            app._stream_label_style("multiagent_call"),
            app._stream_label_style("multiagent_return"),
        )
        self.assertNotEqual(
            app._stream_body_style("multiagent_call"),
            app._stream_body_style("multiagent_return"),
        )
        self.assertNotEqual(app._stream_label_style("multiagent"), app._stream_label_style("tool"))
        self.assertNotEqual(app._stream_body_style("multiagent"), app._stream_body_style("tool"))
        self.assertNotEqual(app._stream_label_style("multiagent"), app._stream_label_style("error"))
        self.assertNotEqual(app._stream_body_style("multiagent"), app._stream_body_style("error"))
        self.assertEqual(
            app._stream_label("multiagent"),
            f"🕸️ {app.translator.text('stream_multiagent')}",
        )
        self.assertIn(app.translator.text("stream_not_in_messages"), app._stream_label("stdout"))

    def test_non_message_stream_kind_detection_includes_preview_and_extra(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        self.assertTrue(app._is_non_message_stream_kind("control_extra"))
        self.assertTrue(app._is_non_message_stream_kind("reply_preview"))
        self.assertFalse(app._is_non_message_stream_kind("reply"))

    def test_live_step_entry_classes_mark_non_message_entries(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        self.assertEqual(app._live_step_entry_classes("control_extra"), "agent-detail non-message-stream")
        self.assertEqual(app._live_step_entry_classes("thinking_preview"), "agent-detail non-message-stream")
        self.assertEqual(app._live_step_entry_classes("thinking"), "agent-detail")

    def test_live_visibility_keeps_ancestor_agents(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        root = AgentRuntimeView(id="root", name="Root", role="root")
        child = AgentRuntimeView(id="child", name="Child", parent_agent_id="root")
        child.stream_entries = [("tool", "shell: rg TODO")]
        app.agent_states = {"root": root, "child": child}
        app.stream_agent_order = ["root", "child"]
        self.assertEqual(app._visible_live_agent_ids(), ["root", "child"])

    def test_live_visibility_includes_new_agent_without_stream_output(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        worker = AgentRuntimeView(id="worker-1", name="Worker 1")
        app.agent_states = {"worker-1": worker}
        app.stream_agent_order = ["worker-1"]
        self.assertEqual(app._visible_live_agent_ids(), ["worker-1"])

    def test_live_widgets_render_agents_as_flat_boxes_not_tree(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        root = AgentRuntimeView(id="root", name="Root", role="root")
        child = AgentRuntimeView(id="child", name="Child", parent_agent_id="root")
        app.agent_states = {"root": root, "child": child}
        app.stream_agent_order = ["root", "child"]

        widgets = app._build_live_widgets()
        self.assertEqual(len(widgets), 2)
        self.assertEqual([widget.agent_id for widget in widgets], ["root", "child"])

    async def test_live_panel_mounts_agents_as_multiple_top_level_widgets(self) -> None:
        try:
            from textual.containers import Vertical

            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            root = AgentRuntimeView(id="root", name="Root", role="root")
            child = AgentRuntimeView(id="child", name="Child", parent_agent_id="root")
            app.agent_states = {"root": root, "child": child}
            app.stream_agent_order = ["root", "child"]

            async with app.run_test() as pilot:
                await pilot.pause()
                await app._refresh_agent_panels()
                await pilot.pause()
                live_content = app.query_one("#live_content", Vertical)
                top_level_ids = [child_widget.id for child_widget in live_content.children]
                self.assertEqual(top_level_ids, ["live-agent-root", "live-agent-child"])

    async def test_live_jump_button_moves_focus_to_target_agent(self) -> None:
        try:
            from textual.widgets import Button, TabbedContent

            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            root = AgentRuntimeView(id="root1234", name="Root", role="root")
            parent = AgentRuntimeView(id="parent12", name="Parent", parent_agent_id="root1234")
            child = AgentRuntimeView(id="child999", name="Child", parent_agent_id="parent12")
            app.agent_states = {root.id: root, parent.id: parent, child.id: child}
            app.stream_agent_order = [root.id, parent.id, child.id]

            async with app.run_test() as pilot:
                await pilot.pause()
                await app._refresh_agent_panels()
                tabs = app.query_one("#main_tabs", TabbedContent)
                tabs.active = "agents_tab"
                await pilot.pause()
                app.query_one("#live-agent-parent12-jump-child-0", Button).press()
                await pilot.pause()
                self.assertEqual(app.current_focus_agent_id, "child999")
                self.assertEqual(tabs.active, "agents_tab")

    async def test_live_agent_card_has_steer_button(self) -> None:
        try:
            from textual.widgets import Button, TabbedContent

            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            agent = AgentRuntimeView(id="agent-live-1", name="Worker", role="worker")
            app.agent_states = {agent.id: agent}
            app.stream_agent_order = [agent.id]

            async with app.run_test() as pilot:
                await pilot.pause()
                await app._refresh_agent_panels()
                tabs = app.query_one("#main_tabs", TabbedContent)
                tabs.active = "agents_tab"
                await pilot.pause()
                steer_button = app.query_one("#live-agent-agent-live-1-steer-button", Button)
                self.assertEqual(str(steer_button.label), app.translator.text("steer_button"))
                terminate_button = app.query_one("#live-agent-agent-live-1-terminate-button", Button)
                self.assertEqual(str(terminate_button.label), app.translator.text("terminate_button"))

    async def test_live_agent_card_terminate_button_calls_orchestrator(self) -> None:
        try:
            from textual.widgets import Button, TabbedContent

            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            agent = AgentRuntimeView(id="agent-live-1", name="Worker", role="worker")
            app.agent_states = {agent.id: agent}
            app.stream_agent_order = [agent.id]
            app.current_session_id = "session-live-terminate"
            app.configured_resume_session_id = "session-live-terminate"

            class _FakeOrchestrator:
                def __init__(self, app_dir: Path) -> None:
                    self.app_dir = app_dir
                    self.calls: list[tuple[str, str, str]] = []

                async def terminate_agent_subtree(
                    self,
                    *,
                    session_id: str,
                    agent_id: str,
                    source: str = "tui",
                ) -> dict[str, object]:
                    self.calls.append((session_id, agent_id, source))
                    return {
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "source": source,
                        "target_agent_ids": [agent_id],
                        "terminated_agent_ids": [agent_id],
                        "cancelled_tool_run_ids": [],
                    }

            fake = _FakeOrchestrator(Path(temp_dir))
            app.orchestrator = fake  # type: ignore[assignment]

            async with app.run_test() as pilot:
                await pilot.pause()
                await app._refresh_agent_panels()
                tabs = app.query_one("#main_tabs", TabbedContent)
                tabs.active = "agents_tab"
                await pilot.pause()
                app.query_one("#live-agent-agent-live-1-terminate-button", Button).press()
                await pilot.pause()
                self.assertEqual(
                    fake.calls,
                    [("session-live-terminate", "agent-live-1", "tui")],
                )

    def test_agent_state_keeps_instruction_for_overview(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = app._ensure_agent_state(
            agent_id="agent-1",
            parent_agent_id=None,
            details={
                "agent_name": "Worker",
                "instruction": "run tests in api package",
                "agent_model": "openai/gpt-4.1-mini",
                "keep_pinned_messages": 2,
                "summary_version": 3,
                "context_latest_summary": "## Context Summary\n- completed API test run",
            },
        )
        self.assertEqual(state.instruction, "run tests in api package")
        self.assertEqual(state.model, "openai/gpt-4.1-mini")
        self.assertEqual(state.keep_pinned_messages, 2)
        self.assertEqual(state.summary_version, 3)
        self.assertEqual(
            state.context_latest_summary,
            "## Context Summary\n- completed API test run",
        )

    def test_agent_state_default_keep_pinned_messages_uses_config(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[runtime.context]
max_context_tokens = 8192
compression_model = "openai/gpt-4.1-mini"
keep_pinned_messages = 4
""".strip(),
                encoding="utf-8",
            )
            app = OpenCompanyApp(project_dir=app_dir, app_dir=app_dir)
            state = app._ensure_agent_state(
                agent_id="agent-config-default",
                parent_agent_id=None,
                details={"agent_name": "Worker"},
            )
            self.assertEqual(state.keep_pinned_messages, 4)

    def test_live_agent_status_body_includes_parent_and_children(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        root = AgentRuntimeView(id="root1234", name="Root", role="root")
        parent = AgentRuntimeView(
            id="parent12",
            name="Parent",
            parent_agent_id="root1234",
            output_tokens_total=42,
            model="openai/gpt-4.1-mini",
        )
        child = AgentRuntimeView(id="child999", name="Child", parent_agent_id="parent12")
        app.agent_states = {root.id: root, parent.id: parent, child.id: child}
        app.stream_agent_order = [root.id, parent.id, child.id]

        status_plain = app._live_agent_status_body(parent).plain
        self.assertIn(app.translator.text("output_tokens_total"), status_plain)
        self.assertIn("42", status_plain)
        self.assertIn(app.translator.text("agent_model_label"), status_plain)
        self.assertIn("openai/gpt-4.1-mini", status_plain)
        self.assertIn(app.translator.text("parent_agent_label"), status_plain)
        self.assertIn("Root (root1234)", status_plain)
        self.assertIn(app.translator.text("child_agents_label"), status_plain)
        self.assertIn("Child (child999)", status_plain)

    def test_effective_step_order_places_pinned_then_summary_then_unsummarized(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(
            id="agent-ctx",
            name="Worker",
            keep_pinned_messages=2,
            context_latest_summary="## Context Summary\n- done",
            compacted_step_ranges=[(1, 3)],
            pinned_prompt_steps={1, 2},
            step_order=[1, 2, 4],
            step_entries={
                1: [("reply", "head-1")],
                2: [("reply", "head-2")],
                4: [("reply", "tail")],
            },
        )
        self.assertEqual(app._effective_step_order(state), [1, 2, 0, 4])
        self.assertEqual(app._entries_for_step(state, 0), [("summary", "## Context Summary\n- done")])
        self.assertEqual(app._entries_for_step(state, 1), [("reply", "head-1")])

    def test_effective_step_order_without_context_summary_keeps_real_steps_only(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(
            id="agent-ctx-empty",
            name="Worker",
            keep_pinned_messages=2,
            context_latest_summary="",
            compacted_step_ranges=[(1, 2)],
            step_order=[1, 2, 3],
            step_entries={
                1: [("reply", "s1")],
                2: [("reply", "s2")],
                3: [("reply", "s3")],
            },
        )
        self.assertEqual(app._effective_step_order(state), [1, 2, 3])
        self.assertEqual(app._entries_for_step(state, 0), [])

    def test_tool_call_results_include_multiagent_events(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {"step_count": 1}},
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call",
                "payload": {
                    "step_count": 1,
                    "action": {"type": "spawn_agent", "name": "Child"},
                    "result_preview": '{"child_agent_id":"agent-child"}',
                },
            },
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call",
                "payload": {
                    "step_count": 1,
                    "action": {"type": "cancel_agent", "agent_id": "agent-child"},
                    "result_preview": '{"cancel_agent_status":true}',
                    "result": {"cancel_agent_status": True},
                },
            },
        )
        self.assertIn(
            ("multiagent_return_extra", "spawn_agent result: child_agent_id=agent-child"),
            state.stream_entries,
        )
        self.assertIn(
            ("multiagent_return_extra", "cancel_agent result: status=True"),
            state.stream_entries,
        )

    def test_shell_tool_return_hides_command_but_keeps_action_command_visible(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")
        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {"step_count": 1}},
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call_started",
                "payload": {
                    "step_count": 1,
                    "action": {"type": "shell", "command": "pwd"},
                },
            },
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call",
                "payload": {
                    "step_count": 1,
                    "action": {"type": "shell", "command": "pwd"},
                    "result": {
                        "exit_code": 0,
                        "stdout": "/tmp/workspace\n",
                        "stderr": "",
                        "command": "pwd",
                    },
                },
            },
        )

        self.assertIn(("tool_call_extra", "shell: pwd"), state.stream_entries)
        tool_return_entries = [text for kind, text in state.stream_entries if kind == "tool_return_extra"]
        self.assertTrue(tool_return_entries)
        self.assertFalse(any('"command"' in text for text in tool_return_entries))

    def test_agent_spawned_event_adds_spawn_result_to_parent_stream(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        parent = AgentRuntimeView(id="agent-parent", name="Root", step_count=2)
        child = AgentRuntimeView(
            id="agent-child",
            name="Child Worker",
            parent_agent_id=parent.id,
            step_count=1,
        )
        app.agent_states = {parent.id: parent, child.id: child}
        app.stream_agent_order = [parent.id, child.id]

        app._update_stream_for_event(
            child,
            {
                "event_type": "agent_spawned",
                "payload": {"step_count": 1},
            },
        )
        self.assertIn(
            ("multiagent_return_extra", "spawn_agent result: child_agent_id=agent-child"),
            parent.stream_entries,
        )

    def test_child_summaries_received_event_is_rendered_in_stream(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-parent", name="Parent")
        app._update_stream_for_event(
            state,
            {
                "event_type": "child_summaries_received",
                "payload": {
                    "step_count": 1,
                    "children": [
                        {
                            "id": "agent-child",
                            "name": "Child",
                            "status": "completed",
                            "summary": "Done",
                            "next_recommendation": "Run verification",
                        }
                    ],
                },
            },
        )
        summary_entries = [entry for entry in state.stream_entries if entry[0] == "multiagent_return_extra"]
        self.assertTrue(summary_entries)
        self.assertIn(app.translator.text("stream_child_summaries"), summary_entries[0][1])
        self.assertIn("Child (agent-child)", summary_entries[0][1])
        self.assertIn("Done", summary_entries[0][1])

    def test_live_stream_entries_follow_event_arrival_order(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")

        app._update_stream_for_event(
            state,
            {"event_type": "agent_prompt", "payload": {"step_count": 1}},
        )
        app._update_stream_for_event(
            state,
            {"event_type": "llm_reasoning", "payload": {"step_count": 1, "token": "inspect"}},
        )
        app._update_stream_for_event(
            state,
            {"event_type": "llm_token", "payload": {"step_count": 1, "token": "reply"}},
        )
        app._update_stream_for_event(
            state,
            {
                "event_type": "tool_call_started",
                "payload": {"step_count": 1, "action": {"type": "shell", "command": "rg TODO"}},
            },
        )

        self.assertEqual(
            state.stream_entries,
            [
                ("thinking_preview", "inspect"),
                ("reply_preview", "reply"),
                ("tool_call_extra", "shell: rg TODO"),
            ],
        )

    def test_workflow_children_keep_spawn_order(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        root = AgentRuntimeView(id="root", name="Root", role="root")
        child_b = AgentRuntimeView(id="child-b", name="B", parent_agent_id="root")
        child_a = AgentRuntimeView(id="child-a", name="A", parent_agent_id="root")
        app.agent_states = {"root": root, "child-a": child_a, "child-b": child_b}
        app.stream_agent_order = ["root", "child-b", "child-a"]

        self.assertEqual(
            [item.id for item in app._sorted_agent_children("root")],
            ["child-b", "child-a"],
        )

    def test_live_step_default_collapse_all_steps(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        self.assertTrue(app._is_live_step_collapsed("agent-1", 1, 2))
        self.assertTrue(app._is_live_step_collapsed("agent-1", 2, 2))

        app.live_step_collapsed_overrides[("agent-1", 1)] = False
        app.live_step_collapsed_overrides[("agent-1", 2)] = True
        self.assertFalse(app._is_live_step_collapsed("agent-1", 1, 2))
        self.assertTrue(app._is_live_step_collapsed("agent-1", 2, 2))

    def test_live_agent_default_expanded(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")

        fingerprint = app._live_agent_render_fingerprint(state)
        self.assertFalse(fingerprint[1])

        app.live_collapsed_agent_ids.add(state.id)
        fingerprint = app._live_agent_render_fingerprint(state)
        self.assertTrue(fingerprint[1])

    def test_workflow_sections_default_to_expanded_task_and_collapsed_summary(self) -> None:
        try:
            from opencompany.tui.app import AgentRuntimeView, OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        state = AgentRuntimeView(id="agent-1", name="Worker")

        fingerprint = app._overview_render_fingerprint(state)
        self.assertFalse(fingerprint[2])
        self.assertTrue(fingerprint[3])

        app.overview_instruction_collapsed_agent_ids.add(state.id)
        app.overview_summary_expanded_agent_ids.add(state.id)
        fingerprint = app._overview_render_fingerprint(state)
        self.assertTrue(fingerprint[2])
        self.assertFalse(fingerprint[3])

    def test_custom_collapsible_widgets_accept_stable_ids(self) -> None:
        try:
            from textual.widgets import Static

            from opencompany.tui.app import AgentCollapsible, AgentSectionCollapsible, LiveStepCollapsible
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        agent = AgentCollapsible(
            Static("agent body"),
            agent_id="agent-1",
            panel_kind="overview",
            title="Agent",
            collapsed=False,
            id="wf-agent-agent-1",
        )
        self.assertEqual(agent.id, "wf-agent-agent-1")

        section = AgentSectionCollapsible(
            Static("section body"),
            agent_id="agent-1",
            section_kind="instruction",
            title="Instruction",
            collapsed=True,
            id="wf-agent-agent-1-instruction",
        )
        self.assertEqual(section.id, "wf-agent-agent-1-instruction")

        step = LiveStepCollapsible(
            Static("step body"),
            agent_id="agent-1",
            step_number=1,
            title="Step 1",
            collapsed=False,
            id="live-agent-agent-1-step-1",
        )
        self.assertEqual(step.id, "live-agent-agent-1-step-1")

        live_agent = AgentCollapsible(
            Static("live body"),
            agent_id="agent-1",
            panel_kind="live",
            title="Live Agent",
            collapsed=False,
            id="live-agent-agent-1",
        )
        self.assertIn("live-agent-card", live_agent.classes)

    def test_session_finalized_marks_root_agent_completed_in_tui_state(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app._consume_runtime_update(
            {
                "event_type": "agent_response",
                "timestamp": "2026-03-09T12:00:00Z",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "phase": "llm",
                "payload": {
                    "agent_name": "Root Coordinator",
                    "agent_role": "root",
                    "agent_status": "running",
                    "step_count": 1,
                    "content": "",
                    "actions": [{"type": "finish", "summary": "任务完成"}],
                },
            }
        )
        self.assertEqual(app.agent_states["agent-root"].status, "running")

        app._consume_runtime_update(
            {
                "event_type": "session_finalized",
                "timestamp": "2026-03-09T12:00:01Z",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "phase": "scheduler",
                "payload": {
                    "user_summary": "任务完成",
                    "completion_state": "completed",
                    "task": "demo task",
                    "session_status": "completed",
                },
            }
        )
        self.assertEqual(app.current_session_status, "completed")
        self.assertEqual(app.agent_states["agent-root"].status, "completed")
        self.assertEqual(app.agent_states["agent-root"].summary, "任务完成")

    def test_session_finalized_preserves_cancelled_root_status(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        app._consume_runtime_update(
            {
                "event_type": "agent_response",
                "timestamp": "2026-03-09T12:00:00Z",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "phase": "llm",
                "payload": {
                    "agent_name": "Root Coordinator",
                    "agent_role": "root",
                    "agent_status": "running",
                    "step_count": 1,
                    "content": "",
                },
            }
        )
        app._consume_runtime_update(
            {
                "event_type": "agent_cancelled",
                "timestamp": "2026-03-09T12:00:01Z",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "phase": "runtime",
                "payload": {
                    "reason": "Cancelled by user.",
                },
            }
        )
        self.assertEqual(app.agent_states["agent-root"].status, "cancelled")

        app._consume_runtime_update(
            {
                "event_type": "session_finalized",
                "timestamp": "2026-03-09T12:00:02Z",
                "agent_id": "agent-root",
                "parent_agent_id": None,
                "phase": "scheduler",
                "payload": {
                    "user_summary": "Cancelled by user.",
                    "completion_state": "partial",
                    "task": "demo task",
                    "session_status": "completed",
                },
            }
        )
        self.assertEqual(app.current_session_status, "completed")
        self.assertEqual(app.agent_states["agent-root"].status, "cancelled")
        self.assertEqual(app.agent_states["agent-root"].summary, "Cancelled by user.")

    def test_agent_cancelled_event_marks_running_agent_cancelled(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd(), locale="en")
        app._consume_runtime_update(
            {
                "event_type": "agent_prompt",
                "timestamp": "2026-03-09T12:00:00Z",
                "agent_id": "agent-worker",
                "parent_agent_id": "agent-root",
                "phase": "llm",
                "payload": {
                    "agent_name": "Worker",
                    "agent_role": "worker",
                    "agent_status": "running",
                    "step_count": 1,
                },
            }
        )
        self.assertEqual(app.agent_states["agent-worker"].status, "running")

        record = {
            "event_type": "agent_cancelled",
            "timestamp": "2026-03-09T12:00:01Z",
            "agent_id": "agent-worker",
            "parent_agent_id": "agent-root",
            "phase": "scheduler",
            "payload": {
                "reason": "Cancelled by parent agent.",
            },
        }
        app._consume_runtime_update(record)
        self.assertEqual(app.agent_states["agent-worker"].status, "cancelled")
        self.assertEqual(app.agent_states["agent-worker"].last_detail, "Cancelled by parent agent.")
        self.assertIn("cancelled", app._format_event(record))

    def test_config_actions_include_apply_and_undo_based_on_sync_state(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        config = app._launch_config()

        app.project_sync_state = {"status": "pending"}
        actions = app._config_actions_text(config)
        self.assertIn(app.translator.text("apply"), actions)

        app.project_sync_state = {"status": "applied"}
        actions = app._config_actions_text(config)
        self.assertIn(app.translator.text("undo"), actions)

    def test_diff_preview_text_styles_text_and_binary_entries(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        class _SyncOrchestrator:
            def __init__(self) -> None:
                self.app_dir = Path.cwd()

            def project_sync_preview(
                self,
                session_id: str,
                *,
                max_files: int = 80,
                max_chars: int = 200_000,
            ) -> dict[str, object]:
                del session_id, max_files, max_chars
                return {
                    "status": "pending",
                    "project_dir": str(Path.cwd()),
                    "added_count": 0,
                    "modified_count": 2,
                    "deleted_count": 0,
                    "truncated": False,
                    "files": [
                        {
                            "path": "src/demo.py",
                            "change_type": "modified",
                            "patch": (
                                "--- a/src/demo.py\n"
                                "+++ b/src/demo.py\n"
                                "@@ -1 +1 @@\n"
                                "-before\n"
                                "+after"
                            ),
                            "is_binary": False,
                            "before_size": 7,
                            "after_size": 6,
                        },
                        {
                            "path": "assets/logo.doc",
                            "change_type": "modified",
                            "patch": "",
                            "is_binary": True,
                            "before_size": 10,
                            "after_size": 12,
                        },
                    ],
                }

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.session_mode = "staged"
        app.orchestrator = _SyncOrchestrator()
        app.current_session_id = "session-123"
        app.project_sync_state = {"status": "pending", "modified": ["src/demo.py", "assets/logo.doc"]}

        rendered = app._diff_preview_text()

        self.assertIn(f"src/demo.py [{app._diff_change_label('modified')}]", rendered.plain)
        self.assertIn(f"assets/logo.doc [{app._diff_change_label('modified')}]", rendered.plain)
        self.assertIn(app.translator.text("diff_binary_modified"), rendered.plain)
        self.assertTrue(any(str(span.style) == "green" for span in rendered.spans))
        self.assertTrue(any(str(span.style) == "red" for span in rendered.spans))
        self.assertTrue(any(str(span.style) == "bold cyan" for span in rendered.spans))

    async def test_apply_and_undo_project_sync_update_tui_state(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        class _SyncOrchestrator:
            def __init__(self) -> None:
                self.app_dir = Path.cwd()
                self.status = "pending"

            def apply_project_sync(self, session_id: str) -> dict[str, object]:
                del session_id
                self.status = "applied"
                return {
                    "status": "applied",
                    "project_dir": str(Path.cwd()),
                    "added": 1,
                    "modified": 2,
                    "deleted": 3,
                    "backup_dir": str(Path.cwd() / ".backup"),
                }

            def undo_project_sync(self, session_id: str) -> dict[str, object]:
                del session_id
                self.status = "reverted"
                return {
                    "status": "reverted",
                    "project_dir": str(Path.cwd()),
                    "removed": 1,
                    "restored": 2,
                    "missing_backups": [],
                }

            def project_sync_status(self, session_id: str) -> dict[str, object]:
                del session_id
                return {"status": self.status}

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.session_mode = "staged"
        app.orchestrator = _SyncOrchestrator()
        app.current_session_id = "session-123"
        app.project_sync_state = {"status": "pending"}

        await app._apply_project_sync()
        self.assertEqual(app.project_sync_state, {"status": "applied"})
        self.assertIsNotNone(app.last_project_sync_operation)
        self.assertIn(app.translator.text("diff_last_sync"), app._last_project_sync_operation_text())
        self.assertIn(str(Path.cwd()), app._last_project_sync_operation_text())

        await app._undo_project_sync()
        self.assertEqual(app.project_sync_state, {"status": "reverted"})
        self.assertIsNotNone(app.last_project_sync_operation)
        self.assertIn(app.translator.text("diff_last_sync"), app._last_project_sync_operation_text())
        self.assertIn("removed=", app._last_project_sync_operation_text())

    async def test_apply_button_click_works_in_80x24_terminal(self) -> None:
        try:
            from textual.widgets import Button, TabbedContent

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        class _SyncOrchestrator:
            def __init__(self) -> None:
                self.app_dir = Path.cwd()
                self.status = "pending"

            def apply_project_sync(self, session_id: str) -> dict[str, object]:
                del session_id
                self.status = "applied"
                return {
                    "status": "applied",
                    "project_dir": str(Path.cwd()),
                    "added": 1,
                    "modified": 0,
                    "deleted": 0,
                    "backup_dir": str(Path.cwd() / ".backup"),
                }

            def project_sync_status(self, session_id: str) -> dict[str, object]:
                del session_id
                return {"status": self.status}

        app = OpenCompanyApp(project_dir=Path.cwd())
        app.session_mode = "staged"
        app.orchestrator = _SyncOrchestrator()
        app.current_session_id = "session-123"
        app.project_sync_state = {"status": "pending"}

        async with app.run_test() as pilot:
            await pilot.resize_terminal(80, 24)
            app.query_one("#main_tabs", TabbedContent).active = "diff_tab"
            await pilot.pause()
            clicked = await pilot.click("#apply_button", offset=(2, 1))
            await pilot.pause()

            self.assertTrue(clicked)
            self.assertEqual(app.project_sync_state, {"status": "applied"})
            self.assertTrue(app.query_one("#apply_button", Button).disabled)
            self.assertFalse(app.query_one("#undo_button", Button).disabled)

    async def test_project_sync_actions_work_with_real_orchestrator_from_tui(self) -> None:
        try:
            from opencompany.models import AgentNode, AgentRole, RunSession, SessionStatus
            from opencompany.orchestrator import Orchestrator
            from opencompany.tui.app import OpenCompanyApp
            from opencompany.workspace import WorkspaceManager
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        def build_test_project(project_dir: Path) -> None:
            from opencompany.orchestrator import default_app_dir

            shutil.copytree(default_app_dir() / "prompts", project_dir / "prompts")
            (project_dir / "opencompany.toml").write_text(
                """
[project]
name = "OpenCompany"
default_locale = "en"
data_dir = ".opencompany"

[llm.openrouter]
model = "fake/model"
temperature = 0.1
max_tokens = 1000

[runtime.limits]
max_children_per_agent = 3
max_active_agents = 2
max_root_steps = 3
max_agent_steps = 4

[sandbox]
backend = "anthropic"
timeout_seconds = 10
""".strip(),
                encoding="utf-8",
            )
            (project_dir / "README.md").write_text("demo\n", encoding="utf-8")

        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            (project_dir / "obsolete.txt").write_text("remove me\n", encoding="utf-8")
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_id = "session-project-sync"
            session_dir = orchestrator.paths.session_dir(session_id)
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root_agent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root",
                role=AgentRole.ROOT,
                instruction="Finalize",
                workspace_id=root_workspace.id,
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir.resolve(),
                task="Sync root workspace",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at="2026-03-09T00:00:00+00:00",
                updated_at="2026-03-09T00:00:00+00:00",
            )

            (root_workspace.path / "README.md").write_text("updated\n", encoding="utf-8")
            (root_workspace.path / "generated" / "result.txt").parent.mkdir(parents=True, exist_ok=True)
            (root_workspace.path / "generated" / "result.txt").write_text("done\n", encoding="utf-8")
            (root_workspace.path / "obsolete.txt").unlink()

            await orchestrator._finalize_root(
                session=session,
                root_agent=root_agent,
                payload={
                    "completion_state": "completed",
                    "user_summary": "Completed.",
                },
                agents={root_agent.id: root_agent},
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=0,
            )

            app = OpenCompanyApp(project_dir=project_dir)
            app.orchestrator = orchestrator
            app.current_session_id = session_id
            app.current_session_status = "completed"
            app.project_sync_state = orchestrator.project_sync_status(session_id)

            await app._apply_project_sync()
            self.assertEqual(app.project_sync_state["status"], "applied")
            self.assertEqual((project_dir / "README.md").read_text(encoding="utf-8"), "updated\n")
            self.assertTrue((project_dir / "generated" / "result.txt").exists())
            self.assertFalse((project_dir / "obsolete.txt").exists())
            self.assertNotIn("SQLite objects created in a thread", app.status_message)

            await app._undo_project_sync()
            self.assertEqual(app.project_sync_state["status"], "reverted")
            self.assertEqual((project_dir / "README.md").read_text(encoding="utf-8"), "demo\n")
            self.assertFalse((project_dir / "generated" / "result.txt").exists())
            self.assertEqual((project_dir / "obsolete.txt").read_text(encoding="utf-8"), "remove me\n")

    async def test_tool_runs_detail_button_opens_modal_for_selected_run(self) -> None:
        try:
            from textual.widgets import Button, Static, TabbedContent

            from opencompany.tui.app import OpenCompanyApp, ToolRunDetailScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            session_id = "session-tool-runs"
            run_id = "toolrun-1"
            app.current_session_id = session_id
            app.tool_runs_snapshot = {
                "tool_runs": [
                    {
                        "id": run_id,
                        "session_id": session_id,
                        "agent_id": "agent-1",
                        "tool_name": "list_agent_runs",
                        "arguments": {},
                        "status": "running",
                        "result": {"files": 2},
                        "error": None,
                        "created_at": "2026-03-10T10:00:00Z",
                        "started_at": "2026-03-10T10:00:01Z",
                        "completed_at": None,
                    }
                ],
                "next_cursor": None,
            }
            app.tool_runs_metrics_snapshot = {
                "total_runs": 1,
                "terminal_runs": 0,
                "status_counts": {"running": 1},
                "duration_ms": {"p50": 100, "p95": 100, "p99": 100},
                "failure_rate": 0.0,
                "failure_or_cancel_rate": 0.0,
            }
            app.tool_runs_status_message = "Tool Runs: 1"
            app.tool_runs_selected_run_id = run_id
            app._tool_run_timeline_by_run_id[run_id] = [
                {
                    "timestamp": "2026-03-10T10:00:00Z",
                    "event_type": "tool_run_submitted",
                    "phase": "tool",
                    "agent_id": "agent-1",
                    "payload": {
                        "tool_run_id": run_id,
                        "tool_name": "list_agent_runs",
                    },
                }
            ]
            app._tool_runs_dirty = False
            app._tool_runs_cache_key = (session_id, app.tool_runs_filter)

            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#main_tabs", TabbedContent).active = "tool_runs_tab"
                app._render_tool_runs_panel()
                await pilot.pause()
                app.query_one("#tool_runs_detail_button", Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen, ToolRunDetailScreen)
                detail_body = str(app.screen.query_one("#tool_run_detail_body", Static).renderable)
                self.assertIn("list_agent_runs", detail_body)
                self.assertIn("tool_run_submitted", detail_body)
                self.assertTrue("参数" in detail_body or "Arguments" in detail_body)

    def test_tool_run_timeline_registration_maps_call_id_to_run_id(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        app = OpenCompanyApp(project_dir=Path.cwd())
        run_id = "toolrun-1"
        app._register_tool_run_timeline_event(
            {
                "timestamp": "2026-03-10T10:00:00Z",
                "event_type": "tool_run_submitted",
                "phase": "tool",
                "agent_id": "agent-1",
                "payload": {
                    "tool_run_id": run_id,
                    "tool_name": "list_agent_runs",
                    "action": {
                        "type": "list_agent_runs",
                        "_tool_call_id": "call-tree-1",
                    },
                },
            }
        )
        app._register_tool_run_timeline_event(
            {
                "timestamp": "2026-03-10T10:00:01Z",
                "event_type": "tool_call",
                "phase": "tool",
                "agent_id": "agent-1",
                "payload": {
                    "action": {
                        "type": "list_agent_runs",
                        "_tool_call_id": "call-tree-1",
                    },
                    "result": {"nodes": 3},
                },
            }
        )

        self.assertEqual(app._tool_run_call_id_to_run_id.get("call-tree-1"), run_id)
        timeline = app._tool_run_timeline_by_run_id.get(run_id, [])
        self.assertEqual([entry.get("event_type") for entry in timeline], ["tool_run_submitted", "tool_call"])

    async def test_tool_run_detail_modal_auto_refreshes_on_runtime_events(self) -> None:
        try:
            from textual.widgets import Button, Static, TabbedContent

            from opencompany.tui.app import OpenCompanyApp, ToolRunDetailScreen
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            session_id = "session-tool-runs"
            run_id = "toolrun-2"
            app.current_session_id = session_id
            app.tool_runs_snapshot = {
                "tool_runs": [
                    {
                        "id": run_id,
                        "session_id": session_id,
                        "agent_id": "agent-2",
                        "tool_name": "shell",
                        "arguments": {"command": "rg TODO"},
                        "status": "running",
                        "result": None,
                        "error": None,
                        "created_at": "2026-03-10T10:00:00Z",
                        "started_at": "2026-03-10T10:00:01Z",
                        "completed_at": None,
                    }
                ],
                "next_cursor": None,
            }
            app.tool_runs_metrics_snapshot = {
                "total_runs": 1,
                "terminal_runs": 0,
                "status_counts": {"running": 1},
                "duration_ms": {"p50": 100, "p95": 100, "p99": 100},
                "failure_rate": 0.0,
                "failure_or_cancel_rate": 0.0,
            }
            app.tool_runs_status_message = "Tool Runs: 1"
            app.tool_runs_selected_run_id = run_id
            app._tool_runs_dirty = False
            app._tool_runs_cache_key = (session_id, app.tool_runs_filter)

            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#main_tabs", TabbedContent).active = "tool_runs_tab"
                app._render_tool_runs_panel()
                await pilot.pause()
                app.query_one("#tool_runs_detail_button", Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen, ToolRunDetailScreen)

                app._consume_runtime_update(
                    {
                        "timestamp": "2026-03-10T10:00:03Z",
                        "session_id": session_id,
                        "agent_id": "agent-2",
                        "event_type": "tool_run_updated",
                        "phase": "tool",
                        "payload": {
                            "tool_run_id": run_id,
                            "tool_name": "shell",
                            "status": "completed",
                        },
                    }
                )
                await pilot.pause()

                detail_body = str(app.screen.query_one("#tool_run_detail_body", Static).renderable)
                self.assertIn("completed", detail_body)
                self.assertIn("tool_run_updated", detail_body)

    async def test_waiting_steer_run_cancel_button_triggers_refresh(self) -> None:
        try:
            from textual.widgets import Static, TabbedContent

            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        def _collect_static_renderables(widget: object) -> list[str]:
            children = getattr(widget, "children", ())
            rendered: list[str] = []
            for child in children:
                if isinstance(child, Static):
                    rendered.append(str(child.renderable))
                rendered.extend(_collect_static_renderables(child))
            return rendered

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir))
            session_id = "session-steer-runs"
            steer_run_id = "steerrun-1"
            app.current_session_id = session_id

            class _FakeOrchestrator:
                def __init__(self, app_dir: Path) -> None:
                    self.app_dir = app_dir
                    self.cancel_called = False

                def list_steer_runs_page(self, _session_id: str, **_kwargs):  # type: ignore[no-untyped-def]
                    status = "cancelled" if self.cancel_called else "waiting"
                    return {
                        "steer_runs": [
                            {
                                "id": steer_run_id,
                                "session_id": session_id,
                                "agent_id": "agent-1",
                                "content": "please focus on tests",
                                "source": "tui",
                                "status": status,
                                "created_at": "2026-03-13T10:00:00Z",
                                "completed_at": None,
                                "cancelled_at": "2026-03-13T10:00:10Z" if self.cancel_called else None,
                                "delivered_step": None,
                            }
                        ],
                        "next_cursor": None,
                        "has_more": False,
                    }

                def steer_run_metrics(self, _session_id: str) -> dict[str, object]:
                    if self.cancel_called:
                        return {
                            "session_id": session_id,
                            "total_runs": 1,
                            "status_counts": {"waiting": 0, "completed": 0, "cancelled": 1},
                        }
                    return {
                        "session_id": session_id,
                        "total_runs": 1,
                        "status_counts": {"waiting": 1, "completed": 0, "cancelled": 0},
                    }

                def cancel_steer_run(
                    self,
                    *,
                    session_id: str,
                    steer_run_id: str,
                ) -> dict[str, object]:
                    del session_id
                    self.cancel_called = True
                    return {
                        "steer_run_id": steer_run_id,
                        "final_status": "cancelled",
                        "cancelled": True,
                    }

            app.orchestrator = _FakeOrchestrator(Path(temp_dir))
            app.steer_runs_snapshot = app.orchestrator.list_steer_runs_page(session_id)
            app.steer_runs_metrics_snapshot = app.orchestrator.steer_run_metrics(session_id)
            app.steer_runs_status_message = "Steer Runs: 1"
            app._steer_runs_dirty = False
            app._steer_runs_cache_key = (session_id, app.steer_runs_filter)

            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#main_tabs", TabbedContent).active = "steer_runs_tab"
                app._render_steer_runs_panel()
                await pilot.pause()
                content_widget = app.query_one("#steer_runs_content")
                rendered = "\n".join(_collect_static_renderables(content_widget))
                self.assertIn("Target Agent: agent-1", rendered)
                self.assertIn("Inserted: Pending delivery", rendered)
                self.assertIn("Message:\nplease focus on tests", rendered)
                app.query_one("#steer-run-cancel-steerrun-1").press()
                await pilot.pause()
                self.assertTrue(app.orchestrator.cancel_called)  # type: ignore[union-attr]
                current_runs = app.steer_runs_snapshot.get("steer_runs", []) if isinstance(app.steer_runs_snapshot, dict) else []
                self.assertEqual(str(current_runs[0].get("status", "")), "cancelled")

    def test_steer_run_delivery_label_formats_inserted_step(self) -> None:
        try:
            from opencompany.tui.app import OpenCompanyApp
        except ImportError:
            self.skipTest("textual is not installed in the current environment")

        with TemporaryDirectory() as temp_dir:
            app = OpenCompanyApp(project_dir=Path(temp_dir), locale="en")
            delivery = app._steer_run_delivery_label(
                {"status": "completed", "delivered_step": 7}
            )
            self.assertEqual(delivery, "Step 7")

    def test_tool_runs_i18n_values_are_not_swapped(self) -> None:
        from opencompany.i18n import TRANSLATIONS

        self.assertEqual(TRANSLATIONS["en"]["tool_runs_tab_title"], "Tool Runs")
        self.assertEqual(TRANSLATIONS["en"]["tool_runs_count"], "Tool Runs")
        self.assertEqual(TRANSLATIONS["zh"]["tool_runs_tab_title"], "工具运行")
        self.assertEqual(TRANSLATIONS["zh"]["tool_runs_count"], "工具运行数")
        self.assertEqual(TRANSLATIONS["en"]["steer_runs_tab_title"], "Steer Runs")
        self.assertEqual(TRANSLATIONS["zh"]["steer_runs_tab_title"], "引导运行")
        self.assertEqual(TRANSLATIONS["en"]["steer_runs_inserted"], "Inserted")
        self.assertEqual(TRANSLATIONS["zh"]["steer_runs_inserted"], "插入位置")
