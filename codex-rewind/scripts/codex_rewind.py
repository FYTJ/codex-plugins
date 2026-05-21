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
SESSION_ROLLBACK_METHOD = "thread/rollback"


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


def write_manifest_file(cwd: Path, manifest: dict[str, Any]) -> None:
    path = manifest_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


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
            return manifest
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


def dirty_tracked_files(root: Path) -> set[str]:
    dirty = set(git_z(root, ["diff", "--name-only", "-z", "--"]))
    dirty.update(git_z(root, ["diff", "--cached", "--name-only", "-z", "--"]))
    dirty.update(git_z(root, ["ls-files", "-d", "-z"]))
    return {path for path in dirty if path}


def untracked_files(root: Path) -> set[str]:
    return set(git_z(root, ["ls-files", "--others", "--exclude-standard", "-z"]))


def write_checkpoint_bytes(cwd: Path, checkpoint: dict[str, Any], rel: str, data: bytes) -> str | None:
    if not data:
        return None
    path = checkpoint_dir(cwd, checkpoint["id"]) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def capture_git_baseline(cwd: Path, checkpoint: dict[str, Any]) -> None:
    root = git_root(cwd)
    if root is None:
        checkpoint["git"] = None
        return

    has_head = git_has_head(root)
    checkpoint["git"] = {
        "root": str(root),
        "has_head": has_head,
        "baseline_untracked": sorted(untracked_files(root)),
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
                if thread_id and not checkpoint.get("thread_id"):
                    checkpoint["thread_id"] = thread_id
                    update_checkpoint(cwd, checkpoint)
                return checkpoint

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = short_hash(f"{thread_id or ''}:{turn_id or ''}:{prompt}:{time.time()}", 8)
    checkpoint = {
        "id": f"{stamp}-{suffix}",
        "created_at": now_iso(),
        "cwd": str(cwd),
        "thread_id": thread_id,
        "turn_id": turn_id,
        "user_message_id": user_message_id,
        "source": source,
        "prompt_preview": prompt.strip().replace("\n", " ")[:240],
        "file_backups": {},
    }
    capture_git_baseline(cwd, checkpoint)
    manifest.setdefault("checkpoints", []).append(checkpoint)
    manifest["checkpoints"] = manifest["checkpoints"][-100:]
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
                if thread_id and not checkpoint.get("thread_id"):
                    checkpoint["thread_id"] = thread_id
                    update_checkpoint(cwd, checkpoint)
                return checkpoint
    if checkpoints:
        return checkpoints[-1]
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
    manifest["checkpoints"] = checkpoints[-100:]
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


def extract_tool_paths(payload: dict[str, Any], cwd: Path) -> list[Path]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    tool_cwd = cwd
    workdir = tool_input.get("workdir")
    if isinstance(workdir, str) and workdir:
        tool_cwd = canonical_cwd(workdir)

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
    command = tool_input.get("command")
    if isinstance(command, str):
        paths.extend(extract_shell_paths(command, tool_cwd))
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
    return 0


def hook_pre_tool(args: argparse.Namespace) -> int:
    payload = read_stdin_json()
    cwd = cwd_from_payload(payload, args.cwd)
    checkpoint = current_checkpoint(cwd, payload)
    for path in extract_tool_paths(payload, cwd):
        add_file_backup(cwd, checkpoint, path, reason="pre-tool")
    update_checkpoint(cwd, checkpoint)
    return 0


def resolve_checkpoint(cwd: Path, selector: str | None) -> dict[str, Any]:
    manifest = load_manifest(cwd)
    checkpoints = manifest.get("checkpoints", [])
    if not checkpoints:
        raise SystemExit("No rewind checkpoints for this cwd.")
    if not selector or selector == "latest":
        return checkpoints[-1]
    if selector.isdigit():
        index = int(selector)
        if index < 1 or index > len(checkpoints):
            raise SystemExit(f"Checkpoint index out of range: {selector}")
        return checkpoints[index - 1]
    matches = [item for item in checkpoints if item.get("id", "").startswith(selector)]
    if len(matches) == 1:
        return matches[0]
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


def parse_event_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def find_checkpoint_for_message(cwd: Path, message: dict[str, Any]) -> dict[str, Any] | None:
    checkpoints = manifest_checkpoints(cwd)
    turn_id = message.get("turn_id")
    if turn_id:
        for checkpoint in reversed(checkpoints):
            if checkpoint.get("turn_id") == turn_id:
                return checkpoint

    preview = message.get("preview") or ""
    if preview:
        for checkpoint in reversed(checkpoints):
            checkpoint_preview = checkpoint.get("prompt_preview") or ""
            if checkpoint_preview and (
                checkpoint_preview.startswith(preview[:80]) or preview.startswith(checkpoint_preview[:80])
            ):
                return checkpoint

    timestamp = parse_event_time(message.get("timestamp"))
    if timestamp is None:
        return None

    candidates: list[tuple[float, dict[str, Any]]] = []
    for checkpoint in checkpoints:
        created_at = parse_event_time(checkpoint.get("created_at"))
        if created_at is None:
            continue
        # UserPromptSubmit checkpoints are normally created seconds after the JSONL user event.
        delta = created_at - timestamp
        if -60 <= delta <= 300:
            candidates.append((abs(created_at - timestamp), checkpoint))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


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
    actions = restore_git_baseline(cwd, checkpoint, dry_run=dry_run)
    for meta in (checkpoint.get("file_backups") or {}).values():
        if isinstance(meta, dict):
            actions.append(restore_backup(cwd, checkpoint, meta, dry_run=dry_run))
    return actions


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
    actions: list[str] = []
    checkpoint_id = str(checkpoint["id"]) if checkpoint is not None and checkpoint.get("id") else None

    if checkpoint is not None:
        actions.extend(file_actions(cwd, checkpoint, dry_run=dry_run))

    if checkpoint is None or not checkpoint_has_git_root(checkpoint):
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
    for message in messages:
        checkpoint = find_checkpoint_for_message(cwd, message)
        message["checkpoint_id"] = checkpoint.get("id") if checkpoint else None
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
    preview = one_line_preview(text, width)
    if len(" ".join(text.strip().split())) > width:
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
            checkpoint = message.get("checkpoint_id") or "-"
            suffix = f"code:{checkpoint}" if checkpoint != "-" else "code:-"
            text = truncate_button_text(index, message.get("text") or message.get("preview") or "", 50)
            button = tk.Button(
                view,
                text=f"{text}  {suffix}",
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
            checkpoint = message.get("checkpoint_id") or "-"
            suffix = f"code:{checkpoint}" if checkpoint != "-" else "code:-"
            text = truncate_button_text(index, message.get("text") or message.get("preview") or "", 58)
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
            canvas.create_text(
                card_x + 10,
                y + 37,
                text=suffix,
                fill=self.PALETTE["dim"],
                anchor="nw",
                font=("Helvetica", 10),
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
            text=truncate_button_text(int(target["index"]), target.get("text") or target.get("preview") or "", 72),
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
        for message in messages:
            checkpoint = find_checkpoint_for_message(cwd, message)
            message["checkpoint_id"] = checkpoint.get("id") if checkpoint else None

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
    for index, checkpoint in enumerate(checkpoints, start=1):
        prompt = checkpoint.get("prompt_preview") or ""
        git_info = checkpoint.get("git") or {}
        dirty = len(git_info.get("baseline_dirty") or [])
        untracked = len(git_info.get("baseline_untracked") or [])
        backups = len(checkpoint.get("file_backups") or {})
        print(
            f"{index:>3}. {checkpoint['id']}  {checkpoint.get('created_at')}  "
            f"backups={backups} dirty={dirty} untracked={untracked}  {prompt}"
        )
    return 0


def git_blob(root: Path, rel: str) -> bytes | None:
    try:
        result = run(["git", "show", f"HEAD:{rel}"], cwd=root, text=False)
        return result.stdout
    except subprocess.CalledProcessError:
        return None


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
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
        mode = meta.get("mode")
        if isinstance(mode, int):
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    return f"restore {path}"


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


def apply_git_patch(root: Path, patch: bytes, args: list[str]) -> None:
    if not patch:
        return
    run_with_input(["git", "apply", "--binary", *args, "-"], cwd=root, input_bytes=patch)


def restore_git_snapshot_v2(cwd: Path, checkpoint: dict[str, Any], git_info: dict[str, Any], *, dry_run: bool) -> list[str]:
    root = Path(git_info["root"])
    baseline_untracked = set(git_info.get("baseline_untracked") or [])
    staged_patch = checkpoint_bytes(cwd, checkpoint, git_info.get("baseline_staged_patch"))
    unstaged_patch = checkpoint_bytes(cwd, checkpoint, git_info.get("baseline_unstaged_patch"))
    actions = [
        f"git-reset-head {root}",
        *delete_new_untracked(root, baseline_untracked, dry_run=True),
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

    delete_new_untracked(root, baseline_untracked, dry_run=False)
    run(["git", "reset", "--hard", "HEAD"], cwd=root)
    delete_new_untracked(root, baseline_untracked, dry_run=False)
    apply_git_patch(root, staged_patch, ["--index"])
    apply_git_patch(root, unstaged_patch, [])
    delete_new_untracked(root, baseline_untracked, dry_run=False)
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
