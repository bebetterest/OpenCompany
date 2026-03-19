import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from opencompany.utils import utc_now

from .events import build_event_batch
from .state import WebUIRuntimeState


def create_webui_app(
    *,
    project_dir: Path | None = None,
    session_id: str | None = None,
    session_mode: str | None = None,
    remote_config: dict[str, Any] | None = None,
    remote_password: str | None = None,
    app_dir: Path | None = None,
    locale: str | None = None,
    debug: bool = False,
):
    try:
        from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - import availability is environment-dependent.
        raise RuntimeError(
            "FastAPI and Uvicorn are required for `opencompany ui`. "
            "Install dependencies with `pip install -e .`."
        ) from exc

    state = WebUIRuntimeState(
        project_dir=project_dir,
        session_id=session_id,
        session_mode=session_mode,
        remote=remote_config,
        remote_password=remote_password,
        app_dir=app_dir,
        locale=locale,
        debug=debug,
    )
    webui_dir = Path(__file__).resolve().parent / "static"

    @asynccontextmanager
    async def lifespan(_app):
        del _app
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="OpenCompany Web UI", version="0.1.0", lifespan=lifespan)
    app.state.runtime_state = state

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "timestamp": utc_now(),
            "running": state.has_running_session(),
        }

    @app.get("/api/bootstrap")
    async def api_bootstrap() -> dict[str, Any]:
        snapshot = state.snapshot()
        snapshot["sessions"] = state.list_session_directories()
        snapshot["config_meta"] = state.read_config_meta()
        return snapshot

    @app.post("/api/locale")
    async def api_set_locale(payload: dict[str, Any]) -> dict[str, Any]:
        locale_value = str(payload.get("locale", "")).strip()
        if not locale_value:
            raise HTTPException(status_code=400, detail="locale is required")
        state.set_locale(locale_value)
        return state.snapshot()

    @app.post("/api/launch-config")
    async def api_set_launch_config(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            remote_payload = payload.get("remote")
            if remote_payload is not None and not isinstance(remote_payload, dict):
                raise ValueError("remote must be an object.")
            session_id_text = _optional_string(payload.get("session_id"))
            sandbox_backend_text = _optional_string(payload.get("sandbox_backend"))
            remote_password_text = _optional_string(payload.get("remote_password"))
            if session_id_text:
                await state.validate_remote_session_load(
                    session_id=session_id_text,
                    sandbox_backend=sandbox_backend_text,
                    remote_password=remote_password_text,
                )
            return state.set_launch_config(
                project_dir=_optional_string(payload.get("project_dir")),
                session_id=session_id_text,
                session_mode=_optional_string(payload.get("session_mode")),
                remote=remote_payload if isinstance(remote_payload, dict) else None,
                remote_password=remote_password_text,
                sandbox_backend=sandbox_backend_text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions")
    async def api_sessions() -> dict[str, Any]:
        return {
            "sessions_dir": str(state.sessions_root_dir()),
            "items": state.list_session_directories(),
        }

    @app.get("/api/directories")
    async def api_directories(path: str | None = None) -> dict[str, Any]:
        try:
            return state.browse_project_directories(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/picker/project")
    async def api_pick_project_dir(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            body = payload if isinstance(payload, dict) else {}
            return await state.pick_project_directory(
                _optional_string(body.get("session_mode")),
                sandbox_backend=_optional_string(body.get("sandbox_backend")),
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/picker/session")
    async def api_pick_session_dir(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            body = payload if isinstance(payload, dict) else {}
            return await state.pick_session_directory(
                sandbox_backend=_optional_string(body.get("sandbox_backend")),
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/run")
    async def api_run(payload: dict[str, Any]) -> dict[str, Any]:
        project_dir_text = _optional_string(payload.get("project_dir"))
        session_id_text = _optional_string(payload.get("session_id"))
        model_text = _optional_string(payload.get("model"))
        root_agent_name_text = _optional_string(payload.get("root_agent_name"))
        session_mode_text = _optional_string(payload.get("session_mode"))
        sandbox_backend_text = _optional_string(payload.get("sandbox_backend"))
        remote_password = _optional_string(payload.get("remote_password"))
        raw_enabled_skill_ids = payload.get("enabled_skill_ids")
        if raw_enabled_skill_ids is not None and not isinstance(raw_enabled_skill_ids, list):
            raise HTTPException(status_code=400, detail="enabled_skill_ids must be an array.")
        remote_payload = payload.get("remote")
        if remote_payload is not None and not isinstance(remote_payload, dict):
            raise HTTPException(status_code=400, detail="remote must be an object.")
        running = state.has_running_session()
        # Keep live runtime context intact while a session loop is active.
        # Re-applying launch config during an active run may clone/switch
        # sessions, which breaks "append root to current running session".
        if (
            project_dir_text is not None
            or session_id_text is not None
            or isinstance(remote_payload, dict)
            or remote_password is not None
        ):
            if running:
                current_session_id = _optional_string(state.current_session_id)
                if (
                    session_id_text is not None
                    and (current_session_id is None or session_id_text != current_session_id)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=state.translator.text("already_running"),
                    )
                if project_dir_text is not None and state.project_dir is not None:
                    if Path(project_dir_text).expanduser().resolve() != state.project_dir.resolve():
                        raise HTTPException(
                            status_code=400,
                            detail=state.translator.text("already_running"),
                        )
            else:
                try:
                    state.set_launch_config(
                        project_dir=project_dir_text,
                        session_id=session_id_text,
                        session_mode=session_mode_text,
                        remote=remote_payload if isinstance(remote_payload, dict) else None,
                        remote_password=remote_password,
                        sandbox_backend=sandbox_backend_text,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
        task = str(payload.get("task", "")).strip()
        try:
            start_run_kwargs: dict[str, Any] = {
                "model": model_text,
                "root_agent_name": root_agent_name_text,
            }
            if isinstance(raw_enabled_skill_ids, list):
                start_run_kwargs["enabled_skill_ids"] = [
                    str(item).strip()
                    for item in raw_enabled_skill_ids
                    if str(item).strip()
                ]
            return await state.start_run(task, **start_run_kwargs)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/skills/discover")
    async def api_skills_discover(payload: dict[str, Any] | None = None) -> JSONResponse:
        body = payload if isinstance(payload, dict) else {}
        remote_payload = body.get("remote")
        if remote_payload is not None and not isinstance(remote_payload, dict):
            raise HTTPException(status_code=400, detail="remote must be an object.")
        try:
            result = await state.discover_skills(
                project_dir=_optional_string(body.get("project_dir")),
                remote=remote_payload if isinstance(remote_payload, dict) else None,
                remote_password=_optional_string(body.get("remote_password")),
            )
            return JSONResponse(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/interrupt")
    async def api_interrupt() -> dict[str, Any]:
        return state.interrupt()

    @app.post("/api/terminal/open")
    async def api_terminal_open(payload: dict[str, Any] | None = None) -> JSONResponse:
        try:
            body = payload if isinstance(payload, dict) else {}
            session_id = _optional_string(body.get("session_id"))
            remote_password = _optional_string(body.get("remote_password"))
            if remote_password is None:
                result = state.open_terminal(session_id)
            else:
                result = state.open_terminal(
                    session_id,
                    remote_password=remote_password,
                )
            return JSONResponse(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/remote/validate")
    async def api_remote_validate(payload: dict[str, Any]) -> JSONResponse:
        body = payload if isinstance(payload, dict) else {}
        remote_payload = body.get("remote")
        if not isinstance(remote_payload, dict):
            raise HTTPException(status_code=400, detail="remote is required.")
        try:
            result = await state.validate_remote_workspace(
                remote=remote_payload,
                remote_password=_optional_string(body.get("remote_password")),
                session_mode=_optional_string(body.get("session_mode")),
                sandbox_backend=_optional_string(body.get("sandbox_backend")),
            )
            return JSONResponse(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/events")
    async def api_session_events(
        session_id: str,
        limit: int | None = None,
        before: str | None = None,
        activity_only: bool = False,
        include_agents: bool = True,
    ) -> dict[str, Any]:
        try:
            if limit is None and before is None and not activity_only:
                records = state.load_session_events(session_id)
                before_cursor = None
                has_more_before = False
            else:
                page = state.list_session_events_page(
                    session_id,
                    limit=limit,
                    before=before,
                    activity_only=activity_only,
                )
                records = page.get("events", [])
                before_cursor = page.get("before_cursor")
                has_more_before = bool(page.get("has_more_before", False))
            agents = state.load_session_agents(session_id) if include_agents else []
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "session_id": session_id,
            "events": records,
            "agents": agents,
            "before_cursor": before_cursor,
            "has_more_before": has_more_before,
        }

    @app.get("/api/session/{session_id}/messages")
    async def api_session_messages(
        session_id: str,
        agent_id: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
        tail: int | None = None,
        before: str | None = None,
    ) -> JSONResponse:
        try:
            page = state.list_session_messages_page(
                session_id,
                agent_id=_optional_string(agent_id),
                cursor=cursor,
                limit=limit,
                tail=tail,
                before=before,
            )
            return JSONResponse(
                {
                    "session_id": session_id,
                    "messages": page.get("messages", []),
                    "next_cursor": page.get("next_cursor"),
                    "has_more": bool(page.get("has_more", False)),
                    "before_cursor": page.get("before_cursor"),
                    "has_more_before": bool(page.get("has_more_before", False)),
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/tool-runs")
    async def api_tool_runs(
        session_id: str,
        status: list[str] | None = Query(default=None),
        limit: int | None = None,
        cursor: str | None = None,
    ) -> JSONResponse:
        try:
            status_filter: str | list[str] | None = status
            if isinstance(status_filter, list) and len(status_filter) == 1:
                status_filter = status_filter[0]
            page = state.list_tool_runs_page(
                session_id,
                status=status_filter,
                limit=limit,
                cursor=cursor,
            )
            return JSONResponse(
                {
                    "session_id": session_id,
                    "tool_runs": page.get("tool_runs", []),
                    "next_cursor": page.get("next_cursor"),
                    "has_more": bool(page.get("has_more", False)),
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/tool-runs/metrics")
    async def api_tool_run_metrics(session_id: str) -> JSONResponse:
        try:
            metrics = state.tool_run_metrics(session_id)
            return JSONResponse(metrics)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/tool-runs/{tool_run_id}")
    async def api_tool_run_detail(session_id: str, tool_run_id: str) -> JSONResponse:
        try:
            run = state.get_tool_run(session_id, tool_run_id)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "tool_run": run,
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/session/{session_id}/steers")
    async def api_submit_steer(session_id: str, payload: dict[str, Any]) -> JSONResponse:
        try:
            agent_id = _optional_string(payload.get("agent_id"))
            content = str(payload.get("content", ""))
            source = _optional_string(payload.get("source")) or "webui"
            if not agent_id:
                raise HTTPException(status_code=400, detail="agent_id is required")
            run = await state.submit_steer_run_with_activation(
                session_id,
                agent_id=agent_id,
                content=content,
                source=source,
            )
            return JSONResponse(
                {
                    "session_id": session_id,
                    "steer_run": run,
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/session/{session_id}/agents/{agent_id}/terminate")
    async def api_terminate_agent(session_id: str, agent_id: str, payload: dict[str, Any] | None = None) -> JSONResponse:
        body = payload if isinstance(payload, dict) else {}
        source = _optional_string(body.get("source")) or "webui"
        try:
            result = await state.terminate_agent_with_subtree(
                session_id,
                agent_id=agent_id,
                source=source,
            )
            return JSONResponse(result)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/steer-runs")
    async def api_steer_runs(
        session_id: str,
        status: list[str] | None = Query(default=None),
        limit: int | None = None,
        cursor: str | None = None,
    ) -> JSONResponse:
        try:
            status_filter: str | list[str] | None = status
            if isinstance(status_filter, list) and len(status_filter) == 1:
                status_filter = status_filter[0]
            page = state.list_steer_runs_page(
                session_id,
                status=status_filter,
                limit=limit,
                cursor=cursor,
            )
            return JSONResponse(
                {
                    "session_id": session_id,
                    "steer_runs": page.get("steer_runs", []),
                    "next_cursor": page.get("next_cursor"),
                    "has_more": bool(page.get("has_more", False)),
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/steer-runs/metrics")
    async def api_steer_run_metrics(session_id: str) -> JSONResponse:
        try:
            metrics = state.steer_run_metrics(session_id)
            return JSONResponse(metrics)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/session/{session_id}/steer-runs/{steer_run_id}/cancel")
    async def api_cancel_steer_run(session_id: str, steer_run_id: str) -> JSONResponse:
        try:
            result = state.cancel_steer_run(session_id, steer_run_id)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "steer_run_id": steer_run_id,
                    **result,
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/project-sync/status")
    async def api_project_sync_status(session_id: str) -> JSONResponse:
        try:
            value = state.project_sync_status(session_id)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "status": value.get("status", "none") if isinstance(value, dict) else "none",
                    "state": value,
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/session/{session_id}/project-sync/preview")
    async def api_project_sync_preview(session_id: str) -> JSONResponse:
        try:
            preview = state.project_sync_preview(session_id)
            return JSONResponse(preview)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/session/{session_id}/project-sync/apply")
    async def api_project_sync_apply(session_id: str) -> JSONResponse:
        try:
            result = await state.apply_project_sync(session_id)
            return JSONResponse(result)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/session/{session_id}/project-sync/undo")
    async def api_project_sync_undo(session_id: str) -> JSONResponse:
        try:
            result = await state.undo_project_sync(session_id)
            return JSONResponse(result)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        try:
            return state.read_config()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/config/reload")
    async def api_config_reload() -> dict[str, Any]:
        try:
            return state.read_config()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/config/meta")
    async def api_config_meta() -> dict[str, Any]:
        try:
            return state.read_config_meta()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/config/save")
    async def api_config_save(payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", ""))
        try:
            return state.save_config(text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.websocket("/api/events")
    async def api_events(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = state.event_hub.subscribe()
        await websocket.send_json(
            {
                "event_type": "runtime_state",
                "timestamp": utc_now(),
                "payload": {"snapshot": state.snapshot()},
            }
        )
        try:
            while True:
                first = await queue.get()
                events = [first]
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 0.10
                while True:
                    timeout = deadline - loop.time()
                    if timeout <= 0:
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    events.append(event)
                await websocket.send_json(build_event_batch(events).as_record())
        except WebSocketDisconnect:
            return
        finally:
            state.event_hub.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=str(webui_dir)), name="webui-static")

    @app.get("/")
    async def webui_index() -> FileResponse:
        return FileResponse(webui_dir / "index.html")

    @app.get("/{path:path}")
    async def webui_spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = webui_dir / path
        if candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(webui_dir / "index.html")

    return app


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
