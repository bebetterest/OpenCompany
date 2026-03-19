from __future__ import annotations

import json
import stat
import unittest
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

from opencompany.llm.openrouter import ChatResult
from opencompany.models import ShellCommandResult
from opencompany.orchestrator import Orchestrator, default_app_dir
from opencompany.remote import normalize_remote_session_config
from opencompany.skills import discover_local_skills


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._index = 0

    async def stream_chat(self, **kwargs) -> ChatResult:
        response = self._responses[self._index]
        self._index += 1
        on_token = kwargs.get("on_token")
        if on_token and response:
            maybe = on_token(response)
            if hasattr(maybe, "__await__"):
                await maybe
        return ChatResult(content=response, raw_events=[])


def build_test_app(app_dir: Path) -> None:
    copytree(default_app_dir() / "prompts", app_dir / "prompts")
    (app_dir / "opencompany.toml").write_text(
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


def write_skill(root: Path, skill_id: str) -> None:
    skill_dir = root / "skills" / skill_id
    resources_dir = skill_dir / "resources"
    resources_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.toml").write_text(
        "\n".join(
            [
                "[skill]",
                f'id = "{skill_id}"',
                f'name = "{skill_id} name"',
                f'name_cn = "{skill_id} 名称"',
                f'description = "{skill_id} description"',
                f'description_cn = "{skill_id} 中文说明"',
                'tags = ["demo"]',
            ]
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(f"# {skill_id}\n", encoding="utf-8")
    (skill_dir / "SKILL_cn.md").write_text(f"# {skill_id} 中文\n", encoding="utf-8")
    script_path = resources_dir / "run.sh"
    script_path.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script_path.chmod(0o755)
    (resources_dir / "blob.bin").write_bytes(b"\x00\x01skill\xff")


class SkillsFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_default_skills_follow_opencompany_layout(self) -> None:
        app_dir = default_app_dir()
        bundled_ids = {"openai-docs", "pdf", "skill-creator", "skill-installer"}

        discovered = discover_local_skills(app_dir=app_dir)

        self.assertTrue(bundled_ids.issubset(discovered))
        for skill_id in bundled_ids:
            skill_dir = app_dir / "skills" / skill_id
            self.assertTrue((skill_dir / "skill.toml").is_file())
            self.assertTrue((skill_dir / "SKILL.md").is_file())
            self.assertTrue((skill_dir / "SKILL_cn.md").is_file())
            self.assertTrue((skill_dir / "resources").is_dir())
            for legacy_name in ("agents", "scripts", "references", "assets"):
                self.assertFalse((skill_dir / legacy_name).exists())

    async def test_remote_skill_discovery_skips_invalid_candidates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_app(app_dir)
            (project_dir / "README.md").write_text("demo\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            remote_config = normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com:22",
                    "remote_dir": "/home/demo/workspace",
                    "auth_mode": "key",
                    "identity_file": "~/.ssh/id_ed25519",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "linux",
                }
            )
            invalid_skill_root = "/home/demo/workspace/skills/bad-skill"
            valid_skill_root = "/home/demo/workspace/skills/good-skill"

            def _fake_apply_remote_runtime(**kwargs) -> None:  # type: ignore[no-untyped-def]
                orchestrator._persist_session_remote_config(
                    str(kwargs["session_id"]),
                    remote_config,
                )

            async def _fake_run_remote_shell_command(*, session_id: str, command: str):  # type: ignore[no-untyped-def]
                del session_id
                if "find \"$skills_root\"" in command:
                    return ShellCommandResult(
                        exit_code=0,
                        stdout=f"{invalid_skill_root}\n{valid_skill_root}\n",
                        stderr="",
                        command=command,
                    )
                if "base64 < \"$target\"" in command:
                    import base64

                    if "bad-skill" in command:
                        payload = "[skill]\nid = \"bad-skill\"\nname = "
                    else:
                        payload = "\n".join(
                            [
                                "[skill]",
                                'id = "good-skill"',
                                'name = "Good Skill"',
                                'name_cn = "好 Skill"',
                                'description = "valid"',
                                'description_cn = "有效"',
                            ]
                        )
                    return ShellCommandResult(
                        exit_code=0,
                        stdout=base64.b64encode(payload.encode("utf-8")).decode("ascii"),
                        stderr="",
                        command=command,
                    )
                if 'if [ -e "$target" ]; then printf "0"; else printf "1"; fi' in command:
                    missing = "SKILL_cn.md" in command
                    return ShellCommandResult(
                        exit_code=0,
                        stdout="1" if missing else "0",
                        stderr="",
                        command=command,
                    )
                raise AssertionError(f"Unexpected remote command: {command}")

            orchestrator._apply_session_remote_runtime = _fake_apply_remote_runtime  # type: ignore[method-assign]
            orchestrator._run_remote_shell_command = _fake_run_remote_shell_command  # type: ignore[method-assign]

            skills = await orchestrator.discover_skills(remote_config=remote_config)

            self.assertEqual([item["id"] for item in skills], ["good-skill"])

    async def test_remote_skill_discovery_cleans_ephemeral_session_artifacts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_app(app_dir)
            (project_dir / "README.md").write_text("demo\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            remote_config = normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com:22",
                    "remote_dir": "/home/demo/workspace",
                    "auth_mode": "key",
                    "identity_file": "~/.ssh/id_ed25519",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "linux",
                }
            )
            remote_skill_root = "/home/demo/workspace/skills/remote-demo"

            def _fake_apply_remote_runtime(**kwargs) -> None:  # type: ignore[no-untyped-def]
                orchestrator._persist_session_remote_config(
                    str(kwargs["session_id"]),
                    remote_config,
                )

            async def _fake_run_remote_shell_command(*, session_id: str, command: str):  # type: ignore[no-untyped-def]
                del session_id
                if "find \"$skills_root\"" in command:
                    return ShellCommandResult(
                        exit_code=0,
                        stdout=f"{remote_skill_root}\n",
                        stderr="",
                        command=command,
                    )
                if "base64 < \"$target\"" in command:
                    payload = "\n".join(
                        [
                            "[skill]",
                            'id = "remote-demo"',
                            'name = "Remote Demo"',
                            'name_cn = "远程 Demo"',
                            'description = "remote skill"',
                            'description_cn = "远程 skill"',
                        ]
                    )
                    import base64

                    return ShellCommandResult(
                        exit_code=0,
                        stdout=base64.b64encode(payload.encode("utf-8")).decode("ascii"),
                        stderr="",
                        command=command,
                    )
                if 'if [ -e "$target" ]; then printf "0"; else printf "1"; fi' in command:
                    missing = "SKILL_cn.md" in command
                    return ShellCommandResult(
                        exit_code=0,
                        stdout="1" if missing else "0",
                        stderr="",
                        command=command,
                    )
                raise AssertionError(f"Unexpected remote command: {command}")

            orchestrator._apply_session_remote_runtime = _fake_apply_remote_runtime  # type: ignore[method-assign]
            orchestrator._run_remote_shell_command = _fake_run_remote_shell_command  # type: ignore[method-assign]

            skills = await orchestrator.discover_skills(remote_config=remote_config)

            self.assertEqual([item["id"] for item in skills], ["remote-demo"])
            leaked = [
                path.name
                for path in orchestrator.paths.sessions_dir.iterdir()
                if path.name.startswith("remote-skills-")
            ]
            self.assertEqual(leaked, [])

    async def test_run_task_materializes_selected_skill_bundle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_app(app_dir)
            (project_dir / "README.md").write_text("demo\n", encoding="utf-8")
            write_skill(project_dir, "project-demo")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "done",
                                }
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task(
                "Use the selected skill.",
                enabled_skill_ids=["project-demo"],
            )

            bundle_root = project_dir / ".opencompany_skills" / session.id
            skill_dir = bundle_root / "project-demo"
            manifest = json.loads((bundle_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(session.enabled_skill_ids, ["project-demo"])
            self.assertTrue(skill_dir.is_dir())
            self.assertEqual(manifest["enabled_skill_ids"], ["project-demo"])
            self.assertEqual(
                (skill_dir / "resources" / "blob.bin").read_bytes(),
                b"\x00\x01skill\xff",
            )
            self.assertTrue(
                bool((skill_dir / "resources" / "run.sh").stat().st_mode & stat.S_IXUSR)
            )

            loaded = orchestrator.load_session_context(session.id)
            self.assertEqual(loaded.enabled_skill_ids, ["project-demo"])
            self.assertEqual(loaded.skill_bundle_root, f".opencompany_skills/{session.id}")

            agent_rows = orchestrator.storage.load_agents(session.id)
            metadata = json.loads(str(agent_rows[0]["metadata_json"]))
            self.assertEqual(
                [entry["id"] for entry in metadata["skills_catalog"]["entries"]],
                ["project-demo"],
            )

    async def test_resume_replaces_skill_bundle_and_removes_old_materialization(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_app(app_dir)
            (project_dir / "README.md").write_text("demo\n", encoding="utf-8")
            write_skill(project_dir, "skill-a")
            write_skill(project_dir, "skill-b")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "first",
                                }
                            ]
                        }
                    )
                ]
            )
            session = await orchestrator.run_task(
                "First run.",
                enabled_skill_ids=["skill-a"],
            )
            first_bundle_root = project_dir / ".opencompany_skills" / session.id
            self.assertTrue((first_bundle_root / "skill-a").exists())

            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "second",
                                }
                            ]
                        }
                    )
                ]
            )
            resumed = await orchestrator.resume(
                session.id,
                "Switch skills.",
                enabled_skill_ids=["skill-b"],
            )

            manifest = json.loads((first_bundle_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed.enabled_skill_ids, ["skill-b"])
            self.assertFalse((first_bundle_root / "skill-a").exists())
            self.assertTrue((first_bundle_root / "skill-b").exists())
            self.assertEqual(manifest["enabled_skill_ids"], ["skill-b"])

    async def test_resume_staged_clone_rebuilds_current_bundle_and_removes_stale_bundle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_app(app_dir)
            (project_dir / "README.md").write_text("demo\n", encoding="utf-8")
            write_skill(project_dir, "skill-a")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "first",
                                }
                            ]
                        }
                    )
                ]
            )
            source_session = await orchestrator.run_task(
                "First staged run.",
                workspace_mode="staged",
                enabled_skill_ids=["skill-a"],
            )
            source_bundle = (
                orchestrator.paths.session_dir(source_session.id)
                / "snapshots"
                / "root"
                / ".opencompany_skills"
                / source_session.id
            )
            self.assertTrue(source_bundle.exists())

            cloned = orchestrator.clone_session(source_session.id)
            cloned_root = orchestrator.paths.session_dir(cloned.id) / "snapshots" / "root"
            stale_bundle = cloned_root / ".opencompany_skills" / source_session.id
            self.assertTrue(stale_bundle.exists())

            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "second",
                                }
                            ]
                        }
                    )
                ]
            )
            resumed = await orchestrator.resume(
                cloned.id,
                "Resume cloned staged session.",
            )

            current_bundle = cloned_root / ".opencompany_skills" / resumed.id
            self.assertEqual(resumed.enabled_skill_ids, ["skill-a"])
            self.assertFalse(stale_bundle.exists())
            self.assertTrue(current_bundle.exists())

    async def test_clone_rewrites_agent_skills_catalog_to_new_session_bundle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_app(app_dir)
            (project_dir / "README.md").write_text("demo\n", encoding="utf-8")
            write_skill(project_dir, "skill-a")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "done",
                                }
                            ]
                        }
                    )
                ]
            )
            source_session = await orchestrator.run_task(
                "Run with skills.",
                enabled_skill_ids=["skill-a"],
            )

            cloned = orchestrator.clone_session(source_session.id)
            cloned_agent_rows = orchestrator._session_agent_rows(cloned.id)
            self.assertTrue(cloned_agent_rows)
            metadata = json.loads(cloned_agent_rows[0]["metadata_json"])
            self.assertEqual(
                metadata["skills_catalog"]["bundle_root"],
                f".opencompany_skills/{cloned.id}",
            )
            self.assertEqual(
                [
                    entry["main_doc_project_path"]
                    for entry in metadata["skills_catalog"]["entries"]
                ],
                [f".opencompany_skills/{cloned.id}/skill-a/SKILL.md"],
            )
