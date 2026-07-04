#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = 1
DEFAULT_HOME = Path.home() / ".codex" / "rewind"
MAX_BASELINE_UNTRACKED_BYTES = int(
    os.environ.get("CODEX_REWIND_MAX_BASELINE_UNTRACKED_BYTES", "1048576")
)
MAX_WORKSPACE_SNAPSHOT_ENTRIES = int(
    os.environ.get("CODEX_REWIND_MAX_WORKSPACE_SNAPSHOT_ENTRIES", "100000")
)
MAX_WORKSPACE_SNAPSHOT_FILE_BYTES = int(
    os.environ.get("CODEX_REWIND_MAX_WORKSPACE_SNAPSHOT_FILE_BYTES", str(MAX_BASELINE_UNTRACKED_BYTES))
)
MAX_WORKSPACE_SNAPSHOT_TOTAL_BYTES = int(
    os.environ.get("CODEX_REWIND_MAX_WORKSPACE_SNAPSHOT_TOTAL_BYTES", "67108864")
)
MAX_WORKSPACE_SNAPSHOT_SKIPPED_PATHS = int(
    os.environ.get("CODEX_REWIND_MAX_WORKSPACE_SNAPSHOT_SKIPPED_PATHS", "200")
)
MAX_FILE_HISTORY_SNAPSHOTS = int(os.environ.get("CODEX_REWIND_MAX_FILE_HISTORY_SNAPSHOTS", "100"))
MAX_FILE_HISTORY_FILE_BYTES = int(
    os.environ.get("CODEX_REWIND_MAX_FILE_HISTORY_FILE_BYTES", str(16 * 1024 * 1024))
)
REWIND_CLEANUP_DAYS = int(os.environ.get("CODEX_REWIND_CLEANUP_DAYS", "30"))
REWIND_CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CODEX_REWIND_CLEANUP_INTERVAL_SECONDS", "86400"))
SESSION_ROLLBACK_METHOD = "thread/rollback"

EXCLUDED_FILE_HISTORY_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".next",
    ".turbo",
    ".venv",
    "venv",
    "target",
    "build",
    "dist",
    "DerivedData",
}
EXCLUDED_FILE_HISTORY_SUFFIXES = {
    ".asar",
    ".dmg",
    ".pkg",
    ".zip",
    ".tar",
    ".tgz",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".iso",
    ".pyc",
    ".o",
    ".a",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".class",
    ".jar",
}
PROJECT_DIR_RE = re.compile(r"^[0-9a-f]{16}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_hash(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:length]


def read_stdin_json() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def storage_home() -> Path:
    return Path(os.environ.get("CODEX_REWIND_HOME", str(DEFAULT_HOME))).expanduser()


def codex_home(value: str | None = None) -> Path:
    return Path(value or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def canonical_cwd(value: str | None) -> Path:
    return Path(value or os.getcwd()).expanduser().resolve()


def first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def payload_identity(payload: dict[str, Any]) -> dict[str, str]:
    mapping = {
        "thread_id": ("thread_id", "threadId", "session_id", "sessionId"),
        "turn_id": ("turn_id", "turnId"),
        "user_message_id": ("user_message_id", "userMessageId", "message_id", "messageId"),
    }
    out: dict[str, str] = {}
    for canonical, keys in mapping.items():
        value = first_string(*(payload.get(key) for key in keys))
        if value:
            out[canonical] = value
    return out


def is_rewind_prompt(prompt: str) -> bool:
    return prompt.strip() == "/rewind"


def emit_user_prompt_block(reason: str) -> int:
    json.dump(
        {
            "suppressOutput": True,
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
            },
        },
        sys.stdout,
        ensure_ascii=False,
    )
    return 0


def cwd_from_payload(payload: dict[str, Any], fallback: str | None = None) -> Path:
    value = payload.get("cwd") or payload.get("current_working_directory") or fallback
    return canonical_cwd(value if isinstance(value, str) else None)


def project_dir(cwd: Path) -> Path:
    return storage_home() / short_hash(str(cwd))


def manifest_path(cwd: Path) -> Path:
    return project_dir(cwd) / "manifest.json"


def file_history_state_path(cwd: Path) -> Path:
    return project_dir(cwd) / "file-history-state.json"


def empty_manifest(cwd: Path) -> dict[str, Any]:
    return {
        "version": VERSION,
        "cwd": str(cwd),
        "created_at": now_iso(),
        "checkpoints": [],
    }


def manifest_lock_path(cwd: Path) -> Path:
    return project_dir(cwd) / "manifest.lock"


@contextlib.contextmanager
def manifest_lock(cwd: Path):
    path = manifest_lock_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


CHECKPOINT_SUMMARY_KEYS = (
    "id",
    "sequence",
    "history_version",
    "created_at",
    "cwd",
    "thread_id",
    "turn_id",
    "user_message_id",
    "source",
    "prompt_preview",
)
CHECKPOINT_HEAVY_KEYS = {
    "tracked_file_backups",
    "file_backups",
    "git",
    "workspace",
}


def checkpoint_data_path(cwd: Path, checkpoint_id: str) -> Path:
    return checkpoint_dir(cwd, checkpoint_id) / "checkpoint.json"


def checkpoint_has_heavy_data(checkpoint: dict[str, Any]) -> bool:
    return any(key in checkpoint for key in CHECKPOINT_HEAVY_KEYS)


def checkpoint_summary(checkpoint: dict[str, Any]) -> dict[str, Any]:
    summary = {key: checkpoint.get(key) for key in CHECKPOINT_SUMMARY_KEYS if key in checkpoint}
    checkpoint_id = checkpoint.get("id")
    if checkpoint_id:
        summary["checkpoint_file"] = f"checkpoints/{checkpoint_id}/checkpoint.json"
    backups = checkpoint.get("tracked_file_backups")
    if not isinstance(backups, dict):
        backups = checkpoint.get("file_backups")
    if isinstance(backups, dict):
        summary["tracked_file_count"] = len(backups)
    git_info = checkpoint.get("git")
    if isinstance(git_info, dict):
        summary["has_git"] = bool(git_info.get("root"))
    workspace = checkpoint.get("workspace")
    if isinstance(workspace, dict):
        summary["has_workspace"] = True
    return summary


def lightweight_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in manifest.items() if key != "tracked_files"}
    checkpoints = out.get("checkpoints")
    if isinstance(checkpoints, list):
        out["checkpoints"] = [
            checkpoint_summary(item) if isinstance(item, dict) else item
            for item in checkpoints[-MAX_FILE_HISTORY_SNAPSHOTS:]
        ]
    else:
        out["checkpoints"] = []
    return out


def manifest_needs_split(manifest: dict[str, Any]) -> bool:
    if "tracked_files" in manifest:
        return True
    checkpoints = manifest.get("checkpoints")
    return isinstance(checkpoints, list) and any(
        isinstance(item, dict) and checkpoint_has_heavy_data(item)
        for item in checkpoints
    )


