#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]


HOME = Path.home()
CODEX_HOME = HOME / ".codex"
CONFIG_PATH = CODEX_HOME / "notify.toml"
STATE_PATH = CODEX_HOME / "tmp/codex_notify_native_state.json"
OUT_LOG = CODEX_HOME / "log/codex-notify.out.log"
ERR_LOG = CODEX_HOME / "log/codex-notify.err.log"


def append_log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except Exception:
        pass


def out(message: str) -> None:
    append_log(OUT_LOG, message)


def err(message: str) -> None:
    append_log(ERR_LOG, message)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        err(f"[native-notify] failed to read config: {exc}")
        return {}
    return raw if isinstance(raw, dict) else {}


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = STATE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp_path.replace(STATE_PATH)
    except Exception as exc:
        err(f"[native-notify] failed to save state: {exc}")


def should_send(payload: dict[str, Any], dedupe_window_sec: int) -> bool:
    turn_id = str(payload.get("turn-id") or "")
    thread_id = str(payload.get("thread-id") or "")
    fingerprint = turn_id or f"{thread_id}:{payload.get('type', '')}"
    if not fingerprint:
        return True

    state = load_state()
    now = time.time()
    last_sent = state.get("last_sent")
    if not isinstance(last_sent, dict):
        last_sent = {}

    previous = last_sent.get(fingerprint)
    if isinstance(previous, (int, float)) and now - float(previous) < dedupe_window_sec:
        return False

    threshold = now - max(dedupe_window_sec, 0)
    last_sent = {
        str(key): float(value)
        for key, value in last_sent.items()
        if isinstance(value, (int, float)) and float(value) >= threshold
    }
    last_sent[fingerprint] = now
    state["last_sent"] = last_sent
    save_state(state)
    return True


def extract_payload(argv: list[str]) -> tuple[str | None, dict[str, Any] | None]:
    event_name: str | None = None
    candidates = list(argv)
    if candidates and not candidates[0].lstrip().startswith("{"):
        event_name = candidates.pop(0)

    if not candidates:
        stdin_text = sys.stdin.read().strip()
        if stdin_text:
            candidates.append(stdin_text)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return event_name, payload
    return event_name, None


def project_name(payload: dict[str, Any]) -> str:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    name = Path(cwd).name
    return name or ""


def apple_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def send_mac(title: str, body: str, *, dry_run: bool) -> int:
    script = (
        f'display notification "{apple_escape(body)}" '
        f'with title "{apple_escape(title)}" '
        'subtitle "Codex Notify"'
    )
    if dry_run:
        out(f"[native-notify][dry-run][mac] {title} | {body}")
        return 0
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    out(f"[native-notify][mac] rc={result.returncode}")
    if result.stderr.strip():
        err(f"[native-notify][mac][stderr] {result.stderr.strip()}")
    return int(result.returncode)


def handle(argv: list[str]) -> int:
    _event_name, payload = extract_payload(argv)

    if payload is None:
        out("[native-notify] no JSON payload; skipped")
        return 0
    if payload.get("type") != "agent-turn-complete":
        out(f"[native-notify] skipped event type={payload.get('type')}")
        return 0

    config = load_config()
    if config.get("enabled") is False:
        out("[native-notify] disabled by config")
        return 0

    dedupe_window = int(config.get("dedupe_window_sec", 10))
    if not should_send(payload, dedupe_window):
        out("[native-notify] duplicate turn skipped")
        return 0

    project = project_name(payload)
    title = f"[{project}] Codex task finished" if project else "Codex task finished"
    body = "Codex task finished"
    dry_run = os.environ.get("CODEX_NOTIFY_DRY_RUN", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    out(f"[{ts}] event=task_completed title={title} source=native-notify")

    if config.get("mac_notify_enabled", True):
        send_mac(title, body, dry_run=dry_run)
    return 0


def main() -> int:
    try:
        return handle(sys.argv[1:])
    except Exception as exc:
        err(f"[native-notify] unhandled error: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
