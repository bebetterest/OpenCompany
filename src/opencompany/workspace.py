from __future__ import annotations

import difflib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencompany.models import WorkspaceMode, WorkspaceRef, normalize_workspace_mode
from opencompany.utils import ensure_directory, hash_bytes, resolve_in_workspace


IGNORED_NAMES = {
    ".git",
    ".opencompany",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
}


def is_ignored_path(path: Path) -> bool:
    for part in path.parts:
        if part in IGNORED_NAMES:
            return True
        if part == ".env" or part.startswith(".env."):
            return True
    return False


@dataclass(slots=True)
class DiffArtifact:
    artifact_path: Path
    added: list[str]
    modified: list[str]
    deleted: list[str]


@dataclass(slots=True)
class WorkspaceChangeSet:
    added: list[str]
    modified: list[str]
    deleted: list[str]


@dataclass(slots=True)
class FileDiffPreview:
    patch: str
    is_binary: bool
    before_size: int | None = None
    after_size: int | None = None


class WorkspaceManager:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.snapshots_dir = ensure_directory(session_dir / "snapshots")
        self.workspaces_dir = ensure_directory(session_dir / "workspaces")
        self.diff_dir = ensure_directory(session_dir / "diffs")
        self._workspaces: dict[str, WorkspaceRef] = {}

    def register(self, workspace: WorkspaceRef) -> WorkspaceRef:
        self._workspaces[workspace.id] = workspace
        return workspace

    def workspace(self, workspace_id: str) -> WorkspaceRef:
        return self._workspaces[workspace_id]

    def all_workspaces(self) -> dict[str, WorkspaceRef]:
        return dict(self._workspaces)

    def root_workspace(self) -> WorkspaceRef | None:
        return self._workspaces.get("root")

    def create_root_workspace(
        self,
        project_dir: Path,
        mode: WorkspaceMode | str | None = None,
    ) -> WorkspaceRef:
        workspace_mode = normalize_workspace_mode(mode)
        normalized_project_dir = project_dir.expanduser()
        root_base_snapshot = self.snapshots_dir / "root_base"
        root_snapshot = self.snapshots_dir / "root"
        if root_base_snapshot.exists():
            shutil.rmtree(root_base_snapshot)
        if root_snapshot.exists():
            shutil.rmtree(root_snapshot)
        if workspace_mode == WorkspaceMode.STAGED:
            resolved_project_dir = normalized_project_dir.resolve()
            self._copy_tree(resolved_project_dir, root_base_snapshot)
            self._copy_tree(root_base_snapshot, root_snapshot)
            workspace_path = root_snapshot
            base_snapshot_path = root_base_snapshot
            readonly = True
        else:
            # Remote-direct sessions may carry an absolute POSIX path that does
            # not exist on the local host; preserve it verbatim.
            if normalized_project_dir.is_absolute() and not normalized_project_dir.exists():
                workspace_path = normalized_project_dir
            else:
                workspace_path = normalized_project_dir.resolve()
            base_snapshot_path = workspace_path
            readonly = False
        workspace = WorkspaceRef(
            id="root",
            path=workspace_path,
            base_snapshot_path=base_snapshot_path,
            parent_workspace_id=None,
            readonly=readonly,
        )
        return self.register(workspace)

    def fork_workspace(self, parent_workspace_id: str, agent_id: str) -> WorkspaceRef:
        parent = self.workspace(parent_workspace_id)
        base_snapshot = self.snapshots_dir / f"{agent_id}_base"
        workspace_path = self.workspaces_dir / agent_id
        if base_snapshot.exists():
            shutil.rmtree(base_snapshot)
        if workspace_path.exists():
            shutil.rmtree(workspace_path)
        self._copy_tree(parent.path, base_snapshot)
        self._copy_tree(base_snapshot, workspace_path)
        workspace = WorkspaceRef(
            id=f"ws-{agent_id}",
            path=workspace_path,
            base_snapshot_path=base_snapshot,
            parent_workspace_id=parent_workspace_id,
            readonly=False,
        )
        return self.register(workspace)

    def create_diff_artifact(self, agent_id: str, workspace_id: str) -> DiffArtifact:
        workspace = self.workspace(workspace_id)
        changes = self.compute_workspace_changes(workspace_id)
        patch_map: dict[str, str] = {}
        patch_metadata: dict[str, dict[str, Any]] = {}
        for path in changes.modified + changes.added + changes.deleted:
            preview = self.build_file_diff_preview(
                workspace.base_snapshot_path / path,
                workspace.path / path,
                path,
            )
            patch_map[path] = preview.patch
            patch_metadata[path] = {
                "is_binary": preview.is_binary,
                "before_size": preview.before_size,
                "after_size": preview.after_size,
            }
        artifact_path = self.diff_dir / f"{agent_id}.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "agent_id": agent_id,
                    "workspace_id": workspace_id,
                    "added": changes.added,
                    "modified": changes.modified,
                    "deleted": changes.deleted,
                    "patches": patch_map,
                    "patch_metadata": patch_metadata,
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        return DiffArtifact(
            artifact_path=artifact_path,
            added=changes.added,
            modified=changes.modified,
            deleted=changes.deleted,
        )

    def compute_workspace_changes(self, workspace_id: str) -> WorkspaceChangeSet:
        workspace = self.workspace(workspace_id)
        return self._compute_changes_between(
            before_root=workspace.base_snapshot_path,
            after_root=workspace.path,
        )

    def apply_workspace_changes(self, workspace_id: str, destination_root: Path) -> WorkspaceChangeSet:
        destination_root = destination_root.resolve()
        if not destination_root.exists():
            destination_root.mkdir(parents=True, exist_ok=True)
        changes = self.compute_workspace_changes(workspace_id)
        if not (changes.added or changes.modified or changes.deleted):
            return changes
        workspace = self.workspace(workspace_id)
        return self._apply_changes(
            source_root=workspace.path,
            destination_root=destination_root,
            changes=changes,
        )

    def describe_tree(
        self,
        workspace_id: str,
        relative_path: str = ".",
        depth: int = 3,
        max_entries: int = 2000,
    ) -> list[str]:
        workspace = self.workspace(workspace_id)
        root = resolve_in_workspace(workspace.path, relative_path)
        if not root.exists() or not root.is_dir():
            return []
        if root.is_symlink():
            return []
        bounded_max_entries = max(1, min(5000, int(max_entries)))
        entries: list[str] = []
        stack = [root]
        while stack and len(entries) < bounded_max_entries:
            current = stack.pop()
            children = sorted(current.iterdir(), key=lambda path: path.name)
            directories_to_visit: list[Path] = []
            for path in children:
                if path.is_symlink():
                    continue
                if is_ignored_path(path.relative_to(workspace.path)):
                    continue
                rel = path.relative_to(root)
                if len(rel.parts) > depth:
                    continue
                prefix = "/" if path.is_dir() else ""
                entries.append(f"{rel}{prefix}")
                if len(entries) >= bounded_max_entries:
                    break
                if path.is_dir() and len(rel.parts) < depth:
                    directories_to_visit.append(path)
            stack.extend(reversed(directories_to_visit))
        return entries

    def read_text_file(
        self,
        workspace_id: str,
        relative_path: str,
        max_chars: int = 8000,
    ) -> tuple[str, bool]:
        workspace = self.workspace(workspace_id)
        file_path = resolve_in_workspace(workspace.path, relative_path)
        if not file_path.exists():
            raise FileNotFoundError(f"{relative_path} was not found.")
        if file_path.is_dir():
            raise IsADirectoryError(
                f"{relative_path} is a directory. Use shell (for example `ls`) to list entries first."
            )
        if file_path.is_symlink():
            raise FileNotFoundError(f"{relative_path} is not a regular file.")
        if not file_path.is_file():
            raise FileNotFoundError(f"{relative_path} is not a regular file.")
        try:
            with file_path.open("r", encoding="utf-8") as handle:
                text = handle.read(max_chars + 1)
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{relative_path} is not a UTF-8 text file. Use shell for binary or Office documents."
            ) from exc
        if len(text) > max_chars:
            return text[:max_chars], True
        return text, False

    def serialize(self) -> dict[str, dict[str, Any]]:
        return {
            workspace_id: {
                "id": workspace.id,
                "path": str(workspace.path),
                "base_snapshot_path": str(workspace.base_snapshot_path),
                "parent_workspace_id": workspace.parent_workspace_id,
                "readonly": workspace.readonly,
            }
            for workspace_id, workspace in self._workspaces.items()
        }

    @classmethod
    def from_state(cls, session_dir: Path, data: dict[str, dict[str, Any]]) -> "WorkspaceManager":
        manager = cls(session_dir)
        manager._workspaces = {
            workspace_id: WorkspaceRef(
                id=payload["id"],
                path=Path(payload["path"]),
                base_snapshot_path=Path(payload["base_snapshot_path"]),
                parent_workspace_id=payload.get("parent_workspace_id"),
                readonly=bool(payload.get("readonly", False)),
            )
            for workspace_id, payload in data.items()
        }
        return manager

    def _copy_tree(self, source: Path, destination: Path) -> None:
        source = source.resolve()

        def _ignore(current_dir: str, names: list[str]) -> set[str]:
            current = Path(current_dir)
            ignored: set[str] = set()
            for name in names:
                candidate = current / name
                if candidate.is_symlink():
                    ignored.add(name)
                    continue
                if is_ignored_path(candidate.relative_to(source)):
                    ignored.add(name)
            return ignored

        shutil.copytree(
            source,
            destination,
            ignore=_ignore,
            dirs_exist_ok=False,
        )

    def _manifest(self, root: Path) -> dict[str, dict[str, Any]]:
        manifest: dict[str, dict[str, Any]] = {}
        if not root.exists():
            return manifest
        for current_root, dir_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            base = Path(current_root)
            allowed_dirs: list[str] = []
            for directory in sorted(dir_names):
                candidate = base / directory
                relative = candidate.relative_to(root)
                if candidate.is_symlink() or is_ignored_path(relative):
                    continue
                allowed_dirs.append(directory)
            dir_names[:] = allowed_dirs

            for filename in sorted(file_names):
                file_path = base / filename
                relative = file_path.relative_to(root)
                if file_path.is_symlink() or is_ignored_path(relative):
                    continue
                if not file_path.is_file():
                    continue
                content = file_path.read_bytes()
                manifest[str(relative)] = {
                    "hash": hash_bytes(content),
                    "size": len(content),
                }
        return manifest

    def _compute_changes_between(self, *, before_root: Path, after_root: Path) -> WorkspaceChangeSet:
        before = self._manifest(before_root)
        after = self._manifest(after_root)
        before_paths = set(before)
        after_paths = set(after)
        added = sorted(after_paths - before_paths)
        deleted = sorted(before_paths - after_paths)
        modified = sorted(
            path for path in before_paths & after_paths if before[path]["hash"] != after[path]["hash"]
        )
        return WorkspaceChangeSet(added=added, modified=modified, deleted=deleted)

    def _apply_changes(
        self,
        *,
        source_root: Path,
        destination_root: Path,
        changes: WorkspaceChangeSet,
    ) -> WorkspaceChangeSet:
        applied_added: list[str] = []
        applied_modified: list[str] = []
        applied_deleted: list[str] = []

        for relative_path in changes.added:
            path_obj = Path(relative_path)
            if is_ignored_path(path_obj):
                continue
            source_path = resolve_in_workspace(source_root, relative_path)
            destination_path = resolve_in_workspace(destination_root, relative_path)
            if (
                source_path.is_symlink()
                or not source_path.exists()
                or not source_path.is_file()
            ):
                continue
            if destination_path.exists() and destination_path.is_symlink():
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            applied_added.append(relative_path)

        for relative_path in changes.modified:
            path_obj = Path(relative_path)
            if is_ignored_path(path_obj):
                continue
            source_path = resolve_in_workspace(source_root, relative_path)
            destination_path = resolve_in_workspace(destination_root, relative_path)
            if (
                source_path.is_symlink()
                or not source_path.exists()
                or not source_path.is_file()
            ):
                continue
            if destination_path.exists() and destination_path.is_symlink():
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            applied_modified.append(relative_path)

        for relative_path in changes.deleted:
            path_obj = Path(relative_path)
            if is_ignored_path(path_obj):
                continue
            destination_path = resolve_in_workspace(destination_root, relative_path)
            if destination_path.is_file() or destination_path.is_symlink():
                destination_path.unlink()
                self._prune_empty_parents(destination_path.parent, destination_root)
                applied_deleted.append(relative_path)

        return WorkspaceChangeSet(
            added=sorted(applied_added),
            modified=sorted(applied_modified),
            deleted=sorted(applied_deleted),
        )

    def _prune_empty_parents(self, directory: Path, stop_at: Path) -> None:
        current = directory
        stop_at = stop_at.resolve()
        while current.resolve() != stop_at:
            if not current.exists() or not current.is_dir():
                break
            if any(current.iterdir()):
                break
            current.rmdir()
            current = current.parent

    def build_file_diff_preview(
        self,
        before_path: Path,
        after_path: Path,
        relative_path: str,
    ) -> FileDiffPreview:
        before_lines, before_is_binary, before_size = self._load_diff_source(before_path)
        after_lines, after_is_binary, after_size = self._load_diff_source(after_path)
        if before_is_binary or after_is_binary:
            return FileDiffPreview(
                patch="",
                is_binary=True,
                before_size=before_size,
                after_size=after_size,
            )
        return FileDiffPreview(
            patch="\n".join(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"a/{relative_path}",
                    tofile=f"b/{relative_path}",
                    lineterm="",
                )
            ),
            is_binary=False,
            before_size=before_size,
            after_size=after_size,
        )

    def _unified_diff(self, before_path: Path, after_path: Path, relative_path: str) -> str:
        return self.build_file_diff_preview(before_path, after_path, relative_path).patch

    def _load_diff_source(self, path: Path) -> tuple[list[str], bool, int | None]:
        if not path.exists():
            return [], False, None
        if path.is_symlink():
            return [], True, None
        size = path.stat().st_size
        try:
            return path.read_text(encoding="utf-8").splitlines(), False, size
        except UnicodeDecodeError:
            return [], True, size
