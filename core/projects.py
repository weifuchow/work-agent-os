"""Project registry — loads projects from data/projects.yaml, caches, merges skills."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import yaml
from claude_agent_sdk import AgentDefinition
from loguru import logger

from core.config import settings


@dataclass
class ProjectConfig:
    name: str
    path: Path
    description: str


@dataclass
class ProjectGitMeta:
    branch: str = ""
    commit_sha: str = ""
    commit_time: datetime | None = None
    describe: str = ""
    is_dirty: bool = False
    version_hint: str = ""


@dataclass
class ProjectRuntimeContext:
    running_project: str
    project_path: Path
    execution_path: Path
    business_project_name: str = ""
    current_branch: str = ""
    current_commit_sha: str = ""
    current_describe: str = ""
    current_version: str = ""
    version_source_field: str = ""
    version_source_value: str = ""
    normalized_version: str = ""
    target_branch: str = ""
    target_branch_ref: str = ""
    target_tag: str = ""
    checkout_ref: str = ""
    recommended_worktree: Path | None = None
    execution_branch: str = ""
    execution_commit_sha: str = ""
    execution_describe: str = ""
    execution_version: str = ""
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "running_project": self.running_project,
            "business_project_name": self.business_project_name,
            "project_path": str(self.project_path),
            "execution_path": str(self.execution_path),
            "current_branch": self.current_branch,
            "current_commit_sha": self.current_commit_sha,
            "current_version": self.current_version,
            "current_describe": self.current_describe,
            "version_source_field": self.version_source_field,
            "version_source_value": self.version_source_value,
            "normalized_version": self.normalized_version,
            "target_branch": self.target_branch,
            "target_branch_ref": self.target_branch_ref,
            "target_tag": self.target_tag,
            "checkout_ref": self.checkout_ref,
            "recommended_worktree": str(self.recommended_worktree) if self.recommended_worktree else "",
            "execution_branch": self.execution_branch,
            "execution_commit_sha": self.execution_commit_sha,
            "execution_describe": self.execution_describe,
            "execution_version": self.execution_version,
            "notes": list(self.notes),
        }


_cache: list[ProjectConfig] | None = None
_cache_signature: tuple[int, int] | None = None


def _projects_file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def load_projects() -> list[ProjectConfig]:
    """Read data/projects.yaml and return validated project configs."""
    projects_file = settings.projects_file
    if not projects_file.exists():
        logger.warning("Projects file not found: {}", projects_file)
        return []

    try:
        raw = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to parse projects.yaml: {}", e)
        return []

    if not raw or not isinstance(raw.get("projects"), list):
        return []

    result: list[ProjectConfig] = []
    for entry in raw["projects"]:
        name = entry.get("name", "").strip()
        path_str = entry.get("path", "").strip()
        desc = entry.get("description", "").strip()

        if not name or not path_str:
            logger.warning("Skipping project entry with missing name or path: {}", entry)
            continue

        path = Path(path_str).resolve()
        if not path.exists():
            logger.warning("Project '{}' path does not exist: {}", name, path)
            continue

        result.append(ProjectConfig(name=name, path=path, description=desc))

    logger.info("Loaded {} projects from {}", len(result), projects_file)
    return result


def get_projects() -> list[ProjectConfig]:
    """Get cached project list (lazy load on first call)."""
    global _cache, _cache_signature
    signature = _projects_file_signature(settings.projects_file)
    if _cache is None or signature != _cache_signature:
        _cache = load_projects()
        _cache_signature = signature
    return _cache


def get_project(name: str) -> ProjectConfig | None:
    """Look up a project by name."""
    for p in get_projects():
        if p.name == name:
            return p
    return None


def refresh_projects() -> list[ProjectConfig]:
    """Clear cache and reload from disk."""
    global _cache, _cache_signature
    _cache = None
    _cache_signature = None
    return get_projects()


def get_project_git_meta(name: str | None = None, path: Path | None = None) -> ProjectGitMeta:
    project_path = path.resolve() if path else None
    if name and not project_path:
        project = get_project(name)
        project_path = project.path if project else None
    if not project_path or not project_path.exists():
        return ProjectGitMeta()

    git_dir = project_path / ".git"
    if not git_dir.exists():
        return ProjectGitMeta()

    branch = _git_capture(project_path, "rev-parse", "--abbrev-ref", "HEAD")
    commit_sha = _git_capture(project_path, "rev-parse", "--short=12", "HEAD")
    commit_time_raw = _git_capture(project_path, "show", "-s", "--format=%cI", "HEAD")
    describe = _git_capture(project_path, "describe", "--tags", "--always", "--dirty")
    dirty = bool(_git_capture(project_path, "status", "--porcelain", "-uno"))
    version_hint = infer_version_from_git(branch=branch, describe=describe, commit_sha=commit_sha)

    commit_time = None
    if commit_time_raw:
        try:
            commit_time = datetime.fromisoformat(commit_time_raw)
        except ValueError:
            commit_time = None

    return ProjectGitMeta(
        branch=branch,
        commit_sha=commit_sha,
        commit_time=commit_time,
        describe=describe,
        is_dirty=dirty,
        version_hint=version_hint,
    )


def infer_version_from_git(
    *,
    branch: str = "",
    describe: str = "",
    commit_sha: str = "",
) -> str:
    for source in [branch, describe]:
        normalized = _extract_semver_like(source)
        if normalized:
            return normalized

    clean_branch = (branch or "").strip()
    if clean_branch and clean_branch not in {"HEAD", "main", "master", "develop", "dev"}:
        if commit_sha:
            return f"{clean_branch}@{commit_sha[:8]}"
        return clean_branch

    clean_describe = (describe or "").strip()
    if clean_describe:
        return clean_describe

    return (commit_sha or "")[:8]


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "Never")
    return env


def _kill_git_process_tree(pid: int) -> None:
    if os.name == "nt" and pid > 0:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            logger.warning("Failed to taskkill git process tree {}: {}", pid, exc)


def _run_git_command(
    project_path: Path,
    *args: str,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-c", "core.longpaths=true", "-C", str(project_path), *args]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_git_process_tree(proc.pid)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _git_capture(project_path: Path, *args: str) -> str:
    try:
        result = _run_git_command(project_path, *args, timeout=5)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_capture_lines(project_path: Path, *args: str) -> tuple[str, ...]:
    output = _git_capture(project_path, *args)
    if not output:
        return ()
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _git_run(project_path: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return _run_git_command(project_path, *args, timeout=timeout)


@lru_cache(maxsize=64)
def _git_ref_inventory(project_path_str: str) -> dict[str, tuple[str, ...]]:
    project_path = Path(project_path_str)
    return {
        "local_branches": _git_capture_lines(
            project_path,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
        ),
        "remote_branches": _git_capture_lines(
            project_path,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/remotes/origin",
        ),
        "tags": _git_capture_lines(
            project_path,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/tags",
        ),
    }


def _extract_semver_like(text: str) -> str:
    if not text:
        return ""

    patterns = [
        r"\b[vV](\d+(?:\.\d+){1,4})\b",
        r"(?:release|hotfix|版本|version|sprint)[/_\- ]*([A-Za-z]*\d+(?:\.\d+){1,4})\b",
        r"\b(?:RIOT|Riot|FMS|Allspark)[/_\- ]*(\d+(?:\.\d+){0,4})\b",
        r"\b(\d+(?:\.\d+){1,4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def _stringify_field_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _is_plain_version_label(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.fullmatch(r"[vV]?\d+(?:\.\d+){1,4}", text))


def _extract_business_project_name(ones_result: dict[str, Any] | None) -> str:
    project = (ones_result or {}).get("project") or {}
    for key in ("display_name", "business_project_name", "title_project_name", "ones_project_name"):
        value = str(project.get(key) or "").strip()
        if value and not _is_plain_version_label(value):
            return value
    return ""


def _extract_ones_version_hint(ones_result: dict[str, Any] | None) -> tuple[str, str, str]:
    snapshot = (ones_result or {}).get("summary_snapshot") or {}
    if isinstance(snapshot, dict):
        for key in ("version_normalized", "version_hint", "version_text"):
            raw_value = _stringify_field_value(snapshot.get(key))
            normalized = _extract_semver_like(raw_value)
            if normalized:
                return f"summary_snapshot.{key}", raw_value, normalized

    named_fields = (ones_result or {}).get("named_fields") or {}
    if not isinstance(named_fields, dict):
        return "", "", ""

    candidate_fields = (
        "软件解决版本号",
        "软件版本",
        "FMS/RIoT版本",
        "FMS/RIOT版本",
        "FMS版本",
        "RIOT版本",
        "关联发布",
        "版本类型",
    )
    for field_name in candidate_fields:
        raw_value = _stringify_field_value(named_fields.get(field_name))
        if not raw_value or raw_value in {"/", "N/A", "n/a"}:
            continue
        normalized = _extract_semver_like(raw_value)
        return field_name, raw_value, normalized
    return "", "", ""


def _clean_branch_candidate(value: str) -> str:
    candidate = str(value or "").strip().strip("`'\"")
    candidate = candidate.removeprefix("refs/remotes/")
    candidate = candidate.removeprefix("refs/heads/")
    return candidate.strip(" ,，;；:：()（）[]【】<>。")


def _find_branch_candidates(text: str) -> list[str]:
    value = str(text or "")
    if not value:
        return []

    patterns = (
        r"(?:分支版本|版本分支|代码分支|git\s*branch|branch|分支)"
        r"[^A-Za-z0-9_/.-]{0,24}"
        r"(?P<branch>(?:origin/)?[A-Za-z0-9._-]+(?:/[A-Za-z0-9._/-]+)*)",
        r"\b(?P<branch>origin/[A-Za-z0-9._/-]+)\b",
        r"\b(?P<branch>(?:origin/)?(?:release|hotfix|sprint|feature|bugfix|fix|support)/[A-Za-z0-9._/-]+)\b",
    )
    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            candidate = _clean_branch_candidate(match.group("branch"))
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _ones_branch_hint_sources(ones_result: dict[str, Any] | None) -> list[tuple[str, str]]:
    if not ones_result:
        return []

    sources: list[tuple[str, str]] = []
    snapshot = (ones_result or {}).get("summary_snapshot") or {}
    if isinstance(snapshot, dict):
        for key in ("version_text", "summary_text", "branch_text", "checkout_ref"):
            raw_value = _stringify_field_value(snapshot.get(key))
            if raw_value:
                sources.append((f"summary_snapshot.{key}", raw_value))
        for key in ("business_identifiers", "observations", "version_fields"):
            raw_items = snapshot.get(key)
            if isinstance(raw_items, list):
                for index, item in enumerate(raw_items, start=1):
                    raw_value = _stringify_field_value(item)
                    if raw_value:
                        sources.append((f"summary_snapshot.{key}[{index}]", raw_value))

    named_fields = (ones_result or {}).get("named_fields") or {}
    if isinstance(named_fields, dict):
        for field_name, field_value in named_fields.items():
            if "分支" not in str(field_name).lower() and "branch" not in str(field_name).lower():
                continue
            raw_value = _stringify_field_value(field_value)
            if raw_value:
                sources.append((str(field_name), raw_value))

    task = (ones_result or {}).get("task") or {}
    if isinstance(task, dict):
        for key in ("description_local", "description"):
            raw_value = _stringify_field_value(task.get(key))
            if raw_value:
                sources.append((f"task.{key}", raw_value))
    return sources


def _resolve_branch_hint(
    candidate: str,
    *,
    local_branches: tuple[str, ...],
    remote_branches: tuple[str, ...],
) -> tuple[str, str] | None:
    clean_candidate = _clean_branch_candidate(candidate)
    if not clean_candidate:
        return None

    if clean_candidate.startswith("origin/"):
        display = clean_candidate.removeprefix("origin/")
        if clean_candidate in remote_branches:
            return display, clean_candidate
        if display in local_branches:
            return display, display
        return None

    if clean_candidate in local_branches:
        return clean_candidate, clean_candidate

    remote_candidate = f"origin/{clean_candidate}"
    if remote_candidate in remote_branches:
        return clean_candidate, remote_candidate
    return None


def _extract_explicit_ones_branch_hint(
    ones_result: dict[str, Any] | None,
    *,
    local_branches: tuple[str, ...],
    remote_branches: tuple[str, ...],
) -> tuple[str, str, str, str]:
    for source_field, raw_value in _ones_branch_hint_sources(ones_result):
        for candidate in _find_branch_candidates(raw_value):
            resolved = _resolve_branch_hint(
                candidate,
                local_branches=local_branches,
                remote_branches=remote_branches,
            )
            if resolved:
                display, ref = resolved
                return source_field, raw_value, display, ref
    return "", "", "", ""


def _branch_candidates_for_version(normalized_version: str) -> list[str]:
    version = (normalized_version or "").strip()
    if not version:
        return []
    parts = [part for part in version.split(".") if part]
    if len(parts) < 2:
        return [version]

    major_minor = f"{parts[0]}.{parts[1]}"
    exact = ".".join(parts[:3]) if len(parts) >= 3 else major_minor
    return [
        f"release/{major_minor}.x",
        f"Release/{major_minor}.x",
        f"sprint/{major_minor}.x",
        f"release/{exact}",
        f"Release/{exact}",
        f"sprint/{exact}",
        exact,
        major_minor,
    ]


def _select_matching_branch(
    normalized_version: str,
    *,
    local_branches: tuple[str, ...],
    remote_branches: tuple[str, ...],
    current_branch: str,
) -> tuple[str, str, list[str]]:
    notes: list[str] = []
    if normalized_version:
        for candidate in _branch_candidates_for_version(normalized_version):
            if candidate in local_branches:
                return candidate, candidate, notes
            remote_candidate = f"origin/{candidate}"
            if remote_candidate in remote_branches:
                return candidate, remote_candidate, notes

        parts = [part for part in normalized_version.split(".") if part]
        fuzzy_tokens = [
            normalized_version,
            ".".join(parts[:2]) if len(parts) >= 2 else "",
        ]
        for token in [item for item in fuzzy_tokens if item]:
            for ref in (*local_branches, *remote_branches):
                lowered = ref.lower()
                if token.lower() in lowered and any(marker in lowered for marker in ("release", "sprint", "master")):
                    display = ref.removeprefix("origin/")
                    notes.append(f"未命中精确版本分支，按模糊规则匹配到 {ref}")
                    return display, ref, notes

        notes.append("未命中版本分支，回退到 master")

    for display, ref in (
        ("master", "master"),
        ("master", "origin/master"),
        ("main", "main"),
        ("main", "origin/main"),
    ):
        if ref in local_branches or ref in remote_branches:
            return display, ref, notes

    fallback = (current_branch or "").strip()
    if fallback:
        notes.append(f"master/main 不存在，回退到当前分支 {fallback}")
        return fallback, fallback, notes
    return "", "", notes


def _select_matching_tag(normalized_version: str, *, tags: tuple[str, ...]) -> str:
    version = (normalized_version or "").strip()
    if not version:
        return ""

    exact_candidates = (version, f"v{version}")
    for candidate in exact_candidates:
        if candidate in tags:
            return candidate

    prefix_matches = [
        tag for tag in tags
        if any(tag.startswith(candidate) for candidate in exact_candidates)
    ]
    if prefix_matches:
        return sorted(prefix_matches, key=len)[0]
    return ""


def _recommended_worktree_path(
    project_name: str,
    *,
    ones_result: dict[str, Any] | None,
    normalized_version: str,
    worktree_token: str = "",
    worktree_root: Path | None = None,
) -> Path | None:
    task = (ones_result or {}).get("task") or {}
    task_number = str(task.get("number") or "").strip()
    task_uuid = str(task.get("uuid") or "").strip()
    task_parts = [part for part in (task_number, task_uuid) if part]
    task_token = worktree_token or ("-".join(task_parts) if task_parts else "current")

    version_token = normalized_version or "master"
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", version_token).strip("-") or "master"
    safe_task = re.sub(r"[^A-Za-z0-9._-]+", "-", task_token).strip("-") or "ones"
    root = Path(worktree_root) if worktree_root else settings.project_root / ".worktrees"
    return root / project_name / f"{safe_task}-{safe_version}"


def _legacy_worktree_paths(
    project_name: str,
    *,
    ones_result: dict[str, Any] | None,
    normalized_version: str,
) -> tuple[Path, ...]:
    if not ones_result:
        return ()

    task = (ones_result or {}).get("task") or {}
    version_token = normalized_version or "master"
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", version_token).strip("-") or "master"
    root = settings.project_root / ".worktrees" / project_name

    candidates: list[Path] = []
    for raw_task_token in (str(task.get("number") or "").strip(), str(task.get("uuid") or "").strip()):
        if not raw_task_token:
            continue
        safe_task = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_task_token).strip("-")
        if safe_task:
            candidates.append(root / f"{safe_task}-{safe_version}")
    return tuple(candidates)


def _git_commit_for_ref(project_path: Path, ref: str) -> str:
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return ""
    return _git_capture(project_path, "rev-parse", f"{clean_ref}^{{commit}}")


def _git_common_dir(project_path: Path) -> Path | None:
    raw = _git_capture(project_path, "rev-parse", "--git-common-dir")
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (project_path / candidate).resolve()
    return candidate.resolve()


def _is_valid_worktree_for_project(project_path: Path, worktree_path: Path) -> bool:
    if not worktree_path.exists():
        return False
    worktree_common = _git_common_dir(worktree_path)
    project_common = _git_common_dir(project_path)
    return bool(worktree_common and project_common and worktree_common == project_common)


def _next_available_worktree_path(base_path: Path) -> Path:
    if not base_path.exists():
        return base_path
    suffix = 2
    while True:
        candidate = base_path.parent / f"{base_path.name}-{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def _normalize_worktree_path(path: Path) -> str:
    try:
        normalized = str(path.resolve())
    except Exception:
        normalized = str(path)
    return normalized.lower() if os.name == "nt" else normalized


def _registered_worktree_flags(project_path: Path) -> dict[str, set[str]]:
    flags_by_path: dict[str, set[str]] = {}
    current_key: str | None = None
    for line in _git_capture_lines(project_path, "worktree", "list", "--porcelain"):
        if line.startswith("worktree "):
            raw_path = line.removeprefix("worktree ").strip()
            current_key = _normalize_worktree_path(Path(raw_path))
            flags_by_path.setdefault(current_key, set())
            continue
        if current_key is None:
            continue
        if line == "prunable" or line.startswith("prunable "):
            flags_by_path[current_key].add("prunable")
        elif line == "locked" or line.startswith("locked "):
            flags_by_path[current_key].add("locked")
    return flags_by_path


def _prune_worktrees(project_path: Path) -> tuple[bool, str]:
    result = _git_run(project_path, "worktree", "prune", "--expire", "now")
    if result.returncode == 0:
        return True, ""
    error_text = (result.stderr or result.stdout or "").strip()
    return False, error_text[:400]


def _worktree_metadata_path(project_path: Path, worktree_path: Path) -> Path | None:
    common_dir = _git_common_dir(project_path)
    if not common_dir:
        return None
    return common_dir / "worktrees" / worktree_path.name


def _cleanup_stale_worktree_state(project_path: Path, worktree_path: Path) -> list[str]:
    notes: list[str] = []
    worktree_key = _normalize_worktree_path(worktree_path)
    registered_flags = _registered_worktree_flags(project_path)
    flags = registered_flags.get(worktree_key, set())
    if "prunable" in flags:
        pruned, error_text = _prune_worktrees(project_path)
        if pruned:
            notes.append(f"已清理失效 worktree 注册: {worktree_path}")
        elif error_text:
            notes.append(f"清理失效 worktree 注册失败，将继续尝试创建。原因: {error_text}")
        registered_flags = _registered_worktree_flags(project_path)

    if worktree_path.exists() or worktree_key in registered_flags:
        return notes

    metadata_path = _worktree_metadata_path(project_path, worktree_path)
    if not metadata_path or not metadata_path.exists():
        return notes

    try:
        shutil.rmtree(metadata_path)
        notes.append(f"已移除残留 worktree 元数据目录: {metadata_path}")
    except Exception as exc:
        notes.append(f"移除残留 worktree 元数据目录失败，将继续尝试创建。原因: {exc}")
    return notes


def _is_retryable_worktree_error(error_text: str) -> bool:
    lowered = error_text.lower()
    retryable_markers = (
        "missing but already registered worktree",
        "already registered worktree",
        "permission denied",
        "file exists",
        "already exists",
    )
    return any(marker in lowered for marker in retryable_markers)


def _checkout_existing_worktree(worktree_path: Path, ref: str) -> tuple[bool, str]:
    result = _git_run(worktree_path, "checkout", "--detach", ref)
    if result.returncode == 0:
        return True, ""
    error_text = (result.stderr or result.stdout or "").strip()
    return False, error_text[:400]


def _create_detached_worktree(
    project_path: Path,
    worktree_path: Path,
    ref: str,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if force:
        args.append("-f")
    args.extend(["--detach", str(worktree_path), ref])
    result = _git_run(project_path, *args, timeout=120)
    if result.returncode == 0:
        return True, ""
    error_text = (result.stderr or result.stdout or "").strip()
    return False, error_text[:400]


def _try_create_worktree_with_recovery(
    project_path: Path,
    worktree_path: Path,
    ref: str,
) -> tuple[bool, str, list[str]]:
    notes = _cleanup_stale_worktree_state(project_path, worktree_path)
    created, error_text = _create_detached_worktree(project_path, worktree_path, ref)
    if created or not error_text or not _is_retryable_worktree_error(error_text):
        return created, error_text, notes

    pruned, prune_error = _prune_worktrees(project_path)
    if pruned:
        notes.append("首次创建失败后已执行 git worktree prune。")
    elif prune_error:
        notes.append(f"首次创建失败后执行 git worktree prune 失败，将继续重试。原因: {prune_error}")
    notes.extend(_cleanup_stale_worktree_state(project_path, worktree_path))

    force_retry = "already registered worktree" in error_text.lower()
    retried, retry_error = _create_detached_worktree(
        project_path,
        worktree_path,
        ref,
        force=force_retry,
    )
    if retried:
        notes.append(f"首次创建失败后已自动恢复并创建 worktree: {worktree_path}")
        return True, "", notes
    return False, retry_error or error_text, notes


def _worktree_create_candidates(
    preferred_path: Path,
    legacy_paths: list[Path],
) -> tuple[Path, ...]:
    bases: list[Path] = []
    for candidate in [preferred_path, *legacy_paths]:
        if candidate not in bases:
            bases.append(candidate)

    candidates = list(bases)
    for base in bases:
        retry_candidate = base.parent / f"{base.name}-2"
        if retry_candidate not in candidates:
            candidates.append(retry_candidate)
    return tuple(candidates)


def _try_reuse_worktree_path(
    project_path: Path,
    worktree_path: Path,
    checkout_ref: str,
    target_commit: str,
) -> tuple[Path | None, list[str]]:
    notes: list[str] = []
    if not worktree_path.exists():
        return None, notes

    if _is_valid_worktree_for_project(project_path, worktree_path):
        current_commit = _git_capture(worktree_path, "rev-parse", "HEAD")
        if current_commit == target_commit:
            notes.append(f"复用已有 worktree: {worktree_path}")
            return worktree_path, notes

        is_dirty = bool(_git_capture(worktree_path, "status", "--porcelain", "-uno"))
        if not is_dirty:
            switched, error_text = _checkout_existing_worktree(worktree_path, checkout_ref)
            if switched:
                notes.append(f"复用并切换已有 worktree 到 {checkout_ref}: {worktree_path}")
                return worktree_path, notes
            notes.append(f"已有 worktree 切换到 {checkout_ref} 失败，将新建目录。原因: {error_text}")
            return None, notes

        notes.append(f"已有 worktree 含未提交变更，不覆盖: {worktree_path}")
        return None, notes

    notes.append(f"目标目录已存在但不是该仓库 worktree，将新建目录: {worktree_path}")
    return None, notes


def _ensure_project_worktree(
    context: ProjectRuntimeContext,
    *,
    ones_result: dict[str, Any] | None = None,
) -> tuple[Path, list[str]]:
    notes: list[str] = []
    worktree_path = Path(context.recommended_worktree) if context.recommended_worktree else None
    checkout_ref = str(context.checkout_ref or "").strip()
    if not worktree_path or not checkout_ref:
        return context.project_path, notes

    target_commit = _git_commit_for_ref(context.project_path, checkout_ref)
    if not target_commit:
        notes.append(f"无法解析检出引用 {checkout_ref}，回退到主仓库目录。")
        return context.project_path, notes

    candidate_paths = [worktree_path]
    default_worktree_root = (settings.project_root / ".worktrees").resolve()
    try:
        allow_legacy_reuse = worktree_path.resolve().is_relative_to(default_worktree_root)
    except ValueError:
        allow_legacy_reuse = False
    if allow_legacy_reuse:
        for legacy_path in _legacy_worktree_paths(
            context.running_project,
            ones_result=ones_result,
            normalized_version=context.normalized_version,
        ):
            if legacy_path not in candidate_paths:
                candidate_paths.append(legacy_path)

    preferred_path = worktree_path
    for candidate_path in candidate_paths:
        reused_path, reuse_notes = _try_reuse_worktree_path(
            context.project_path,
            candidate_path,
            checkout_ref,
            target_commit,
        )
        notes.extend(reuse_notes)
        if reused_path is not None:
            return reused_path, notes

    create_candidates = _worktree_create_candidates(preferred_path, candidate_paths[1:])
    last_error = ""
    for index, create_candidate in enumerate(create_candidates):
        candidate_path = create_candidate
        if candidate_path.exists():
            candidate_path = _next_available_worktree_path(candidate_path)

        created, error_text, create_notes = _try_create_worktree_with_recovery(
            context.project_path,
            candidate_path,
            checkout_ref,
        )
        notes.extend(create_notes)
        if created:
            if candidate_path != preferred_path:
                notes.append(f"首选 worktree 路径不可用，改用备用路径: {candidate_path}")
            notes.append(f"已创建 worktree 并检出 {checkout_ref}: {candidate_path}")
            return candidate_path, notes

        last_error = error_text
        if index < len(create_candidates) - 1:
            notes.append(f"worktree 创建失败，尝试下一个候选目录。候选: {candidate_path}。原因: {error_text}")

    notes.append(f"创建 worktree 失败，回退到主仓库目录。原因: {last_error}")
    return context.project_path, notes


def resolve_project_runtime_context(
    project_name: str,
    *,
    ones_result: dict[str, Any] | None = None,
    worktree_root: Path | None = None,
    worktree_token: str = "",
) -> ProjectRuntimeContext | None:
    project = get_project(project_name)
    if not project:
        return None

    git_meta = get_project_git_meta(path=project.path)
    inventory = _git_ref_inventory(str(project.path.resolve()))
    version_field, version_value, normalized_version = _extract_ones_version_hint(ones_result)
    current_version = git_meta.version_hint or git_meta.describe or git_meta.branch or git_meta.commit_sha[:8]
    branch_hint_field, branch_hint_value, explicit_branch, explicit_branch_ref = _extract_explicit_ones_branch_hint(
        ones_result,
        local_branches=inventory["local_branches"],
        remote_branches=inventory["remote_branches"],
    )
    explicit_branch_selected = bool(explicit_branch_ref)
    if explicit_branch_selected:
        target_branch = explicit_branch
        target_branch_ref = explicit_branch_ref
        branch_notes = [f"ONES 分支线索来自 {branch_hint_field}: {branch_hint_value}"]
        target_tag = _select_matching_tag(normalized_version, tags=inventory["tags"])
    elif ones_result or normalized_version:
        target_branch, target_branch_ref, branch_notes = _select_matching_branch(
            normalized_version,
            local_branches=inventory["local_branches"],
            remote_branches=inventory["remote_branches"],
            current_branch=git_meta.branch,
        )
        target_tag = _select_matching_tag(normalized_version, tags=inventory["tags"])
    else:
        target_branch, target_branch_ref, branch_notes = _select_matching_branch(
            "",
            local_branches=inventory["local_branches"],
            remote_branches=inventory["remote_branches"],
            current_branch=git_meta.branch,
        )
        target_tag = ""
        if target_branch_ref in {"master", "origin/master"}:
            branch_notes = ["未提供版本线索，默认按 master 准备 session worktree。"]
        elif target_branch_ref in {"main", "origin/main"}:
            branch_notes = ["未提供版本线索，master 不存在，默认按 main 准备 session worktree。", *branch_notes]

    notes = list(branch_notes)
    if version_field and version_value:
        notes.insert(0, f"ONES 版本线索来自 {version_field}: {version_value}")
    if target_tag and explicit_branch_selected:
        notes.append(f"同时存在版本 Tag: {target_tag}")
    elif target_tag:
        notes.append(f"匹配到版本 Tag: {target_tag}")
    elif normalized_version:
        notes.append("未匹配到精确 Tag")
    if explicit_branch_selected and normalized_version:
        notes.append(f"检测到明确分支 {target_branch_ref}，{normalized_version} 仅作为版本/迭代线索，不优先检出 Tag。")
    if ones_result:
        notes.append("如需切换版本，优先使用 worktree，避免污染当前工作区。")

    checkout_ref = (
        target_branch_ref
        if explicit_branch_selected
        else target_tag or target_branch_ref or target_branch or git_meta.branch or ""
    )
    execution_path = project.path
    worktree_version_token = (
        f"{normalized_version}-{target_branch}"
        if explicit_branch_selected and normalized_version and target_branch
        else target_branch
        if explicit_branch_selected and target_branch
        else (
            normalized_version
            or (target_branch if not ones_result else "")
            or current_version
            or git_meta.branch
            or git_meta.commit_sha[:8]
        )
    )
    recommended_worktree = _recommended_worktree_path(
        project_name,
        ones_result=ones_result,
        normalized_version=worktree_version_token,
        worktree_token=worktree_token,
        worktree_root=worktree_root,
    )

    return ProjectRuntimeContext(
        running_project=project.name,
        project_path=project.path,
        execution_path=execution_path,
        business_project_name=_extract_business_project_name(ones_result),
        current_branch=git_meta.branch,
        current_commit_sha=git_meta.commit_sha,
        current_describe=git_meta.describe,
        current_version=current_version,
        version_source_field=version_field,
        version_source_value=version_value,
        normalized_version=normalized_version,
        target_branch=target_branch,
        target_branch_ref=target_branch_ref,
        target_tag=target_tag,
        checkout_ref=checkout_ref,
        recommended_worktree=recommended_worktree,
        execution_branch=git_meta.branch,
        execution_commit_sha=git_meta.commit_sha,
        execution_describe=git_meta.describe,
        execution_version=current_version,
        notes=notes,
    )


def prepare_project_runtime_context(
    project_name: str,
    *,
    ones_result: dict[str, Any] | None = None,
    worktree_root: Path | None = None,
    worktree_token: str = "",
) -> ProjectRuntimeContext | None:
    context = resolve_project_runtime_context(
        project_name,
        ones_result=ones_result,
        worktree_root=worktree_root,
        worktree_token=worktree_token,
    )
    if not context:
        return None
    execution_path, worktree_notes = _ensure_project_worktree(context, ones_result=ones_result)
    updated_notes = [*context.notes, *worktree_notes]
    execution_meta = get_project_git_meta(path=execution_path)
    return replace(
        context,
        execution_path=execution_path,
        recommended_worktree=execution_path if execution_path != context.project_path else context.recommended_worktree,
        execution_branch=execution_meta.branch,
        execution_commit_sha=execution_meta.commit_sha,
        execution_describe=execution_meta.describe,
        execution_version=(
            execution_meta.version_hint
            or execution_meta.describe
            or execution_meta.branch
            or execution_meta.commit_sha[:8]
        ),
        notes=updated_notes,
    )


def merge_skills(
    global_skills: dict[str, AgentDefinition],
    project_path: Path,
    include_global: bool = True,
) -> dict[str, AgentDefinition]:
    """Merge project-local skills over global skills (project wins on name collision).

    Scans {project_path}/.claude/agents/*.md and {project_path}/.claude/skills/*/SKILL.md.
    Returns a new dict; does not mutate global_skills.
    """
    from core.skill_registry import discover_skills

    agents_dir = project_path / ".claude" / "agents"
    skills_dir = project_path / ".claude" / "skills"

    project_skills, _ = discover_skills(agents_dir=agents_dir, skills_dir=skills_dir)

    if not project_skills:
        return dict(global_skills) if include_global else {}

    merged = dict(global_skills) if include_global else {}
    for name, defn in project_skills.items():
        if name in merged:
            logger.info("Project skill '{}' overrides global skill", name)
        merged[name] = defn

    logger.info("Merged skills: {} global + {} project = {} total (include_global={})",
                len(global_skills), len(project_skills), len(merged), include_global)
    return merged