def write_checkpoint_data(cwd: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint_id = checkpoint.get("id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        return
    path = checkpoint_data_path(cwd, checkpoint_id)
    if not checkpoint_has_heavy_data(checkpoint) and path.exists():
        return
    write_json_file(path, checkpoint)


def load_checkpoint(cwd: Path, checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    checkpoint_id = checkpoint.get("id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        return checkpoint
    path = checkpoint_data_path(cwd, checkpoint_id)
    if not path.exists():
        return checkpoint
    try:
        full = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return checkpoint
    if not isinstance(full, dict):
        return checkpoint
    for key, value in checkpoint.items():
        full.setdefault(key, value)
    return full


def load_file_history_state(cwd: Path) -> dict[str, Any]:
    path = file_history_state_path(cwd)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tracked = payload.get("tracked_files") if isinstance(payload, dict) else None
    return tracked if isinstance(tracked, dict) else {}


def write_file_history_state(cwd: Path, tracked_files: dict[str, Any]) -> None:
    write_json_file(
        file_history_state_path(cwd),
        {
            "version": VERSION,
            "cwd": str(cwd),
            "updated_at": now_iso(),
            "tracked_files": tracked_files,
        },
    )


def write_manifest_file(cwd: Path, manifest: dict[str, Any]) -> None:
    tracked = manifest.get("tracked_files")
    if isinstance(tracked, dict):
        write_file_history_state(cwd, tracked)

    checkpoints = manifest.get("checkpoints")
    if isinstance(checkpoints, list):
        for checkpoint in checkpoints:
            if isinstance(checkpoint, dict):
                write_checkpoint_data(cwd, checkpoint)

    write_json_file(manifest_path(cwd), lightweight_manifest(manifest))


def backup_corrupt_manifest(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    if backup.exists():
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{time.time_ns()}")
    shutil.copy2(path, backup)
    return backup


def load_manifest_unlocked(cwd: Path) -> dict[str, Any]:
    path = manifest_path(cwd)
    if not path.exists():
        return empty_manifest(cwd)
    data = path.read_bytes()
    decode_error: UnicodeDecodeError | None = None
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        decode_error = exc
        raw = data.decode("utf-8", errors="replace")
    try:
        manifest = json.loads(raw)
        if isinstance(manifest, dict) and manifest_needs_split(manifest):
            write_manifest_file(cwd, manifest)
            manifest = lightweight_manifest(manifest)
        if decode_error is not None:
            backup_corrupt_manifest(path)
            write_manifest_file(cwd, manifest)
        return manifest
    except json.JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        try:
            manifest, end = decoder.raw_decode(raw)
        except json.JSONDecodeError:
            raise
        if decode_error is not None or raw[end:].strip():
            backup_corrupt_manifest(path)
            write_manifest_file(cwd, manifest)
            return lightweight_manifest(manifest) if isinstance(manifest, dict) else manifest
        raise exc


def load_manifest(cwd: Path) -> dict[str, Any]:
    with manifest_lock(cwd):
        return load_manifest_unlocked(cwd)


def save_manifest(cwd: Path, manifest: dict[str, Any]) -> None:
    with manifest_lock(cwd):
        write_manifest_file(cwd, manifest)


def checkpoint_dir(cwd: Path, checkpoint_id: str) -> Path:
    return project_dir(cwd) / "checkpoints" / checkpoint_id


def run(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    env = os.environ.copy()
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        check=check,
        text=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_with_input(
    argv: list[str],
    *,
    cwd: Path,
    input_bytes: bytes,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        input=input_bytes,
        check=check,
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_root(cwd: Path) -> Path | None:
    try:
        result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def zsplit(data: bytes) -> list[str]:
    if not data:
        return []
    return [item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item]


def git_z(root: Path, args: list[str]) -> list[str]:
    try:
        result = run(["git", *args], cwd=root, text=False)
    except subprocess.CalledProcessError:
        return []
    return zsplit(result.stdout)


def git_has_head(root: Path) -> bool:
    try:
        run(["git", "rev-parse", "--verify", "HEAD"], cwd=root)
        return True
    except subprocess.CalledProcessError:
        return False


def git_bytes(root: Path, args: list[str], *, check: bool = True) -> bytes:
    try:
        result = run(["git", *args], cwd=root, text=False, check=check)
    except subprocess.CalledProcessError:
        if check:
            raise
        return b""
    return result.stdout


def safe_rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_ancestor_of(path: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(path.resolve())
        return path.resolve() != child.resolve()
    except ValueError:
        return False


def workspace_rel(path: Path, root: Path) -> str:
    path_abs = Path(os.path.abspath(path))
    root_abs = Path(os.path.abspath(root))
    return path_abs.relative_to(root_abs).as_posix()


def is_workspace_path_inside(path: Path, root: Path) -> bool:
    try:
        workspace_rel(path, root)
        return True
    except ValueError:
        return False


def env_truthy(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


def env_path_substrings(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item for item in raw.split(os.pathsep) if item]


def has_app_bundle_component(path: Path) -> bool:
    return any(part.endswith(".app") for part in path.parts)


def file_history_excluded_reason(path: Path, cwd: Path) -> str | None:
    if env_truthy("CODEX_REWIND_DISABLE_EXCLUDES"):
        return None

    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = Path(os.path.abspath(path.expanduser()))

    for needle in env_path_substrings("CODEX_REWIND_EXCLUDE_SUBSTRINGS"):
        if needle and needle in str(resolved):
            return f"configured-exclude:{needle}"

    try:
        if is_inside(resolved, storage_home().resolve()):
            return "rewind-storage"
    except OSError:
        pass

    home_codex = codex_home()
    for rel in ("rewind", "backups", "tmp"):
        try:
            if is_inside(resolved, (home_codex / rel).resolve()):
                return f"codex-{rel}"
        except OSError:
            continue

    if str(resolved).startswith("/Applications/") or has_app_bundle_component(resolved):
        return "app-bundle"

    for part in resolved.parts:
        if part in EXCLUDED_FILE_HISTORY_DIR_NAMES:
            return f"generated-dir:{part}"

    suffix = resolved.suffix.lower()
    if suffix in EXCLUDED_FILE_HISTORY_SUFFIXES:
        return f"binary-or-archive:{suffix}"

    return None


def file_history_skip_meta(
    *,
    cwd: Path,
    path: Path,
    key: str,
    version: int,
    reason: str,
    capture_reason: str,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "path": key,
        "rel": safe_rel(path, cwd),
        "version": version,
        "reason": capture_reason,
        "captured_at": now_iso(),
        "backup": None,
        "skipped_reason": reason,
    }
    try:
        if path.exists() and path.is_file():
            stat = path.stat()
            meta.update(
                {
                    "existed": True,
                    "kind": "file",
                    "mode": stat.st_mode,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "max_file_bytes": MAX_FILE_HISTORY_FILE_BYTES,
                }
            )
        elif path.exists() and path.is_dir():
            meta.update({"existed": True, "kind": "directory"})
        else:
            meta.update({"existed": False, "kind": "missing"})
    except OSError:
        meta.update({"existed": False, "kind": "missing"})
    return meta


def file_history_meta_skipped(meta: dict[str, Any]) -> bool:
    return isinstance(meta.get("skipped_reason"), str) and bool(meta.get("skipped_reason"))


def normalize_skipped_file_history_meta(cwd: Path, meta: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(meta)
    out["backup"] = None
    out["skipped_reason"] = reason
    out.setdefault("max_file_bytes", MAX_FILE_HISTORY_FILE_BYTES)
    path_value = out.get("path")
    if isinstance(path_value, str):
        out["rel"] = safe_rel(Path(path_value).expanduser(), cwd)
    return out


def copy_file_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def add_file_backup(
    cwd: Path,
    checkpoint: dict[str, Any],
    file_path: Path,
    *,
    reason: str,
    force: bool = False,
) -> None:
    file_path = file_path.expanduser().resolve()
    key = str(file_path)
    backups = checkpoint.setdefault("file_backups", {})
    if key in backups and not force:
        return

    checkpoint_id = checkpoint["id"]
    backup_name = short_hash(key, 32)
    backup_rel = f"files/{backup_name}"
    backup_path = checkpoint_dir(cwd, checkpoint_id) / backup_rel

    meta: dict[str, Any] = {
        "path": key,
        "rel": safe_rel(file_path, cwd),
        "reason": reason,
        "captured_at": now_iso(),
    }

    if file_path.exists() and file_path.is_file():
        copy_file_backup(file_path, backup_path)
        stat = file_path.stat()
        meta.update(
            {
                "existed": True,
                "backup": backup_rel,
                "mode": stat.st_mode,
                "size": stat.st_size,
            }
        )
    else:
        meta.update({"existed": False, "backup": None})

    backups[key] = meta


def file_history_backup_rel(file_path: Path, version: int) -> str:
    key = str(file_path.expanduser().resolve())
    return f"file-history/{short_hash(key, 32)}@v{version}"


def file_history_backup_path(cwd: Path, backup_rel: str) -> Path:
    return project_dir(cwd) / backup_rel


def file_history_meta_path(meta: dict[str, Any]) -> Path:
    return Path(str(meta["path"])).expanduser().resolve()


def normalize_file_history_path(file_path: Path) -> tuple[str, Path]:
    path = file_path.expanduser().resolve()
    return str(path), path


def checkpoint_history_backups(checkpoint: dict[str, Any]) -> dict[str, Any]:
    backups = checkpoint.get("tracked_file_backups")
    if isinstance(backups, dict):
        return backups
    backups = checkpoint.get("file_backups")
    if isinstance(backups, dict):
        checkpoint["tracked_file_backups"] = backups
        return backups
    backups = {}
    checkpoint["tracked_file_backups"] = backups
    return backups


def checkpoint_file_backups_alias(checkpoint: dict[str, Any]) -> None:
    backups = checkpoint_history_backups(checkpoint)
    checkpoint["file_backups"] = backups


def tracked_file_versions(entry: dict[str, Any]) -> dict[str, Any]:
    versions = entry.get("versions")
    if isinstance(versions, dict):
        return versions
    versions = {}
    entry["versions"] = versions
    return versions


def latest_tracked_backup(entry: dict[str, Any]) -> dict[str, Any] | None:
    versions = tracked_file_versions(entry)
    latest = entry.get("latest_version")
    if isinstance(latest, int):
        meta = versions.get(str(latest))
        return meta if isinstance(meta, dict) else None
    numeric_versions: list[int] = []
    for key in versions:
        try:
            numeric_versions.append(int(key))
        except (TypeError, ValueError):
            continue
    if not numeric_versions:
        return None
    version = max(numeric_versions)
    entry["latest_version"] = version
    meta = versions.get(str(version))
    return meta if isinstance(meta, dict) else None


def first_tracked_backup(manifest: dict[str, Any], key: str) -> dict[str, Any] | None:
    tracked = manifest.get("tracked_files")
    if isinstance(tracked, dict):
        entry = tracked.get(key)
        if isinstance(entry, dict):
            versions = tracked_file_versions(entry)
            numeric_versions: list[int] = []
            for version_key in versions:
                try:
                    numeric_versions.append(int(version_key))
                except (TypeError, ValueError):
                    continue
            if numeric_versions:
                meta = versions.get(str(min(numeric_versions)))
                if isinstance(meta, dict):
                    return meta

    checkpoints = manifest.get("checkpoints")
    if not isinstance(checkpoints, list):
        return None
    ordered = sorted(
        (item for item in checkpoints if isinstance(item, dict)),
        key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")),
    )
    for checkpoint in ordered:
        backups = checkpoint.get("tracked_file_backups") or checkpoint.get("file_backups")
        if isinstance(backups, dict):
            meta = backups.get(key)
            if isinstance(meta, dict):
                return meta
    return None


def store_tracked_backup(
    manifest: dict[str, Any],
    *,
    key: str,
    path: Path,
    cwd: Path,
    meta: dict[str, Any],
) -> None:
    tracked = manifest.setdefault("tracked_files", {})
    if not isinstance(tracked, dict):
        tracked = {}
        manifest["tracked_files"] = tracked
    entry = tracked.get(key)
    if not isinstance(entry, dict):
        entry = {
            "path": key,
            "rel": safe_rel(path, cwd),
            "versions": {},
        }
        tracked[key] = entry
    versions = tracked_file_versions(entry)
    version = meta.get("version")
    if isinstance(version, int):
        versions[str(version)] = dict(meta)
        entry["latest_version"] = version
        entry["latest_backup"] = dict(meta)
    entry["path"] = key
    entry["rel"] = safe_rel(path, cwd)
    entry["updated_at"] = now_iso()


def create_file_history_backup(cwd: Path, file_path: Path, *, version: int, reason: str) -> dict[str, Any]:
    key, path = normalize_file_history_path(file_path)
    meta: dict[str, Any] = {
        "path": key,
        "rel": safe_rel(path, cwd),
        "version": version,
        "reason": reason,
        "captured_at": now_iso(),
    }

    excluded_reason = file_history_excluded_reason(path, cwd)
    if excluded_reason is not None:
        return file_history_skip_meta(
            cwd=cwd,
            path=path,
            key=key,
            version=version,
            reason=excluded_reason,
            capture_reason=reason,
        )

    if path.exists() and path.is_file():
        stat = path.stat()
        if stat.st_size > MAX_FILE_HISTORY_FILE_BYTES:
            return file_history_skip_meta(
                cwd=cwd,
                path=path,
                key=key,
                version=version,
                reason="large-file",
                capture_reason=reason,
            )
        backup_rel = file_history_backup_rel(path, version)
        backup_path = file_history_backup_path(cwd, backup_rel)
        copy_file_backup(path, backup_path)
        meta.update(
            {
                "existed": True,
                "kind": "file",
                "backup": backup_rel,
                "mode": stat.st_mode,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    elif path.exists() and path.is_dir():
        meta.update({"existed": True, "kind": "directory", "backup": None})
    else:
        meta.update({"existed": False, "kind": "missing", "backup": None})
    return meta


def file_matches_history_backup(cwd: Path, path: Path, meta: dict[str, Any]) -> bool:
    existed = bool(meta.get("existed"))
    if not existed:
        return not path.exists()
    if meta.get("kind") == "directory":
        return path.exists() and path.is_dir()
    if file_history_meta_skipped(meta):
        try:
            if not path.exists() or not path.is_file():
                return False
            stat = path.stat()
            size = meta.get("size")
            mtime_ns = meta.get("mtime_ns")
            if isinstance(size, int) and stat.st_size != size:
                return False
            if isinstance(mtime_ns, int) and stat.st_mtime_ns != mtime_ns:
                return False
            return True
        except OSError:
            return False
    backup = meta.get("backup")
    if not isinstance(backup, str) or not backup:
        return False
    source = file_history_backup_path(cwd, backup)
    if not path.is_file() or not source.exists():
        return False
    mode = meta.get("mode")
    try:
        if isinstance(mode, int) and path.stat().st_mode != mode:
            return False
        if path.stat().st_size != source.stat().st_size:
            return False
        return path.read_bytes() == source.read_bytes()
    except OSError:
        return False


def next_file_history_meta(
    manifest: dict[str, Any],
    *,
    cwd: Path,
    key: str,
    path: Path,
    reason: str,
) -> dict[str, Any]:
    tracked = manifest.setdefault("tracked_files", {})
    if not isinstance(tracked, dict):
        tracked = {}
        manifest["tracked_files"] = tracked
    entry = tracked.get(key)
    if not isinstance(entry, dict):
        meta = create_file_history_backup(cwd, path, version=1, reason=reason)
        store_tracked_backup(manifest, key=key, path=path, cwd=cwd, meta=meta)
        return meta

    latest = latest_tracked_backup(entry)
    if latest is not None and file_matches_history_backup(cwd, path, latest):
        return dict(latest)
    latest_version = entry.get("latest_version")
    version = latest_version + 1 if isinstance(latest_version, int) else 1
    meta = create_file_history_backup(cwd, path, version=version, reason=reason)
    store_tracked_backup(manifest, key=key, path=path, cwd=cwd, meta=meta)
    return meta


def capture_file_history_snapshot(cwd: Path, checkpoint: dict[str, Any], manifest: dict[str, Any]) -> None:
    tracked = manifest.get("tracked_files")
    if not isinstance(tracked, dict):
        tracked = {}
        manifest["tracked_files"] = tracked

    checkpoint["history_version"] = 2
    snapshot_backups: dict[str, Any] = {}
    for key in sorted(tracked):
        entry = tracked.get(key)
        if not isinstance(entry, dict):
            continue
        path = Path(key).expanduser().resolve()
        if file_history_excluded_reason(path, cwd) is not None:
            continue
        latest = latest_tracked_backup(entry)
        if latest is None or not file_matches_history_backup(cwd, path, latest):
            meta = next_file_history_meta(
                manifest,
                cwd=cwd,
                key=key,
                path=path,
                reason="snapshot",
            )
        else:
            meta = dict(latest)
        snapshot_backups[key] = dict(meta)

    checkpoint["tracked_file_backups"] = snapshot_backups
    checkpoint["file_backups"] = snapshot_backups


def track_file_history_edits(cwd: Path, checkpoint: dict[str, Any], paths: list[Path], *, reason: str) -> None:
    if not paths:
        return
    manifest = load_manifest(cwd)
    manifest["tracked_files"] = load_file_history_state(cwd)
    checkpoints = manifest.setdefault("checkpoints", [])
    if not isinstance(checkpoints, list):
        checkpoints = []
        manifest["checkpoints"] = checkpoints

    checkpoint_id = checkpoint.get("id")
    target_checkpoint: dict[str, Any] | None = None
    for index, existing in enumerate(checkpoints):
        if isinstance(existing, dict) and existing.get("id") == checkpoint_id:
            target_checkpoint = load_checkpoint(cwd, existing) or existing
            checkpoints[index] = target_checkpoint
            break
    if target_checkpoint is None:
        target_checkpoint = checkpoint
        checkpoints.append(target_checkpoint)

    target_checkpoint["history_version"] = 2
    snapshot_backups = checkpoint_history_backups(target_checkpoint)
    for file_path in dedupe_paths(paths):
        key, path = normalize_file_history_path(file_path)
        if file_history_excluded_reason(path, cwd) is not None:
            continue
        if key in snapshot_backups:
            continue
        meta = next_file_history_meta(
            manifest,
            cwd=cwd,
            key=key,
            path=path,
            reason=reason,
        )
        snapshot_backups[key] = dict(meta)

    checkpoint_file_backups_alias(target_checkpoint)
    manifest["checkpoints"] = checkpoints[-MAX_FILE_HISTORY_SNAPSHOTS:]
    manifest["updated_at"] = now_iso()
    save_manifest(cwd, manifest)


def dirty_tracked_files(root: Path) -> set[str]:
    dirty = set(git_z(root, ["diff", "--name-only", "-z", "--"]))
    dirty.update(git_z(root, ["diff", "--cached", "--name-only", "-z", "--"]))
    dirty.update(git_z(root, ["ls-files", "-d", "-z"]))
    return {path for path in dirty if path}


def tracked_files(root: Path) -> set[str]:
    return set(git_z(root, ["ls-files", "-z"]))


def untracked_files(root: Path) -> set[str]:
    return set(git_z(root, ["ls-files", "--others", "--exclude-standard", "-z"]))


def ignored_files(root: Path) -> set[str]:
    return set(git_z(root, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]))


def write_checkpoint_bytes(cwd: Path, checkpoint: dict[str, Any], rel: str, data: bytes) -> str | None:
    if not data:
        return None
    path = checkpoint_dir(cwd, checkpoint["id"]) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def workspace_excluded_roots(cwd: Path) -> list[Path]:
    root = cwd.resolve()
    candidates = [storage_home().resolve()]
    return [path for path in candidates if is_inside(path, root)]


def is_workspace_excluded(path: Path, excluded_roots: list[Path]) -> bool:
    return any(is_inside(path, excluded) for excluded in excluded_roots)


def is_workspace_excluded_ancestor(path: Path, excluded_roots: list[Path]) -> bool:
    return any(is_ancestor_of(path, excluded) for excluded in excluded_roots)


def workspace_inventory(
    root: Path,
    *,
    excluded_roots: list[Path],
    max_entries: int = MAX_WORKSPACE_SNAPSHOT_ENTRIES,
) -> dict[str, Any]:
    files: set[str] = set()
    dirs: set[str] = set()
    skipped: list[str] = []
    stack = [root.resolve()]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if is_workspace_excluded(path, excluded_roots):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_workspace_excluded_ancestor(path, excluded_roots):
                                dirs.add(workspace_rel(path, root))
                            stack.append(path)
                        else:
                            files.add(workspace_rel(path, root))
                    except OSError:
                        skipped.append(safe_rel(path, root))
                    if len(files) + len(dirs) > max_entries:
                        return {
                            "complete": False,
                            "files": sorted(files),
                            "dirs": sorted(dirs),
                            "skipped": skipped,
                            "truncated_at": max_entries,
                        }
        except OSError:
            skipped.append(safe_rel(current, root))

    return {
        "complete": True,
        "files": sorted(files),
        "dirs": sorted(dirs),
        "skipped": skipped,
    }


def remember_workspace_snapshot_skip(skipped_paths: list[str], rel: str) -> None:
    if len(skipped_paths) < MAX_WORKSPACE_SNAPSHOT_SKIPPED_PATHS:
        skipped_paths.append(rel)


def capture_workspace_file_backups(
    cwd: Path,
    checkpoint: dict[str, Any],
    root: Path,
    baseline_files: list[str],
) -> dict[str, Any]:
    total_bytes = 0
    captured_files = 0
    skipped_files = 0
    skipped_paths: list[str] = []

    for rel in baseline_files:
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file():
                skipped_files += 1
                remember_workspace_snapshot_skip(skipped_paths, rel)
                continue
            size = path.stat().st_size
        except OSError:
            skipped_files += 1
            remember_workspace_snapshot_skip(skipped_paths, rel)
            continue

        if size > MAX_WORKSPACE_SNAPSHOT_FILE_BYTES or total_bytes + size > MAX_WORKSPACE_SNAPSHOT_TOTAL_BYTES:
            skipped_files += 1
            remember_workspace_snapshot_skip(skipped_paths, rel)
            continue

        add_file_backup(cwd, checkpoint, path, reason="workspace-baseline", force=True)
        total_bytes += size
        captured_files += 1

    info: dict[str, Any] = {
        "snapshot_version": 1,
        "captured_files": captured_files,
        "captured_bytes": total_bytes,
        "skipped_files": skipped_files,
        "complete": skipped_files == 0,
        "max_file_bytes": MAX_WORKSPACE_SNAPSHOT_FILE_BYTES,
        "max_total_bytes": MAX_WORKSPACE_SNAPSHOT_TOTAL_BYTES,
    }
    if skipped_paths:
        info["skipped_paths"] = skipped_paths
    return info


def capture_workspace_baseline(cwd: Path, checkpoint: dict[str, Any]) -> None:
    root = cwd.resolve()
    excluded_roots = workspace_excluded_roots(root)
    inventory = workspace_inventory(root, excluded_roots=excluded_roots)
    checkpoint["workspace"] = {
        "snapshot_version": 1,
        "root": str(root),
        "baseline_files": inventory["files"],
        "baseline_dirs": inventory["dirs"],
        "baseline_complete": bool(inventory["complete"]),
        "baseline_skipped": inventory.get("skipped") or [],
        "excluded_roots": [str(path) for path in excluded_roots],
        "max_entries": MAX_WORKSPACE_SNAPSHOT_ENTRIES,
    }
    checkpoint["workspace"]["baseline_file_backups"] = capture_workspace_file_backups(
        cwd,
        checkpoint,
        root,
        list(inventory["files"]),
    )
    if not inventory["complete"]:
        checkpoint["workspace"]["baseline_truncated_at"] = inventory.get("truncated_at")


def capture_git_baseline(cwd: Path, checkpoint: dict[str, Any]) -> None:
    root = git_root(cwd)
    if root is None:
        checkpoint["git"] = None
        capture_workspace_baseline(cwd, checkpoint)
        return

    has_head = git_has_head(root)
    checkpoint["git"] = {
        "root": str(root),
        "has_head": has_head,
        "baseline_tracked": sorted(tracked_files(root)),
        "baseline_untracked": sorted(untracked_files(root)),
        "baseline_ignored": sorted(ignored_files(root)),
        "baseline_dirty": sorted(dirty_tracked_files(root)),
        "max_baseline_untracked_bytes": MAX_BASELINE_UNTRACKED_BYTES,
    }
    git_info = checkpoint["git"]

    if has_head:
        staged_patch = git_bytes(root, ["diff", "--cached", "--binary", "--"], check=False)
        unstaged_patch = git_bytes(root, ["diff", "--binary", "--"], check=False)
        status_z = git_bytes(root, ["status", "--porcelain=v1", "-z"], check=False)
        git_info.update(
            {
                "snapshot_version": 2,
                "baseline_staged_patch": write_checkpoint_bytes(cwd, checkpoint, "git/staged.patch", staged_patch),
                "baseline_unstaged_patch": write_checkpoint_bytes(cwd, checkpoint, "git/unstaged.patch", unstaged_patch),
                "baseline_status_z": write_checkpoint_bytes(cwd, checkpoint, "git/status.z", status_z),
            }
        )

    for rel in git_info["baseline_dirty"]:
        add_file_backup(
            cwd,
            checkpoint,
            root / rel,
            reason="git-dirty-baseline",
            force=True,
        )

    skipped_untracked: list[str] = []
    for rel in git_info["baseline_untracked"]:
        path = root / rel
        try:
            if path.is_file() and path.stat().st_size <= MAX_BASELINE_UNTRACKED_BYTES:
                add_file_backup(
                    cwd,
                    checkpoint,
                    path,
                    reason="git-untracked-baseline",
                    force=True,
                )
            else:
                skipped_untracked.append(rel)
        except OSError:
            skipped_untracked.append(rel)
            continue
    if skipped_untracked:
        git_info["baseline_untracked_skipped"] = sorted(skipped_untracked)


def make_checkpoint(
    cwd: Path,
    *,
    prompt: str = "",
    thread_id: str | None = None,
    turn_id: str | None = None,
    user_message_id: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    manifest = load_manifest(cwd)
    if turn_id:
        for checkpoint in manifest.get("checkpoints", []):
            same_turn = checkpoint.get("turn_id") == turn_id
            same_thread = not thread_id or checkpoint.get("thread_id") in (None, thread_id)
            if same_turn and same_thread:
                checkpoint = load_checkpoint(cwd, checkpoint) or checkpoint
                if thread_id and not checkpoint.get("thread_id"):
                    checkpoint["thread_id"] = thread_id
                    update_checkpoint(cwd, checkpoint)
                return checkpoint

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = short_hash(f"{thread_id or ''}:{turn_id or ''}:{prompt}:{time.time()}", 8)
    sequence = int(manifest.get("snapshot_sequence") or len(manifest.get("checkpoints", []))) + 1
    checkpoint = {
        "id": f"{stamp}-{suffix}",
        "sequence": sequence,
        "history_version": 2,
        "created_at": now_iso(),
        "cwd": str(cwd),
        "thread_id": thread_id,
        "turn_id": turn_id,
        "user_message_id": user_message_id,
        "source": source,
        "prompt_preview": prompt.strip().replace("\n", " ")[:240],
        "tracked_file_backups": {},
        "file_backups": {},
    }
    manifest["tracked_files"] = load_file_history_state(cwd)
    capture_file_history_snapshot(cwd, checkpoint, manifest)
    manifest.setdefault("checkpoints", []).append(checkpoint)
    manifest["checkpoints"] = manifest["checkpoints"][-MAX_FILE_HISTORY_SNAPSHOTS:]
    manifest["snapshot_sequence"] = sequence
    manifest["updated_at"] = now_iso()
    save_manifest(cwd, manifest)
    return checkpoint


def current_checkpoint(cwd: Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = load_manifest(cwd)
    checkpoints = manifest.get("checkpoints", [])
    identity = payload_identity(payload or {})
    turn_id = identity.get("turn_id")
    thread_id = identity.get("thread_id")
    if turn_id:
        for checkpoint in reversed(checkpoints):
            same_turn = checkpoint.get("turn_id") == turn_id
            same_thread = not thread_id or checkpoint.get("thread_id") in (None, thread_id)
            if same_turn and same_thread:
                checkpoint = load_checkpoint(cwd, checkpoint) or checkpoint
                if thread_id and not checkpoint.get("thread_id"):
                    checkpoint["thread_id"] = thread_id
                    update_checkpoint(cwd, checkpoint)
                return checkpoint
    if checkpoints:
        return load_checkpoint(cwd, checkpoints[-1]) or checkpoints[-1]
    return make_checkpoint(cwd, source="auto")


def update_checkpoint(cwd: Path, checkpoint: dict[str, Any]) -> None:
    manifest = load_manifest(cwd)
    checkpoints = manifest.get("checkpoints", [])
    for index, existing in enumerate(checkpoints):
        if existing.get("id") == checkpoint.get("id"):
            checkpoints[index] = checkpoint
            manifest["updated_at"] = now_iso()
            save_manifest(cwd, manifest)
            return
    checkpoints.append(checkpoint)
    manifest["checkpoints"] = checkpoints[-MAX_FILE_HISTORY_SNAPSHOTS:]
    manifest["updated_at"] = now_iso()
    save_manifest(cwd, manifest)


def extract_patch_paths(patch_text: str, cwd: Path) -> list[Path]:
    paths: list[Path] = []
    prefixes = (
        "*** Update File: ",
        "*** Delete File: ",
        "*** Add File: ",
    )
    for line in patch_text.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                raw = line[len(prefix) :].strip()
                if raw:
                    paths.append((cwd / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve())
    return paths


def shell_path(value: str, cwd: Path) -> Path | None:
    if not value or value.startswith("-"):
        return None
    if value in {"&&", "||", ";", "|"}:
        return None
    if re.match(r"^[0-9]?(?:>>?|<|&>)", value):
        return None
    if value in {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"}:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def shell_command_operands(parts: list[str]) -> list[str]:
    operands: list[str] = []
    skip_next = False
    control_tokens = {"&&", "||", ";", "|"}
    redirection_tokens = {">", ">>", "<", "2>", "2>>", "1>", "1>>", "&>"}
    for item in parts[1:]:
        if skip_next:
            skip_next = False
            continue
        if item in control_tokens:
            break
        if item in redirection_tokens:
            skip_next = True
            continue
        if re.match(r"^[0-9]?(?:>>?|<|&>)", item):
            continue
        if item.startswith("-"):
            continue
        operands.append(item)
    return operands


def extract_shell_paths(command: str, cwd: Path) -> list[Path]:
    paths: list[Path] = []
    for match in re.finditer(r"(?:^|[;&|]\s*|\s)(?:[0-9]?>|>>|&>)\s*(['\"]?)([^'\"\s;&|]+)\1", command):
        path = shell_path(match.group(2), cwd)
        if path is not None:
            paths.append(path)

    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return paths
    if not parts:
        return paths

    command_name = Path(parts[0]).name
    if command_name in {"touch", "rm", "unlink", "mkdir", "rmdir"}:
        for item in shell_command_operands(parts):
            path = shell_path(item, cwd)
            if path is not None:
                paths.append(path)
    elif command_name in {"cp", "mv"}:
        operands = shell_command_operands(parts)
        for item in operands[-2:]:
            path = shell_path(item, cwd)
            if path is not None:
                paths.append(path)
    elif command_name in {"install"}:
        operands = shell_command_operands(parts)
        for item in operands[-2:]:
            path = shell_path(item, cwd)
            if path is not None:
                paths.append(path)
    return paths


def payload_tool_name(payload: dict[str, Any]) -> str:
    return first_string(
        payload.get("tool_name"),
        payload.get("toolName"),
        payload.get("name"),
        payload.get("tool"),
    ) or ""


def is_bash_tool_payload(payload: dict[str, Any], tool_input: dict[str, Any]) -> bool:
    name = payload_tool_name(payload).lower()
    if name in {"bash", "shell", "shell_command", "functions.shell_command"}:
        return True
    return isinstance(tool_input.get("command"), str) and not any(
        isinstance(tool_input.get(key), str)
        for key in ("path", "file_path", "filepath", "notebook_path", "target_file")
    )


def extract_simulated_sed_paths(tool_input: dict[str, Any], cwd: Path) -> list[Path]:
    simulated = tool_input.get("_simulatedSedEdit")
    if not isinstance(simulated, dict):
        simulated = tool_input.get("simulatedSedEdit")
    if not isinstance(simulated, dict):
        return []
    file_path = first_string(simulated.get("filePath"), simulated.get("file_path"))
    if not file_path:
        return []
    path = Path(file_path).expanduser()
    return [path.resolve() if path.is_absolute() else (cwd / path).resolve()]


def extract_tool_paths(payload: dict[str, Any], cwd: Path) -> list[Path]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    tool_cwd = cwd
    workdir = tool_input.get("workdir")
    if isinstance(workdir, str) and workdir:
        tool_cwd = canonical_cwd(workdir)

    if is_bash_tool_payload(payload, tool_input):
        return dedupe_paths(extract_simulated_sed_paths(tool_input, tool_cwd))

    candidates: list[str] = []
    for key in (
        "path",
        "file_path",
        "filepath",
        "notebook_path",
        "target_file",
        "target",
        "output_path",
        "out_file",
    ):
        value = tool_input.get(key)
        if isinstance(value, str):
            candidates.append(value)

    patch_text = ""
    for key in ("patch", "input", "new_xml"):
        value = tool_input.get(key)
        if isinstance(value, str) and "*** " in value:
            patch_text += "\n" + value
    if isinstance(payload.get("input"), str) and "*** " in payload["input"]:
        patch_text += "\n" + payload["input"]

    paths = [(tool_cwd / item).resolve() if not Path(item).is_absolute() else Path(item).resolve() for item in candidates]
    paths.extend(extract_patch_paths(patch_text, tool_cwd))
    return dedupe_paths(paths)


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def hook_user_prompt(args: argparse.Namespace) -> int:
    payload = read_stdin_json()
    cwd = cwd_from_payload(payload, args.cwd)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""

    if is_rewind_prompt(prompt):
        result = run_rewind_gui_from_payload(payload, args)
        status = result.get("status")
        if status == "applied":
            return emit_user_prompt_block(
                f"rewind applied: target={result.get('target')} mode={result.get('mode')}"
            )
        if status == "dismissed":
            return emit_user_prompt_block("rewind session dismissed")
        if status == "failed":
            return emit_user_prompt_block(f"rewind failed: {result.get('error')}")
        return emit_user_prompt_block("rewind session dismissed")

    identity = payload_identity(payload)
    make_checkpoint(
        cwd,
        prompt=prompt,
        thread_id=identity.get("thread_id"),
        turn_id=identity.get("turn_id"),
        user_message_id=identity.get("user_message_id"),
        source="UserPromptSubmit",
    )
    maybe_cleanup_old_rewind_storage()
    return 0


def hook_pre_tool(args: argparse.Namespace) -> int:
    payload = read_stdin_json()
    cwd = cwd_from_payload(payload, args.cwd)
    checkpoint = current_checkpoint(cwd, payload)
    track_file_history_edits(cwd, checkpoint, extract_tool_paths(payload, cwd), reason="pre-tool")
    return 0


def resolve_checkpoint(cwd: Path, selector: str | None) -> dict[str, Any]:
    manifest = load_manifest(cwd)
    checkpoints = manifest.get("checkpoints", [])
    if not checkpoints:
        raise SystemExit("No rewind checkpoints for this cwd.")
    if not selector or selector == "latest":
        return load_checkpoint(cwd, checkpoints[-1]) or checkpoints[-1]
    if selector.isdigit():
        index = int(selector)
        if index < 1 or index > len(checkpoints):
            raise SystemExit(f"Checkpoint index out of range: {selector}")
        checkpoint = checkpoints[index - 1]
        return load_checkpoint(cwd, checkpoint) or checkpoint
    matches = [item for item in checkpoints if item.get("id", "").startswith(selector)]
    if len(matches) == 1:
        return load_checkpoint(cwd, matches[0]) or matches[0]
    if not matches:
        raise SystemExit(f"No checkpoint matches: {selector}")
    raise SystemExit(f"Ambiguous checkpoint prefix: {selector}")


def thread_db(home: Path) -> Path:
    return home / "state_5.sqlite"


def row_to_thread(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "rollout_path": row["rollout_path"],
        "cwd": row["cwd"],
        "title": row["title"],
        "preview": row["preview"],
        "updated_at_ms": row["updated_at_ms"],
    }


def load_thread_from_state(
    *,
    home: Path,
    cwd: Path,
    thread_id: str | None,
    rollout_path: str | None,
) -> dict[str, Any]:
    if rollout_path:
        path = Path(rollout_path).expanduser()
        return {
            "id": thread_id or path.stem.rsplit("-", 1)[-1],
            "rollout_path": str(path),
            "cwd": str(cwd),
            "title": "",
            "preview": "",
            "updated_at_ms": None,
        }

    db_path = thread_db(home)
    if not db_path.exists():
        raise SystemExit(f"Codex state database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if thread_id and thread_id != "latest":
            row = conn.execute(
                "select id, rollout_path, cwd, title, preview, updated_at_ms "
                "from threads where id = ?",
                (thread_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "select id, rollout_path, cwd, title, preview, updated_at_ms "
                "from threads where archived = 0 and cwd = ? "
                "order by coalesce(updated_at_ms, updated_at * 1000) desc, id desc limit 1",
                (str(cwd),),
            ).fetchone()
        if row is None:
            target = thread_id if thread_id and thread_id != "latest" else f"latest cwd={cwd}"
            raise SystemExit(f"No Codex thread found for {target}")
        return row_to_thread(row)
    finally:
        conn.close()


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def user_visible_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    internal_prefixes = (
        "# AGENTS.md instructions",
        "<turn_aborted>",
        "<skill>",
    )
    return not any(stripped.startswith(prefix) for prefix in internal_prefixes)


def one_line_preview(text: str, width: int = 120) -> str:
    preview = " ".join(text.strip().split())
    return preview[:width]


MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\((?:\\.|[^)])*\)")
CODE_TRAILER_RE = re.compile(r"(?:^|\s+)code:\S+")


def markdown_display_text(text: str) -> str:
    def replace_link(match: re.Match[str]) -> str:
        label = match.group(1)
        return label.replace(r"\[", "[").replace(r"\]", "]")

    previous = text
    for _ in range(4):
        current = MARKDOWN_LINK_RE.sub(replace_link, previous)
        if current == previous:
            break
        previous = current
    return previous


def strip_code_trailers(text: str) -> str:
    return CODE_TRAILER_RE.sub("", text)


def target_display_text(message: dict[str, Any]) -> str:
    text = message.get("text") or message.get("preview") or ""
    display = markdown_display_text(str(text))
    display = strip_code_trailers(display)
    return " ".join(display.strip().split())


def parse_session_user_messages(path: Path, *, include_rewind_commands: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"Codex session record not found: {path}")

    active_turns: list[dict[str, Any]] = []
    turns_by_id: dict[str, dict[str, Any]] = {}
    current_turn_id: str | None = None
    current_cwd: str | None = None

    def ensure_turn(turn_id: str | None, *, line_no: int, timestamp: Any = None) -> dict[str, Any] | None:
        if not turn_id:
            return None
        turn = turns_by_id.get(turn_id)
        if turn is None:
            turn = {
                "turn_id": turn_id,
                "line": line_no,
                "timestamp": timestamp,
                "cwd": current_cwd,
                "messages": [],
            }
            turns_by_id[turn_id] = turn
            active_turns.append(turn)
        elif turn not in active_turns:
            active_turns.append(turn)
        return turn

    def rollback_turns(count: Any) -> None:
        try:
            total = int(count)
        except (TypeError, ValueError):
            return
        for _ in range(max(0, total)):
            if active_turns:
                active_turns.pop()

    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            if event_type == "turn_context":
                current_turn_id = first_string(payload.get("turn_id"), payload.get("turnId"))
                current_cwd = first_string(payload.get("cwd"))
                turn = ensure_turn(current_turn_id, line_no=line_no, timestamp=event.get("timestamp"))
                if turn is not None and current_cwd:
                    turn["cwd"] = current_cwd
                continue

            if event_type == "event_msg":
                payload_type = payload.get("type")
                if payload_type == "task_started":
                    ensure_turn(first_string(payload.get("turn_id"), payload.get("turnId")), line_no=line_no, timestamp=event.get("timestamp"))
                elif payload_type == "thread_rolled_back":
                    rollback_turns(payload.get("num_turns", payload.get("numTurns")))
                continue

            if event_type != "response_item":
                continue
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue

            text = content_text(payload.get("content"))
            if not user_visible_text(text):
                continue
            turn = ensure_turn(current_turn_id, line_no=line_no, timestamp=event.get("timestamp"))
            if turn is None:
                continue
            turn.setdefault("messages", []).append(
                {
                    "line": line_no,
                    "timestamp": event.get("timestamp"),
                    "turn_id": current_turn_id,
                    "cwd": current_cwd,
                    "text": text,
                    "preview": one_line_preview(text),
                }
            )

    messages: list[dict[str, Any]] = []
    visible_user_index = 0
    for turn_index, turn in enumerate(active_turns):
        rollback_num_turns = len(active_turns) - turn_index
        for message in turn.get("messages") or []:
            text = message.get("text") or ""
            if not include_rewind_commands and is_rewind_prompt(text):
                continue
            visible_user_index += 1
            message["index"] = len(messages) + 1
            message["raw_index"] = visible_user_index
            message["rollback_num_turns"] = rollback_num_turns
            message["active_turn_index"] = turn_index + 1
            message["active_turn_count"] = len(active_turns)
            messages.append(message)

    for message in messages:
        message["raw_user_count"] = visible_user_index
    return messages


def manifest_checkpoints(cwd: Path) -> list[dict[str, Any]]:
    return list(load_manifest(cwd).get("checkpoints", []))


class CheckpointIndex:
    def __init__(self, checkpoints: list[dict[str, Any]]) -> None:
        self.checkpoints = checkpoints
        self.by_turn: dict[str, dict[str, Any]] = {}
        for checkpoint in checkpoints:
            turn_id = checkpoint.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                self.by_turn[turn_id] = checkpoint

    def find(self, message: dict[str, Any]) -> dict[str, Any] | None:
        turn_id = message.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            checkpoint = self.by_turn.get(turn_id)
            if checkpoint is not None:
                return checkpoint

        preview = message.get("preview") or ""
        if preview:
            preview_prefix = str(preview)[:80]
            for checkpoint in reversed(self.checkpoints):
                checkpoint_preview = checkpoint.get("prompt_preview") or ""
                if checkpoint_preview and (
                    checkpoint_preview.startswith(preview_prefix) or preview.startswith(str(checkpoint_preview)[:80])
                ):
                    return checkpoint

        timestamp = parse_event_time(message.get("timestamp"))
        if timestamp is None:
            return None

        candidates: list[tuple[float, dict[str, Any]]] = []
        for checkpoint in self.checkpoints:
            created_at = parse_event_time(checkpoint.get("created_at"))
            if created_at is None:
                continue
            # UserPromptSubmit checkpoints are normally created seconds after the JSONL user event.
            delta = created_at - timestamp
            if -60 <= delta <= 300:
                candidates.append((abs(delta), checkpoint))
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item[0])[0][1]


def checkpoint_index(cwd: Path) -> CheckpointIndex:
    return CheckpointIndex(manifest_checkpoints(cwd))


def parse_event_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def find_checkpoint_for_message(cwd: Path, message: dict[str, Any]) -> dict[str, Any] | None:
    return checkpoint_index(cwd).find(message)


def annotate_checkpoint_ids(cwd: Path, messages: list[dict[str, Any]]) -> None:
    index = checkpoint_index(cwd)
    for message in messages:
        checkpoint = index.find(message)
        message["checkpoint_id"] = checkpoint.get("id") if checkpoint else None


def select_message(messages: list[dict[str, Any]], selector: str | None) -> dict[str, Any]:
    if not messages:
        raise SystemExit("No user messages found in this Codex thread.")
    if not selector or selector == "latest":
        return messages[-1]
    if selector.isdigit():
        index = int(selector)
        if index < 1 or index > len(messages):
            raise SystemExit(f"Target index out of range: {selector}")
        return messages[index - 1]
    matches = [item for item in messages if str(item.get("turn_id") or "").startswith(selector)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"No rewind target matches: {selector}")
    raise SystemExit(f"Ambiguous rewind target prefix: {selector}")


def reverse_rollout_update(cwd: Path, path: Path, unified_diff: str, *, dry_run: bool) -> str:
    try:
        rel = path.resolve().relative_to(cwd.resolve())
    except ValueError:
        return f"warn-rollout-update-outside-cwd {path}"
    patch_text = f"--- {rel}\t\n+++ {rel}\t\n{unified_diff.rstrip()}\n"
    if dry_run:
        return f"reverse-rollout-update {path}"
    result = run_with_input(
        ["patch", "-R", "-p0", "--batch"],
        cwd=cwd,
        input_bytes=patch_text.encode("utf-8", "surrogateescape"),
        check=False,
    )
    if result.returncode == 0:
        return f"reverse-rollout-update {path}"
    detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip().splitlines()
    suffix = f": {detail[-1]}" if detail else ""
    return f"warn-rollout-update-reverse-failed {path}{suffix}"


def rollout_patch_file_actions(
    *,
    thread: dict[str, Any],
    target: dict[str, Any],
    cwd: Path,
    dry_run: bool,
) -> list[str]:
    rollout_path = Path(str(thread.get("rollout_path") or "")).expanduser()
    target_line = int(target.get("line") or 0)
    if target_line <= 0 or not rollout_path.exists():
        return []

    entries: list[tuple[int, Path, dict[str, Any]]] = []
    seen_events: set[tuple[int, str]] = set()
    with rollout_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no < target_line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") != "event_msg" or payload.get("type") != "patch_apply_end":
                continue
            if payload.get("success") is False:
                continue
            changes = payload.get("changes")
            if not isinstance(changes, dict):
                continue
            for raw_path, meta in changes.items():
                if not isinstance(raw_path, str) or not isinstance(meta, dict):
                    continue
                path = Path(raw_path).expanduser()
                if not path.is_absolute():
                    path = cwd / path
                path = path.resolve()
                if not is_inside(path, cwd):
                    continue
                key = (line_no, str(path))
                if key in seen_events:
                    continue
                seen_events.add(key)
                entries.append((line_no, path, meta))

    actions: list[str] = []
    for _line_no, path, meta in reversed(entries):
        change_type = meta.get("type")
        if change_type == "add":
            if path.exists():
                actions.append(f"delete-rollout-added {path}")
                if not dry_run:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    remove_empty_parents(path.parent, cwd)
            else:
                actions.append(f"skip absent rollout-added {path}")
        elif change_type == "delete":
            content = meta.get("content")
            if isinstance(content, str):
                actions.append(f"restore-rollout-deleted {path}")
                if not dry_run:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
            else:
                actions.append(f"warn-rollout-delete-no-content {path}")
        elif change_type == "update":
            diff = meta.get("unified_diff")
            if isinstance(diff, str) and diff.strip():
                actions.append(reverse_rollout_update(cwd, path, diff, dry_run=dry_run))
            else:
                actions.append(f"warn-rollout-update-no-diff {path}")
        else:
            actions.append(f"warn-rollout-{change_type or 'change'}-no-preimage {path}")
    return actions


def file_actions(cwd: Path, checkpoint: dict[str, Any], *, dry_run: bool) -> list[str]:
    if checkpoint_uses_file_history(checkpoint):
        return restore_file_history_snapshot(cwd, checkpoint, dry_run=dry_run)
    return legacy_file_actions(cwd, checkpoint, dry_run=dry_run)


def checkpoint_has_git_root(checkpoint: dict[str, Any] | None) -> bool:
    if checkpoint is None:
        return False
    git_info = checkpoint.get("git")
    return isinstance(git_info, dict) and bool(git_info.get("root"))


def code_rewind_actions(
    *,
    thread: dict[str, Any],
    target: dict[str, Any],
    cwd: Path,
    dry_run: bool,
) -> tuple[str | None, list[str]]:
    checkpoint = find_checkpoint_for_message(cwd, target)
    if checkpoint is not None:
        checkpoint = load_checkpoint(cwd, checkpoint) or checkpoint
    actions: list[str] = []
    checkpoint_id = str(checkpoint["id"]) if checkpoint is not None and checkpoint.get("id") else None

    if checkpoint is not None:
        actions.extend(file_actions(cwd, checkpoint, dry_run=dry_run))
    else:
        actions.extend(rollout_patch_file_actions(thread=thread, target=target, cwd=cwd, dry_run=dry_run))

    if checkpoint is None and not actions:
        raise RuntimeError("No matching workspace checkpoint or rollout file actions for selected target.")

    return checkpoint_id, actions


def resolve_thread_and_messages(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    cwd = canonical_cwd(args.cwd)
    home = codex_home(getattr(args, "codex_home", None))
    thread = load_thread_from_state(
        home=home,
        cwd=cwd,
        thread_id=getattr(args, "thread_id", None),
        rollout_path=getattr(args, "rollout_path", None),
    )
    messages = parse_session_user_messages(Path(thread["rollout_path"]).expanduser())
    return thread, messages, cwd


def enriched_targets(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    thread, messages, cwd = resolve_thread_and_messages(args)
    annotate_checkpoint_ids(cwd, messages)
    return thread, messages, cwd


def list_targets(args: argparse.Namespace) -> int:
    thread, messages, cwd = enriched_targets(args)
    if args.json:
        print(json.dumps({"thread": thread, "cwd": str(cwd), "targets": messages}, ensure_ascii=False, indent=2))
        return 0

    print(f"Rewind targets for thread {thread['id']} ({cwd}):")
    for message in messages:
        checkpoint = message.get("checkpoint_id") or "-"
        turn_id = message.get("turn_id") or "-"
        print(f"{message['index']:>3}. turn={turn_id} checkpoint={checkpoint}  {message['preview']}")
    return 0


def build_rewind_plan(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    cwd: Path,
    selector: str | None,
    mode: str,
) -> dict[str, Any]:
    target = select_message(messages, selector)
    raw_index = int(target.get("raw_index") or target["index"])
    raw_count = int(target.get("raw_user_count") or len(messages))
    visible_user_turns = raw_count - raw_index + 1
    num_turns = int(target.get("rollback_num_turns") or (raw_count - raw_index + 1))
    plan: dict[str, Any] = {
        "mode": mode,
        "thread": thread,
        "cwd": str(cwd),
        "target": {
            key: target.get(key)
            for key in ("index", "line", "timestamp", "turn_id", "preview", "rollback_num_turns")
        },
        "session": {
            "method": SESSION_ROLLBACK_METHOD,
            "params": {"threadId": thread["id"], "numTurns": num_turns},
            "drops_user_turns": visible_user_turns,
            "drops_turns": num_turns,
        },
        "code": None,
    }

    if mode in {"code", "both"}:
        try:
            checkpoint_id, actions = code_rewind_actions(
                thread=thread,
                target=target,
                cwd=cwd,
                dry_run=True,
            )
            plan["code"] = {
                "checkpoint_id": checkpoint_id,
                "actions": actions,
            }
        except RuntimeError as exc:
            plan["code"] = {"error": str(exc)}

    return plan


def preview_rewind(args: argparse.Namespace) -> int:
    thread, messages, cwd = resolve_thread_and_messages(args)
    plan = build_rewind_plan(
        thread=thread,
        messages=messages,
        cwd=cwd,
        selector=args.target,
        mode=args.mode,
    )
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    target = plan["target"]
    print(f"Rewind preview for thread {thread['id']}")
    print(f"- target: #{target['index']} turn={target.get('turn_id') or '-'} {target['preview']}")
    if args.mode in {"session", "both"}:
        print(f"- session rollback: {SESSION_ROLLBACK_METHOD} numTurns={plan['session']['drops_user_turns']}")
    if args.mode in {"code", "both"}:
        code = plan.get("code") or {}
        if code.get("error"):
            print(f"- code rollback: {code['error']}")
        else:
            print(f"- code checkpoint: {code.get('checkpoint_id')}")
            for action in code.get("actions") or ["no file changes detected"]:
                print(f"  - {action}")
    return 0


def app_server_sock(args: argparse.Namespace, home: Path) -> str | None:
    explicit = first_string(
        getattr(args, "app_server_sock", None),
        os.environ.get("CODEX_APP_SERVER_SOCK"),
    )
    if explicit:
        return explicit
    default = home / "app-server-control" / "app-server-control.sock"
    return str(default) if default.exists() else None


def call_app_server_rollback(*, sock: str, thread_id: str, num_turns: int) -> subprocess.CompletedProcess[Any]:
    request = {
        "id": 1,
        "method": SESSION_ROLLBACK_METHOD,
        "params": {"threadId": thread_id, "numTurns": num_turns},
    }
    cmd = ["codex", "app-server", "proxy", "--sock", sock]
    return subprocess.run(
        cmd,
        input=json.dumps(request) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )


def parse_app_server_response(result: subprocess.CompletedProcess[Any], *, request_id: int) -> dict[str, Any]:
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") != request_id:
            continue
        if "error" in payload:
            error = payload["error"]
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                message = str(error)
            raise RuntimeError(message)
        response = payload.get("result")
        return response if isinstance(response, dict) else {}

    detail = result.stdout.strip() or result.stderr.strip()
    if detail:
        raise RuntimeError(f"Codex app-server returned no response for {SESSION_ROLLBACK_METHOD}: {detail}")
    raise RuntimeError(f"Codex app-server returned no response for {SESSION_ROLLBACK_METHOD}")


def apply_rewind(args: argparse.Namespace) -> int:
    if not args.yes:
        raise SystemExit("Refusing to rewind without --yes. Run preview first.")

    thread, messages, cwd = resolve_thread_and_messages(args)
    plan = build_rewind_plan(
        thread=thread,
        messages=messages,
        cwd=cwd,
        selector=args.target,
        mode=args.mode,
    )

    target = select_message(messages, args.target)
    applied_actions: list[str] = []
    if args.mode in {"code", "both"}:
        code = plan.get("code") or {}
        if code.get("error"):
            raise SystemExit(str(code["error"]))

    sock = None
    if args.mode in {"session", "both"}:
        sock = app_server_sock(args, codex_home(getattr(args, "codex_home", None)))
        if not sock:
            print(json.dumps(plan, ensure_ascii=False, indent=2), file=sys.stderr)
            raise SystemExit(
                "Session rollback requires a live Codex app-server control socket for the current thread. "
                "Pass --app-server-sock or run this through a native Codex CLI/App integration."
            )

    if args.mode in {"code", "both"}:
        _, applied_actions = code_rewind_actions(
            thread=thread,
            target=target,
            cwd=cwd,
            dry_run=False,
        )

    rollback_result: subprocess.CompletedProcess[Any] | None = None
    if args.mode in {"session", "both"} and sock:
        params = plan["session"]["params"]
        rollback_result = call_app_server_rollback(
            sock=sock,
            thread_id=params["threadId"],
            num_turns=params["numTurns"],
        )
        if rollback_result.returncode != 0:
            if applied_actions:
                print("Code rollback was applied, but session rollback failed.", file=sys.stderr)
            print(rollback_result.stderr.strip(), file=sys.stderr)
            raise SystemExit(rollback_result.returncode)
        try:
            parse_app_server_response(rollback_result, request_id=1)
        except RuntimeError as exc:
            if applied_actions:
                print("Code rollback was applied, but session rollback failed.", file=sys.stderr)
            raise SystemExit(f"Session rollback failed: {exc}")

    print(f"APPLIED rewind target #{plan['target']['index']} mode={args.mode}")
    if applied_actions:
        for action in applied_actions:
            print(f"- {action}")
    if rollback_result is not None:
        print(f"- session rollback: {rollback_result.stdout.strip() or 'ok'}")
    return 0


def perform_rewind_plan(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    cwd: Path,
    target_index: int,
    mode: str,
    app_sock: str | None,
) -> dict[str, Any]:
    plan = build_rewind_plan(
        thread=thread,
        messages=messages,
        cwd=cwd,
        selector=str(target_index),
        mode=mode,
    )

    target = select_message(messages, str(target_index))
    if mode in {"code", "both"}:
        code = plan.get("code") or {}
        if code.get("error"):
            raise RuntimeError(str(code["error"]))

    if mode in {"session", "both"} and not app_sock:
        raise RuntimeError(
            "session rollback requires a live Codex app-server control socket for the current thread; "
            "only code rollback is available from the external hook"
        )

    applied_actions: list[str] = []
    if mode in {"code", "both"}:
        _, applied_actions = code_rewind_actions(
            thread=thread,
            target=target,
            cwd=cwd,
            dry_run=False,
        )

    session_output = ""
    if mode in {"session", "both"}:
        params = plan["session"]["params"]
        result = call_app_server_rollback(
            sock=str(app_sock),
            thread_id=params["threadId"],
            num_turns=params["numTurns"],
        )
        session_output = result.stdout.strip()
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "thread rollback failed")
        parse_app_server_response(result, request_id=1)

    return {
        "status": "applied",
        "target": target_index,
        "mode": mode,
        "actions": applied_actions,
        "session_output": session_output,
    }


def build_no_apply_selection(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    cwd: Path,
    target_index: int,
    mode: str,
) -> dict[str, Any]:
    plan = build_rewind_plan(
        thread=thread,
        messages=messages,
        cwd=cwd,
        selector=str(target_index),
        mode=mode,
    )
    return {
        "status": "selected",
        "target": target_index,
        "mode": mode,
        "no_apply": True,
        "plan": plan,
    }


def truncate_button_text(index: int, text: str, width: int = 92) -> str:
    normalized = " ".join(markdown_display_text(text).strip().split())
    preview = one_line_preview(normalized, width)
    if len(normalized) > width:
        preview = preview.rstrip() + "..."
    return f"{index}. {preview}"


def alert_error(message: str) -> None:
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                (
                    'display dialog '
                    + json.dumps(message)
                    + ' with title "Codex Rewind" buttons {"OK"} default button "OK" with icon caution'
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


def appkit_source_path() -> Path:
    return Path(__file__).resolve().with_name("codex_rewind_appkit.swift")


def appkit_binary_path() -> Path:
    return storage_home() / "bin" / "codex-rewind-appkit"


def ensure_appkit_binary() -> Path | None:
    source = appkit_source_path()
    if not source.exists() or not shutil.which("swiftc"):
        return None
    binary = appkit_binary_path()
    try:
        needs_build = not binary.exists() or source.stat().st_mtime > binary.stat().st_mtime
        if needs_build:
            binary.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["swiftc", "-o", str(binary), str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return None
        return binary if binary.exists() else None
    except OSError:
        return None


def run_appkit_gui(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    if os.environ.get("CODEX_REWIND_GUI_TK") or os.environ.get("CODEX_REWIND_GUI_DISABLE_APPKIT"):
        return None
    binary = ensure_appkit_binary()
    if binary is None:
        return None

    input_path = Path(f"/tmp/codex-rewind-appkit-{os.getpid()}-{int(time.time() * 1000)}.json")
    payload = {
        "targets": [
            {
                "index": int(message["index"]),
                "preview": message.get("preview") or "",
                "text": message.get("text") or "",
                "display_text": target_display_text(message),
                "checkpoint_id": message.get("checkpoint_id"),
            }
            for message in messages
        ]
    }
    try:
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [str(binary), str(input_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip().splitlines()
        if not raw:
            return {"status": "dismissed"}
        parsed = json.loads(raw[-1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
    finally:
        try:
            input_path.unlink()
        except FileNotFoundError:
            pass


class RewindGui:
    WIDTH = 560
    MIN_HEIGHT = 300
    MAX_HEIGHT = 430
    PAGE_SIZE = 7
    PALETTE = {
        "bg": "#202422",
        "panel": "#2b302e",
        "panel_active": "#35413c",
        "panel_border": "#454b49",
        "fg": "#f2f2ee",
        "muted": "#b8bbb7",
        "dim": "#8f9490",
    }

    def __init__(
        self,
        *,
        thread: dict[str, Any],
        messages: list[dict[str, Any]],
        cwd: Path,
        app_sock: str | None,
    ) -> None:
        import tkinter as tk

        self.tk = tk
        self.thread = thread
        self.messages = messages
        self.cwd = cwd
        self.app_sock = app_sock
        self.result: dict[str, Any] = {"status": "dismissed"}
        self.selected_target: int | None = None
        self.digit_buffer = ""
        self.digit_after: str | None = None
        self.view: Any | None = None
        self.body_window_id: int | None = None
        self.target_canvas: Any | None = None
        self.target_hitboxes: list[tuple[int, int, int]] = []
        self.hover_target: int | None = None
        self.target_page = 0

        self.root = tk.Tk()
        self.root.title("Codex Rewind")
        self.root.configure(bg=self.PALETTE["bg"])
        self.root.resizable(False, False)
        self.set_window_geometry()
        self.root.protocol("WM_DELETE_WINDOW", self.dismiss)
        self.root.bind_all("<Escape>", self.on_escape)
        self.root.bind_all("<Key>", self.on_key)

        self.show_targets()
        self.root.after(60, self.focus_window)

    def run(self) -> dict[str, Any]:
        self.root.mainloop()
        return self.result

    def set_window_geometry(self) -> None:
        visible_rows = min(max(len(self.messages), 3), self.PAGE_SIZE)
        nav_rows = 1 if len(self.messages) > self.PAGE_SIZE else 0
        height = min(self.MAX_HEIGHT, max(self.MIN_HEIGHT, 64 + visible_rows * 42 + nav_rows * 40))
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(24, int((screen_w - self.WIDTH) / 2))
        y = max(48, int((screen_h - height) / 3))
        self.root.geometry(f"{self.WIDTH}x{height}+{x}+{y}")

    def focus_window(self) -> None:
        try:
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def reset_digits(self) -> None:
        self.digit_buffer = ""
        if self.digit_after:
            self.root.after_cancel(self.digit_after)
            self.digit_after = None

    def replace_view(self) -> Any:
        tk = self.tk
        self.reset_digits()
        old = self.view
        if old is not None:
            for child in list(old.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    pass
            old.pack_forget()
            old.destroy()
        self.root.update_idletasks()
        self.body_window_id = None
        self.target_canvas = None
        self.target_hitboxes = []
        self.hover_target = None
        self.view = tk.Frame(self.root, bg=self.PALETTE["bg"])
        self.view.pack(fill="both", expand=True, padx=16, pady=(10, 12))
        return self.view

    def header(self, parent: Any, title: str, *, back: bool = False) -> None:
        tk = self.tk
        frame = tk.Frame(parent, bg=self.PALETTE["bg"])
        frame.pack(fill="x", pady=(0, 8))
        if back:
            btn = self.clickable(
                frame,
                text="< 返回",
                command=self.queue_show_targets,
                padx=4,
                pady=5,
                width=7,
            )
            btn.pack(side="left")
        label = tk.Label(
            frame,
            text=title,
            bg=self.PALETTE["bg"],
            fg=self.PALETTE["fg"],
            font=("Helvetica", 14, "bold"),
            anchor="w",
        )
        label.pack(side="left", padx=(12 if back else 0, 0))

    def clickable(
        self,
        parent: Any,
        *,
        text: str,
        command: Any,
        anchor: str = "center",
        justify: str = "center",
        wraplength: int | None = None,
        font: tuple[str, int] | tuple[str, int, str] | None = None,
        padx: int = 10,
        pady: int = 7,
        width: int | None = None,
    ) -> Any:
        tk = self.tk
        widget = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.PALETTE["panel"],
            fg=self.PALETTE["fg"],
            activebackground=self.PALETTE["panel_active"],
            activeforeground=self.PALETTE["fg"],
            anchor=anchor,
            justify=justify,
            wraplength=wraplength or 0,
            font=font,
            padx=padx,
            pady=pady,
            width=width or 0,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        return widget

    def show_targets(self) -> None:
        tk = self.tk
        view = self.replace_view()
        self.selected_target = None
        self.root.bind_all("<MouseWheel>", self.on_mousewheel)
        self.root.bind_all("<Button-4>", self.on_mousewheel)
        self.root.bind_all("<Button-5>", self.on_mousewheel)

        if not self.messages:
            tk.Button(
                view,
                text="当前线程没有可回退的用户对话。",
                state="disabled",
                relief="flat",
                padx=10,
                pady=10,
            ).pack(fill="x", pady=4)
            return

        page_count = max(1, (len(self.messages) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.target_page = min(max(self.target_page, 0), page_count - 1)
        start = self.target_page * self.PAGE_SIZE
        visible_messages = self.messages[start : start + self.PAGE_SIZE]

        for message in visible_messages:
            index = int(message["index"])
            text = truncate_button_text(index, target_display_text(message), 72)
            button = tk.Button(
                view,
                text=text,
                command=lambda value=index: self.choose_target(value),
                anchor="w",
                justify="left",
                wraplength=self.WIDTH - 56,
                bg=self.PALETTE["panel"],
                activebackground=self.PALETTE["panel_active"],
                fg=self.PALETTE["fg"],
                activeforeground=self.PALETTE["fg"],
                relief="flat",
                padx=10,
                pady=7,
                font=("Helvetica", 12, "bold"),
            )
            button.pack(fill="x", pady=4)

        if page_count > 1:
            nav = tk.Frame(view, bg=self.PALETTE["bg"])
            nav.pack(fill="x", pady=(6, 0))
            prev_btn = tk.Button(
                nav,
                text="< 上一页",
                command=self.prev_page,
                state=("normal" if self.target_page > 0 else "disabled"),
                relief="flat",
                padx=8,
                pady=6,
            )
            prev_btn.pack(side="left")
            page_btn = tk.Button(
                nav,
                text=f"{self.target_page + 1}/{page_count}",
                state="disabled",
                relief="flat",
                padx=12,
                pady=6,
            )
            page_btn.pack(side="left", expand=True)
            next_btn = tk.Button(
                nav,
                text="下一页 >",
                command=self.next_page,
                state=("normal" if self.target_page + 1 < page_count else "disabled"),
                relief="flat",
                padx=8,
                pady=6,
            )
            next_btn.pack(side="right")

    def draw_targets(self) -> None:
        canvas = self.target_canvas
        if canvas is None:
            return
        canvas.delete("all")
        self.target_hitboxes = []

        width = max(int(canvas.winfo_width()), self.WIDTH - 48)
        card_x = 2
        card_w = width - 16
        card_h = 58
        gap = 8
        y = 2

        if not self.messages:
            canvas.create_text(
                8,
                10,
                text="当前线程没有可回退的用户对话。",
                fill=self.PALETTE["dim"],
                anchor="nw",
                font=("Helvetica", 12),
                width=card_w - 16,
            )
            canvas.configure(scrollregion=(0, 0, width, self.MIN_HEIGHT))
            return

        for message in self.messages:
            index = int(message["index"])
            text = truncate_button_text(index, target_display_text(message), 74)
            fill = self.PALETTE["panel_active"] if self.hover_target == index else self.PALETTE["panel"]
            canvas.create_rectangle(
                card_x,
                y,
                card_x + card_w,
                y + card_h,
                fill=fill,
                outline=self.PALETTE["panel_border"],
                width=1,
            )
            canvas.create_text(
                card_x + 10,
                y + 10,
                text=text,
                fill=self.PALETTE["fg"],
                anchor="nw",
                font=("Helvetica", 12, "bold"),
                width=card_w - 20,
            )
            self.target_hitboxes.append((y, y + card_h, index))
            y += card_h + gap

        canvas.configure(scrollregion=(0, 0, width, max(y, int(canvas.winfo_height()))))
        if os.environ.get("CODEX_REWIND_GUI_DEBUG_LAYOUT"):
            print(
                f"draw_targets messages={len(self.messages)} width={width} "
                f"height={canvas.winfo_height()} items={len(canvas.find_all())}",
                file=sys.stderr,
                flush=True,
            )

    def on_target_canvas_click(self, event: Any) -> str:
        canvas = self.target_canvas
        if canvas is None:
            return "break"
        y = int(canvas.canvasy(event.y))
        for top, bottom, index in self.target_hitboxes:
            if top <= y <= bottom:
                self.choose_target(index)
                break
        return "break"

    def on_target_canvas_motion(self, event: Any) -> None:
        canvas = self.target_canvas
        if canvas is None:
            return
        y = int(canvas.canvasy(event.y))
        hovered = None
        for top, bottom, index in self.target_hitboxes:
            if top <= y <= bottom:
                hovered = index
                break
        if hovered != self.hover_target:
            self.hover_target = hovered
            self.draw_targets()

    def on_target_canvas_leave(self, _event: Any) -> None:
        if self.hover_target is not None:
            self.hover_target = None
            self.draw_targets()

    def on_mousewheel(self, event: Any) -> str:
        if self.selected_target is None and len(self.messages) > self.PAGE_SIZE:
            raw_delta = getattr(event, "delta", 0)
            if getattr(event, "num", None) == 4 or raw_delta > 0:
                self.prev_page()
            elif getattr(event, "num", None) == 5 or raw_delta < 0:
                self.next_page()
            return "break"
        canvas = self.target_canvas
        if canvas is None:
            return "break"
        if getattr(event, "num", None) == 4:
            delta = -3
        elif getattr(event, "num", None) == 5:
            delta = 3
        else:
            delta = -1 * int(getattr(event, "delta", 0) / 120)
        if delta:
            canvas.yview_scroll(delta, "units")
        return "break"

    def choose_target(self, index: int) -> None:
        self.selected_target = index
        self.root.after_idle(self.show_modes)

    def queue_show_targets(self) -> None:
        self.root.after_idle(self.show_targets)

    def prev_page(self) -> None:
        if self.target_page > 0:
            self.target_page -= 1
            self.queue_show_targets()

    def next_page(self) -> None:
        page_count = max(1, (len(self.messages) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self.target_page + 1 < page_count:
            self.target_page += 1
            self.queue_show_targets()

    def show_modes(self) -> None:
        tk = self.tk
        view = self.replace_view()
        self.header(view, "选择回退内容", back=True)

        target = select_message(self.messages, str(self.selected_target))
        detail = tk.Button(
            view,
            text=truncate_button_text(int(target["index"]), target_display_text(target), 72),
            state="disabled",
            anchor="w",
            justify="left",
            wraplength=self.WIDTH - 72,
            padx=10,
            pady=8,
            relief="flat",
        )
        detail.pack(fill="x", pady=(0, 12))

        buttons = tk.Frame(view, bg=self.PALETTE["bg"])
        buttons.pack(fill="x")
        options = [
            ("1. 仅对话", "session"),
            ("2. 仅代码", "code"),
            ("3. 对话和代码", "both"),
        ]
        for label, mode in options:
            button = self.clickable(
                buttons,
                text=label,
                command=lambda value=mode: self.apply_mode(value),
                anchor="center",
                padx=10,
                pady=9,
                font=("Helvetica", 13),
            )
            button.pack(fill="x", pady=5)

        hint = tk.Label(
            view,
            text="按 1/2/3 选择；Esc 或返回键回到对话选择界面。",
            bg=self.PALETTE["bg"],
            fg=self.PALETTE["muted"],
            font=("Helvetica", 11),
        )
        hint.pack(fill="x", pady=(10, 0))

    def apply_mode(self, mode: str) -> None:
        target = int(self.selected_target or 0)
        self.result = {"status": "selected", "target": target, "mode": mode}
        self.root.destroy()

    def dismiss(self) -> None:
        self.result = {"status": "dismissed"}
        self.root.destroy()

    def on_escape(self, _event: Any) -> str:
        if self.selected_target is None:
            self.dismiss()
        else:
            self.queue_show_targets()
        return "break"

    def on_key(self, event: Any) -> str | None:
        char = getattr(event, "char", "")
        if not char or not char.isdigit():
            return None
        if self.selected_target is None:
            if len(self.messages) < 10:
                index = int(char)
                if 1 <= index <= len(self.messages):
                    self.choose_target(index)
                return "break"
            self.digit_buffer += char
            if self.digit_after:
                self.root.after_cancel(self.digit_after)
            self.digit_after = self.root.after(350, self.flush_target_digits)
            return "break"
        if char in {"1", "2", "3"}:
            self.apply_mode({"1": "session", "2": "code", "3": "both"}[char])
            return "break"
        return None

    def flush_target_digits(self) -> None:
        raw = self.digit_buffer
        self.digit_buffer = ""
        self.digit_after = None
        if not raw:
            return
        index = int(raw)
        if 1 <= index <= len(self.messages):
            self.choose_target(index)


def run_rewind_gui_from_payload(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    try:
        cwd = cwd_from_payload(payload, args.cwd)
        identity = payload_identity(payload)
        transcript_path = first_string(payload.get("transcript_path"), payload.get("transcriptPath"))
        thread = load_thread_from_state(
            home=codex_home(getattr(args, "codex_home", None)),
            cwd=cwd,
            thread_id=identity.get("thread_id") or "latest",
            rollout_path=transcript_path,
        )
        messages = parse_session_user_messages(Path(thread["rollout_path"]).expanduser())
        annotate_checkpoint_ids(cwd, messages)

        if os.environ.get("CODEX_REWIND_GUI_AUTODISMISS"):
            return {"status": "dismissed"}

        no_apply = bool(getattr(args, "no_apply", False) or os.environ.get("CODEX_REWIND_GUI_NO_APPLY"))
        auto_choice = os.environ.get("CODEX_REWIND_GUI_CHOICE", "").strip()
        if auto_choice:
            target_text, _, mode_text = auto_choice.partition(":")
            target_index = int(target_text or "1")
            mode = mode_text or "code"
            if no_apply:
                return build_no_apply_selection(
                    thread=thread,
                    messages=messages,
                    cwd=cwd,
                    target_index=target_index,
                    mode=mode,
                )
            return perform_rewind_plan(
                thread=thread,
                messages=messages,
                cwd=cwd,
                target_index=target_index,
                mode=mode,
                app_sock=app_server_sock(args, codex_home(getattr(args, "codex_home", None))),
            )

        app_sock = app_server_sock(args, codex_home(getattr(args, "codex_home", None)))
        choice = run_appkit_gui(messages)
        if choice is None:
            gui = RewindGui(
                thread=thread,
                messages=messages,
                cwd=cwd,
                app_sock=app_sock,
            )
            choice = gui.run()
        if choice.get("status") != "selected":
            return {"status": "dismissed"}
        if no_apply:
            return build_no_apply_selection(
                thread=thread,
                messages=messages,
                cwd=cwd,
                target_index=int(choice["target"]),
                mode=str(choice["mode"]),
            )

        try:
            return perform_rewind_plan(
                thread=thread,
                messages=messages,
                cwd=cwd,
                target_index=int(choice["target"]),
                mode=str(choice["mode"]),
                app_sock=app_sock,
            )
        except Exception as exc:
            alert_error(f"Rewind failed:\n{exc}")
            return {"status": "failed", "error": str(exc)}
    except Exception as exc:
        detail = f"{exc}\n{traceback.format_exc(limit=4)}"
        alert_error(f"Codex Rewind failed:\n{exc}")
        return {"status": "failed", "error": detail}


def list_checkpoints(args: argparse.Namespace) -> int:
    cwd = canonical_cwd(args.cwd)
    manifest = load_manifest(cwd)
    checkpoints = manifest.get("checkpoints", [])
    if not checkpoints:
        print(f"No rewind checkpoints for {cwd}")
        return 0
    print(f"Rewind checkpoints for {cwd}:")
    tracked_count = len(load_file_history_state(cwd))
    for index, checkpoint in enumerate(checkpoints, start=1):
        prompt = checkpoint.get("prompt_preview") or ""
        backups = checkpoint.get("tracked_file_count")
        if not isinstance(backups, int):
            backups = len(checkpoint_history_backups(checkpoint))
        tracked = tracked_count if checkpoint_uses_file_history(checkpoint) else 0
        print(
            f"{index:>3}. {checkpoint['id']}  {checkpoint.get('created_at')}  "
            f"backups={backups} tracked={tracked}  {prompt}"
        )
    return 0


def git_blob(root: Path, rel: str) -> bytes | None:
    try:
        result = run(["git", "show", f"HEAD:{rel}"], cwd=root, text=False)
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def restore_file_history_backup(cwd: Path, meta: dict[str, Any], *, dry_run: bool) -> str | None:
    path = file_history_meta_path(meta)
    if not meta.get("existed"):
        if not path.exists():
            return None
        if not dry_run:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            if is_inside(path.parent, cwd):
                remove_empty_parents(path.parent, cwd)
        return f"delete {path}"

    if meta.get("kind") == "directory":
        return None if path.exists() and path.is_dir() else f"warn-directory-history-unsupported {path}"

    if file_history_meta_skipped(meta):
        return f"warn-skipped-file-history {meta.get('skipped_reason')} {path}"

    backup = meta.get("backup")
    if not isinstance(backup, str) or not backup:
        return f"warn-missing-backup {path}"
    source = file_history_backup_path(cwd, backup)
    if not source.exists():
        return f"warn-missing-backup {path}"
    if file_matches_history_backup(cwd, path, meta):
        return None
    if not dry_run:
        mode = meta.get("mode")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
        if isinstance(mode, int):
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    return f"restore {path}"


def restore_file_history_snapshot(cwd: Path, checkpoint: dict[str, Any], *, dry_run: bool) -> list[str]:
    manifest = load_manifest(cwd)
    manifest["tracked_files"] = load_file_history_state(cwd)
    target_backups = checkpoint.get("tracked_file_backups")
    if not isinstance(target_backups, dict):
        target_backups = checkpoint.get("file_backups")
    if not isinstance(target_backups, dict):
        target_backups = {}

    tracked = manifest.get("tracked_files")
    tracked_keys = set(tracked.keys()) if isinstance(tracked, dict) else set()
    keys = tracked_keys | set(target_backups.keys())
    actions: list[str] = []
    for key in sorted(keys, key=lambda item: (item.count("/"), item), reverse=True):
        meta = target_backups.get(key)
        if not isinstance(meta, dict):
            meta = first_tracked_backup(manifest, key)
        if not isinstance(meta, dict):
            actions.append(f"warn-no-file-history {key}")
            continue
        action = restore_file_history_backup(cwd, meta, dry_run=dry_run)
        if action:
            actions.append(action)
    return actions


def restore_backup(cwd: Path, checkpoint: dict[str, Any], meta: dict[str, Any], *, dry_run: bool) -> str:
    path = Path(meta["path"])
    if not meta.get("existed"):
        if path.exists():
            if not dry_run:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            return f"delete {path}"
        return f"skip absent {path}"

    backup = meta.get("backup")
    if not backup:
        return f"skip missing-backup {path}"
    source = checkpoint_dir(cwd, checkpoint["id"]) / backup
    if not source.exists():
        return f"skip missing-backup {path}"
    if not dry_run:
        mode = meta.get("mode")
        try:
            same_content = path.is_file() and path.read_bytes() == source.read_bytes()
            same_mode = not isinstance(mode, int) or path.stat().st_mode == mode
            if same_content and same_mode:
                return f"skip unchanged {path}"
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
        if isinstance(mode, int):
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    return f"restore {path}"


def checkpoint_uses_file_history(checkpoint: dict[str, Any]) -> bool:
    if checkpoint.get("history_version") == 2:
        return True
    return isinstance(checkpoint.get("tracked_file_backups"), dict)


def legacy_file_actions(cwd: Path, checkpoint: dict[str, Any], *, dry_run: bool) -> list[str]:
    actions: list[str] = []
    for meta in (checkpoint.get("file_backups") or {}).values():
        if isinstance(meta, dict):
            actions.append(restore_backup(cwd, checkpoint, meta, dry_run=dry_run))
    actions.extend(restore_workspace_baseline(cwd, checkpoint, dry_run=dry_run))
    return actions


def checkpoint_bytes(cwd: Path, checkpoint: dict[str, Any], rel: str | None) -> bytes:
    if not rel:
        return b""
    path = checkpoint_dir(cwd, checkpoint["id"]) / rel
    if not path.exists():
        return b""
    return path.read_bytes()


def changed_tracked_since_head(root: Path) -> set[str]:
    return dirty_tracked_files(root)


def remove_empty_parents(path: Path, stop: Path) -> None:
    try:
        path = path.resolve()
        stop = stop.resolve()
    except OSError:
        return
    while path != stop and is_inside(path, stop):
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def delete_new_untracked(root: Path, baseline_untracked: set[str], *, dry_run: bool) -> list[str]:
    actions: list[str] = []
    current_untracked = untracked_files(root)
    for rel in sorted(current_untracked - baseline_untracked, key=lambda item: (item.count("/"), item), reverse=True):
        path = root / rel
        if not is_inside(path, root):
            continue
        actions.append(f"delete-new {path}")
        if not dry_run:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                remove_empty_parents(path.parent, root)
            except FileNotFoundError:
                pass
    return actions


def remove_workspace_path(path: Path, root: Path) -> None:
    if not is_inside(path, root):
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        remove_empty_parents(path.parent, root)
    except FileNotFoundError:
        pass


def remove_empty_new_workspace_parents(path: Path, root: Path, baseline_dirs: set[str]) -> None:
    try:
        path = Path(os.path.abspath(path))
        root = Path(os.path.abspath(root))
    except OSError:
        return
    while path != root and is_workspace_path_inside(path, root):
        try:
            rel = workspace_rel(path, root)
        except ValueError:
            return
        if rel in baseline_dirs:
            return
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def remove_new_workspace_path(path: Path, root: Path, baseline_dirs: set[str]) -> None:
    if not is_workspace_path_inside(path, root):
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        remove_empty_new_workspace_parents(path.parent, root, baseline_dirs)
    except FileNotFoundError:
        pass


def workspace_content_snapshot_warnings(workspace: dict[str, Any], root: Path) -> list[str]:
    info = workspace.get("baseline_file_backups")
    if not isinstance(info, dict) or info.get("complete", True):
        return []
    skipped = info.get("skipped_files")
    suffix = f" skipped={skipped}" if isinstance(skipped, int) else ""
    return [f"warn-workspace-content-snapshot-incomplete {root}{suffix}"]


def restore_workspace_baseline(cwd: Path, checkpoint: dict[str, Any], *, dry_run: bool) -> list[str]:
    workspace = checkpoint.get("workspace")
    if not isinstance(workspace, dict):
        return []
    if not workspace.get("baseline_complete", True):
        root = Path(str(workspace.get("root") or cwd)).expanduser()
        return [
            f"warn-workspace-baseline-incomplete {workspace.get('root') or cwd}",
            *workspace_content_snapshot_warnings(workspace, root),
        ]

    root = Path(str(workspace.get("root") or cwd)).expanduser().resolve()
    if not root.exists():
        return [f"skip missing workspace root {root}"]

    excluded_roots = [
        Path(str(item)).expanduser().resolve()
        for item in (workspace.get("excluded_roots") or [])
        if isinstance(item, str) and item
    ]
    current = workspace_inventory(root, excluded_roots=excluded_roots)
    if not current.get("complete", True):
        return [f"warn-workspace-current-incomplete {root}"]

    baseline_files = set(workspace.get("baseline_files") or [])
    baseline_dirs = set(workspace.get("baseline_dirs") or [])
    current_files = set(current.get("files") or [])
    current_dirs = set(current.get("dirs") or [])

    actions: list[str] = workspace_content_snapshot_warnings(workspace, root)
    for rel in sorted(current_files - baseline_files, key=lambda item: (item.count("/"), item), reverse=True):
        path = root / rel
        actions.append(f"delete-new-workspace {path}")
        if not dry_run:
            remove_new_workspace_path(path, root, baseline_dirs)

    for rel in sorted(current_dirs - baseline_dirs, key=lambda item: (item.count("/"), item), reverse=True):
        path = root / rel
        if not path.exists():
            continue
        actions.append(f"delete-new-workspace-dir {path}")
        if not dry_run:
            remove_new_workspace_path(path, root, baseline_dirs)

    return actions


def delete_new_ignored(root: Path, baseline_ignored: set[str], *, dry_run: bool) -> list[str]:
    actions: list[str] = []
    current_ignored = ignored_files(root)
    for rel in sorted(current_ignored - baseline_ignored, key=lambda item: (item.count("/"), item), reverse=True):
        path = root / rel
        if not is_inside(path, root):
            continue
        actions.append(f"delete-new-ignored {path}")
        if not dry_run:
            remove_workspace_path(path, root)
    return actions


def delete_new_tracked(
    root: Path,
    baseline_tracked: set[str],
    *,
    baseline_untracked: set[str],
    baseline_ignored: set[str],
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    baseline_existing = baseline_tracked | baseline_untracked | baseline_ignored
    current_tracked = tracked_files(root)
    for rel in sorted(current_tracked - baseline_existing, key=lambda item: (item.count("/"), item), reverse=True):
        path = root / rel
        if not is_inside(path, root):
            continue
        actions.append(f"delete-new-tracked {path}")
        if not dry_run:
            run(["git", "rm", "--cached", "--ignore-unmatch", "--", rel], cwd=root, check=False)
            remove_workspace_path(path, root)
    return actions


def apply_git_patch(root: Path, patch: bytes, args: list[str]) -> None:
    if not patch:
        return
    run_with_input(["git", "apply", "--binary", *args, "-"], cwd=root, input_bytes=patch)


def restore_git_snapshot_v2(cwd: Path, checkpoint: dict[str, Any], git_info: dict[str, Any], *, dry_run: bool) -> list[str]:
    root = Path(git_info["root"])
    baseline_tracked = set(git_info.get("baseline_tracked") or [])
    baseline_untracked = set(git_info.get("baseline_untracked") or [])
    baseline_ignored = set(git_info.get("baseline_ignored") or [])
    staged_patch = checkpoint_bytes(cwd, checkpoint, git_info.get("baseline_staged_patch"))
    unstaged_patch = checkpoint_bytes(cwd, checkpoint, git_info.get("baseline_unstaged_patch"))
    new_tracked_actions = (
        delete_new_tracked(
            root,
            baseline_tracked,
            baseline_untracked=baseline_untracked,
            baseline_ignored=baseline_ignored,
            dry_run=True,
        )
        if "baseline_tracked" in git_info
        else []
    )
    new_ignored_actions = (
        delete_new_ignored(root, baseline_ignored, dry_run=True) if "baseline_ignored" in git_info else []
    )
    actions = [
        f"git-reset-head {root}",
        *new_tracked_actions,
        *delete_new_untracked(root, baseline_untracked, dry_run=True),
        *new_ignored_actions,
    ]
    if staged_patch:
        actions.append(f"restore-index {root}")
    if unstaged_patch:
        actions.append(f"restore-worktree-diff {root}")
    skipped = git_info.get("baseline_untracked_skipped") or []
    for rel in skipped:
        actions.append(f"warn-untracked-not-backed-up {root / rel}")

    if dry_run:
        return actions

    if "baseline_tracked" in git_info:
        delete_new_tracked(
            root,
            baseline_tracked,
            baseline_untracked=baseline_untracked,
            baseline_ignored=baseline_ignored,
            dry_run=False,
        )
    delete_new_untracked(root, baseline_untracked, dry_run=False)
    if "baseline_ignored" in git_info:
        delete_new_ignored(root, baseline_ignored, dry_run=False)
    run(["git", "reset", "--hard", "HEAD"], cwd=root)
    delete_new_untracked(root, baseline_untracked, dry_run=False)
    if "baseline_ignored" in git_info:
        delete_new_ignored(root, baseline_ignored, dry_run=False)
    apply_git_patch(root, staged_patch, ["--index"])
    apply_git_patch(root, unstaged_patch, [])
    delete_new_untracked(root, baseline_untracked, dry_run=False)
    if "baseline_ignored" in git_info:
        delete_new_ignored(root, baseline_ignored, dry_run=False)
    return actions


def restore_git_baseline(cwd: Path, checkpoint: dict[str, Any], *, dry_run: bool) -> list[str]:
    actions: list[str] = []
    git_info = checkpoint.get("git")
    if not isinstance(git_info, dict) or not git_info.get("root"):
        return actions
    root = Path(git_info["root"])
    if not root.exists():
        actions.append(f"skip missing git root {root}")
        return actions
    if git_info.get("snapshot_version") == 2 and git_info.get("has_head"):
        return restore_git_snapshot_v2(cwd, checkpoint, git_info, dry_run=dry_run)

    baseline_untracked = set(git_info.get("baseline_untracked") or [])
    actions.extend(delete_new_untracked(root, baseline_untracked, dry_run=dry_run))
    if "baseline_ignored" in git_info:
        actions.extend(delete_new_ignored(root, set(git_info.get("baseline_ignored") or []), dry_run=dry_run))

    backed_paths = {
        safe_rel(Path(meta["path"]), root)
        for meta in (checkpoint.get("file_backups") or {}).values()
        if isinstance(meta, dict)
        and isinstance(meta.get("path"), str)
        and is_inside(Path(meta["path"]), root)
    }

    for rel in sorted(changed_tracked_since_head(root) - backed_paths):
        path = root / rel
        blob = git_blob(root, rel)
        if blob is None:
            actions.append(f"delete-tracked-new {path}")
            if not dry_run:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            continue
        actions.append(f"restore-head {path}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)

    return actions


def apply_checkpoint(args: argparse.Namespace) -> int:
    cwd = canonical_cwd(args.cwd)
    checkpoint = resolve_checkpoint(cwd, args.checkpoint)
    dry_run = bool(args.dry_run)
    if not dry_run and not args.yes:
        raise SystemExit("Refusing to restore without --yes. Run --dry-run first.")

    actions = file_actions(cwd, checkpoint, dry_run=dry_run)

    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(f"{mode} checkpoint {checkpoint['id']}")
    if actions:
        for action in actions:
            print(f"- {action}")
    else:
        print("- no file changes detected")
    return 0


def diff_checkpoint(args: argparse.Namespace) -> int:
    args.dry_run = True
    args.yes = False
    return apply_checkpoint(args)


def checkpoint_command(args: argparse.Namespace) -> int:
    checkpoint = make_checkpoint(
        canonical_cwd(args.cwd),
        prompt=args.prompt or "",
        thread_id=args.thread_id,
        turn_id=args.turn_id,
        user_message_id=args.user_message_id,
        source="manual",
    )
    print(checkpoint["id"])
    return 0


def gui_command(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "cwd": str(canonical_cwd(args.cwd)),
        "prompt": "/rewind",
    }
    if args.thread_id and args.thread_id != "latest":
        payload["session_id"] = args.thread_id
    if args.rollout_path:
        payload["transcript_path"] = args.rollout_path
    result = run_rewind_gui_from_payload(payload, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"applied", "dismissed", "selected"} else 1


def parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def path_file_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0
    return 0


def iter_rewind_project_dirs() -> list[Path]:
    home = storage_home()
    if not home.exists():
        return []
    return sorted(
        path
        for path in home.iterdir()
        if path.is_dir() and PROJECT_DIR_RE.match(path.name)
    )


def project_manifest_path(project: Path) -> Path:
    return project / "manifest.json"


def load_project_manifest(project: Path) -> dict[str, Any] | None:
    path = project_manifest_path(project)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def project_activity_time(project: Path, manifest: dict[str, Any] | None) -> float:
    if manifest is not None:
        for key in ("updated_at", "created_at"):
            timestamp = parse_iso_timestamp(manifest.get(key))
            if timestamp is not None:
                return timestamp
    try:
        return project.stat().st_mtime
    except OSError:
        return 0.0


def checkpoint_backup_metas(checkpoint: dict[str, Any]) -> dict[str, Any]:
    backups = checkpoint.get("tracked_file_backups")
    if isinstance(backups, dict):
        return backups
    backups = checkpoint.get("file_backups")
    if isinstance(backups, dict):
        return backups
    return {}


def meta_backup_rel(meta: dict[str, Any]) -> str | None:
    backup = meta.get("backup")
    return backup if isinstance(backup, str) and backup else None


def meta_size_from_backup(cwd: Path, meta: dict[str, Any]) -> int | None:
    size = meta.get("size")
    if isinstance(size, int):
        return size
    backup = meta_backup_rel(meta)
    if not backup:
        return None
    path = file_history_backup_path(cwd, backup)
    try:
        return path.stat().st_size
    except OSError:
        return None


def policy_filtered_meta(cwd: Path, key: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(key).expanduser()
    excluded = file_history_excluded_reason(path, cwd)
    if excluded is not None:
        return None
    size = meta_size_from_backup(cwd, meta)
    if isinstance(size, int) and size > MAX_FILE_HISTORY_FILE_BYTES:
        return normalize_skipped_file_history_meta(cwd, meta, "large-file")
    return dict(meta)


def sanitize_checkpoint_backups(cwd: Path, checkpoint: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    backups = checkpoint_backup_metas(checkpoint)
    changed = False
    sanitized: dict[str, Any] = {}
    for key, value in backups.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            changed = True
            continue
        meta = policy_filtered_meta(cwd, key, value)
        if meta is None:
            changed = True
            continue
        if meta != value:
            changed = True
        sanitized[key] = meta
    if changed:
        checkpoint["tracked_file_backups"] = sanitized
        checkpoint["file_backups"] = sanitized
    return sanitized, changed


def retained_checkpoint_summaries(
    checkpoints: list[dict[str, Any]],
    *,
    max_age_days: int | None,
) -> list[dict[str, Any]]:
    retained = list(checkpoints[-MAX_FILE_HISTORY_SNAPSHOTS:])
    if max_age_days is None:
        return retained
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    return [
        checkpoint
        for checkpoint in retained
        if (parse_iso_timestamp(checkpoint.get("created_at")) or 0.0) >= cutoff
    ]


def numeric_version_keys(versions: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for key in versions:
        try:
            out.append(int(key))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def rewrite_tracked_files_for_gc(
    cwd: Path,
    tracked: dict[str, Any],
    retained_versions: dict[str, set[int]],
) -> tuple[dict[str, Any], set[str], int]:
    new_tracked: dict[str, Any] = {}
    live_backup_refs: set[str] = set()
    removed_versions = 0

    for key, entry in tracked.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            removed_versions += 1
            continue
        if file_history_excluded_reason(Path(key).expanduser(), cwd) is not None:
            versions = entry.get("versions")
            removed_versions += len(versions) if isinstance(versions, dict) else 1
            continue

        versions = entry.get("versions")
        if not isinstance(versions, dict):
            removed_versions += 1
            continue

        numeric = numeric_version_keys(versions)
        keep_versions: set[int] = set(retained_versions.get(key, set()))
        if numeric:
            first = numeric[0]
            latest = numeric[-1]
            first_meta = versions.get(str(first))
            # Keep a missing first-version marker so files created after the
            # rewind target can still be deleted without retaining large bytes.
            if isinstance(first_meta, dict) and first_meta.get("backup") is None:
                keep_versions.add(first)
            keep_versions.add(latest)

        new_versions: dict[str, Any] = {}
        for version_key, value in versions.items():
            try:
                version = int(version_key)
            except (TypeError, ValueError):
                removed_versions += 1
                continue
            if version not in keep_versions:
                removed_versions += 1
                continue
            if not isinstance(value, dict):
                removed_versions += 1
                continue
            meta = policy_filtered_meta(cwd, key, value)
            if meta is None:
                removed_versions += 1
                continue
            backup = meta_backup_rel(meta)
            if backup:
                live_backup_refs.add(backup)
            new_versions[str(version)] = meta

        if not new_versions:
            continue

        latest_version = max(int(key) for key in new_versions)
        new_entry = dict(entry)
        new_entry["path"] = key
        new_entry["rel"] = safe_rel(Path(key).expanduser(), cwd)
        new_entry["versions"] = new_versions
        new_entry["latest_version"] = latest_version
        new_entry["latest_backup"] = dict(new_versions[str(latest_version)])
        new_entry["updated_at"] = now_iso()
        new_tracked[key] = new_entry

    return new_tracked, live_backup_refs, removed_versions


def gc_project_dir(project: Path, *, dry_run: bool, max_age_days: int | None = None) -> dict[str, Any]:
    manifest = load_project_manifest(project)
    if manifest is None:
        return {
            "project": str(project),
            "cwd": None,
            "removed_checkpoints": 0,
            "removed_backups": 0,
            "removed_bytes": 0,
            "updated": False,
            "skipped": "missing-manifest",
        }

    raw_cwd = manifest.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return {
            "project": str(project),
            "cwd": None,
            "removed_checkpoints": 0,
            "removed_backups": 0,
            "removed_bytes": 0,
            "updated": False,
            "skipped": "missing-cwd",
        }
    cwd = Path(raw_cwd).expanduser()

    with manifest_lock(cwd):
        manifest = load_manifest_unlocked(cwd)
        checkpoints = [
            checkpoint
            for checkpoint in manifest.get("checkpoints", [])
            if isinstance(checkpoint, dict)
        ]
        retained_summaries = retained_checkpoint_summaries(checkpoints, max_age_days=max_age_days)
        retained_ids = {
            checkpoint.get("id")
            for checkpoint in retained_summaries
            if isinstance(checkpoint.get("id"), str)
        }

        retained_full: list[dict[str, Any]] = []
        retained_versions: dict[str, set[int]] = {}
        live_backup_refs: set[str] = set()
        changed = len(retained_summaries) != len(checkpoints)

        for summary in retained_summaries:
            checkpoint = load_checkpoint(cwd, summary) or summary
            backups, backup_changed = sanitize_checkpoint_backups(cwd, checkpoint)
            changed = changed or backup_changed
            for key, meta in backups.items():
                if not isinstance(meta, dict):
                    continue
                version = meta.get("version")
                if isinstance(version, int):
                    retained_versions.setdefault(key, set()).add(version)
                backup = meta_backup_rel(meta)
                if backup:
                    live_backup_refs.add(backup)
            retained_full.append(checkpoint)

        tracked = load_file_history_state(cwd)
        new_tracked, state_backup_refs, removed_versions = rewrite_tracked_files_for_gc(
            cwd,
            tracked,
            retained_versions,
        )
        live_backup_refs.update(state_backup_refs)
        if new_tracked != tracked:
            changed = True

        checkpoints_root = project / "checkpoints"
        removed_checkpoints = 0
        removed_bytes = 0
        if checkpoints_root.exists():
            for checkpoint_dir_path in checkpoints_root.iterdir():
                if not checkpoint_dir_path.is_dir():
                    continue
                if checkpoint_dir_path.name in retained_ids:
                    continue
                removed_checkpoints += 1
                removed_bytes += path_file_size(checkpoint_dir_path)
                if not dry_run:
                    shutil.rmtree(checkpoint_dir_path, ignore_errors=True)

        removed_backups = 0
        history_root = project / "file-history"
        if history_root.exists():
            for backup_path in history_root.iterdir():
                if not backup_path.is_file():
                    continue
                rel = f"file-history/{backup_path.name}"
                if rel in live_backup_refs:
                    continue
                removed_backups += 1
                try:
                    removed_bytes += backup_path.stat().st_size
                except OSError:
                    pass
                if not dry_run:
                    try:
                        backup_path.unlink()
                    except FileNotFoundError:
                        pass

        if changed and not dry_run:
            manifest["tracked_files"] = new_tracked
            manifest["checkpoints"] = retained_full
            manifest["updated_at"] = now_iso()
            write_manifest_file(cwd, manifest)

        return {
            "project": str(project),
            "cwd": str(cwd),
            "removed_checkpoints": removed_checkpoints,
            "removed_backups": removed_backups,
            "removed_versions": removed_versions,
            "removed_bytes": removed_bytes,
            "updated": changed,
            "retained_checkpoints": len(retained_full),
            "retained_tracked_files": len(new_tracked),
        }


def cleanup_old_rewind_storage(*, dry_run: bool, max_age_days: int = REWIND_CLEANUP_DAYS) -> dict[str, Any]:
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    reports: list[dict[str, Any]] = []
    removed_projects = 0
    removed_bytes = 0

    for project in iter_rewind_project_dirs():
        manifest = load_project_manifest(project)
        activity = project_activity_time(project, manifest)
        if activity and activity < cutoff:
            removed_projects += 1
            removed_bytes += path_file_size(project)
            reports.append(
                {
                    "project": str(project),
                    "cwd": manifest.get("cwd") if isinstance(manifest, dict) else None,
                    "removed_project": True,
                    "removed_bytes": path_file_size(project),
                }
            )
            if not dry_run:
                shutil.rmtree(project, ignore_errors=True)
            continue
        reports.append(gc_project_dir(project, dry_run=dry_run, max_age_days=max_age_days))

    return {
        "max_age_days": max_age_days,
        "removed_projects": removed_projects,
        "removed_project_bytes": removed_bytes,
        "projects": reports,
    }


def cleanup_marker_path() -> Path:
    return storage_home() / ".cleanup-marker"


def cleanup_lock_path() -> Path:
    return storage_home() / ".cleanup.lock"


def maybe_cleanup_old_rewind_storage() -> None:
    if env_truthy("CODEX_REWIND_DISABLE_AUTO_GC"):
        return
    marker = cleanup_marker_path()
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < REWIND_CLEANUP_INTERVAL_SECONDS:
            return
    except OSError:
        return

    lock = cleanup_lock_path()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if marker.exists() and time.time() - marker.stat().st_mtime < REWIND_CLEANUP_INTERVAL_SECONDS:
                return
            cleanup_old_rewind_storage(dry_run=False, max_age_days=REWIND_CLEANUP_DAYS)
            marker.write_text(now_iso() + "\n", encoding="utf-8")
    except Exception:
        # Rewind hooks must never fail user prompts because housekeeping failed.
        return


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
        amount /= 1024
    return f"{value}B"


def gc_command(args: argparse.Namespace) -> int:
    dry_run = not bool(args.yes)
    max_age_days = args.max_age_days
    if args.all and max_age_days is None:
        max_age_days = REWIND_CLEANUP_DAYS
    if args.all:
        report = cleanup_old_rewind_storage(dry_run=dry_run, max_age_days=max_age_days)
        reports = report["projects"]
    else:
        cwd = canonical_cwd(args.cwd)
        reports = [gc_project_dir(project_dir(cwd), dry_run=dry_run, max_age_days=max_age_days)]

    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(f"{mode} rewind gc")
    total_bytes = 0
    total_backups = 0
    total_checkpoints = 0
    for report in reports:
        if report.get("removed_project"):
            size = int(report.get("removed_bytes") or 0)
            total_bytes += size
            print(f"- remove-project {report.get('cwd') or report['project']} bytes={format_bytes(size)}")
            continue
        size = int(report.get("removed_bytes") or 0)
        backups = int(report.get("removed_backups") or 0)
        checkpoints = int(report.get("removed_checkpoints") or 0)
        total_bytes += size
        total_backups += backups
        total_checkpoints += checkpoints
        print(
            f"- {report.get('cwd') or report['project']} "
            f"remove_backups={backups} remove_checkpoints={checkpoints} "
            f"remove_bytes={format_bytes(size)} retained_checkpoints={report.get('retained_checkpoints', '-')}"
        )
    print(
        f"total: remove_backups={total_backups} remove_checkpoints={total_checkpoints} "
        f"remove_bytes={format_bytes(total_bytes)}"
    )
    if dry_run:
        print("pass --yes to apply")
    return 0


def add_thread_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd")
    parser.add_argument("--codex-home")
    parser.add_argument("--thread-id", default="latest")
    parser.add_argument("--rollout-path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex local workspace/session rewind checkpoints")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hook-user-prompt")
    p.add_argument("--cwd")
    p.add_argument("--codex-home")
    p.add_argument("--app-server-sock")
    p.set_defaults(func=hook_user_prompt)

    p = sub.add_parser("hook-pre-tool")
    p.add_argument("--cwd")
    p.set_defaults(func=hook_pre_tool)

    p = sub.add_parser("checkpoint")
    p.add_argument("--cwd")
    p.add_argument("--prompt")
    p.add_argument("--thread-id")
    p.add_argument("--turn-id")
    p.add_argument("--user-message-id")
    p.set_defaults(func=checkpoint_command)

    p = sub.add_parser("list")
    p.add_argument("--cwd")
    p.set_defaults(func=list_checkpoints)

    p = sub.add_parser("diff")
    p.add_argument("checkpoint", nargs="?", default="latest")
    p.add_argument("--cwd")
    p.set_defaults(func=diff_checkpoint)

    p = sub.add_parser("apply")
    p.add_argument("checkpoint", nargs="?", default="latest")
    p.add_argument("--cwd")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=apply_checkpoint)

    for command_name in ("gc", "prune"):
        p = sub.add_parser(command_name)
        p.add_argument("--cwd")
        p.add_argument("--all", action="store_true")
        p.add_argument("--max-age-days", type=int)
        p.add_argument("--yes", action="store_true")
        p.set_defaults(func=gc_command)

    p = sub.add_parser("targets")
    add_thread_args(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=list_targets)

    p = sub.add_parser("preview")
    add_thread_args(p)
    p.add_argument("target", nargs="?", default="latest")
    p.add_argument("--mode", choices=("session", "code", "both"), default="both")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=preview_rewind)

    p = sub.add_parser("rewind")
    add_thread_args(p)
    p.add_argument("target", nargs="?", default="latest")
    p.add_argument("--mode", choices=("session", "code", "both"), default="both")
    p.add_argument("--app-server-sock")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=apply_rewind)

    p = sub.add_parser("gui")
    add_thread_args(p)
    p.add_argument("--app-server-sock")
    p.add_argument("--no-apply", action="store_true")
    p.set_defaults(func=gui_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
