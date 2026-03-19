from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.workspace import WorkspaceManager


class WorkspaceTests(unittest.TestCase):
    def test_create_root_workspace_staged_mode_excludes_existing_skill_bundles(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            (project_dir / "visible.txt").write_text("safe\n", encoding="utf-8")
            stale_bundle = project_dir / ".opencompany_skills" / "other-session"
            stale_bundle.mkdir(parents=True, exist_ok=True)
            (stale_bundle / "manifest.json").write_text("{}", encoding="utf-8")

            session_dir = Path(temp_dir) / "session"
            manager = WorkspaceManager(session_dir)
            root = manager.create_root_workspace(project_dir, mode="staged")

            self.assertTrue((root.path / "visible.txt").exists())
            self.assertFalse((root.path / ".opencompany_skills").exists())

    def test_create_root_workspace_direct_mode_uses_live_project_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            (project_dir / "visible.txt").write_text("safe\n", encoding="utf-8")
            (project_dir / ".env").write_text("SECRET=1\n", encoding="utf-8")

            session_dir = Path(temp_dir) / "session"
            manager = WorkspaceManager(session_dir)
            root = manager.create_root_workspace(project_dir, mode="direct")

            self.assertEqual(root.id, "root")
            self.assertEqual(root.path, project_dir.resolve())
            self.assertEqual(root.base_snapshot_path, project_dir.resolve())
            self.assertFalse(root.readonly)
            self.assertFalse((session_dir / "snapshots" / "root").exists())
            self.assertFalse((session_dir / "snapshots" / "root_base").exists())

    def test_create_root_workspace_direct_mode_preserves_nonexistent_absolute_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "session"
            manager = WorkspaceManager(session_dir)

            root = manager.create_root_workspace(Path("/home/ubuntu/test"), mode="direct")

            self.assertEqual(str(root.path), "/home/ubuntu/test")
            self.assertEqual(str(root.base_snapshot_path), "/home/ubuntu/test")
            self.assertFalse(root.readonly)

    def test_diff_artifact_reports_added_modified_and_deleted_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            (project_dir / "keep.txt").write_text("before\n", encoding="utf-8")
            (project_dir / "remove.txt").write_text("gone\n", encoding="utf-8")

            session_dir = Path(temp_dir) / "session"
            manager = WorkspaceManager(session_dir)
            manager.create_root_workspace(project_dir)
            child = manager.fork_workspace("root", "agent-1")
            (child.path / "keep.txt").write_text("after\n", encoding="utf-8")
            (child.path / "new.txt").write_text("new\n", encoding="utf-8")
            (child.path / "remove.txt").unlink()

            artifact = manager.create_diff_artifact("agent-1", child.id)
            data = json.loads(artifact.artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(data["added"], ["new.txt"])
            self.assertEqual(data["modified"], ["keep.txt"])
            self.assertEqual(data["deleted"], ["remove.txt"])

    def test_root_workspace_excludes_env_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            (project_dir / ".env").write_text("OPENROUTER_API_KEY=secret\n", encoding="utf-8")
            (project_dir / ".env.local").write_text("LOCAL_SECRET=yes\n", encoding="utf-8")
            (project_dir / "visible.txt").write_text("safe\n", encoding="utf-8")

            session_dir = Path(temp_dir) / "session"
            manager = WorkspaceManager(session_dir)
            root = manager.create_root_workspace(project_dir)

            self.assertFalse((root.path / ".env").exists())
            self.assertFalse((root.path / ".env.local").exists())
            self.assertTrue((root.path / "visible.txt").exists())

    def test_diff_artifact_marks_binary_files_without_fake_text_patch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            (project_dir / "sample.doc").write_bytes(b"\xd0\xcf\x11\xe0before")

            session_dir = Path(temp_dir) / "session"
            manager = WorkspaceManager(session_dir)
            manager.create_root_workspace(project_dir)
            child = manager.fork_workspace("root", "agent-1")
            (child.path / "sample.doc").write_bytes(b"\xd0\xcf\x11\xe0after!")

            artifact = manager.create_diff_artifact("agent-1", child.id)
            data = json.loads(artifact.artifact_path.read_text(encoding="utf-8"))

            self.assertEqual(data["modified"], ["sample.doc"])
            self.assertEqual(data["patches"]["sample.doc"], "")
            self.assertEqual(
                data["patch_metadata"]["sample.doc"],
                {"is_binary": True, "before_size": 10, "after_size": 10},
            )

    def test_root_workspace_skips_symlinked_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "project"
            project_dir.mkdir()
            outside_secret = root / "outside_secret.txt"
            outside_secret.write_text("SECRET_TOKEN=outside\n", encoding="utf-8")
            symlink_path = project_dir / "linked_secret.txt"
            try:
                symlink_path.symlink_to(outside_secret)
            except OSError as exc:
                self.skipTest(f"symlink not supported in this environment: {exc}")

            session_dir = root / "session"
            manager = WorkspaceManager(session_dir)
            root_workspace = manager.create_root_workspace(project_dir)

            self.assertFalse((root_workspace.path / "linked_secret.txt").exists())

    def test_workspace_changes_and_apply_skip_symlink_entries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "project"
            project_dir.mkdir()
            (project_dir / "safe.txt").write_text("before\n", encoding="utf-8")
            outside_file = root / "outside.txt"
            outside_file.write_text("outside-content\n", encoding="utf-8")

            session_dir = root / "session"
            manager = WorkspaceManager(session_dir)
            manager.create_root_workspace(project_dir)
            child = manager.fork_workspace("root", "agent-1")
            (child.path / "safe.txt").write_text("after\n", encoding="utf-8")
            linked_path = child.path / "linked.txt"
            try:
                linked_path.symlink_to(outside_file)
            except OSError as exc:
                self.skipTest(f"symlink not supported in this environment: {exc}")

            changes = manager.compute_workspace_changes(child.id)
            touched_paths = set(changes.added + changes.modified + changes.deleted)
            self.assertNotIn("linked.txt", touched_paths)

            applied = manager.apply_workspace_changes(child.id, project_dir)
            applied_paths = set(applied.added + applied.modified + applied.deleted)
            self.assertNotIn("linked.txt", applied_paths)
            self.assertFalse((project_dir / "linked.txt").exists())
            self.assertEqual((project_dir / "safe.txt").read_text(encoding="utf-8"), "after\n")
