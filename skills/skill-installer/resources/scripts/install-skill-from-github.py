#!/usr/bin/env python3
"""Import GitHub-hosted skills into an OpenCompany skills directory."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path

from github_utils import github_request

DEFAULT_REF = "main"
DEFAULT_DEST = "skills"
LEGACY_RESOURCE_DIRS = ("scripts", "references", "assets")
LEGACY_HINT_PATTERNS = (
    r"\bCodex\b",
    r"\$CODEX_HOME",
    r"\bcodex mcp\b",
    r"Restart Codex",
    r"mcp__",
)


@dataclass
class Args:
    url: str | None = None
    repo: str | None = None
    path: list[str] | None = None
    ref: str = DEFAULT_REF
    dest: str | None = None
    name: str | None = None
    method: str = "auto"


@dataclass
class Source:
    owner: str
    repo: str
    ref: str
    paths: list[str]
    repo_url: str | None = None


class InstallError(Exception):
    pass


def _tmp_root() -> str:
    base = os.path.join(tempfile.gettempdir(), "opencompany-skill-import")
    os.makedirs(base, exist_ok=True)
    return base


def _request(url: str) -> bytes:
    return github_request(url, "opencompany-skill-import")


def _parse_github_url(url: str, default_ref: str) -> tuple[str, str, str, str | None]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "github.com":
        raise InstallError("Only GitHub URLs are supported.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise InstallError("Invalid GitHub URL.")
    owner, repo = parts[0], parts[1]
    ref = default_ref
    subpath = ""
    if len(parts) > 2:
        if parts[2] in ("tree", "blob"):
            if len(parts) < 4:
                raise InstallError("GitHub URL missing ref or path.")
            ref = parts[3]
            subpath = "/".join(parts[4:])
        else:
            subpath = "/".join(parts[2:])
    return owner, repo, ref, subpath or None


def _download_repo_zip(owner: str, repo: str, ref: str, dest_dir: str) -> str:
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    zip_path = os.path.join(dest_dir, "repo.zip")
    try:
        payload = _request(zip_url)
    except urllib.error.HTTPError as exc:
        raise InstallError(f"Download failed: HTTP {exc.code}") from exc
    with open(zip_path, "wb") as handle:
        handle.write(payload)
    with zipfile.ZipFile(zip_path, "r") as archive:
        _safe_extract_zip(archive, dest_dir)
        top_levels = {name.split("/")[0] for name in archive.namelist() if name}
    if len(top_levels) != 1:
        raise InstallError("Unexpected archive layout.")
    return os.path.join(dest_dir, next(iter(top_levels)))


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: str) -> None:
    dest_root = os.path.realpath(dest_dir)
    for info in zip_file.infolist():
        extracted_path = os.path.realpath(os.path.join(dest_dir, info.filename))
        if extracted_path == dest_root or extracted_path.startswith(dest_root + os.sep):
            continue
        raise InstallError("Archive contains files outside the destination.")
    zip_file.extractall(dest_dir)


def _run_git(args: list[str]) -> None:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise InstallError(result.stderr.strip() or "Git command failed.")


def _git_sparse_checkout(repo_url: str, ref: str, paths: list[str], dest_dir: str) -> str:
    repo_dir = os.path.join(dest_dir, "repo")
    clone_cmd = [
        "git",
        "clone",
        "--filter=blob:none",
        "--depth",
        "1",
        "--sparse",
        "--single-branch",
        "--branch",
        ref,
        repo_url,
        repo_dir,
    ]
    try:
        _run_git(clone_cmd)
    except InstallError:
        _run_git(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth",
                "1",
                "--sparse",
                "--single-branch",
                repo_url,
                repo_dir,
            ]
        )
    _run_git(["git", "-C", repo_dir, "sparse-checkout", "set", *paths])
    _run_git(["git", "-C", repo_dir, "checkout", ref])
    return repo_dir


def _prepare_repo(source: Source, method: str, tmp_dir: str) -> str:
    if method in ("download", "auto"):
        try:
            return _download_repo_zip(source.owner, source.repo, source.ref, tmp_dir)
        except InstallError as exc:
            if method == "download":
                raise
            if not any(code in str(exc) for code in ("HTTP 401", "HTTP 403", "HTTP 404")):
                raise
    if method in ("git", "auto"):
        repo_url = source.repo_url or f"https://github.com/{source.owner}/{source.repo}.git"
        try:
            return _git_sparse_checkout(repo_url, source.ref, source.paths, tmp_dir)
        except InstallError:
            ssh_url = f"git@github.com:{source.owner}/{source.repo}.git"
            return _git_sparse_checkout(ssh_url, source.ref, source.paths, tmp_dir)
    raise InstallError("Unsupported download method.")


def _validate_relative_path(path: str) -> None:
    normalized = os.path.normpath(path)
    if os.path.isabs(path) or normalized.startswith(".."):
        raise InstallError("Skill path must stay inside the repository.")


def _validate_skill_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise InstallError("Skill name must be a single path segment.")


def _extract_frontmatter(skill_doc: Path) -> tuple[dict[str, str], str]:
    content = skill_doc.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return {}, content
    _, remainder = content.split("---\n", 1)
    if "\n---\n" not in remainder:
        return {}, content
    frontmatter_block, body = remainder.split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in frontmatter_block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in metadata:
            metadata[key] = value
    return metadata, body.lstrip()


def _title_case(skill_name: str) -> str:
    return " ".join(part.capitalize() for part in skill_name.replace("_", "-").split("-") if part)


def _toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_skill_toml(skill_dir: Path, *, skill_name: str, frontmatter: dict[str, str]) -> None:
    metadata_path = skill_dir / "skill.toml"
    if metadata_path.exists():
        return
    name = frontmatter.get("name") or _title_case(skill_name)
    description = (
        frontmatter.get("description")
        or "Imported skill. Review and refine this metadata before relying on the skill."
    )
    description_cn = "导入的 skill。请在使用前补充并核对中文说明。"
    metadata_path.write_text(
        "\n".join(
            [
                "[skill]",
                f"id = {_toml_quote(skill_name)}",
                f"name = {_toml_quote(name)}",
                f"name_cn = {_toml_quote(name)}",
                f"description = {_toml_quote(description)}",
                f"description_cn = {_toml_quote(description_cn)}",
                'tags = ["imported"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _merge_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, copy_function=shutil.copy2, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)
    shutil.rmtree(source, ignore_errors=True)


def _normalize_skill(skill_dir: Path, *, skill_name: str) -> list[str]:
    skill_doc = skill_dir / "SKILL.md"
    if not skill_doc.is_file():
        raise InstallError("SKILL.md not found in selected skill directory.")
    frontmatter, body = _extract_frontmatter(skill_doc)
    if body != skill_doc.read_text(encoding="utf-8"):
        skill_doc.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")

    for legacy_name in LEGACY_RESOURCE_DIRS:
        legacy_path = skill_dir / legacy_name
        if legacy_path.is_dir():
            _merge_tree(legacy_path, skill_dir / "resources" / legacy_name)

    agents_dir = skill_dir / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir, ignore_errors=True)

    _write_skill_toml(skill_dir, skill_name=skill_name, frontmatter=frontmatter)

    warnings: list[str] = []
    normalized_doc = skill_doc.read_text(encoding="utf-8")
    for pattern in LEGACY_HINT_PATTERNS:
        if re.search(pattern, normalized_doc):
            warnings.append(f"SKILL.md still contains legacy pattern: {pattern}")
    return warnings


def _validate_skill(path: Path) -> None:
    if not path.is_dir():
        raise InstallError(f"Skill path not found: {path}")
    if not (path / "SKILL.md").is_file():
        raise InstallError("SKILL.md not found in selected skill directory.")


def _copy_skill(src: Path, dest_dir: Path) -> None:
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    if dest_dir.exists():
        raise InstallError(f"Destination already exists: {dest_dir}")
    shutil.copytree(src, dest_dir, copy_function=shutil.copy2)


def _resolve_source(args: Args) -> Source:
    if args.url:
        owner, repo, ref, url_path = _parse_github_url(args.url, args.ref)
        paths = list(args.path) if args.path is not None else ([url_path] if url_path else [])
        if not paths:
            raise InstallError("Missing --path for GitHub URL.")
        return Source(owner=owner, repo=repo, ref=ref, paths=paths)

    if not args.repo:
        raise InstallError("Provide --repo or --url.")
    if "://" in args.repo:
        return _resolve_source(Args(url=args.repo, path=args.path, ref=args.ref))
    repo_parts = [item for item in args.repo.split("/") if item]
    if len(repo_parts) != 2:
        raise InstallError("--repo must be in owner/repo format.")
    if not args.path:
        raise InstallError("Missing --path for --repo.")
    return Source(owner=repo_parts[0], repo=repo_parts[1], ref=args.ref, paths=list(args.path))


def _parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description="Import a skill from GitHub into an OpenCompany skills root.")
    parser.add_argument("--repo", help="owner/repo")
    parser.add_argument("--url", help="https://github.com/owner/repo[/tree/ref/path]")
    parser.add_argument("--path", nargs="+", help="Path(s) to skill(s) inside the repo")
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--dest", default=DEFAULT_DEST, help="Destination OpenCompany skills root")
    parser.add_argument("--name", help="Destination skill name for single-skill imports")
    parser.add_argument("--method", choices=["auto", "download", "git"], default="auto")
    return parser.parse_args(argv, namespace=Args())


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        source = _resolve_source(args)
        for raw_path in source.paths:
            _validate_relative_path(raw_path)
        dest_root = Path(args.dest or DEFAULT_DEST).resolve()
        tmp_dir = tempfile.mkdtemp(prefix="skill-install-", dir=_tmp_root())
        installed: list[tuple[str, Path, list[str]]] = []
        try:
            repo_root = Path(_prepare_repo(source, args.method, tmp_dir))
            for raw_path in source.paths:
                skill_name = args.name if len(source.paths) == 1 and args.name else Path(raw_path).name
                _validate_skill_name(skill_name)
                skill_src = repo_root / raw_path
                _validate_skill(skill_src)
                dest_dir = dest_root / skill_name
                _copy_skill(skill_src, dest_dir)
                warnings = _normalize_skill(dest_dir, skill_name=skill_name)
                installed.append((skill_name, dest_dir, warnings))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        for skill_name, dest_dir, warnings in installed:
            print(f"Installed {skill_name} to {dest_dir}")
            for warning in warnings:
                print(f"Warning: {warning}")
        return 0
    except InstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
