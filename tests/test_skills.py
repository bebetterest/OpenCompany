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
from opencompany.skills import (
    build_manifest_payload,
    copy_local_skill_tree,
    describe_skill_drift,
    discover_local_skills,
    is_skill_bundle_relative_path,
    load_local_skill_descriptor,
    normalize_skill_id,
    normalize_skill_ids,
    render_skills_prompt,
    skill_bundle_root_relative,
    skill_manifest_relative,
    skills_catalog_for_agent,
)


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


class SkillsHelperTests(unittest.TestCase):
    def test_normalize_skill_helpers_validate_and_preserve_order(self) -> None:
        self.assertEqual(normalize_skill_id(" skill.demo-1 "), "skill.demo-1")
        self.assertEqual(
            normalize_skill_ids(["skill-a", " skill-b ", "skill-a"]),
            ["skill-a", "skill-b"],
        )
        with self.assertRaisesRegex(ValueError, "Skill id is required"):
            normalize_skill_id(" ")
        with self.assertRaisesRegex(ValueError, "Invalid skill id"):
            normalize_skill_id("bad skill")

    def test_skill_bundle_path_helpers_match_materialized_paths(self) -> None:
        session_id = "session-123"
        bundle_root = skill_bundle_root_relative(session_id)
        manifest_path = skill_manifest_relative(session_id)

        self.assertEqual(bundle_root, ".opencompany_skills/session-123")
        self.assertEqual(manifest_path, ".opencompany_skills/session-123/manifest.json")
        self.assertTrue(
            is_skill_bundle_relative_path(
                ".opencompany_skills/session-123/skill-a/SKILL.md",
                session_id,
            )
        )
        self.assertTrue(is_skill_bundle_relative_path(bundle_root, session_id))
        self.assertFalse(
            is_skill_bundle_relative_path(
                ".opencompany_skills/session-999/skill-a/SKILL.md",
                session_id,
            )
        )

    def test_manifest_prompt_and_catalog_helpers_render_localized_entries(self) -> None:
        payload = build_manifest_payload(
            session_id="session-123",
            bundle_root=".opencompany_skills/session-123",
            entries=[
                {
                    "id": "skill-a",
                    "name": "Skill A",
                    "name_cn": "技能 A",
                    "description": "demo",
                    "description_cn": "示例",
                    "main_doc_project_path": ".opencompany_skills/session-123/skill-a/SKILL.md",
                    "localized_doc_project_path": ".opencompany_skills/session-123/skill-a/SKILL_cn.md",
                    "resource_count": 2,
                },
                {"id": " "},
            ],
            warnings=[
                {
                    "message": "warn-en",
                    "message_cn": "warn-zh",
                }
            ],
        )

        self.assertEqual(payload["enabled_skill_ids"], ["skill-a"])
        prompt_en = render_skills_prompt(
            locale="en",
            bundle_root=".opencompany_skills/session-123",
            manifest_path=".opencompany_skills/session-123/manifest.json",
            skills_state=payload,
        )
        prompt_zh = render_skills_prompt(
            locale="zh",
            bundle_root=".opencompany_skills/session-123",
            manifest_path=".opencompany_skills/session-123/manifest.json",
            skills_state=payload,
        )
        catalog_zh = skills_catalog_for_agent(
            session_id="session-123",
            locale="zh",
            skills_state=payload,
        )

        self.assertIn("Enabled Skills:", prompt_en)
        self.assertIn("warning: warn-en", prompt_en)
        self.assertIn("已启用 Skills:", prompt_zh)
        self.assertIn("告警: warn-zh", prompt_zh)
        self.assertEqual(
            catalog_zh["entries"][0]["preferred_doc_project_path"],
            ".opencompany_skills/session-123/skill-a/SKILL_cn.md",
        )
        self.assertEqual(
            catalog_zh["manifest_path"],
            ".opencompany_skills/session-123/manifest.json",
        )

    def test_describe_skill_drift_detects_file_and_source_changes(self) -> None:
        current_entry = {
            "source_type": "project",
            "source_path": "/tmp/project/skills/skill-a",
            "files": [
                {
                    "relative_path": "SKILL.md",
                    "sha256": "hash-a",
                    "size": 10,
                    "mode": "644",
                    "is_executable": False,
                }
            ],
        }

        self.assertFalse(describe_skill_drift(None, current_entry))
        self.assertTrue(
            describe_skill_drift(
                {
                    **current_entry,
                    "files": [
                        {
                            "relative_path": "SKILL.md",
                            "sha256": "hash-b",
                            "size": 10,
                            "mode": "644",
                            "is_executable": False,
                        }
                    ],
                },
                current_entry,
            )
        )
        self.assertTrue(
            describe_skill_drift(
                {**current_entry, "source_path": "/tmp/other/skills/skill-a"},
                current_entry,
            )
        )

    def test_discover_local_skills_prefers_project_source_and_falls_back_without_cn_doc(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            write_skill(app_dir, "shared-skill")
            write_skill(project_dir, "shared-skill")
            write_skill(project_dir, "english-only")
            (project_dir / "skills" / "shared-skill" / "SKILL.md").write_text(
                "# project override\n",
                encoding="utf-8",
            )
            (project_dir / "skills" / "english-only" / "SKILL_cn.md").unlink()

            discovered = discover_local_skills(app_dir=app_dir, project_dir=project_dir)

            self.assertEqual(discovered["shared-skill"].source_type, "project")
            self.assertEqual(
                Path(discovered["shared-skill"].source_path),
                (project_dir / "skills" / "shared-skill").resolve(),
            )
            self.assertEqual(
                discovered["english-only"].localized_doc_path,
                "SKILL.md",
            )

    def test_load_local_skill_descriptor_and_copy_ignore_symlinks(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "skills"
            source_root.mkdir()
            write_skill(root, "demo-skill")
            skill_dir = source_root / "demo-skill"
            external = root / "external.txt"
            external.write_text("outside\n", encoding="utf-8")
            (skill_dir / "resources" / "linked.txt").symlink_to(external)

            descriptor = load_local_skill_descriptor(
                source_root=source_root,
                skill_dir=skill_dir,
                source_type="project",
            )

            files_by_path = {
                entry.relative_path: entry
                for entry in descriptor.files
            }
            self.assertNotIn("resources/linked.txt", files_by_path)
            self.assertTrue(files_by_path["resources/blob.bin"].is_binary)
            self.assertTrue(files_by_path["resources/run.sh"].is_executable)
            self.assertEqual(files_by_path["resources/run.sh"].mode, "755")

            destination = root / "bundle" / "demo-skill"
            copy_local_skill_tree(skill_dir, destination)
            self.assertFalse((destination / "resources" / "linked.txt").exists())
            self.assertEqual(
                (destination / "resources" / "blob.bin").read_bytes(),
                b"\x00\x01skill\xff",
            )

    def test_load_local_skill_descriptor_rejects_invalid_layout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "skills"
            source_root.mkdir()
            write_skill(root, "demo-skill")
            skill_dir = source_root / "demo-skill"

            (skill_dir / "skill.toml").write_text(
                "\n".join(
                    [
                        "[skill]",
                        'id = "other-skill"',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not match directory"):
                load_local_skill_descriptor(
                    source_root=source_root,
                    skill_dir=skill_dir,
                    source_type="project",
                )

            write_skill(root, "missing-main-doc")
            invalid_dir = source_root / "missing-main-doc"
            (invalid_dir / "SKILL.md").unlink()
            with self.assertRaisesRegex(ValueError, "main doc is missing"):
                load_local_skill_descriptor(
                    source_root=source_root,
                    skill_dir=invalid_dir,
                    source_type="project",
                )


class SkillsFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_default_skills_follow_opencompany_layout(self) -> None:
        app_dir = default_app_dir()
        bundled_ids = {"hf-cli", "openai-docs", "pdf", "skill-creator", "skill-installer"}

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

    async def test_discover_skills_includes_resource_count_for_local_skills(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            project_dir = root / "project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_app(app_dir)
            write_skill(project_dir, "project-demo")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            discovered = await orchestrator.discover_skills(project_dir=project_dir)
            by_id = {str(item.get("id", "")): item for item in discovered}

            self.assertIn("project-demo", by_id)
            self.assertGreater(int(by_id["project-demo"].get("resource_count", 0) or 0), 0)

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

    async def test_remote_skill_discovery_cleans_ephemeral_session_artifacts_on_failure(
        self,
    ) -> None:
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

            def _fake_apply_remote_runtime(**kwargs) -> None:  # type: ignore[no-untyped-def]
                orchestrator._persist_session_remote_config(
                    str(kwargs["session_id"]),
                    remote_config,
                )

            async def _fake_run_remote_shell_command(*, session_id: str, command: str):  # type: ignore[no-untyped-def]
                del session_id, command
                return ShellCommandResult(
                    exit_code=1,
                    stdout="",
                    stderr="find failed",
                    command="find",
                )

            orchestrator._apply_session_remote_runtime = _fake_apply_remote_runtime  # type: ignore[method-assign]
            orchestrator._run_remote_shell_command = _fake_run_remote_shell_command  # type: ignore[method-assign]

            with self.assertRaisesRegex(ValueError, "find failed"):
                await orchestrator.discover_skills(remote_config=remote_config)

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

    async def test_run_task_skips_missing_skill_and_records_warning(self) -> None:
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
                "Use the selected skills.",
                enabled_skill_ids=["missing-skill", "project-demo"],
            )

            self.assertEqual(session.enabled_skill_ids, ["project-demo"])
            self.assertEqual(
                [warning["type"] for warning in session.skills_state["warnings"]],
                ["missing_source"],
            )
            self.assertEqual(
                session.skills_state["warnings"][0]["skill_id"],
                "missing-skill",
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

    async def test_resume_without_explicit_skill_ids_keeps_existing_selection(self) -> None:
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
            session = await orchestrator.run_task(
                "First run.",
                enabled_skill_ids=["skill-a"],
            )

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
                "Continue with existing skills.",
            )

            bundle_root = project_dir / ".opencompany_skills" / resumed.id
            self.assertEqual(resumed.enabled_skill_ids, ["skill-a"])
            self.assertTrue((bundle_root / "skill-a").exists())

    async def test_resume_rebuilds_skill_when_source_content_drifts(self) -> None:
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
            session = await orchestrator.run_task(
                "First run.",
                enabled_skill_ids=["skill-a"],
            )

            (project_dir / "skills" / "skill-a" / "SKILL.md").write_text(
                "# skill-a updated\n",
                encoding="utf-8",
            )
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
                "Rebuild skills after source changes.",
            )

            bundle_root = project_dir / ".opencompany_skills" / resumed.id
            self.assertEqual(resumed.enabled_skill_ids, ["skill-a"])
            self.assertEqual(
                [warning["type"] for warning in resumed.skills_state["warnings"]],
                ["content_drift"],
            )
            self.assertEqual(
                (bundle_root / "skill-a" / "SKILL.md").read_text(encoding="utf-8"),
                "# skill-a updated\n",
            )

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
