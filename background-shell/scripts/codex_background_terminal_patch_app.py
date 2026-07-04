#!/usr/bin/env python3
"""Fail-closed controller for Codex App background-shell patching.

This script targets the installed official Codex.app in /Applications.
It still keeps the existing clean-source, ASAR integrity, launch, screenshot,
and verification gates so failed hooks stop before being reported as success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import plistlib
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
RESOURCE_ROOT = SCRIPT_DIR.parent
BACKGROUND_TERMINAL_ROOT = RESOURCE_ROOT / "background-terminal"
SYSTEM_APP = Path("/Applications/Codex.app")
PATCH_TARGET_APP = SYSTEM_APP
# Kept as a compatibility alias for existing report keys and helper names.
DEFAULT_USER_APP = PATCH_TARGET_APP
DOWNLOADS = HOME / "Downloads"
TRASH = HOME / ".Trash"
REPORT_ROOT = BACKGROUND_TERMINAL_ROOT / "reports"
LAUNCH_USER_DATA_DIR = HOME / ".codex" / "tmp" / "codex-usercopy-profile"
UPSTREAM_CODEX_REPO = RESOURCE_ROOT / "external-sources" / "openai-codex"
CODEX_RS = UPSTREAM_CODEX_REPO / "codex-rs"
RUST_TOOLCHAIN = os.environ.get("CODEX_BACKGROUND_SHELL_RUST_TOOLCHAIN")
RUST_TOOLCHAIN_DIR = Path(RUST_TOOLCHAIN).expanduser() if RUST_TOOLCHAIN else None
RUSTC = Path(os.environ.get("RUSTC", str((RUST_TOOLCHAIN_DIR / "rustc") if RUST_TOOLCHAIN_DIR else "rustc"))).expanduser()
CARGO = Path(os.environ.get("CARGO", str((RUST_TOOLCHAIN_DIR / "cargo") if RUST_TOOLCHAIN_DIR else "cargo"))).expanduser()
BUILT_CODEX_BINARY = CODEX_RS / "target" / "release" / "codex"
NATIVE_PATCH_FILE = Path(__file__).with_name("openai-codex-background-shell.patch")
CHANGE_ID = "change-20260704-034011"
APP_BUNDLE_ID = "com.openai.codex"
OPENAI_TEAM_ID = "2DC432GLL2"
AUTO_BACKGROUND_THRESHOLD_SECONDS = 450
PATCH_FRAMEWORK_MARKER = "codex-background-terminal-native-framework"
CTRL_B_ACTION = "background-active-terminal"
CTRL_B_NATIVE_METHOD = "thread/backgroundTerminals/backgroundActive"
LIST_BG_ACTION = "list-background-terminals"
LIST_BG_NATIVE_METHOD = "thread/backgroundTerminals/list"
TERMINATE_BG_ACTION = "terminate-background-terminal"
TERMINATE_BG_NATIVE_METHOD = "thread/backgroundTerminals/terminate"
APP_CONTROL_MARKER = "codex-background-terminal-app-control"
APP_CONTROL_DIR = BACKGROUND_TERMINAL_ROOT / "app-control"
APP_CONTROL_MAIN_REL = ".vite/build/main-z6HVz-xR.js"
APP_CONTROL_JS_PARTS = ",".join(json.dumps(part) for part in APP_CONTROL_DIR.relative_to(HOME).parts)
DMG_MOUNT_DIR = Path(tempfile.gettempdir()) / "codex-dmg-mount"
MACOS_VOLUMES_DIR = Path(
    os.environ.get("CODEX_BACKGROUND_SHELL_VOLUMES_DIR", str(Path(Path.cwd().anchor) / "Volumes"))
).expanduser()

OLD_PATCH_MARKERS = (
    b"__codexBackgroundTerminal",
    b"codex_background_terminal_patch_app.py",
    b"background-terminal-probe",
    b"legacy_sidecar",
    b"background-terminal-adopt",
    b"background-terminal-scheduler-records",
)

NEW_PATCH_MARKERS = (
    PATCH_FRAMEWORK_MARKER.encode("utf-8"),
    CTRL_B_ACTION.encode("utf-8"),
    CTRL_B_NATIVE_METHOD.encode("utf-8"),
    LIST_BG_ACTION.encode("utf-8"),
    LIST_BG_NATIVE_METHOD.encode("utf-8"),
    TERMINATE_BG_ACTION.encode("utf-8"),
    TERMINATE_BG_NATIVE_METHOD.encode("utf-8"),
    APP_CONTROL_MARKER.encode("utf-8"),
)

NATIVE_PATCH_MARKERS = (
    b"run_in_background",
    b"thread/backgroundTerminals/backgroundActive",
    b"foreground_adopt",
    b"auto_threshold",
    b"user_shortcut",
    b"background_wakeup",
    b"model_observed",
    b"dispatch_failed",
    b"guided-message-stuck",
    b"busy_deferred_idle_turn",
    b"idle_direct_turn",
)

SCENARIO_TESTS = {
    "explicit-background": (
        ("codex-core", "explicit_background_exec_returns_session_without_waiting"),
        ("codex-core", "shell_command_run_in_background_registers_native_terminal"),
    ),
    "foreground-to-background": ("codex-core", "manual_background_request_wakes_initial_exec"),
    "auto-threshold-450s": ("codex-core", "auto_background_threshold_is_450_seconds"),
    "timeout-to-background": ("codex-core", "yield_timeout_background_exec_records_timeout_source"),
    "ctrl-b": ("codex-core", "user_shortcut_background_request_records_user_shortcut_source"),
    "sleep-denylist": ("codex-core", "sleep_denylist_matches_claude_leading_sleep_boundaries"),
    "summary-output": ("codex-core", "background_terminal_summary_exposes_command_title"),
    "stop-restart": ("codex-core", "background_terminal_native_terminate_controls_single_process"),
    "busy-wakeup-30s": ("codex-core", "background_terminal_exit_wakeup_is_model_observed_then_delivered"),
    "idle-wakeup-2min": ("codex-core", "background_terminal_idle_wakeup_uses_idle_direct_turn"),
}

FULL_VERIFY_SCENARIOS = (
    ("T1", "explicit-background"),
    ("T2", "foreground-to-background"),
    ("T3", "auto-threshold-450s"),
    ("T4", "timeout-to-background"),
    ("T5", "ctrl-b"),
    ("T6", "summary-output"),
    ("T7", "busy-wakeup-30s"),
    ("T8", "idle-wakeup-2min"),
    ("T9", "stop-restart"),
    ("T10", "sleep-denylist"),
)

REQUIRED_UI_SCREENSHOT_STEPS = (
    "T0-launch",
    "T6-summary-output",
    "T6-output-view",
    "T7-busy-wakeup",
    "T8-idle-wakeup",
    "T9-stop",
    "T9-restart",
)


class ControllerError(RuntimeError):
    def __init__(self, reason: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def run(
    argv: list[str],
    *,
    timeout: float = 30.0,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> CommandResult:
    env = None
    if env_extra:
        env = os.environ.copy()
        env.update(env_extra)
    completed = subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def current_millis() -> int:
    return int(time.time() * 1000)


def sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_plist(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        value = plistlib.load(handle)
    return value if isinstance(value, dict) else {}


def is_system_app(path: Path) -> bool:
    try:
        return path.resolve() == SYSTEM_APP.resolve()
    except FileNotFoundError:
        return path == SYSTEM_APP


def path_is_user_writable_app(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        resolved = path
    if is_system_app(resolved):
        return True
    try:
        resolved.relative_to(HOME)
    except ValueError:
        return False
    return resolved.name.endswith(".app")


def file_contains_any(path: Path, markers: tuple[bytes, ...]) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    found: list[str] = []
    with path.open("rb") as handle:
        data_tail = b""
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            data = data_tail + chunk
            for marker in markers:
                if marker.decode("utf-8", "replace") not in found and marker in data:
                    found.append(marker.decode("utf-8", "replace"))
            data_tail = data[-max(len(m) for m in markers) :]
    return found


def read_pickle_payload(buffer: bytes) -> memoryview:
    if len(buffer) < 4:
        raise RuntimeError("Invalid asar pickle: too short")
    payload_size = struct.unpack_from("<I", buffer, 0)[0]
    header_size = len(buffer) - payload_size
    if header_size < 4 or header_size % 4 != 0:
        raise RuntimeError("Invalid asar pickle header size")
    if header_size + payload_size > len(buffer):
        raise RuntimeError("Invalid asar pickle payload size")
    return memoryview(buffer)[header_size : header_size + payload_size]


def read_pickle_uint32(buffer: bytes) -> int:
    payload = read_pickle_payload(buffer)
    if len(payload) < 4:
        raise RuntimeError("Invalid asar pickle uint32 payload")
    return struct.unpack_from("<I", payload, 0)[0]


def read_pickle_string_bytes(buffer: bytes) -> bytes:
    payload = read_pickle_payload(buffer)
    if len(payload) < 4:
        raise RuntimeError("Invalid asar pickle string payload")
    size = struct.unpack_from("<i", payload, 0)[0]
    if size < 0 or 4 + size > len(payload):
        raise RuntimeError("Invalid asar pickle string length")
    return bytes(payload[4 : 4 + size])


def asar_header_hash(asar_path: Path) -> str | None:
    if not asar_path.exists() or not asar_path.is_file():
        return None
    with asar_path.open("rb") as handle:
        size_buffer = handle.read(8)
        if len(size_buffer) != 8:
            raise RuntimeError(f"Cannot read asar header size from {asar_path}")
        header_size = read_pickle_uint32(size_buffer)
        header_buffer = handle.read(header_size)
        if len(header_buffer) != header_size:
            raise RuntimeError(f"Cannot read asar header from {asar_path}")
    return hashlib.sha256(read_pickle_string_bytes(header_buffer)).hexdigest()


def make_pickle_payload(payload: bytes) -> bytes:
    padding = (4 - (len(payload) % 4)) % 4
    return struct.pack("<I", len(payload)) + payload + (b"\0" * padding)


def make_pickle_uint32(value: int) -> bytes:
    return make_pickle_payload(struct.pack("<I", value))


def make_pickle_string(value: bytes) -> bytes:
    return make_pickle_payload(struct.pack("<i", len(value)) + value)


def read_asar_header(asar_path: Path) -> tuple[dict[str, Any], int, int]:
    with asar_path.open("rb") as handle:
        size_buffer = handle.read(8)
        if len(size_buffer) != 8:
            raise ControllerError("asar-read-failed", f"Cannot read asar size pickle: {asar_path}")
        header_size = read_pickle_uint32(size_buffer)
        header_buffer = handle.read(header_size)
        if len(header_buffer) != header_size:
            raise ControllerError("asar-read-failed", f"Cannot read asar header: {asar_path}")
    header = json.loads(read_pickle_string_bytes(header_buffer).decode("utf-8"))
    return header, header_size, 8 + header_size


def asar_entry(header: dict[str, Any], rel_path: str) -> dict[str, Any]:
    node: dict[str, Any] = header
    for part in rel_path.split("/"):
        files = node.get("files")
        if not isinstance(files, dict) or part not in files:
            raise ControllerError("asar-entry-missing", f"ASAR entry not found: {rel_path}")
        child = files[part]
        if not isinstance(child, dict):
            raise ControllerError("asar-entry-invalid", f"ASAR entry is invalid: {rel_path}")
        node = child
    return node


def iter_asar_entries(header: dict[str, Any], prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    files = header.get("files")
    if not isinstance(files, dict):
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for name, entry in files.items():
        if not isinstance(entry, dict):
            continue
        path = f"{prefix}/{name}" if prefix else name
        if "files" in entry:
            entries.extend(iter_asar_entries(entry, path))
        else:
            entries.append((path, entry))
    return entries


def read_asar_file(asar_path: Path, header: dict[str, Any], data_offset: int, rel_path: str) -> bytes:
    entry = asar_entry(header, rel_path)
    if entry.get("unpacked"):
        raise ControllerError("asar-entry-unpacked", f"ASAR entry is unpacked: {rel_path}")
    offset = int(entry["offset"])
    size = int(entry["size"])
    with asar_path.open("rb") as handle:
        handle.seek(data_offset + offset)
        data = handle.read(size)
    if len(data) != size:
        raise ControllerError("asar-read-failed", f"ASAR entry was truncated: {rel_path}")
    return data


def update_asar_file_integrity(entry: dict[str, Any], content: bytes) -> None:
    previous = entry.get("integrity") if isinstance(entry.get("integrity"), dict) else {}
    block_size = int(previous.get("blockSize") or 4_194_304)
    blocks = [
        hashlib.sha256(content[index : index + block_size]).hexdigest()
        for index in range(0, len(content), block_size)
    ]
    if not blocks:
        blocks = [hashlib.sha256(b"").hexdigest()]
    entry["size"] = len(content)
    entry["integrity"] = {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(content).hexdigest(),
        "blockSize": block_size,
        "blocks": blocks,
    }


def write_asar_archive(
    asar_path: Path,
    header: dict[str, Any],
    original_data_offset: int,
    replacements: dict[str, bytes],
) -> dict[str, Any]:
    packed_data: list[bytes] = []
    modified_files: list[dict[str, Any]] = []
    offset = 0
    for rel_path, entry in iter_asar_entries(header):
        if entry.get("unpacked"):
            continue
        content = replacements.get(rel_path)
        if content is None:
            content = read_asar_file(asar_path, header, original_data_offset, rel_path)
        else:
            modified_files.append(
                {
                    "path": rel_path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
        entry["offset"] = str(offset)
        update_asar_file_integrity(entry, content)
        packed_data.append(content)
        offset += len(content)

    header_json = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header_pickle = make_pickle_string(header_json)
    size_pickle = make_pickle_uint32(len(header_pickle))
    temp_path = asar_path.with_suffix(".asar.tmp")
    with temp_path.open("wb") as handle:
        handle.write(size_pickle)
        handle.write(header_pickle)
        for content in packed_data:
            handle.write(content)
    temp_path.replace(asar_path)
    return {
        "modifiedFiles": modified_files,
        "asarHeaderSha256": hashlib.sha256(header_json).hexdigest(),
        "asarFileSha256": sha256(asar_path),
    }


def electron_asar_integrity(info: dict[str, Any]) -> str | None:
    value = info.get("ElectronAsarIntegrity")
    if not isinstance(value, dict):
        return None
    asar = value.get("Resources/app.asar")
    if not isinstance(asar, dict):
        return None
    hash_value = asar.get("hash")
    return hash_value if isinstance(hash_value, str) else None


def parse_codesign_details(stderr: str) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for line in stderr.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        details[key.strip()] = value.strip()
    return details


def app_paths(app: Path) -> dict[str, Path]:
    return {
        "info": app / "Contents" / "Info.plist",
        "executable": app / "Contents" / "MacOS" / "Codex",
        "resources": app / "Contents" / "Resources",
        "asar": app / "Contents" / "Resources" / "app.asar",
        "codex": app / "Contents" / "Resources" / "codex",
    }


def analyze_app(app: Path, *, role: str) -> dict[str, Any]:
    paths = app_paths(app)
    info = read_plist(paths["info"])
    asar_file_hash = sha256(paths["asar"])
    try:
        asar_integrity_hash = asar_header_hash(paths["asar"])
        asar_integrity_error = None
    except Exception as exc:
        asar_integrity_hash = None
        asar_integrity_error = str(exc)
    codex_hash = sha256(paths["codex"])
    declared_asar_hash = electron_asar_integrity(info)
    verify = run(["codesign", "--verify", "--deep", "--strict", str(app)], timeout=60)
    codesign_detail_result = run(["codesign", "-dv", "--verbose=4", str(app)], timeout=60)
    codesign_details = parse_codesign_details(codesign_detail_result.stderr)
    version_result: CommandResult | None = None
    if paths["codex"].exists():
        version_result = run([str(paths["codex"]), "--version"], timeout=10)
    old_markers = file_contains_any(paths["asar"], OLD_PATCH_MARKERS)
    new_markers = file_contains_any(paths["asar"], NEW_PATCH_MARKERS)
    native_markers = file_contains_any(paths["codex"], NATIVE_PATCH_MARKERS)
    bundle_id = info.get("CFBundleIdentifier")
    result: dict[str, Any] = {
        "role": role,
        "path": str(app),
        "exists": app.exists(),
        "isSystemApp": is_system_app(app),
        "isUserWritableTarget": path_is_user_writable_app(app),
        "bundleId": bundle_id,
        "shortVersion": info.get("CFBundleShortVersionString"),
        "bundleVersion": info.get("CFBundleVersion"),
        "codexVersion": version_result.stdout if version_result and version_result.returncode == 0 else None,
        "codexVersionCommand": version_result.as_dict() if version_result else None,
        "asarFileSha256": asar_file_hash,
        "asarHeaderSha256": asar_integrity_hash,
        "codexHash": codex_hash,
        "declaredAsarHash": declared_asar_hash,
        "asarIntegrityError": asar_integrity_error,
        "asarIntegrityOk": bool(
            asar_integrity_hash
            and declared_asar_hash
            and asar_integrity_hash == declared_asar_hash
        ),
        "oldPatchMarkers": old_markers,
        "newPatchMarkers": new_markers,
        "nativePatchMarkers": native_markers,
        "nativePatchMarkersOk": sorted(native_markers) == sorted(
            marker.decode("utf-8", "replace") for marker in NATIVE_PATCH_MARKERS
        ),
        "codesignVerify": verify.as_dict(),
        "codesignDetails": codesign_details,
        "signatureValid": verify.returncode == 0,
        "signatureAdhoc": codesign_details.get("Signature") == "adhoc",
        "teamIdentifier": codesign_details.get("TeamIdentifier"),
    }
    result["cleanSourceOk"] = (
        result["exists"] is True
        and result["bundleId"] == APP_BUNDLE_ID
        and result["signatureAdhoc"] is False
        and result["teamIdentifier"] == OPENAI_TEAM_ID
        and result["asarIntegrityOk"] is True
        and not old_markers
        and not new_markers
        and not native_markers
    )
    reasons: list[str] = []
    warnings: list[str] = []
    if not result["exists"]:
        reasons.append("app-missing")
    if result["bundleId"] != APP_BUNDLE_ID:
        reasons.append("bundle-id-mismatch")
    if not result["signatureValid"]:
        warnings.append("signature-invalid")
    if result["signatureAdhoc"]:
        reasons.append("signature-adhoc")
    if result["teamIdentifier"] != OPENAI_TEAM_ID:
        reasons.append("team-id-mismatch")
    if not result["asarIntegrityOk"]:
        reasons.append("asar-integrity-mismatch")
    if old_markers:
        reasons.append("old-patch-markers-present")
    if new_markers:
        reasons.append("new-patch-markers-present")
    if native_markers:
        reasons.append("native-patch-markers-present")
    result["cleanSourceRejectReasons"] = reasons
    result["cleanSourceWarnings"] = warnings
    return result


def mounted_codex_apps() -> list[Path]:
    candidates: list[Path] = []
    volumes = [MACOS_VOLUMES_DIR, DMG_MOUNT_DIR]
    for volume in volumes:
        if not volume.exists():
            continue
        for app in volume.glob("**/Codex.app"):
            candidates.append(app)
    return sorted(set(candidates), key=lambda p: str(p))


def dmg_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for root in (DOWNLOADS, TRASH):
        if not root.exists():
            continue
        for path in sorted(root.glob("*.dmg")):
            if "codex" not in path.name.lower():
                continue
            verify = run(["hdiutil", "verify", str(path)], timeout=90)
            candidates.append(
                {
                    "path": str(path),
                    "sha256": sha256(path),
                    "hdiutilVerify": verify.as_dict(),
                    "validImage": verify.returncode == 0,
                }
            )
    return candidates


def status_report() -> dict[str, Any]:
    source_candidates = [analyze_app(SYSTEM_APP, role="system-app")]
    for app in mounted_codex_apps():
        source_candidates.append(analyze_app(app, role="mounted-dmg-app"))
    patch_target = analyze_app(DEFAULT_USER_APP, role="patch-target-app") if DEFAULT_USER_APP.exists() else None
    clean = [candidate for candidate in source_candidates if candidate.get("cleanSourceOk")]
    selected_source = clean[0] if clean else None
    fatal_reason = None
    if selected_source is None:
        fatal_reason = "clean-source-contaminated" if source_candidates else "clean-source-not-found"
    return {
        "ok": selected_source is not None,
        "fatalReason": fatal_reason,
        "changeId": CHANGE_ID,
        "generatedAtMs": int(time.time() * 1000),
        "autoBackgroundThresholdSeconds": AUTO_BACKGROUND_THRESHOLD_SECONDS,
        "selectedCleanSource": selected_source,
        "sourceCandidates": source_candidates,
        "dmgCandidates": dmg_candidates(),
        "patchTargetApp": patch_target,
        "patchTargetAppPath": str(DEFAULT_USER_APP),
        # Compatibility fields for older report consumers.
        "defaultUserCopy": patch_target,
        "defaultUserCopyPath": str(DEFAULT_USER_APP),
    }


def require_clean_source(report: dict[str, Any]) -> dict[str, Any]:
    source = report.get("selectedCleanSource")
    if isinstance(source, dict):
        return source
    raise ControllerError(
        str(report.get("fatalReason") or "clean-source-not-found"),
        "No clean Codex source passed the fail-closed gate.",
        details=report,
    )


def prepare_user_copy(*, yes: bool) -> dict[str, Any]:
    report = status_report()
    source = require_clean_source(report)
    source_path = Path(str(source["path"]))
    if is_system_app(DEFAULT_USER_APP):
        target = analyze_app(DEFAULT_USER_APP, role="official-patch-target")
        return {
            "ok": target.get("exists") is True and target.get("asarIntegrityOk") is not False,
            "source": source,
            "stopUserApp": {"attempted": False, "remainingPids": []},
            "backupPath": None,
            "prepared": target,
            "skipped": True,
            "reason": "official-app-target-already-installed",
        }
    if not yes:
        raise ControllerError("confirmation-required", "Pass --yes to replace the configured Codex.app target.")
    DEFAULT_USER_APP.parent.mkdir(parents=True, exist_ok=True)
    stop_report = stop_user_app(DEFAULT_USER_APP, timeout=10) if DEFAULT_USER_APP.exists() else {"attempted": False, "remainingPids": []}
    if stop_report.get("remainingPids"):
        raise ControllerError("app-stop-failed", "Configured Codex.app target could not be stopped before replacement.", details=stop_report)
    backup_path = None
    if DEFAULT_USER_APP.exists():
        backup_path = DEFAULT_USER_APP.with_name(f"Codex.old-{int(time.time())}.app")
        DEFAULT_USER_APP.rename(backup_path)
    shutil.copytree(source_path, DEFAULT_USER_APP, symlinks=True)
    copied = analyze_app(DEFAULT_USER_APP, role="prepared-codex-app-target")
    return {
        "ok": copied.get("exists") is True and copied.get("isUserWritableTarget") is True,
        "source": source,
        "stopUserApp": stop_report,
        "backupPath": str(backup_path) if backup_path else None,
        "prepared": copied,
    }


def first_shell_segment(command: str) -> str:
    quote: str | None = None
    escaped = False
    index = 0
    segment: list[str] = []
    while index < len(command):
        char = command[index]
        if escaped:
            segment.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            segment.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            segment.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            segment.append(char)
            index += 1
            continue
        if char == "\n" or char == ";":
            break
        if command[index : index + 2] in ("&&", "||"):
            break
        segment.append(char)
        index += 1
    return "".join(segment).strip()


def blocked_sleep_seconds(command: str) -> int | None:
    segment = first_shell_segment(command)
    match = re.fullmatch(r"sleep\s+([0-9]+)", segment)
    if match is None:
        return None
    seconds = int(match.group(1))
    return seconds if seconds >= 2 else None


def is_leading_sleep(command: str) -> bool:
    return blocked_sleep_seconds(command) is not None


def self_test() -> dict[str, Any]:
    checks = []

    def check(name: str, condition: bool) -> None:
        checks.append({"name": name, "ok": bool(condition)})

    check("default patch target is the official system app", is_system_app(DEFAULT_USER_APP))
    check("auto threshold is 450 seconds", AUTO_BACKGROUND_THRESHOLD_SECONDS == 450)
    check("leading sleep detected", is_leading_sleep("sleep 10"))
    check("leading sleep followed by shell op detected", is_leading_sleep("sleep 10 && echo done"))
    check("sleep below two seconds not detected", not is_leading_sleep("sleep 1"))
    check("fractional sleep not detected", not is_leading_sleep("sleep 0.5"))
    check("bin sleep not detected", not is_leading_sleep("/bin/sleep 10"))
    check("env sleep not detected", not is_leading_sleep("env sleep 10"))
    check("non-leading sleep not detected", not is_leading_sleep("pwd && sleep 10"))
    check("shell wrapper is not leading sleep", not is_leading_sleep("sh -c 'sleep 10'"))
    check("busy wakeup scenario is registered", "busy-wakeup-30s" in SCENARIO_TESTS)
    check("idle wakeup scenario is registered", "idle-wakeup-2min" in SCENARIO_TESTS)
    check("background wakeup native marker is required", b"background_wakeup" in NATIVE_PATCH_MARKERS)
    check("guided stuck marker is required", b"guided-message-stuck" in NATIVE_PATCH_MARKERS)
    merged_text, merged_step = replace_text_variants_in_text(
        "alpha beta gamma",
        "sample.js",
        [("beta", "BETA")],
        step_name="sample-merge",
    )
    check("sequential ASAR text replacement works", merged_text == "alpha BETA gamma" and not merged_step["alreadyApplied"])
    reapplied_text, reapplied_step = replace_text_variants_in_text(
        merged_text,
        "sample.js",
        [("beta", "BETA")],
        step_name="sample-merge",
    )
    check("sequential ASAR text replacement is idempotent", reapplied_text == merged_text and reapplied_step["alreadyApplied"])
    screenshot_without_window = capture_screenshot(None, scenario="self-test-window-required")
    check("screenshot requires codex window id", screenshot_without_window.get("reason") == "codex-window-id-required")
    check("screenshot disallows fullscreen fallback", screenshot_without_window.get("fullscreenFallbackAllowed") is False)
    check("screenshot records elevated execution requirement", screenshot_without_window.get("requiresElevatedExecution") is True)
    sample = Path(os.environ.get("CODEX_BG_SELF_TEST_SAMPLE", sys.argv[0]))
    check("old marker detector is bounded", isinstance(file_contains_any(sample, OLD_PATCH_MARKERS), list))
    ok = all(item["ok"] for item in checks)
    return {"ok": ok, "checks": checks}


def write_report(name: str, payload: dict[str, Any]) -> Path:
    report_dir = REPORT_ROOT / CHANGE_ID
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{name}-{current_millis()}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def print_payload(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if payload.get("ok"):
        print("ok")
    else:
        print(f"failed: {payload.get('fatalReason') or payload.get('reason') or 'unknown'}")


def parse_ps_table(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 3)
        if len(parts) < 3:
            continue
        pid_raw, ppid_raw, command = parts[0], parts[1], parts[2]
        args = parts[3] if len(parts) > 3 else command
        try:
            pid = int(pid_raw)
            ppid = int(ppid_raw)
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": ppid, "command": command, "args": args})
    return rows


def ps_table() -> list[dict[str, Any]]:
    result = run(["ps", "-axww", "-o", "pid=,ppid=,comm=,command="], timeout=10)
    if result.returncode != 0:
        raise ControllerError("process-table-unavailable", "Unable to inspect local process table.", details=result.as_dict())
    return parse_ps_table(result.stdout)


def app_processes(app: Path) -> list[dict[str, Any]]:
    app_contents = str((app / "Contents").resolve())
    matches: list[dict[str, Any]] = []
    for row in ps_table():
        if app_contents in str(row.get("command", "")) or app_contents in str(row.get("args", "")):
            matches.append(row)
    return sorted(matches, key=lambda item: int(item["pid"]))


def descendant_pids(root_pids: set[int], rows: list[dict[str, Any]]) -> set[int]:
    by_parent: dict[int, list[int]] = {}
    for row in rows:
        by_parent.setdefault(int(row["ppid"]), []).append(int(row["pid"]))
    seen = set(root_pids)
    frontier = list(root_pids)
    while frontier:
        parent = frontier.pop(0)
        for child in by_parent.get(parent, []):
            if child in seen:
                continue
            seen.add(child)
            frontier.append(child)
    return seen


def process_tree_for_app(app: Path) -> dict[str, Any]:
    rows = ps_table()
    app_contents = str((app / "Contents").resolve())
    direct = [
        row
        for row in rows
        if app_contents in str(row.get("command", "")) or app_contents in str(row.get("args", ""))
    ]
    direct_pids = {int(row["pid"]) for row in direct}
    all_pids = descendant_pids(direct_pids, rows)
    tree = [row for row in rows if int(row["pid"]) in all_pids]
    root_candidates = [row for row in direct if int(row["ppid"]) not in all_pids]
    executable_candidates = [
        row
        for row in direct
        if "/Contents/MacOS/Codex" in str(row.get("command", ""))
        or "/Contents/MacOS/Codex" in str(row.get("args", ""))
    ]
    root = executable_candidates[0] if executable_candidates else (root_candidates[0] if root_candidates else (direct[0] if direct else None))
    executable_path = None
    if root:
        args = str(root.get("args", ""))
        command = str(root.get("command", ""))
        executable_path = args.split(" --", 1)[0].split(" -", 1)[0] if args.startswith("/") else command
    return {
        "appPath": str(app),
        "rootPid": int(root["pid"]) if root else None,
        "executablePath": executable_path,
        "pids": sorted(all_pids),
        "processes": sorted(tree, key=lambda item: int(item["pid"])),
        "targetPathMatched": bool(root and app_contents in f"{root.get('command', '')} {root.get('args', '')}"),
    }


def stop_user_app(app: Path, *, timeout: float = 10.0) -> dict[str, Any]:
    before = app_processes(app)
    pids = [int(item["pid"]) for item in before]
    term_errors: list[dict[str, Any]] = []
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            term_errors.append({"pid": pid, "signal": "TERM", "error": str(exc)})
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = [int(item["pid"]) for item in app_processes(app)]
        if not remaining:
            return {
                "attempted": bool(pids),
                "terminatedPids": pids,
                "forcedPids": [],
                "remainingPids": [],
                "errors": term_errors,
            }
        time.sleep(0.25)
    forced: list[int] = []
    for item in app_processes(app):
        pid = int(item["pid"])
        try:
            os.kill(pid, signal.SIGKILL)
            forced.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            term_errors.append({"pid": pid, "signal": "KILL", "error": str(exc)})
    time.sleep(0.5)
    remaining_after_force = [int(item["pid"]) for item in app_processes(app)]
    return {
        "attempted": bool(pids),
        "terminatedPids": pids,
        "forcedPids": forced,
        "remainingPids": remaining_after_force,
        "errors": term_errors,
    }


def wait_for_app_process(app: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_tree: dict[str, Any] = {"pids": [], "processes": []}
    while time.time() < deadline:
        last_tree = process_tree_for_app(app)
        if last_tree.get("pids"):
            return {"ok": True, "processTree": last_tree}
        time.sleep(0.5)
    return {"ok": False, "processTree": last_tree}


def launch_command_args(app: Path) -> list[str]:
    LAUNCH_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return [
        "open",
        "-n",
        str(app),
        "--args",
        f"--user-data-dir={LAUNCH_USER_DATA_DIR}",
        "--use-mock-keychain",
    ]


WINDOW_ENUM_SWIFT = r'''
import Foundation
import CoreGraphics

func jsonString(_ value: Any) -> String {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [])
    return String(data: data, encoding: .utf8)!
}

let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let windowList = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
var output: [[String: Any]] = []

for window in windowList {
    let ownerName = window[kCGWindowOwnerName as String] as? String ?? ""
    let ownerPid = window[kCGWindowOwnerPID as String] as? Int ?? -1
    let windowId = window[kCGWindowNumber as String] as? Int ?? -1
    let title = window[kCGWindowName as String] as? String ?? ""
    let layer = window[kCGWindowLayer as String] as? Int ?? -999
    let alpha = window[kCGWindowAlpha as String] as? Double ?? 0.0
    var bounds: [String: Double] = [:]
    if let rawBounds = window[kCGWindowBounds as String] as? [String: Any] {
        for (key, value) in rawBounds {
            if let number = value as? NSNumber {
                bounds[key] = number.doubleValue
            }
        }
    }
    output.append([
        "windowId": windowId,
        "ownerPid": ownerPid,
        "ownerName": ownerName,
        "title": title,
        "layer": layer,
        "alpha": alpha,
        "bounds": bounds,
    ])
}

print(jsonString(["ok": true, "windows": output]))
'''


def collect_window_info() -> dict[str, Any]:
    cache_dir = HOME / ".codex" / "tmp" / "clang-module-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = run(
        ["swift", "-e", WINDOW_ENUM_SWIFT],
        timeout=60,
        env_extra={"CLANG_MODULE_CACHE_PATH": str(cache_dir)},
    )
    if result.returncode != 0:
        return {"ok": False, "reason": "window-enumeration-failed", "command": result.as_dict(), "windows": []}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "reason": "window-enumeration-json-invalid",
            "message": str(exc),
            "command": result.as_dict(),
            "windows": [],
        }
    if not isinstance(parsed, dict):
        return {"ok": False, "reason": "window-enumeration-json-invalid", "command": result.as_dict(), "windows": []}
    parsed["command"] = result.as_dict()
    return parsed


def window_visible_area(window: dict[str, Any]) -> float:
    bounds = window.get("bounds") if isinstance(window.get("bounds"), dict) else {}
    width = float(bounds.get("Width") or bounds.get("width") or 0)
    height = float(bounds.get("Height") or bounds.get("height") or 0)
    return width * height


def select_codex_window(window_info: dict[str, Any], process_pids: set[int]) -> dict[str, Any] | None:
    windows = window_info.get("windows") if isinstance(window_info.get("windows"), list) else []
    candidates = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        owner_pid = int(window.get("ownerPid") or -1)
        if owner_pid not in process_pids:
            continue
        if int(window.get("layer") or 0) != 0:
            continue
        if float(window.get("alpha") or 0.0) <= 0.0:
            continue
        if window_visible_area(window) < 200 * 150:
            continue
        candidates.append(window)
    candidates.sort(key=window_visible_area, reverse=True)
    return candidates[0] if candidates else None


def visible_dialog_signals(window_info: dict[str, Any]) -> list[dict[str, Any]]:
    windows = window_info.get("windows") if isinstance(window_info.get("windows"), list) else []
    signals: list[dict[str, Any]] = []
    owner_patterns = ("securityagent", "coreservicesuiagent", "keychain access")
    title_patterns = ("keychain", "password", "\u94a5\u5319\u4e32", "\u5bc6\u7801")
    for window in windows:
        if not isinstance(window, dict) or window_visible_area(window) < 100 * 50:
            continue
        owner = str(window.get("ownerName") or "").lower()
        title = str(window.get("title") or "").lower()
        if any(pattern in owner for pattern in owner_patterns) or any(pattern in title for pattern in title_patterns):
            signals.append(window)
    return signals


def normalized_bounds(window: dict[str, Any] | None) -> dict[str, int] | None:
    if not window:
        return None
    bounds = window.get("bounds") if isinstance(window.get("bounds"), dict) else {}
    try:
        x = int(round(float(bounds.get("X") if "X" in bounds else bounds.get("x"))))
        y = int(round(float(bounds.get("Y") if "Y" in bounds else bounds.get("y"))))
        width = int(round(float(bounds.get("Width") if "Width" in bounds else bounds.get("width"))))
        height = int(round(float(bounds.get("Height") if "Height" in bounds else bounds.get("height"))))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def screenshot_file_ok(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 4096


def png_visual_stats(path: Path, *, max_samples: int = 200_000) -> dict[str, Any]:
    try:
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return {"ok": False, "reason": "not-a-png"}
        position = 8
        width = height = bit_depth = color_type = interlace = None
        idat_chunks: list[bytes] = []
        while position < len(data):
            length = struct.unpack(">I", data[position : position + 4])[0]
            chunk_type = data[position + 4 : position + 8]
            chunk = data[position + 8 : position + 8 + length]
            position += 12 + length
            if chunk_type == b"IHDR":
                width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", chunk)
            elif chunk_type == b"IDAT":
                idat_chunks.append(chunk)
            elif chunk_type == b"IEND":
                break
        if not width or not height or bit_depth != 8 or interlace != 0:
            return {"ok": False, "reason": "unsupported-png-format", "width": width, "height": height}
        channels_by_type = {0: 1, 2: 3, 6: 4}
        channels = channels_by_type.get(int(color_type))
        if channels is None:
            return {"ok": False, "reason": "unsupported-png-color-type", "colorType": color_type}
        raw = zlib.decompress(b"".join(idat_chunks))
        row_stride = int(width) * channels
        previous = bytearray(row_stride)
        offset = 0
        sample_step = max(1, (int(width) * int(height)) // max_samples)
        pixel_index = 0
        sample_count = 0
        luma_sum = 0.0
        luma_sum_sq = 0.0
        buckets: Counter[tuple[int, int, int]] = Counter()
        for _y in range(int(height)):
            filter_type = raw[offset]
            offset += 1
            current = bytearray(raw[offset : offset + row_stride])
            offset += row_stride
            bytes_per_pixel = channels
            for i in range(row_stride):
                left = current[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                up = previous[i]
                up_left = previous[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                if filter_type == 1:
                    current[i] = (current[i] + left) & 0xFF
                elif filter_type == 2:
                    current[i] = (current[i] + up) & 0xFF
                elif filter_type == 3:
                    current[i] = (current[i] + ((left + up) // 2)) & 0xFF
                elif filter_type == 4:
                    predictor = left + up - up_left
                    pa = abs(predictor - left)
                    pb = abs(predictor - up)
                    pc = abs(predictor - up_left)
                    predicted = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                    current[i] = (current[i] + predicted) & 0xFF
                elif filter_type != 0:
                    return {"ok": False, "reason": "unsupported-png-filter", "filterType": filter_type}
            for x in range(int(width)):
                if pixel_index % sample_step == 0:
                    base = x * channels
                    if color_type == 0:
                        red = green = blue = current[base]
                    else:
                        red, green, blue = current[base], current[base + 1], current[base + 2]
                    luma = (red + green + blue) / 3.0
                    luma_sum += luma
                    luma_sum_sq += luma * luma
                    buckets[(red // 16, green // 16, blue // 16)] += 1
                    sample_count += 1
                pixel_index += 1
            previous = current
        if sample_count == 0:
            return {"ok": False, "reason": "no-pixel-samples", "width": width, "height": height}
        mean = luma_sum / sample_count
        variance = max(0.0, (luma_sum_sq / sample_count) - (mean * mean))
        dominant_bucket_count = buckets.most_common(1)[0][1] if buckets else 0
        dominant_share = dominant_bucket_count / sample_count
        unique_buckets = len(buckets)
        likely_loading_or_blank = dominant_share >= 0.80 and unique_buckets <= 40
        return {
            "ok": True,
            "width": width,
            "height": height,
            "sampleCount": sample_count,
            "meanLuma": round(mean, 3),
            "stddevLuma": round(math.sqrt(variance), 3),
            "dominantBucketShare": round(dominant_share, 4),
            "uniqueColorBuckets": unique_buckets,
            "likelyLoadingOrBlank": likely_loading_or_blank,
        }
    except Exception as exc:
        return {"ok": False, "reason": "png-analysis-failed", "message": str(exc)}


def attach_visual_stats(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    stats = png_visual_stats(path) if payload.get("ok") else {"ok": False, "reason": "screenshot-not-ok"}
    payload["visualStats"] = stats
    payload["visualReady"] = stats.get("ok") is True and stats.get("likelyLoadingOrBlank") is False
    return payload


def capture_screenshot(window: dict[str, Any] | None, *, scenario: str) -> dict[str, Any]:
    screenshot_dir = REPORT_ROOT / CHANGE_ID / "screenshots" / scenario
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    path = screenshot_dir / f"{current_millis()}.png"
    attempts: list[dict[str, Any]] = []
    bounds = normalized_bounds(window)
    window_id = int(window.get("windowId") or -1) if window else -1
    base_payload = {
        "path": str(path),
        "captureMethod": "window",
        "fullscreenFallbackAllowed": False,
        "fullscreenFallbackUsed": False,
        "requiresElevatedExecution": True,
        "windowId": window_id if window_id > 0 else None,
        "bounds": bounds,
        "attempts": attempts,
    }

    if window_id <= 0:
        return attach_visual_stats({
            **base_payload,
            "ok": False,
            "reason": "codex-window-id-required",
            "fileSizeBytes": 0,
        }, path)

    result = run(["screencapture", "-x", f"-l{window_id}", str(path)], timeout=30)
    attempts.append({"method": "window", "windowId": window_id, "command": result.as_dict()})
    ok = result.returncode == 0 and screenshot_file_ok(path)
    payload = {
        **base_payload,
        "ok": ok,
        "reason": None if ok else "window-screenshot-failed",
        "fileSizeBytes": path.stat().st_size if path.exists() else 0,
    }
    return attach_visual_stats(payload, path)


def capture_screenshot_until_ready(
    window: dict[str, Any] | None,
    *,
    scenario: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    visual_attempts: list[dict[str, Any]] = []
    last_screenshot: dict[str, Any] | None = None
    while True:
        last_screenshot = capture_screenshot(window, scenario=scenario)
        visual_attempts.append(
            {
                "path": last_screenshot.get("path"),
                "ok": last_screenshot.get("ok"),
                "reason": last_screenshot.get("reason"),
                "visualReady": last_screenshot.get("visualReady"),
                "visualStats": last_screenshot.get("visualStats"),
                "captureMethod": last_screenshot.get("captureMethod"),
                "fullscreenFallbackAllowed": last_screenshot.get("fullscreenFallbackAllowed"),
                "fullscreenFallbackUsed": last_screenshot.get("fullscreenFallbackUsed"),
                "requiresElevatedExecution": last_screenshot.get("requiresElevatedExecution"),
            }
        )
        if last_screenshot.get("ok") and last_screenshot.get("visualReady"):
            last_screenshot["visualAttempts"] = visual_attempts
            return last_screenshot
        if time.time() >= deadline:
            last_screenshot["visualAttempts"] = visual_attempts
            return last_screenshot
        time.sleep(2.0)


def launch_verify(*, timeout: float) -> dict[str, Any]:
    app = DEFAULT_USER_APP
    started_at = current_millis()
    target = analyze_app(app, role="launch-target-codex-app") if app.exists() else {"exists": False, "path": str(app)}
    failures: list[str] = []
    warnings: list[str] = []
    if not target.get("exists"):
        failures.append("patch-target-missing")
    if not target.get("isUserWritableTarget"):
        failures.append("patch-target-not-supported")
    if target.get("bundleId") not in (APP_BUNDLE_ID, None):
        failures.append("bundle-id-mismatch")
    if target.get("oldPatchMarkers"):
        failures.append("old-patch-markers-present")
    if target.get("asarIntegrityOk") is False:
        failures.append("asar-integrity-mismatch")

    stop_report = stop_user_app(app, timeout=10) if app.exists() else {"attempted": False, "remainingPids": []}
    if stop_report.get("remainingPids"):
        failures.append("app-stop-failed")

    launch_command: dict[str, Any] | None = None
    process_wait: dict[str, Any] = {"ok": False, "processTree": {"pids": []}}
    if not failures:
        launch = run(launch_command_args(app), timeout=15)
        launch_command = launch.as_dict()
        if launch.returncode != 0:
            failures.append("app-launch-command-failed")
        else:
            process_wait = wait_for_app_process(app, timeout=timeout)
            if not process_wait.get("ok"):
                failures.append("app-process-not-found")

    process_tree = process_wait.get("processTree") if isinstance(process_wait.get("processTree"), dict) else {}
    process_pids = {int(pid) for pid in process_tree.get("pids", [])}
    if process_pids and not process_tree.get("targetPathMatched"):
        failures.append("launched-target-not-configured-codex-app")

    window_info = {"ok": False, "reason": "process-not-found", "windows": []}
    codex_window = None
    dialog_signals: list[dict[str, Any]] = []
    if process_pids:
        deadline = time.time() + min(timeout, 45.0)
        while time.time() < deadline:
            process_tree = process_tree_for_app(app)
            process_pids = {int(pid) for pid in process_tree.get("pids", [])}
            window_info = collect_window_info()
            if window_info.get("ok"):
                codex_window = select_codex_window(window_info, process_pids)
                dialog_signals = visible_dialog_signals(window_info)
                if codex_window is not None or dialog_signals:
                    break
            time.sleep(1.0)
    if not window_info.get("ok"):
        failures.append("window-info-unavailable")
    if process_wait.get("ok") and not process_pids:
        failures.append("app-process-exited")
    if process_pids and codex_window is None:
        failures.append("launched-window-not-configured-codex-app")
    if dialog_signals:
        failures.append("keychain-password-required")

    screenshot = capture_screenshot_until_ready(codex_window, scenario="launch", timeout=min(timeout, 60.0))
    if not screenshot.get("ok"):
        failures.append("screenshot-capture-failed")
    if screenshot.get("captureMethod") != "window" or screenshot.get("fullscreenFallbackUsed") is True:
        failures.append("screenshot-not-codex-window")
    elif screenshot.get("visualStats", {}).get("ok") is not True:
        failures.append("visual-state-unavailable")
    elif screenshot.get("visualReady") is not True:
        failures.append("app-launch-stuck")

    visual_evidence = {
        "requiresManualInspection": True,
        "manualInspectionInstruction": "确认截图显示目标 Codex App 主窗口已正常打开，无加载卡死、空白、可见错误或钥匙串密码框。",
        "automatedSignals": {
            "windowFound": codex_window is not None,
            "windowId": screenshot.get("windowId"),
            "captureMethod": screenshot.get("captureMethod"),
            "fullscreenFallbackAllowed": screenshot.get("fullscreenFallbackAllowed"),
            "fullscreenFallbackUsed": screenshot.get("fullscreenFallbackUsed"),
            "requiresElevatedExecution": screenshot.get("requiresElevatedExecution"),
            "keychainDialogSignals": dialog_signals,
            "screenshotNonTiny": screenshot.get("fileSizeBytes", 0) > 4096,
            "visualReady": screenshot.get("visualReady") is True,
            "visualStats": screenshot.get("visualStats"),
        },
    }

    fatal_reason = failures[0] if failures else None
    return {
        "ok": not failures,
        "fatalReason": fatal_reason,
        "failures": failures,
        "warnings": warnings,
        "changeId": CHANGE_ID,
        "startedAtMs": started_at,
        "finishedAtMs": current_millis(),
        "targetApp": target,
        "stopUserApp": stop_report,
        "launchResolution": {
            "launchCommand": launch_command,
            "launchUserDataDir": str(LAUNCH_USER_DATA_DIR),
            "rootPid": process_tree.get("rootPid"),
            "executablePath": process_tree.get("executablePath"),
            "processTree": process_tree,
            "window": codex_window,
            "windowInfo": window_info,
            "targetPathMatched": process_tree.get("targetPathMatched") is True,
        },
        "screenshotManifest": screenshot,
        "visualEvidence": visual_evidence,
    }


def git_value(repo: Path, args: list[str]) -> str | None:
    if not repo.exists():
        return None
    result = run(["git", "-C", str(repo), *args], timeout=30)
    return result.stdout if result.returncode == 0 else None


def file_line(path: Path, pattern: str) -> int | None:
    if not path.exists():
        return None
    for index, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if pattern in line:
            return index
    return None


def app_asar_summary(app: Path) -> dict[str, Any]:
    paths = app_paths(app)
    header, _header_size, data_offset = read_asar_header(paths["asar"])
    entries = iter_asar_entries(header)
    package_data = json.loads(read_asar_file(paths["asar"], header, data_offset, "package.json").decode("utf-8"))
    return {
        "asarPath": str(paths["asar"]),
        "asarHeaderSha256": asar_header_hash(paths["asar"]),
        "packedFileCount": sum(1 for _path, entry in entries if not entry.get("unpacked")),
        "unpackedFileCount": sum(1 for _path, entry in entries if entry.get("unpacked")),
        "rootEntries": sorted(header.get("files", {}).keys()) if isinstance(header.get("files"), dict) else [],
        "package": {
            "name": package_data.get("name"),
            "productName": package_data.get("productName"),
            "version": package_data.get("version"),
            "main": package_data.get("main"),
            "hasPatchFrameworkMarker": PATCH_FRAMEWORK_MARKER
            in json.dumps(package_data, ensure_ascii=False),
        },
        "bootstrapExists": any(path == ".vite/build/bootstrap.js" for path, _entry in entries),
    }


def patch_file_summary(path: Path) -> dict[str, Any]:
    line_count = None
    if path.exists() and path.is_file():
        with path.open("rb") as handle:
            line_count = sum(1 for _line in handle)
    return {
        "path": str(path),
        "exists": path.exists(),
        "sha256": sha256(path),
        "lineCount": line_count,
    }


def analyze_native() -> dict[str, Any]:
    repo = UPSTREAM_CODEX_REPO
    app = DEFAULT_USER_APP
    source_head = git_value(repo, ["rev-parse", "HEAD"])
    source_status = git_value(repo, ["status", "--short", "--branch"])
    evidence_files = {
        "backgroundProtocol": repo / "codex-rs/app-server-protocol/src/protocol/v2/thread.rs",
        "backgroundProcessor": repo / "codex-rs/app-server/src/request_processors/thread_processor.rs",
        "codexThread": repo / "codex-rs/core/src/codex_thread.rs",
        "unifiedExecMod": repo / "codex-rs/core/src/unified_exec/mod.rs",
        "unifiedExecManager": repo / "codex-rs/core/src/unified_exec/process_manager.rs",
        "asyncWatcher": repo / "codex-rs/core/src/unified_exec/async_watcher.rs",
        "sessionInject": repo / "codex-rs/core/src/session/inject.rs",
        "sessionEvents": repo / "codex-rs/core/src/session/mod.rs",
        "turnProtocol": repo / "codex-rs/app-server-protocol/src/protocol/v2/turn.rs",
        "turnProcessor": repo / "codex-rs/app-server/src/request_processors/turn_processor.rs",
        "appServerReadme": repo / "codex-rs/app-server/README.md",
    }
    evidence = {
        name: {
            "path": str(path),
            "exists": path.exists(),
        }
        for name, path in evidence_files.items()
    }
    evidence["backgroundProtocol"]["line"] = file_line(evidence_files["backgroundProtocol"], "pub struct ThreadBackgroundTerminal")
    evidence["backgroundProtocol"]["backgroundActiveLine"] = file_line(
        evidence_files["backgroundProtocol"],
        "ThreadBackgroundTerminalsBackgroundActiveParams",
    )
    evidence["backgroundProcessor"]["line"] = file_line(evidence_files["backgroundProcessor"], "thread_background_terminals_list_inner")
    evidence["backgroundProcessor"]["backgroundActiveLine"] = file_line(
        evidence_files["backgroundProcessor"],
        "thread_background_terminals_background_active_inner",
    )
    evidence["unifiedExecManager"]["line"] = file_line(evidence_files["unifiedExecManager"], "pub(crate) async fn list_processes")
    evidence["unifiedExecManager"]["backgroundActiveLine"] = file_line(
        evidence_files["unifiedExecManager"],
        "background_active_initial_exec",
    )
    evidence["asyncWatcher"]["wakeupDispatchLine"] = file_line(
        evidence_files["asyncWatcher"],
        "dispatch_background_terminal_wakeup",
    )
    evidence["sessionInject"]["wakeupDispatchLine"] = file_line(
        evidence_files["sessionInject"],
        "dispatch_background_terminal_wakeup",
    )
    evidence["sessionInject"]["guidedStuckLine"] = file_line(
        evidence_files["sessionInject"],
        "guided-message-stuck",
    )
    evidence["sessionEvents"]["modelConsumptionLine"] = file_line(
        evidence_files["sessionEvents"],
        "observe_background_wakeup_model_event",
    )
    evidence["turnProtocol"]["line"] = file_line(evidence_files["turnProtocol"], "pub struct TurnSteerParams")
    evidence["turnProcessor"]["line"] = file_line(evidence_files["turnProcessor"], "async fn turn_steer_inner")
    app_summary = app_asar_summary(app) if app.exists() else {"exists": False, "path": str(app)}
    return {
        "ok": app.exists() and repo.exists() and source_head is not None,
        "changeId": CHANGE_ID,
        "generatedAtMs": current_millis(),
        "upstream": {
            "repo": str(repo),
            "head": source_head,
            "status": source_status,
            "remote": git_value(repo, ["remote", "get-url", "origin"]),
        },
        "nativePatchFile": patch_file_summary(NATIVE_PATCH_FILE),
        "app": {
            "path": str(app),
            "analysis": analyze_app(app, role="native-analysis-target") if app.exists() else None,
            "asar": app_summary,
        },
        "nativeInterfaces": {
            "backgroundTerminals": {
                "methods": [
                    "thread/backgroundTerminals/list",
                    "thread/backgroundTerminals/terminate",
                    "thread/backgroundTerminals/clean",
                    "thread/backgroundTerminals/backgroundActive",
                ],
                "startMethodPresent": "explicit background is represented by shell_command.run_in_background in the default App tool path and by exec_command.run_in_background when unified exec is enabled",
                "listFields": ["itemId", "processId", "command", "cwd", "source", "output", "osPid", "cpuPercent", "rssKb"],
                "currentServerOsMetrics": "osPid/cpuPercent/rssKb are defined in protocol but currently returned as null by app-server mapping.",
            },
            "unifiedExec": {
                "tools": ["shell_command", "exec_command"],
                "toolArgs": {
                    "shell_command": ["command", "workdir", "timeout_ms", "run_in_background"],
                    "exec_command": ["cmd", "workdir", "yield_time_ms", "max_output_tokens", "run_in_background"],
                },
                "processStore": "UnifiedExecProcessManager stores live processes before initial yield so they can remain listed as background terminals.",
                "autoBackgroundThresholdMs": AUTO_BACKGROUND_THRESHOLD_SECONDS * 1000,
                "defaultMaxBackgroundTerminalTimeoutMs": 300_000,
                "maxYieldTimeMs": 30_000,
                "minEmptyYieldTimeMs": 5_000,
                "sources": ["explicit_tool", "foreground_adopt", "auto_threshold", "timeout", "user_shortcut"],
            },
            "guidedWakeup": {
                "idleStart": "turn/start",
                "busySteer": "turn/steer",
                "busyConstraint": "turn/steer only accepts active regular turns; review/manual compact turns reject steering.",
            },
            "oneOffCommandExec": {
                "methods": [
                    "command/exec",
                    "command/exec/write",
                    "command/exec/resize",
                    "command/exec/terminate",
                ],
                "note": "This is a connection-scoped app-server command runner, not the model tool-call background terminal path.",
            },
        },
        "evidence": evidence,
        "patchPlan": build_patch_plan(app),
    }


def build_patch_plan(app: Path) -> list[dict[str, Any]]:
    return [
        {
            "name": "native-codex-binary-build",
            "target": str(BUILT_CODEX_BINARY),
            "type": "cargo-build",
            "sourceRepo": str(UPSTREAM_CODEX_REPO),
            "sourcePatch": patch_file_summary(NATIVE_PATCH_FILE),
            "purpose": "Build the patched Codex native binary that owns background terminal semantics.",
        },
        {
            "name": "native-codex-binary-install",
            "target": str(app_paths(app)["codex"]),
            "type": "binary-replace",
            "source": str(BUILT_CODEX_BINARY),
            "purpose": "Install the patched native binary into the configured Codex.app target.",
        },
        {
            "name": "ctrl-b-ui-binding",
            "target": "webview bundled assets",
            "type": "asar-text-replace",
            "method": CTRL_B_NATIVE_METHOD,
            "action": CTRL_B_ACTION,
            "purpose": "Bind plain Ctrl+B to native foreground-to-background transition for the active local conversation.",
        },
        {
            "name": "native-background-terminal-controls",
            "target": "webview/assets/local-conversation-thread-*.js",
            "type": "asar-text-replace",
            "method": TERMINATE_BG_NATIVE_METHOD,
            "action": TERMINATE_BG_ACTION,
            "purpose": "Keep summary-panel background terminal rows running without OS metrics and route stop/restart through native process ids.",
        },
        {
            "name": "app-control-thread-start-bridge",
            "target": APP_CONTROL_MAIN_REL,
            "type": "asar-text-replace",
            "marker": APP_CONTROL_MARKER,
            "purpose": "Allow the verification controller to ask the running Codex App target to start real local threads/turns and query native background-terminal state.",
        },
        {
            "name": "package-json-framework-marker",
            "target": "package.json",
            "type": "json-upsert",
            "match": {
                "name": "openai-codex-electron",
                "productName": "Codex",
                "main": ".vite/build/bootstrap.js",
            },
            "marker": PATCH_FRAMEWORK_MARKER,
            "purpose": "Verify audited ASAR patch/repack/integrity flow before functional hook steps are added.",
        }
    ]


def cargo_env() -> dict[str, str]:
    path = os.environ.get("PATH", "")
    if RUST_TOOLCHAIN_DIR is not None:
        path = f"{RUST_TOOLCHAIN_DIR}:{path}"
    return {
        "CODEX_SANDBOX": "",
        "RUST_MIN_STACK": str(8 * 1024 * 1024),
        "RUSTC": str(RUSTC),
        "PATH": path,
    }


def executable_available(path: Path) -> bool:
    return path.exists() or shutil.which(str(path)) is not None


def build_native_binary() -> dict[str, Any]:
    if not CODEX_RS.exists():
        raise ControllerError("native-source-missing", "Codex Rust source checkout was not found.", details={"path": str(CODEX_RS)})
    if not executable_available(CARGO) or not executable_available(RUSTC):
        raise ControllerError(
            "native-toolchain-missing",
            "Rust toolchain was not found. Put cargo/rustc on PATH or set CODEX_BACKGROUND_SHELL_RUST_TOOLCHAIN.",
            details={"cargo": str(CARGO), "rustc": str(RUSTC)},
        )
    before_hash = sha256(BUILT_CODEX_BINARY)
    build = run(
        [str(CARGO), "build", "-p", "codex-cli", "--release"],
        timeout=1800,
        env_extra=cargo_env(),
        cwd=CODEX_RS,
    )
    after_hash = sha256(BUILT_CODEX_BINARY)
    if build.returncode != 0 or after_hash is None:
        raise ControllerError("native-build-failed", "Patched Codex native binary failed to build.", details=build.as_dict())
    version = run([str(BUILT_CODEX_BINARY), "--version"], timeout=20)
    markers = file_contains_any(BUILT_CODEX_BINARY, NATIVE_PATCH_MARKERS)
    if sorted(markers) != sorted(marker.decode("utf-8", "replace") for marker in NATIVE_PATCH_MARKERS):
        raise ControllerError(
            "native-patch-markers-missing",
            "Built Codex binary does not contain all required native patch markers.",
            details={"markers": markers, "binary": str(BUILT_CODEX_BINARY)},
        )
    return {
        "name": "native-codex-binary-build",
        "ok": True,
        "sourceRepo": str(UPSTREAM_CODEX_REPO),
        "workdir": str(CODEX_RS),
        "binary": str(BUILT_CODEX_BINARY),
        "beforeSha256": before_hash,
        "afterSha256": after_hash,
        "sizeBytes": BUILT_CODEX_BINARY.stat().st_size,
        "command": build.as_dict(),
        "versionCommand": version.as_dict(),
        "nativePatchMarkers": markers,
    }


def install_native_binary(app: Path, build_step: dict[str, Any]) -> dict[str, Any]:
    if not path_is_user_writable_app(app):
        raise ControllerError("patch-target-not-supported", "Native binary install target must be the configured Codex.app target.")
    source = Path(str(build_step["binary"]))
    target = app_paths(app)["codex"]
    if not source.exists() or not source.is_file():
        raise ControllerError("native-build-missing", "Built Codex native binary is missing.", details={"source": str(source)})
    if not target.exists():
        raise ControllerError("native-install-target-missing", "Target Codex native binary is missing.", details={"target": str(target)})
    backup_dir = REPORT_ROOT / CHANGE_ID / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"codex-{current_millis()}"
    before_hash = sha256(target)
    shutil.copy2(target, backup)
    shutil.copy2(source, target)
    target.chmod(0o755)
    after_hash = sha256(target)
    source_hash = sha256(source)
    version = run([str(target), "--version"], timeout=20)
    markers = file_contains_any(target, NATIVE_PATCH_MARKERS)
    ok = (
        source_hash is not None
        and after_hash == source_hash
        and sorted(markers) == sorted(marker.decode("utf-8", "replace") for marker in NATIVE_PATCH_MARKERS)
    )
    return {
        "name": "native-codex-binary-install",
        "ok": ok,
        "app": str(app),
        "source": str(source),
        "target": str(target),
        "backup": str(backup),
        "beforeSha256": before_hash,
        "afterSha256": after_hash,
        "sourceSha256": source_hash,
        "versionCommand": version.as_dict(),
        "nativePatchMarkers": markers,
    }


def run_native_scenario_test(scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIO_TESTS:
        raise ControllerError("unknown-scenario", f"Unknown scenario: {scenario}")
    configured_tests = SCENARIO_TESTS[scenario]
    if isinstance(configured_tests[0], str):
        tests = [configured_tests]
    else:
        tests = list(configured_tests)
    if not CODEX_RS.exists():
        raise ControllerError("native-source-missing", "Codex Rust source checkout was not found.", details={"path": str(CODEX_RS)})
    commands = []
    ok = True
    for package, test_name in tests:
        result = run(
            [str(CARGO), "test", "-p", package, test_name],
            timeout=900,
            env_extra=cargo_env(),
            cwd=CODEX_RS,
        )
        commands.append(result.as_dict())
        ok = ok and result.returncode == 0
    return {
        "ok": ok,
        "scenario": scenario,
        "tests": [{"package": package, "testName": test_name} for package, test_name in tests],
        "commands": commands,
    }


def javascript_syntax_check(rel_path: str, text: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        return {
            "ok": False,
            "reason": "node-missing",
            "target": rel_path,
            "message": "node is required for renderer bundle syntax validation.",
        }
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    temp_path = Path(tempfile.gettempdir()) / f"codex-{digest}-{Path(rel_path).name}"
    temp_path.write_text(text, encoding="utf-8")
    try:
        result = run([node, "--check", str(temp_path)], timeout=30)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return {
        "ok": result.returncode == 0,
        "target": rel_path,
        "node": node,
        "command": result.as_dict(),
    }


def scan_ctrl_b_shortcut_conflicts(app: Path) -> dict[str, Any]:
    if not app.exists():
        return {"ok": False, "reason": "app-missing", "matches": []}
    paths = app_paths(app)
    header, _header_size, data_offset = read_asar_header(paths["asar"])
    matches: list[dict[str, Any]] = []
    action_marker_count = 0
    method_marker_count = 0
    list_action_marker_count = 0
    list_method_marker_count = 0
    patterns = ("Ctrl+B", "Control+B", "CmdOrCtrl+B", "CommandOrControl+B")
    for rel_path, entry in iter_asar_entries(header):
        if entry.get("unpacked"):
            continue
        if not (rel_path.endswith(".js") or rel_path.endswith(".json") or rel_path.endswith(".html")):
            continue
        try:
            text = read_asar_file(paths["asar"], header, data_offset, rel_path).decode("utf-8", "replace")
        except ControllerError:
            continue
        action_marker_count += text.count(CTRL_B_ACTION)
        method_marker_count += text.count(CTRL_B_NATIVE_METHOD)
        for pattern in patterns:
            offset = -1
            while True:
                offset = text.find(pattern, offset + 1)
                if offset < 0:
                    break
                embedded_in_cmd_or_ctrl = pattern == "Ctrl+B" and text[max(0, offset - 5) : offset] == "CmdOr"
                start = max(0, offset - 80)
                end = min(len(text), offset + len(pattern) + 80)
                matches.append(
                    {
                        "path": rel_path,
                        "pattern": pattern,
                        "offset": offset,
                        "embeddedInCmdOrCtrl": embedded_in_cmd_or_ctrl,
                        "snippet": text[start:end],
                    }
                )
    ctrl_conflicts = [
        match
        for match in matches
        if match["pattern"] in ("Ctrl+B", "Control+B")
        and not match.get("embeddedInCmdOrCtrl")
    ]
    return {
        "ok": not ctrl_conflicts and action_marker_count > 0 and method_marker_count > 0,
        "matches": matches,
        "conflicts": ctrl_conflicts,
        "uiBindingPresent": action_marker_count > 0 and method_marker_count > 0,
        "actionMarkerCount": action_marker_count,
        "methodMarkerCount": method_marker_count,
        "note": "CmdOrCtrl+B is recorded separately because it is Command+B on macOS and does not conflict with plain Ctrl+B.",
    }


def scan_task005_ui_bindings(app: Path) -> dict[str, Any]:
    if not app.exists():
        return {"ok": False, "reason": "app-missing", "checks": {}, "matches": {}}
    paths = app_paths(app)
    header, _header_size, data_offset = read_asar_header(paths["asar"])
    local_thread_rel = "webview/assets/local-conversation-thread-CRryh-25.js"
    automations_rel = "webview/assets/app-initial~app-main~automations-page-Bl6HoLGr.js"
    try:
        local_text = read_asar_file(paths["asar"], header, data_offset, local_thread_rel).decode("utf-8", "replace")
        automations_text = read_asar_file(paths["asar"], header, data_offset, automations_rel).decode("utf-8", "replace")
    except ControllerError as exc:
        return {"ok": False, "reason": exc.reason, "checks": {}, "matches": {}, "details": exc.details}

    syntax_check = javascript_syntax_check(local_thread_rel, local_text)
    automations_syntax_check = javascript_syntax_check(automations_rel, automations_text)
    action_marker_count = 0
    method_marker_count = 0
    list_action_marker_count = 0
    list_method_marker_count = 0
    for rel_path, entry in iter_asar_entries(header):
        if entry.get("unpacked"):
            continue
        if not (rel_path.endswith(".js") or rel_path.endswith(".json") or rel_path.endswith(".html")):
            continue
        try:
            text = read_asar_file(paths["asar"], header, data_offset, rel_path).decode("utf-8", "replace")
        except ControllerError:
            continue
        action_marker_count += text.count(TERMINATE_BG_ACTION)
        method_marker_count += text.count(TERMINATE_BG_NATIVE_METHOD)
        list_action_marker_count += text.count(LIST_BG_ACTION)
        list_method_marker_count += text.count(LIST_BG_NATIVE_METHOD)

    checks = {
        "summaryPollsNativeBackgroundTerminalList": f"_n(`{LIST_BG_ACTION}`,{{conversationId:i,cursor:null,limit:50}})" in local_text,
        "summaryMergesNativeBackgroundTerminalList": "backgroundTerminals:f" in local_text
        and "f=[...Bt,...f.filter" in local_text,
        "summaryMapsNativeBackgroundTerminalOutput": "output:String(e.output??``)" in local_text,
        "summaryUsesCommandTitle": "e.terminal.command.length>0?e.terminal.command" in local_text,
        "outputMenuPresent": "codex.localConversation.backgroundTerminals.openOutput" in local_text
        and "Open output" in local_text,
        "nativeListHostActionPresent": list_action_marker_count > 0,
        "nativeListMethodPresent": list_method_marker_count > 0,
        "nativeListHostCommandRegistryPresent": f'"{LIST_BG_ACTION}":Q7' in automations_text
        and "listBackgroundTerminals" in automations_text,
        "nativeTerminateHostActionPresent": action_marker_count > 0,
        "nativeTerminateMethodPresent": method_marker_count > 0,
        "nativeStatusStaysRunningWithoutMetrics": "e.process.source===`background-terminal`?`running`" in local_text,
        "nativeRowsEnabledWithoutOsPid": "o.metrics?.pid==null&&o.process.source!==`background-terminal`" in local_text,
        "nativeStopUsesProcessId": (
            f"_n(`{TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
            in local_text
        ),
        "nativeRestartUsesExistingCommandCwdBridge": "Gt.runHeadlessAction(i,{command:n.command,cwd:n.cwd})" in local_text,
        "nativeStopAndRestartBothBound": local_text.count(f"_n(`{TERMINATE_BG_ACTION}`") >= 2,
        "localThreadJsSyntaxOk": syntax_check.get("ok") is True,
        "outputTabReceivesCommandAndOutputProps": "props:{conversationId:n,terminalId:t.id,command:t.command,output:t.output??``}" in automations_text,
        "outputTabPrependsCommandLine": all(
            marker in automations_text
            for marker in (
                "u=c?.aggregatedOutput??l?.buffer??a??``",
                "d.length===0&&(d=i??``)",
                "f=d.length>0?",
                "`${d}\\n${u}`",
                "Ece,{output:f}",
            )
        ),
        "outputTabJsSyntaxOk": automations_syntax_check.get("ok") is True,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "matches": {
            "target": local_thread_rel,
            "terminateActionMarkerCount": action_marker_count,
            "terminateMethodMarkerCount": method_marker_count,
            "listActionMarkerCount": list_action_marker_count,
            "listMethodMarkerCount": list_method_marker_count,
            "localTerminateCallCount": local_text.count(f"_n(`{TERMINATE_BG_ACTION}`"),
            "localListCallCount": local_text.count(f"_n(`{LIST_BG_ACTION}`"),
            "summaryCommandTitleCount": local_text.count("e.terminal.command.length>0?e.terminal.command"),
            "openOutputCount": local_text.count("codex.localConversation.backgroundTerminals.openOutput"),
            "outputCommandPropCount": automations_text.count("props:{conversationId:n,terminalId:t.id,command:t.command,output:t.output??``}"),
            "outputPrependedCommandCount": automations_text.count("u=c?.aggregatedOutput??l?.buffer??a??``"),
            "automationsListActionCount": automations_text.count(LIST_BG_ACTION),
            "automationsListManagerMethodCount": automations_text.count("listBackgroundTerminals"),
        },
        "syntaxCheck": syntax_check,
        "outputTabSyntaxCheck": automations_syntax_check,
        "note": "Restart intentionally uses Codex's existing headless action bridge after native terminate because the native list API exposes command/cwd but not a full restartable ExecCommandRequest.",
    }


def scan_task006_wakeup_bindings() -> dict[str, Any]:
    files = {
        "codexThread": CODEX_RS / "core/src/codex_thread.rs",
        "sessionInject": CODEX_RS / "core/src/session/inject.rs",
        "sessionEvents": CODEX_RS / "core/src/session/mod.rs",
        "asyncWatcher": CODEX_RS / "core/src/unified_exec/async_watcher.rs",
        "unifiedExecTests": CODEX_RS / "core/src/unified_exec/mod_tests.rs",
    }
    texts: dict[str, str] = {}
    missing_files = []
    for name, path in files.items():
        if not path.exists():
            missing_files.append(name)
            texts[name] = ""
        else:
            texts[name] = path.read_text(encoding="utf-8")

    session_inject = texts["sessionInject"]
    codex_thread = texts["codexThread"]
    async_watcher = texts["asyncWatcher"]
    tests = texts["unifiedExecTests"]
    checks = {
        "phaseEnumPresent": all(
            marker in codex_thread
            for marker in (
                "BackgroundWakeupPhase",
                "Pending",
                "Dispatching",
                "RpcAccepted",
                "ModelObserved",
                "Delivered",
                "DispatchFailed",
            )
        ),
        "busyDeferredPathUsesIdleTurn": "spawn_deferred_background_wakeup_idle_turn" in session_inject
        and "busy_deferred_idle_turn" in session_inject,
        "idleDirectPathUsesNativeGate": "try_start_turn_if_idle" in session_inject
        and "idle_direct_turn" in session_inject,
        "completionWatcherDispatchesWakeup": "dispatch_background_terminal_wakeup" in async_watcher
        and "background_wakeup_armed" in async_watcher,
        "wakeupPromptContainsRequiredFields": all(
            marker in session_inject
            for marker in (
                "Task id:",
                "Command:",
                "Exit status:",
                "Output path:",
                "Output entry:",
                "Output summary:",
                "Suggested next step:",
            )
        ),
        "deliveredObservationHookPresent": "observe_background_wakeup_model_event" in texts["sessionEvents"]
        and "BackgroundWakeupPhase::Delivered" in session_inject,
        "guidedMessageStuckGuardPresent": "guided-message-stuck" in session_inject
        and "update_background_wakeup_if_still_pending_delivery" in session_inject,
        "busyScenarioTestPresent": "background_terminal_exit_wakeup_is_model_observed_then_delivered" in tests,
        "idleScenarioTestPresent": "background_terminal_idle_wakeup_uses_idle_direct_turn" in tests,
    }
    return {
        "ok": not missing_files and all(checks.values()),
        "missingFiles": missing_files,
        "checks": checks,
        "files": {name: str(path) for name, path in files.items()},
    }


def scan_app_control_bridge(app: Path) -> dict[str, Any]:
    if not app.exists():
        return {"ok": False, "reason": "app-missing", "checks": {}, "matches": {}}
    paths = app_paths(app)
    header, _header_size, data_offset = read_asar_header(paths["asar"])
    try:
        main_text = read_asar_file(paths["asar"], header, data_offset, APP_CONTROL_MAIN_REL).decode("utf-8", "replace")
    except ControllerError as exc:
        return {"ok": False, "reason": exc.reason, "checks": {}, "matches": {}, "details": exc.details}

    syntax_check = javascript_syntax_check(APP_CONTROL_MAIN_REL, main_text)
    checks = {
        "markerPresent": APP_CONTROL_MARKER in main_text,
        "bridgeFunctionPresent": "__cbtAppControl" in main_text,
        "bridgeStartedFromAppController": "__cbtAppControl(this)" in main_text,
        "startsNativeThreads": "start-ui-thread" in main_text and ".startThread({" in main_text,
        "startsNativeTurns": "start-turn" in main_text and ".startTurn(" in main_text,
        "queriesNativeBackgroundTerminals": "thread/backgroundTerminals/list" in main_text,
        "terminatesNativeBackgroundTerminals": "thread/backgroundTerminals/terminate" in main_text,
        "usesPrimaryWindowRouteNavigation": "navigate-to-route" in main_text and "getPrimaryWindow()" in main_text,
        "rendererDomAutomationBounded": "renderer-click-text" in main_text and "renderer-dom-text" in main_text,
        "rendererRightPanelAutomationBounded": "renderer-right-panel-text" in main_text
        and "renderer-click-right-panel-text" in main_text,
        "mainJsSyntaxOk": syntax_check.get("ok") is True,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "matches": {
            "target": APP_CONTROL_MAIN_REL,
            "markerCount": main_text.count(APP_CONTROL_MARKER),
            "functionCount": main_text.count("__cbtAppControl"),
            "requestActionCount": main_text.count("c.action==="),
        },
        "syntaxCheck": syntax_check,
        "note": "The bridge is a fail-closed verification harness in the configured Codex App target. It starts real app-server threads/turns and reads native background-terminal state; it does not fabricate background tasks.",
    }


def scenario_report(scenario: str) -> dict[str, Any]:
    test = run_native_scenario_test(scenario)
    app = DEFAULT_USER_APP
    target = analyze_app(app, role=f"scenario-{scenario}-codex-app-target") if app.exists() else {"exists": False, "path": str(app)}
    failures: list[str] = []
    warnings: list[str] = []
    if not test.get("ok"):
        failures.append("native-scenario-test-failed")
    if not target.get("exists") or not target.get("isUserWritableTarget"):
        failures.append("patch-target-not-supported")
    if target.get("nativePatchMarkersOk") is not True:
        failures.append("native-patch-markers-missing")
    if target.get("oldPatchMarkers"):
        failures.append("old-patch-markers-present")
    if target.get("asarIntegrityOk") is False:
        failures.append("asar-integrity-mismatch")
    shortcut_conflicts: dict[str, Any] | None = None
    if scenario == "ctrl-b":
        shortcut_conflicts = scan_ctrl_b_shortcut_conflicts(app)
        if shortcut_conflicts.get("ok") is not True:
            if shortcut_conflicts.get("uiBindingPresent") is not True:
                failures.append("ctrl-b-ui-binding-missing")
            if shortcut_conflicts.get("conflicts"):
                failures.append("ctrl-b-shortcut-conflict")
    task005_ui: dict[str, Any] | None = None
    if scenario in ("summary-output", "stop-restart"):
        task005_ui = scan_task005_ui_bindings(app)
        checks = task005_ui.get("checks", {})
        if scenario == "summary-output":
            required = ("summaryUsesCommandTitle", "outputMenuPresent", "localThreadJsSyntaxOk")
        else:
            required = (
                "nativeTerminateHostActionPresent",
                "nativeTerminateMethodPresent",
                "nativeStatusStaysRunningWithoutMetrics",
                "nativeRowsEnabledWithoutOsPid",
                "nativeStopUsesProcessId",
                "nativeRestartUsesExistingCommandCwdBridge",
                "nativeStopAndRestartBothBound",
                "localThreadJsSyntaxOk",
            )
        missing = [name for name in required if checks.get(name) is not True]
        if missing:
            failures.append("task005-ui-binding-missing")
            warnings.append(f"missing task005 UI checks: {', '.join(missing)}")
    task006_native: dict[str, Any] | None = None
    if scenario in ("busy-wakeup-30s", "idle-wakeup-2min"):
        task006_native = scan_task006_wakeup_bindings()
        if task006_native.get("ok") is not True:
            failures.append("task006-wakeup-binding-missing")
    return {
        "ok": not failures,
        "fatalReason": failures[0] if failures else None,
        "failures": failures,
        "warnings": warnings,
        "changeId": CHANGE_ID,
        "generatedAtMs": current_millis(),
        "scenario": scenario,
        "level": "native-code-and-codex-app-target-binary",
        "nativeTest": test,
        "targetApp": target,
        "ctrlBShortcutConflicts": shortcut_conflicts,
        "task005UiBindings": task005_ui,
        "task006WakeupBindings": task006_native,
    }


def strict_window_screenshot_check(launch_payload: dict[str, Any]) -> dict[str, Any]:
    screenshot = launch_payload.get("screenshotManifest") if isinstance(launch_payload, dict) else None
    launch_resolution = launch_payload.get("launchResolution") if isinstance(launch_payload, dict) else None
    if not isinstance(screenshot, dict) or not isinstance(launch_resolution, dict):
        return {"ok": False, "failures": ["launch-screenshot-manifest-missing"]}

    window = launch_resolution.get("window") if isinstance(launch_resolution.get("window"), dict) else {}
    process_tree = launch_resolution.get("processTree") if isinstance(launch_resolution.get("processTree"), dict) else {}
    process_pids = {int(pid) for pid in process_tree.get("pids", []) if str(pid).isdigit()}
    expected_executable = str(app_paths(DEFAULT_USER_APP)["executable"])
    failures: list[str] = []

    if screenshot.get("ok") is not True:
        failures.append("screenshot-not-ok")
    if screenshot.get("captureMethod") != "window":
        failures.append("screenshot-not-window")
    if screenshot.get("fullscreenFallbackAllowed") is not False:
        failures.append("fullscreen-fallback-not-explicitly-disabled")
    if screenshot.get("fullscreenFallbackUsed") is not False:
        failures.append("fullscreen-fallback-used")
    if screenshot.get("requiresElevatedExecution") is not True:
        failures.append("screenshot-elevation-not-recorded")
    if not screenshot.get("windowId") or screenshot.get("windowId") != window.get("windowId"):
        failures.append("screenshot-window-id-mismatch")
    if window.get("ownerPid") not in process_pids:
        failures.append("window-owner-not-in-target-process-tree")
    if process_tree.get("executablePath") != expected_executable:
        failures.append("launch-executable-not-configured-codex-app")
    if process_tree.get("targetPathMatched") is not True:
        failures.append("launch-target-path-not-matched")
    if screenshot.get("visualReady") is not True:
        failures.append("screenshot-not-visually-ready")
    if (screenshot.get("visualStats") or {}).get("likelyLoadingOrBlank") is True:
        failures.append("screenshot-loading-or-blank")
    path = Path(str(screenshot.get("path") or ""))
    if not screenshot_file_ok(path):
        failures.append("screenshot-file-missing-or-empty")

    return {
        "ok": not failures,
        "failures": failures,
        "path": screenshot.get("path"),
        "windowId": screenshot.get("windowId"),
        "ownerPid": window.get("ownerPid"),
        "executablePath": process_tree.get("executablePath"),
        "captureMethod": screenshot.get("captureMethod"),
        "fullscreenFallbackAllowed": screenshot.get("fullscreenFallbackAllowed"),
        "fullscreenFallbackUsed": screenshot.get("fullscreenFallbackUsed"),
        "requiresElevatedExecution": screenshot.get("requiresElevatedExecution"),
        "visualReady": screenshot.get("visualReady"),
        "visualStats": screenshot.get("visualStats"),
    }


def capture_ui_step_screenshot(
    step: str,
    *,
    timeout: float = 30.0,
    thread_id: str | None = None,
) -> dict[str, Any]:
    navigate_response = None
    if thread_id:
        navigate_response = app_control_request("navigate", {"path": f"/local/{thread_id}"}, timeout=20)

    expected_executable = str(app_paths(DEFAULT_USER_APP)["executable"])
    deadline = time.time() + timeout
    attempts: list[dict[str, Any]] = []
    last_payload: dict[str, Any] | None = None

    while True:
        process_tree = process_tree_for_app(DEFAULT_USER_APP)
        process_pids = {int(pid) for pid in process_tree.get("pids", []) if str(pid).isdigit()}
        window_info = collect_window_info()
        codex_window = select_codex_window(window_info, process_pids) if process_pids else None
        dialog_signals = visible_dialog_signals(window_info) if window_info.get("ok") else []
        screenshot = capture_screenshot(codex_window, scenario=step)
        failures: list[str] = []
        if not process_pids:
            failures.append("app-process-not-found")
        if process_tree.get("executablePath") != expected_executable:
            failures.append("launch-executable-not-configured-codex-app")
        if process_tree.get("targetPathMatched") is not True:
            failures.append("launch-target-path-not-matched")
        if codex_window is None:
            failures.append("codex-window-not-found")
        if dialog_signals:
            failures.append("keychain-password-required")
        if screenshot.get("ok") is not True:
            failures.append("screenshot-not-ok")
        if screenshot.get("captureMethod") != "window":
            failures.append("screenshot-not-window")
        if screenshot.get("fullscreenFallbackAllowed") is not False:
            failures.append("fullscreen-fallback-not-explicitly-disabled")
        if screenshot.get("fullscreenFallbackUsed") is not False:
            failures.append("fullscreen-fallback-used")
        if screenshot.get("requiresElevatedExecution") is not True:
            failures.append("screenshot-elevation-not-recorded")
        if codex_window and screenshot.get("windowId") != codex_window.get("windowId"):
            failures.append("screenshot-window-id-mismatch")
        if codex_window and codex_window.get("ownerPid") not in process_pids:
            failures.append("window-owner-not-in-target-process-tree")
        if screenshot.get("visualReady") is not True:
            failures.append("screenshot-not-visually-ready")
        if (screenshot.get("visualStats") or {}).get("likelyLoadingOrBlank") is True:
            failures.append("screenshot-loading-or-blank")
        path = Path(str(screenshot.get("path") or ""))
        if not screenshot_file_ok(path):
            failures.append("screenshot-file-missing-or-empty")

        last_payload = {
            "ok": not failures,
            "step": step,
            "failures": failures,
            "path": screenshot.get("path"),
            "windowId": screenshot.get("windowId"),
            "ownerPid": codex_window.get("ownerPid") if codex_window else None,
            "executablePath": process_tree.get("executablePath"),
            "captureMethod": screenshot.get("captureMethod"),
            "fullscreenFallbackAllowed": screenshot.get("fullscreenFallbackAllowed"),
            "fullscreenFallbackUsed": screenshot.get("fullscreenFallbackUsed"),
            "requiresElevatedExecution": screenshot.get("requiresElevatedExecution"),
            "visualReady": screenshot.get("visualReady"),
            "visualStats": screenshot.get("visualStats"),
            "screenshot": screenshot,
            "processTree": process_tree,
            "window": codex_window,
            "dialogSignals": dialog_signals,
            "navigateResponse": navigate_response,
        }
        attempts.append(
            {
                "ok": last_payload["ok"],
                "failures": failures,
                "windowId": screenshot.get("windowId"),
                "ownerPid": last_payload.get("ownerPid"),
                "visualReady": screenshot.get("visualReady"),
                "path": screenshot.get("path"),
            }
        )
        if last_payload["ok"] or time.time() >= deadline:
            last_payload["windowAttempts"] = attempts
            return last_payload
        time.sleep(2.0)


def app_control_request(action: str, payload: dict[str, Any] | None = None, *, timeout: float = 60.0) -> dict[str, Any]:
    APP_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    request_id = f"{current_millis()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    request_path = APP_CONTROL_DIR / f"request-{request_id}.json"
    response_path = APP_CONTROL_DIR / f"response-{request_id}.json"
    processing_path = APP_CONTROL_DIR / f"processing-{request_id}.json"
    request = {"id": request_id, "action": action, **(payload or {})}
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if response_path.exists():
            try:
                response = json.loads(response_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "reason": "app-control-response-json-invalid",
                    "message": str(exc),
                    "request": request,
                    "responsePath": str(response_path),
                }
            return {
                "ok": response.get("ok") is True,
                "reason": None if response.get("ok") is True else "app-control-action-failed",
                "request": request,
                "response": response,
                "responsePath": str(response_path),
            }
        time.sleep(0.25)
    return {
        "ok": False,
        "reason": "app-control-timeout",
        "request": request,
        "requestPath": str(request_path),
        "processingPathExists": processing_path.exists(),
        "responsePath": str(response_path),
        "timeoutSeconds": timeout,
    }


def app_control_result(response: dict[str, Any]) -> Any:
    raw = response.get("response") if isinstance(response.get("response"), dict) else {}
    return raw.get("result") if isinstance(raw, dict) else None


def renderer_dom_text(*, max_chars: int = 8000, timeout: float = 20.0) -> dict[str, Any]:
    return app_control_request("renderer-dom-text", {"maxChars": max_chars}, timeout=timeout)


def renderer_right_panel_text(*, max_chars: int = 8000, timeout: float = 20.0) -> dict[str, Any]:
    return app_control_request("renderer-right-panel-text", {"maxChars": max_chars}, timeout=timeout)


def renderer_click_text(text: str, *, timeout: float = 20.0) -> dict[str, Any]:
    return app_control_request("renderer-click-text", {"text": text}, timeout=timeout)


def renderer_click_right_panel_text(text: str, *, timeout: float = 20.0) -> dict[str, Any]:
    return app_control_request("renderer-click-right-panel-text", {"text": text}, timeout=timeout)


def wait_for_background_terminal(
    thread_id: str,
    *,
    command_substring: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    attempts: list[dict[str, Any]] = []
    while time.time() < deadline:
        response = app_control_request(
            "list-background-terminals",
            {"threadId": thread_id, "limit": 50},
            timeout=20,
        )
        result = app_control_result(response)
        data = result.get("data") if isinstance(result, dict) else None
        terminals = data if isinstance(data, list) else []
        match = next(
            (
                terminal
                for terminal in terminals
                if isinstance(terminal, dict) and command_substring in str(terminal.get("command") or "")
            ),
            None,
        )
        attempts.append(
            {
                "ok": response.get("ok"),
                "terminalCount": len(terminals),
                "matched": match is not None,
                "reason": response.get("reason"),
                "errorMessage": (response.get("response") or {}).get("errorMessage")
                if isinstance(response.get("response"), dict)
                else None,
            }
        )
        if match is not None:
            return {"ok": True, "terminal": match, "terminals": terminals, "attempts": attempts}
        time.sleep(2.0)
    return {
        "ok": False,
        "reason": "background-terminal-not-found",
        "commandSubstring": command_substring,
        "attempts": attempts,
    }


def response_thread_session_path(response: dict[str, Any]) -> Path | None:
    result = app_control_result(response)
    if not isinstance(result, dict):
        return None
    candidates = [
        result.get("path"),
        (result.get("thread") or {}).get("path") if isinstance(result.get("thread"), dict) else None,
    ]
    for candidate in candidates:
        if candidate:
            return Path(str(candidate))
    return None


def response_turn_id(response: dict[str, Any]) -> str | None:
    result = app_control_result(response)
    if not isinstance(result, dict):
        return None
    turn_id = result.get("turnId")
    if turn_id:
        return str(turn_id)
    turn = result.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn["id"])
    return None


def iter_session_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    if not path.exists() or not path.is_file():
        return []
    events: list[tuple[int, dict[str, Any]]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append((line_number, value))
    return events


def session_message_text(payload: dict[str, Any]) -> str:
    if payload.get("type") == "agent_message":
        return str(payload.get("message") or "")
    if payload.get("type") != "message":
        return ""
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def wait_for_session_shell_background(
    session_path: Path | None,
    *,
    command_substring: str,
    timeout: float,
    turn_id: str | None = None,
) -> dict[str, Any]:
    if session_path is None:
        return {"ok": False, "reason": "session-path-missing", "commandSubstring": command_substring}

    deadline = time.time() + timeout
    attempts: list[dict[str, Any]] = []
    process_pattern = re.compile(r"Process running with session ID\s+(\d+)")
    last_call: dict[str, Any] | None = None

    while time.time() < deadline:
        call_ids: dict[str, dict[str, Any]] = {}
        events = iter_session_jsonl(session_path)
        for line_number, event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if payload.get("type") == "function_call" and payload.get("name") == "shell_command":
                metadata = payload.get("internal_chat_message_metadata_passthrough")
                payload_turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
                if turn_id and payload_turn_id != turn_id:
                    continue
                try:
                    args = json.loads(str(payload.get("arguments") or "{}"))
                except json.JSONDecodeError:
                    continue
                command = str(args.get("command") or "")
                if command_substring not in command or args.get("run_in_background") is not True:
                    continue
                call_id = str(payload.get("call_id") or "")
                last_call = {
                    "line": line_number,
                    "timestamp": event.get("timestamp"),
                    "callId": call_id,
                    "turnId": payload_turn_id,
                    "command": command,
                    "arguments": args,
                }
                if call_id:
                    call_ids[call_id] = last_call
            elif payload.get("type") == "function_call_output":
                call_id = str(payload.get("call_id") or "")
                if call_id not in call_ids:
                    continue
                output = str(payload.get("output") or "")
                process_match = process_pattern.search(output)
                process_id = process_match.group(1) if process_match else None
                return {
                    "ok": True,
                    "sessionPath": str(session_path),
                    "commandSubstring": command_substring,
                    "line": line_number,
                    "timestamp": event.get("timestamp"),
                    "call": call_ids[call_id],
                    "callId": call_id,
                    "processId": process_id,
                    "output": output,
                    "attempts": attempts,
                }
        attempts.append(
            {
                "lineCount": len(events),
                "callSeen": last_call is not None,
                "lastCall": last_call,
            }
        )
        time.sleep(2.0)

    return {
        "ok": False,
        "reason": "session-background-shell-call-not-observed",
        "sessionPath": str(session_path),
        "commandSubstring": command_substring,
        "turnId": turn_id,
        "lastCall": last_call,
        "attempts": attempts,
    }


def wait_for_session_assistant_text(
    session_path: Path | None,
    needle: str,
    *,
    timeout: float,
    after_line: int = 0,
) -> dict[str, Any]:
    if session_path is None:
        return {"ok": False, "reason": "session-path-missing", "needle": needle}

    deadline = time.time() + timeout
    attempts: list[dict[str, Any]] = []
    while time.time() < deadline:
        events = iter_session_jsonl(session_path)
        for line_number, event in events:
            if line_number <= after_line:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            text = session_message_text(payload)
            role = payload.get("role")
            if needle in text and (payload.get("type") == "agent_message" or role == "assistant"):
                return {
                    "ok": True,
                    "sessionPath": str(session_path),
                    "needle": needle,
                    "line": line_number,
                    "timestamp": event.get("timestamp"),
                    "text": text,
                    "attempts": attempts,
                }
        attempts.append({"lineCount": len(events), "needle": needle})
        time.sleep(2.0)
    return {
        "ok": False,
        "reason": "session-assistant-text-not-observed",
        "sessionPath": str(session_path),
        "needle": needle,
        "afterLine": after_line,
        "attempts": attempts,
    }


def wait_for_background_wakeup_consumed(
    session_path: Path | None,
    *,
    command_substring: str,
    timeout: float,
    after_line: int = 0,
) -> dict[str, Any]:
    if session_path is None:
        return {"ok": False, "reason": "session-path-missing", "commandSubstring": command_substring}

    deadline = time.time() + timeout
    attempts: list[dict[str, Any]] = []
    task_id_pattern = re.compile(r"Task id:\s*(\S+)")
    completion: dict[str, Any] | None = None

    while time.time() < deadline:
        events = iter_session_jsonl(session_path)
        if completion is None:
            for line_number, event in events:
                if line_number <= after_line:
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                text = session_message_text(payload)
                if (
                    payload.get("role") == "user"
                    and "Background terminal completed." in text
                    and command_substring in text
                ):
                    match = task_id_pattern.search(text)
                    completion = {
                        "line": line_number,
                        "timestamp": event.get("timestamp"),
                        "taskId": match.group(1) if match else None,
                        "text": text,
                    }
                    break
        if completion and completion.get("taskId"):
            task_id = str(completion["taskId"])
            for line_number, event in events:
                if line_number <= int(completion["line"]):
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                text = session_message_text(payload)
                role = payload.get("role")
                if task_id in text and (payload.get("type") == "agent_message" or role == "assistant"):
                    return {
                        "ok": True,
                        "sessionPath": str(session_path),
                        "commandSubstring": command_substring,
                        "completion": completion,
                        "consumption": {
                            "line": line_number,
                            "timestamp": event.get("timestamp"),
                            "text": text,
                        },
                        "attempts": attempts,
                    }
        attempts.append(
            {
                "lineCount": len(events),
                "completionSeen": completion is not None,
                "taskId": completion.get("taskId") if completion else None,
            }
        )
        time.sleep(2.0)

    return {
        "ok": False,
        "reason": "background-wakeup-not-consumed",
        "sessionPath": str(session_path),
        "commandSubstring": command_substring,
        "completion": completion,
        "afterLine": after_line,
        "attempts": attempts,
    }


def wait_for_dom_text(needles: list[str], *, timeout: float, max_chars: int = 12000) -> dict[str, Any]:
    deadline = time.time() + timeout
    attempts: list[dict[str, Any]] = []
    while time.time() < deadline:
        response = renderer_dom_text(max_chars=max_chars, timeout=20)
        result = app_control_result(response)
        text = result.get("result") if isinstance(result, dict) else result
        visible_text = str(text or "")
        missing = [needle for needle in needles if needle not in visible_text]
        attempts.append(
            {
                "ok": response.get("ok"),
                "visibleTextLength": len(visible_text),
                "missing": missing,
                "reason": response.get("reason"),
            }
        )
        if response.get("ok") and not missing:
            return {"ok": True, "visibleText": visible_text, "attempts": attempts}
        time.sleep(2.0)
    return {"ok": False, "reason": "dom-text-not-found", "needles": needles, "attempts": attempts}


def wait_for_right_panel_text(needles: list[str], *, timeout: float, max_chars: int = 12000) -> dict[str, Any]:
    deadline = time.time() + timeout
    attempts: list[dict[str, Any]] = []
    while time.time() < deadline:
        response = renderer_right_panel_text(max_chars=max_chars, timeout=20)
        result = app_control_result(response)
        text = result.get("result") if isinstance(result, dict) else result
        panel_text = str(text or "")
        missing = [needle for needle in needles if needle not in panel_text]
        attempts.append(
            {
                "ok": response.get("ok"),
                "panelTextLength": len(panel_text),
                "missing": missing,
                "reason": response.get("reason"),
                "panelText": panel_text[-1000:],
            }
        )
        if response.get("ok") and not missing:
            return {"ok": True, "panelText": panel_text, "attempts": attempts}
        time.sleep(2.0)
    return {"ok": False, "reason": "right-panel-text-not-found", "needles": needles, "attempts": attempts}


def extract_bt_ui_output_token(text: Any) -> str | None:
    match = re.search(r"\bbt-ui-output-\d+\b", str(text or ""))
    return match.group(0) if match else None


def background_shell_prompt(command: str, *, label: str) -> str:
    return (
        "请只执行一个 shell_command 工具调用，不要改写命令，不要解释步骤。\n"
        "工具调用参数必须包含 command、workdir 和 run_in_background，不要设置 timeout_ms。\n"
        "run_in_background 必须为 true，command 必须是下面的完整命令：\n"
        f"{command}\n"
        f"workdir 必须是 {HOME}。\n"
        f"工具返回后只回复 {label}。"
    )


def busy_guided_background_shell_prompt(background_command: str, foreground_command: str) -> str:
    return (
        "请严格按顺序执行两个 shell_command 工具调用，不要改写命令，不要解释步骤。\n"
        "第一个工具调用必须启动后台命令，参数必须包含 command、workdir 和 run_in_background；"
        "run_in_background 必须为 true，不要设置 timeout_ms。第一个 command 必须是：\n"
        f"{background_command}\n"
        "第一个 workdir 必须是 "
        f"{HOME}。\n"
        "第一个工具返回后，立即执行第二个 shell_command 前台命令，第二个工具调用必须包含 "
        "command、workdir 和 timeout_ms，run_in_background 必须为 false 或省略。第二个 command 必须是：\n"
        f"{foreground_command}\n"
        "第二个 workdir 必须是 "
        f"{HOME}，timeout_ms 必须至少为 70000。第二个工具返回前不要结束本轮；"
        "第二个工具返回后只回复 busy-background-started。"
    )


def real_codex_app_ui_verify() -> dict[str, Any]:
    started_at = current_millis()
    failures: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    screenshot_checks: list[dict[str, Any]] = []
    captured_steps: list[str] = []

    def add_check(name: str, payload: dict[str, Any], *, test_id: str = "T6-T9") -> dict[str, Any]:
        check = {
            "name": name,
            "testId": test_id,
            "ok": payload.get("ok") is True,
            "fatalReason": payload.get("fatalReason") or payload.get("reason"),
            "payload": payload,
        }
        checks.append(check)
        if not check["ok"]:
            failures.append(
                {
                    "testId": test_id,
                    "name": name,
                    "fatalReason": check["fatalReason"] or "ui-verification-step-failed",
                }
            )
        return payload

    def add_screenshot(step: str, payload: dict[str, Any], *, test_id: str) -> dict[str, Any]:
        screenshot_checks.append(payload)
        if payload.get("ok"):
            captured_steps.append(step)
        else:
            failures.append(
                {
                    "testId": test_id,
                    "name": f"{step}-screenshot",
                    "fatalReason": "strict-window-screenshot-failed",
                    "failures": payload.get("failures", []),
                }
            )
        return payload

    ping = add_check("app-control-ping", app_control_request("ping", timeout=20), test_id="T0")
    if ping.get("ok") is not True:
        return {
            "ok": False,
            "fatalReason": "app-control-unavailable",
            "failures": failures,
            "checks": checks,
            "screenshotChecks": screenshot_checks,
            "capturedSteps": captured_steps,
            "startedAtMs": started_at,
            "finishedAtMs": current_millis(),
        }

    main_command = "sh -lc 'printf \"bt-ui-output-%s\\n\" \"$$\"; sleep 120; printf \"bt-ui-end\\n\"'"
    start_response = add_check(
        "thread-start-real-ui-conversation",
        app_control_request(
            "start-ui-thread",
            {
                "cwd": str(HOME),
                "title": "Background terminal UI verification",
                "prompt": background_shell_prompt(main_command, label="background-started"),
                "threadSource": "user",
                "navigate": True,
            },
            timeout=120,
        ),
        test_id="T6",
    )
    start_result = app_control_result(start_response)
    thread_id = start_result.get("threadId") if isinstance(start_result, dict) else None
    session_path = response_thread_session_path(start_response)
    start_turn_id = response_turn_id(start_response)
    if not thread_id:
        failures.append({"testId": "T6", "name": "thread-id-present", "fatalReason": "thread-start-missing-thread-id"})
        return {
            "ok": False,
            "fatalReason": failures[0]["fatalReason"] if failures else "thread-start-failed",
            "failures": failures,
            "checks": checks,
            "screenshotChecks": screenshot_checks,
            "capturedSteps": captured_steps,
            "startedAtMs": started_at,
            "finishedAtMs": current_millis(),
        }

    main_launch = add_check(
        "session-jsonl-sees-background-shell-call",
        wait_for_session_shell_background(
            session_path,
            command_substring="bt-ui-output",
            timeout=900,
            turn_id=start_turn_id,
        ),
        test_id="T6",
    )
    main_launch_line = int(main_launch.get("line") or 0)
    terminal_wait = add_check(
        "native-list-sees-background-terminal",
        wait_for_background_terminal(thread_id, command_substring="bt-ui-output", timeout=180),
        test_id="T6",
    )
    terminal = terminal_wait.get("terminal") if isinstance(terminal_wait.get("terminal"), dict) else {}
    command = str(terminal.get("command") or main_command)
    process_id = str(terminal.get("processId") or terminal.get("process_id") or "")
    add_check(
        "native-list-command-title",
        {
            "ok": "bt-ui-output" in command and "sleep 120" in command,
            "command": command,
            "terminal": terminal,
        },
        test_id="T6",
    )
    output_token = extract_bt_ui_output_token(main_launch.get("output"))
    token_attempts: list[dict[str, Any]] = []
    deadline = time.time() + 45
    while output_token is None and time.time() < deadline:
        output_wait = wait_for_background_terminal(thread_id, command_substring="bt-ui-output", timeout=4)
        output_terminal = output_wait.get("terminal") if isinstance(output_wait.get("terminal"), dict) else {}
        output_token = extract_bt_ui_output_token(output_terminal.get("output"))
        token_attempts.append(
            {
                "ok": output_wait.get("ok"),
                "outputLength": len(str(output_terminal.get("output") or "")),
                "token": output_token,
                "reason": output_wait.get("reason"),
            }
        )
    add_check(
        "native-list-output-token-present",
        {
            "ok": output_token is not None,
            "outputToken": output_token,
            "initialToolOutput": main_launch.get("output"),
            "attempts": token_attempts,
        },
        test_id="T6",
    )
    add_check(
        "summary-right-panel-shows-command-title",
        wait_for_right_panel_text(["bt-ui-output"], timeout=45),
        test_id="T6",
    )
    add_screenshot(
        "T6-summary-output",
        capture_ui_step_screenshot("T6-summary-output", timeout=30, thread_id=thread_id),
        test_id="T6",
    )

    click_output_response = renderer_click_right_panel_text("bt-ui-output", timeout=20)
    click_output_result = app_control_result(click_output_response)
    click_output_inner = click_output_result.get("result") if isinstance(click_output_result, dict) else {}
    click_output = add_check(
        "open-output-tab-by-command-row",
        {
            **click_output_response,
            "ok": click_output_response.get("ok") is True
            and isinstance(click_output_inner, dict)
            and click_output_inner.get("clicked") is True,
            "reason": None
            if (
                click_output_response.get("ok") is True
                and isinstance(click_output_inner, dict)
                and click_output_inner.get("clicked") is True
            )
            else "right-panel-command-row-not-clicked",
            "clickResult": click_output_inner,
        },
        test_id="T6",
    )
    if click_output.get("ok"):
        time.sleep(2.0)
    output_needles = ["bt-ui-output"]
    if output_token is not None:
        output_needles.append(output_token)
    add_check("output-tab-dom-command-and-output", wait_for_dom_text(output_needles, timeout=30), test_id="T6")
    output_panel = wait_for_right_panel_text(output_needles, timeout=30)
    output_panel_text = str(output_panel.get("panelText") or "")
    add_check(
        "output-tab-right-panel-command-header",
        {
            **output_panel,
            "ok": output_panel.get("ok") is True
            and "任务" not in output_panel_text
            and "暂无输出" not in output_panel_text,
            "reason": None
            if (
                output_panel.get("ok") is True
                and "任务" not in output_panel_text
                and "暂无输出" not in output_panel_text
            )
            else "output-tab-command-header-not-visible",
        },
        test_id="T6",
    )
    add_screenshot(
        "T6-output-view",
        capture_ui_step_screenshot("T6-output-view", timeout=30, thread_id=thread_id),
        test_id="T6",
    )
    add_check(
        "background-start-turn-final-answer",
        wait_for_session_assistant_text(session_path, "background-started", timeout=180, after_line=main_launch_line),
        test_id="T6",
    )

    busy_command = "sh -lc 'printf \"bt-busy-start\\n\"; sleep 30; printf \"bt-busy-end\\n\"'"
    busy_foreground_command = "sh -lc 'sleep 45; printf \"bt-busy-foreground-done\\n\"'"
    busy_response = add_check(
        "busy-wakeup-start-turn",
        app_control_request(
            "start-turn",
            {
                "threadId": thread_id,
                "cwd": str(HOME),
                "prompt": busy_guided_background_shell_prompt(busy_command, busy_foreground_command),
            },
            timeout=120,
        ),
        test_id="T7",
    )
    busy_turn_id = response_turn_id(busy_response)
    busy_launch = add_check(
        "busy-session-jsonl-sees-background-shell-call",
        wait_for_session_shell_background(
            session_path,
            command_substring="bt-busy-start",
            timeout=300,
            turn_id=busy_turn_id,
        ),
        test_id="T7",
    )
    busy_launch_line = int(busy_launch.get("line") or 0)
    add_check(
        "busy-background-terminal-visible",
        wait_for_background_terminal(thread_id, command_substring="bt-busy-start", timeout=120),
        test_id="T7",
    )
    add_check(
        "busy-wakeup-consumed-by-model",
        wait_for_background_wakeup_consumed(
            session_path,
            command_substring="bt-busy-start",
            timeout=180,
            after_line=busy_launch_line,
        ),
        test_id="T7",
    )
    add_check("busy-wakeup-not-stuck-dom", wait_for_dom_text(["bt-busy-end"], timeout=60), test_id="T7")
    add_screenshot(
        "T7-busy-wakeup",
        capture_ui_step_screenshot("T7-busy-wakeup", timeout=30, thread_id=thread_id),
        test_id="T7",
    )

    add_check(
        "idle-wakeup-consumed-by-model",
        wait_for_background_wakeup_consumed(
            session_path,
            command_substring="bt-ui-output",
            timeout=240,
            after_line=main_launch_line,
        ),
        test_id="T8",
    )
    add_check("idle-wakeup-completion-dom", wait_for_dom_text(["bt-ui-end"], timeout=90), test_id="T8")
    add_screenshot(
        "T8-idle-wakeup",
        capture_ui_step_screenshot("T8-idle-wakeup", timeout=30, thread_id=thread_id),
        test_id="T8",
    )

    stop_command = "sh -lc 'printf \"bt-stop-start\\n\"; sleep 90; printf \"bt-stop-end\\n\"'"
    stop_response = add_check(
        "stop-background-terminal-start-turn",
        app_control_request(
            "start-turn",
            {
                "threadId": thread_id,
                "cwd": str(HOME),
                "prompt": background_shell_prompt(stop_command, label="stop-background-started"),
            },
            timeout=120,
        ),
        test_id="T9",
    )
    stop_turn_id = response_turn_id(stop_response)
    add_check(
        "stop-session-jsonl-sees-background-shell-call",
        wait_for_session_shell_background(
            session_path,
            command_substring="bt-stop-start",
            timeout=300,
            turn_id=stop_turn_id,
        ),
        test_id="T9",
    )
    stop_wait = add_check(
        "stop-terminal-visible",
        wait_for_background_terminal(thread_id, command_substring="bt-stop-start", timeout=120),
        test_id="T9",
    )
    stop_terminal = stop_wait.get("terminal") if isinstance(stop_wait.get("terminal"), dict) else {}
    stop_process_id = str(stop_terminal.get("processId") or stop_terminal.get("process_id") or "")
    if stop_process_id:
        add_check(
            "native-stop-background-terminal",
            app_control_request(
                "terminate-background-terminal",
                {"threadId": thread_id, "processId": stop_process_id},
                timeout=30,
            ),
            test_id="T9",
        )
    else:
        failures.append({"testId": "T9", "name": "process-id-present", "fatalReason": "background-terminal-process-id-missing"})
    add_screenshot(
        "T9-stop",
        capture_ui_step_screenshot("T9-stop", timeout=30, thread_id=thread_id),
        test_id="T9",
    )

    restart_command = "sh -lc 'printf \"bt-restart-start\\n\"; sleep 90; printf \"bt-restart-end\\n\"'"
    restart_response = add_check(
        "restart-background-terminal-through-real-turn",
        app_control_request(
            "start-turn",
            {
                "threadId": thread_id,
                "cwd": str(HOME),
                "prompt": background_shell_prompt(restart_command, label="restart-background-started"),
            },
            timeout=120,
        ),
        test_id="T9",
    )
    add_check(
        "restart-session-jsonl-sees-background-shell-call",
        wait_for_session_shell_background(
            session_path,
            command_substring="bt-restart-start",
            timeout=300,
            turn_id=None,
        ),
        test_id="T9",
    )
    add_check(
        "restart-terminal-visible",
        wait_for_background_terminal(thread_id, command_substring="bt-restart-start", timeout=120),
        test_id="T9",
    )
    add_screenshot(
        "T9-restart",
        capture_ui_step_screenshot("T9-restart", timeout=30, thread_id=thread_id),
        test_id="T9",
    )

    fatal_reason = failures[0]["fatalReason"] if failures else None
    return {
        "ok": not failures,
        "fatalReason": fatal_reason,
        "failures": failures,
        "checks": checks,
        "screenshotChecks": screenshot_checks,
        "capturedSteps": captured_steps,
        "threadId": thread_id,
        "sessionPath": str(session_path) if session_path else None,
        "mainTerminal": terminal,
        "startedAtMs": started_at,
        "finishedAtMs": current_millis(),
        "note": "This uses app-server thread/start and turn/start inside the running configured Codex App target, then combines native list checks, bounded renderer DOM checks, and strict window screenshots.",
    }


def old_implementation_clearance_report(
    status_payload: dict[str, Any] | None,
    apply_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    patch_target = analyze_app(DEFAULT_USER_APP, role="full-verify-codex-app-target")
    selected_clean_source = status_payload.get("selectedCleanSource") if isinstance(status_payload, dict) else None
    target_after = apply_payload.get("targetAfter") if isinstance(apply_payload, dict) else None
    if not isinstance(target_after, dict):
        target_after = patch_target

    checks = {
        "cleanSourceSelected": isinstance(selected_clean_source, dict)
        and bool(selected_clean_source.get("cleanSourceOk")),
        "cleanSourceOldMarkersAbsent": isinstance(selected_clean_source, dict)
        and not selected_clean_source.get("oldPatchMarkers"),
        "targetAppIsPatchTarget": patch_target.get("exists") is True
        and patch_target.get("isUserWritableTarget") is True
        and str(patch_target.get("path")) == str(DEFAULT_USER_APP),
        "targetAppOldMarkersAbsent": not target_after.get("oldPatchMarkers"),
        "targetAppAsarIntegrityOk": target_after.get("asarIntegrityOk") is True,
        "nativeMarkersPresent": target_after.get("nativePatchMarkersOk") is True,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "selectedCleanSource": selected_clean_source,
        "targetAfter": target_after,
    }


def full_verify(*, launch_timeout: float) -> dict[str, Any]:
    started_at = current_millis()
    steps: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    status_payload: dict[str, Any] | None = None
    analyze_payload: dict[str, Any] | None = None
    apply_payload: dict[str, Any] | None = None
    launch_payload: dict[str, Any] | None = None

    def add_step(test_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = {
            "testId": test_id,
            "name": name,
            "ok": payload.get("ok") is True,
            "fatalReason": payload.get("fatalReason") or payload.get("reason"),
            "payload": payload,
        }
        steps.append(step)
        if not step["ok"]:
            failures.append(
                {
                    "testId": test_id,
                    "name": name,
                    "fatalReason": step["fatalReason"] or "step-failed",
                }
            )
        return payload

    def run_step(test_id: str, name: str, fn: Any) -> dict[str, Any]:
        try:
            payload = fn()
        except ControllerError as exc:
            payload = {"ok": False, "reason": exc.reason, "message": str(exc), "details": exc.details}
        except Exception as exc:
            payload = {"ok": False, "reason": "unexpected-exception", "message": str(exc)}
        return add_step(test_id, name, payload)

    status_payload = run_step("T-1", "clean-source-and-target-status", status_report)
    analyze_payload = run_step("T-1", "native-interface-analysis", analyze_native)
    patch_target = status_payload.get("patchTargetApp") if isinstance(status_payload, dict) else None
    needs_target_prepare = (
        not isinstance(patch_target, dict)
        or patch_target.get("exists") is not True
        or bool(patch_target.get("oldPatchMarkers"))
        or patch_target.get("asarIntegrityOk") is False
    )
    restore_payload: dict[str, Any] | None = None
    if needs_target_prepare:
        restore_payload = run_step("T-1", "prepare-codex-app-target-before-apply", lambda: prepare_user_copy(yes=True))
    apply_payload = run_step("T-1", "apply-patch-to-codex-app-target", lambda: apply_patch_to_user_copy(yes=True))
    launch_payload = run_step("T0", "launch-codex-app-target-and-capture-window", lambda: launch_verify(timeout=launch_timeout))

    screenshot_checks: list[dict[str, Any]] = []
    captured_ui_steps: list[str] = []
    if isinstance(launch_payload, dict):
        strict_check = strict_window_screenshot_check(launch_payload)
        strict_check["step"] = "T0-launch"
        screenshot_checks.append(strict_check)
        if strict_check.get("ok"):
            captured_ui_steps.append("T0-launch")
        if strict_check.get("ok") is not True:
            failures.append(
                {
                    "testId": "T0",
                    "name": "strict-window-screenshot",
                    "fatalReason": "strict-window-screenshot-failed",
                }
            )

    ui_payload: dict[str, Any] | None = None
    if isinstance(launch_payload, dict) and launch_payload.get("ok") is True:
        ui_payload = run_step("T6-T9", "real-codex-app-thread-start-ui-verification", real_codex_app_ui_verify)
        for check in ui_payload.get("screenshotChecks", []) if isinstance(ui_payload.get("screenshotChecks"), list) else []:
            if isinstance(check, dict):
                screenshot_checks.append(check)
        for step in ui_payload.get("capturedSteps", []) if isinstance(ui_payload.get("capturedSteps"), list) else []:
            if step not in captured_ui_steps:
                captured_ui_steps.append(step)
    else:
        failures.append(
            {
                "testId": "T6-T9",
                "name": "real-codex-app-thread-start-ui-verification",
                "fatalReason": "launch-required-before-real-ui-verification",
            }
        )

    for test_id, scenario in FULL_VERIFY_SCENARIOS:
        run_step(test_id, f"scenario-{scenario}", lambda scenario=scenario: scenario_report(scenario))

    old_clearance = run_step("T11", "old-implementation-clearance", lambda: old_implementation_clearance_report(status_payload, apply_payload))
    missing_ui_steps = [step for step in REQUIRED_UI_SCREENSHOT_STEPS if step not in captured_ui_steps]
    screenshot_manifest = {
        "ok": not missing_ui_steps and all(check.get("ok") is True for check in screenshot_checks),
        "requiredSteps": list(REQUIRED_UI_SCREENSHOT_STEPS),
        "capturedSteps": captured_ui_steps,
        "missingSteps": missing_ui_steps,
        "checks": screenshot_checks,
        "note": "Full verification remains fail-closed until real Codex App UI screenshots cover summary/output, busy/idle wakeup, stop, and restart.",
    }
    if missing_ui_steps:
        failures.append(
            {
                "testId": "T6-T9",
                "name": "ui-screenshot-coverage",
                "fatalReason": "ui-screenshot-coverage-incomplete",
                "missingSteps": missing_ui_steps,
            }
        )

    fatal_reason = failures[0]["fatalReason"] if failures else None
    return {
        "ok": not failures,
        "fatalReason": fatal_reason,
        "failures": failures,
        "changeId": CHANGE_ID,
        "startedAtMs": started_at,
        "finishedAtMs": current_millis(),
        "level": "full-fail-closed",
        "steps": steps,
        "screenshotManifest": screenshot_manifest,
        "status": status_payload,
        "nativeAnalysis": analyze_payload,
        "restoreUserCopy": restore_payload,
        "applyPatch": apply_payload,
        "launchVerify": launch_payload,
        "realUiVerify": ui_payload,
        "oldImplementationClearance": old_clearance,
    }


def apply_package_json_framework_marker(asar_path: Path, header: dict[str, Any], data_offset: int) -> dict[str, Any]:
    rel_path = "package.json"
    original = read_asar_file(asar_path, header, data_offset, rel_path)
    package = json.loads(original.decode("utf-8"))
    expected = {
        "name": "openai-codex-electron",
        "productName": "Codex",
        "main": ".vite/build/bootstrap.js",
    }
    mismatches = {
        key: {"expected": value, "actual": package.get(key)}
        for key, value in expected.items()
        if package.get(key) != value
    }
    if mismatches:
        raise ControllerError(
            "patch-match-failed",
            "package.json did not match the expected Codex Electron package.",
            details={"target": rel_path, "mismatches": mismatches},
        )
    marker_payload = {
        "marker": PATCH_FRAMEWORK_MARKER,
        "changeId": CHANGE_ID,
        "version": 1,
    }
    already_applied = package.get("codexBackgroundTerminalPatchFramework") == marker_payload
    package["codexBackgroundTerminalPatchFramework"] = marker_payload
    updated = (json.dumps(package, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if PATCH_FRAMEWORK_MARKER.encode("utf-8") not in updated:
        raise ControllerError("patch-marker-missing", "Patch marker was not present after package.json update.")
    return {
        "target": rel_path,
        "beforeSha256": hashlib.sha256(original).hexdigest(),
        "afterSha256": hashlib.sha256(updated).hexdigest(),
        "beforeSize": len(original),
        "afterSize": len(updated),
        "alreadyApplied": already_applied,
        "content": updated,
    }


def replace_asar_text(
    asar_path: Path,
    header: dict[str, Any],
    data_offset: int,
    rel_path: str,
    before: str,
    after: str,
    *,
    step_name: str,
) -> dict[str, Any]:
    original = read_asar_file(asar_path, header, data_offset, rel_path)
    text = original.decode("utf-8")
    before_count = text.count(before)
    already_applied = after in text
    if before_count != 1 and not already_applied:
        raise ControllerError(
            "patch-match-failed",
            f"Expected exactly one ASAR text anchor for {step_name}.",
            details={"target": rel_path, "beforeCount": before_count, "step": step_name},
        )
    updated_text = text if already_applied else text.replace(before, after, 1)
    updated = updated_text.encode("utf-8")
    return {
        "name": step_name,
        "target": rel_path,
        "beforeSha256": hashlib.sha256(original).hexdigest(),
        "afterSha256": hashlib.sha256(updated).hexdigest(),
        "beforeSize": len(original),
        "afterSize": len(updated),
        "alreadyApplied": already_applied,
        "content": updated,
    }


def replace_asar_text_variants(
    asar_path: Path,
    header: dict[str, Any],
    data_offset: int,
    rel_path: str,
    variants: list[tuple[str, str]],
    *,
    step_name: str,
) -> dict[str, Any]:
    original = read_asar_file(asar_path, header, data_offset, rel_path)
    text = original.decode("utf-8")
    for _before, after in variants:
        if after in text:
            updated = text.encode("utf-8")
            return {
                "name": step_name,
                "target": rel_path,
                "beforeSha256": hashlib.sha256(original).hexdigest(),
                "afterSha256": hashlib.sha256(updated).hexdigest(),
                "beforeSize": len(original),
                "afterSize": len(updated),
                "alreadyApplied": True,
                "matchedVariant": "already-applied",
                "content": updated,
            }

    matches = [(index, before, after, text.count(before)) for index, (before, after) in enumerate(variants)]
    exact = [(index, before, after) for index, before, after, count in matches if count == 1]
    if not exact:
        raise ControllerError(
            "patch-match-failed",
            f"Expected exactly one ASAR text anchor variant for {step_name}.",
            details={
                "target": rel_path,
                "step": step_name,
                "variantCounts": [
                    {"variant": index, "beforeCount": count} for index, _before, _after, count in matches
                ],
            },
        )
    variant_index, before, after = max(exact, key=lambda candidate: len(candidate[1]))
    updated_text = text.replace(before, after, 1)
    updated = updated_text.encode("utf-8")
    return {
        "name": step_name,
        "target": rel_path,
        "beforeSha256": hashlib.sha256(original).hexdigest(),
        "afterSha256": hashlib.sha256(updated).hexdigest(),
        "beforeSize": len(original),
        "afterSize": len(updated),
        "alreadyApplied": False,
        "matchedVariant": variant_index,
        "content": updated,
    }


def replace_text_variants_in_text(
    text: str,
    rel_path: str,
    variants: list[tuple[str, str]],
    *,
    step_name: str,
) -> tuple[str, dict[str, Any]]:
    for _before, after in variants:
        if after in text:
            return text, {
                "name": step_name,
                "target": rel_path,
                "alreadyApplied": True,
                "matchedVariant": "already-applied",
            }

    matches = [(index, before, after, text.count(before)) for index, (before, after) in enumerate(variants)]
    exact = [(index, before, after) for index, before, after, count in matches if count == 1]
    if not exact:
        raise ControllerError(
            "patch-match-failed",
            f"Expected exactly one ASAR text anchor variant for {step_name}.",
            details={
                "target": rel_path,
                "step": step_name,
                "variantCounts": [
                    {"variant": index, "beforeCount": count} for index, _before, _after, count in matches
                ],
            },
        )
    variant_index, before, after = max(exact, key=lambda candidate: len(candidate[1]))
    return text.replace(before, after, 1), {
        "name": step_name,
        "target": rel_path,
        "alreadyApplied": False,
        "matchedVariant": variant_index,
    }


def apply_ctrl_b_ui_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> list[dict[str, Any]]:
    hotkey_rel = "webview/assets/app-initial~app-main~worktree-init-v2-page~remote-conversation-page~onboarding-page~hotkey-~ke3yc5wu-BLQiF1Gs.js"
    automations_rel = "webview/assets/app-initial~app-main~automations-page-Bl6HoLGr.js"
    remote_rel = "webview/assets/app-initial~app-main~remote-conversation-page~new-thread-panel-page~appgen-library-page~hot~djo67r4n-BdcfLXho.js"

    manager_clean_before = ")},!1)}getArchiveConversationContext(){"
    manager_ctrl_b_after = (
        ")},!1)}async backgroundActiveTerminal(e){let t=this.getStreamRole(e);"
        "if(t?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let n=this.conversations.get(e);"
        f"await this.sendRequest(`{CTRL_B_NATIVE_METHOD}`,{{threadId:n?.id??e,source:`user_shortcut`}})"
        "}getArchiveConversationContext(){"
    )
    manager_ctrl_b_terminate_after = (
        ")},!1)}async backgroundActiveTerminal(e){let t=this.getStreamRole(e);"
        "if(t?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let n=this.conversations.get(e);"
        f"await this.sendRequest(`{CTRL_B_NATIVE_METHOD}`,{{threadId:n?.id??e,source:`user_shortcut`}})"
        "}async terminateBackgroundTerminal(e,t){let n=this.getStreamRole(e);"
        "if(n?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let r=this.conversations.get(e);"
        f"return await this.sendRequest(`{TERMINATE_BG_NATIVE_METHOD}`,{{threadId:r?.id??e,processId:t}})"
        "}getArchiveConversationContext(){"
    )
    manager_after = (
        ")},!1)}async backgroundActiveTerminal(e){let t=this.getStreamRole(e);"
        "if(t?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let n=this.conversations.get(e);"
        f"await this.sendRequest(`{CTRL_B_NATIVE_METHOD}`,{{threadId:n?.id??e,source:`user_shortcut`}})"
        "}async listBackgroundTerminals(e,t,n){let r=this.getStreamRole(e);"
        "if(r?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let i=this.conversations.get(e);"
        f"return await this.sendRequest(`{LIST_BG_NATIVE_METHOD}`,{{threadId:i?.id??e,cursor:t??null,limit:n??50}})"
        "}async terminateBackgroundTerminal(e,t){let n=this.getStreamRole(e);"
        "if(n?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let r=this.conversations.get(e);"
        f"return await this.sendRequest(`{TERMINATE_BG_NATIVE_METHOD}`,{{threadId:r?.id??e,processId:t}})"
        "}getArchiveConversationContext(){"
    )

    command_before = (
        '"interrupt-conversation":Q7(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_ctrl_b_after = (
        command_before
        + f',"background-active-terminal":Q7(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
    )
    command_ctrl_b_terminate_after = (
        command_before
        + f',"background-active-terminal":Q7(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
        + f',"terminate-background-terminal":Q7(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
    )
    command_after = (
        command_before
        + f',"background-active-terminal":Q7(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
        + f',"{LIST_BG_ACTION}":Q7(async(e,{{conversationId:t,cursor:n,limit:r}})=>{{return await e.listBackgroundTerminals(t,n,r)}})'
        + f',"terminate-background-terminal":Q7(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
    )

    keydown_before = (
        "(0,DG.useEffect)(()=>{let e=Nl(Un.view,{b:e=>"
        "!(B_()?e.metaKey&&!e.ctrlKey:e.ctrlKey&&!e.metaKey)||e.shiftKey||e.altKey?!1:"
        "(ae(`toggleSidebar`,`composer_sidebar_shortcut`),e.preventDefault(),e.stopPropagation(),!0)});"
        "return()=>{e()}},[Un])"
    )
    keydown_after = (
        "(0,DG.useEffect)(()=>{let e=Nl(Un.view,{b:e=>"
        "!(B_()?e.metaKey&&!e.ctrlKey:e.ctrlKey&&!e.metaKey)||e.shiftKey||e.altKey?!1:"
        "(ae(`toggleSidebar`,`composer_sidebar_shortcut`),e.preventDefault(),e.stopPropagation(),!0)}),"
        "t=e=>{e.type===`keydown`&&e.key.toLowerCase()===`b`&&e.ctrlKey===!0&&e.metaKey!==!0&&e.altKey!==!0&&e.shiftKey!==!0&&H?.type===`local`&&"
        f"(e.preventDefault(),e.stopPropagation(),_o(`{CTRL_B_ACTION}`,{{conversationId:H.localConversationId}}).catch(e=>{{}}))}};"
        "return window.addEventListener(`keydown`,t,!0),()=>{e(),window.removeEventListener(`keydown`,t,!0)}},[Un,H])"
    )

    return [
        replace_asar_text_variants(
            asar_path,
            header,
            data_offset,
            hotkey_rel,
            [
                (manager_clean_before, manager_after),
                (manager_ctrl_b_after, manager_after),
                (manager_ctrl_b_terminate_after, manager_after),
            ],
            step_name="ctrl-b-conversation-manager-method",
        ),
        replace_asar_text_variants(
            asar_path,
            header,
            data_offset,
            automations_rel,
            [
                (command_before, command_after),
                (command_ctrl_b_after, command_after),
                (command_ctrl_b_terminate_after, command_after),
            ],
            step_name="ctrl-b-host-command",
        ),
        replace_asar_text(
            asar_path,
            header,
            data_offset,
            remote_rel,
            keydown_before,
            keydown_after,
            step_name="ctrl-b-global-keydown",
        ),
    ]


def apply_task005_ui_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> list[dict[str, Any]]:
    local_thread_rel = "webview/assets/local-conversation-thread-CRryh-25.js"
    original = read_asar_file(asar_path, header, data_offset, local_thread_rel)
    text = original.decode("utf-8")

    status_before = "function Sp(e,t,n){return t==null?!n||e.metrics!=null?`running`:`not-found`:t.status}"
    status_after = (
        "function Sp(e,t,n){return t==null?!n||e.metrics!=null||e.process.source===`background-terminal`"
        "?`running`:`not-found`:t.status}"
    )

    missing_pid_before = "m=!f&&!p&&o.metrics?.pid==null,h=o.process.cwd!=null&&!u&&!d&&!m"
    missing_pid_after = (
        "m=!f&&!p&&o.metrics?.pid==null&&o.process.source!==`background-terminal`,"
        "h=o.process.cwd!=null&&!u&&!d&&!m"
    )

    summary_native_list_before = (
        "let f=d,p;t[5]!==l||t[6]!==u||t[7]!==c||t[8]!==s.id||t[9]!==n||t[10]!==i?"
    )
    summary_native_list_after = (
        "let f=d,[Bt,St]=(0,By.useState)([]);"
        "(0,By.useEffect)(()=>{if(!n||i==null){St([]);return}let e=!1,t=async()=>{"
        "try{let r=await "
        f"_n(`{LIST_BG_ACTION}`,{{conversationId:i,cursor:null,limit:50}})"
        ";if(e)return;let a=Array.isArray(r?.data)?r.data:[];"
        "St(a.map(e=>({id:String(e.itemId??e.id??e.processId??`${i}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "output:String(e.output??``),startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))}catch{e||St([])}};"
        "t();let r=setInterval(t,1e3);return()=>{e=!0,clearInterval(r)}},[n,i]);"
        "Bt.length>0&&(f=[...f,...Bt.filter(e=>!f.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
        "let p;t[5]!==l||t[6]!==u||t[7]!==c||t[8]!==s.id||t[9]!==n||t[10]!==i?"
    )
    summary_native_list_after_native_wins = summary_native_list_after.replace(
        "Bt.length>0&&(f=[...f,...Bt.filter(e=>!f.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);",
        "Bt.length>0&&(f=[...Bt,...f.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);",
    )
    summary_native_list_after_without_output = summary_native_list_after.replace(
        "output:String(e.output??``),", ""
    )
    summary_native_list_after_native_wins_without_output = summary_native_list_after_native_wins.replace(
        "output:String(e.output??``),", ""
    )

    stop_function_before = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)},()=>{u(),il(f,e.process.id)}))}"
    )
    stop_function_after = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)},()=>{u(),il(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"_n(`{TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)},()=>{u(),il(f,e.process.id)}))}"
    )
    stop_function_broken_extra_brace = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)},()=>{u(),il(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"_n(`{TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)}},()=>{u(),il(f,e.process.id)}))}"
    )

    restart_function_before = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),il(f,e.process.id)}))}"
    )
    restart_function_after = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),il(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"_n(`{TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "M(e,t)},()=>{l(),il(f,e.process.id)}))}"
    )
    restart_function_broken_extra_brace = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),il(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"_n(`{TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "M(e,t)}},()=>{l(),il(f,e.process.id)}))}"
    )

    stop_disabled_before = "let O=o.metrics?.pid==null||u||d||f,k;"
    stop_disabled_after = (
        "let O=o.metrics?.pid==null&&o.process.source!==`background-terminal`||u||d||f,k;"
    )
    stop_tooltip_before = "k=o.metrics?.pid==null?(0,kp.jsx)(W,{...jp.stopMissingProcessTooltip}):void 0"
    stop_tooltip_after = (
        "k=o.metrics?.pid==null&&o.process.source!==`background-terminal`"
        "?(0,kp.jsx)(W,{...jp.stopMissingProcessTooltip}):void 0"
    )
    stop_interactive_before = "let A=o.metrics?.pid==null,j;"
    stop_interactive_after = "let A=o.metrics?.pid==null&&o.process.source!==`background-terminal`,j;"

    replacements = [
        (
            "task005-summary-native-terminal-list",
            [
                (summary_native_list_before, summary_native_list_after_native_wins),
                (summary_native_list_after, summary_native_list_after_native_wins),
                (summary_native_list_after_without_output, summary_native_list_after_native_wins),
                (summary_native_list_after_native_wins_without_output, summary_native_list_after_native_wins),
            ],
        ),
        ("task005-native-terminal-status-running", [(status_before, status_after)]),
        ("task005-native-terminal-restart-enabled", [(missing_pid_before, missing_pid_after)]),
        (
            "task005-native-terminal-stop-action",
            [(stop_function_before, stop_function_after), (stop_function_broken_extra_brace, stop_function_after)],
        ),
        (
            "task005-native-terminal-restart-action",
            [(restart_function_before, restart_function_after), (restart_function_broken_extra_brace, restart_function_after)],
        ),
        ("task005-native-terminal-stop-enabled", [(stop_disabled_before, stop_disabled_after)]),
        ("task005-native-terminal-stop-tooltip", [(stop_tooltip_before, stop_tooltip_after)]),
        ("task005-native-terminal-stop-tooltip-interactive", [(stop_interactive_before, stop_interactive_after)]),
    ]
    substeps = []
    for step_name, variants in replacements:
        text, substep = replace_text_variants_in_text(text, local_thread_rel, variants, step_name=step_name)
        substeps.append(substep)

    updated = text.encode("utf-8")
    syntax_check = javascript_syntax_check(local_thread_rel, text)
    if syntax_check.get("ok") is not True:
        raise ControllerError(
            "javascript-syntax-check-failed",
            "Patched local conversation thread bundle failed JavaScript syntax validation.",
            details=syntax_check,
        )
    return [
        {
            "name": "task005-native-terminal-controls",
            "target": local_thread_rel,
            "beforeSha256": hashlib.sha256(original).hexdigest(),
            "afterSha256": hashlib.sha256(updated).hexdigest(),
            "beforeSize": len(original),
            "afterSize": len(updated),
            "alreadyApplied": all(step["alreadyApplied"] for step in substeps),
            "substeps": substeps,
            "syntaxCheck": syntax_check,
            "action": TERMINATE_BG_ACTION,
            "method": TERMINATE_BG_NATIVE_METHOD,
            "content": updated,
        }
    ]


def app_control_bridge_js() -> str:
    return (
        "function __cbtAppControl(e){"
        f"const t=`{APP_CONTROL_MARKER}`;"
        "if(globalThis.__cbtAppControlStarted)return;"
        "globalThis.__cbtAppControlStarted=!0;"
        "let n=!1;"
        f"const r=s.default.join(o.default.homedir(),{APP_CONTROL_JS_PARTS}),"
        "i=async(e,t)=>{await d.default.mkdir(r,{recursive:!0});await d.default.writeFile(s.default.join(r,e),JSON.stringify({...t,generatedAtMs:Date.now()},null,2),`utf8`)},"
        "a=()=>e.appServerConnectionRegistry.getConnection(B),"
        "o2=async(e,t)=>{let n=e.windowManager.getPrimaryWindow();"
        "if(n==null||n.isDestroyed())return{ok:!1,reason:`primary-window-missing`};"
        "let r=await n.webContents.executeJavaScript(t,!0);return{ok:!0,result:r}},"
        "s2=async(e,t)=>await o2(e,`(()=>{const n=${JSON.stringify('${TEXT}')};"
        "const v=Array.from(document.querySelectorAll('button,[role=\"button\"],a,li,div')).filter(e=>(e.innerText||e.textContent||'').includes(n));"
        "if(v.length===0)return{clicked:false,count:0,text:document.body?.innerText?.slice(0,2000)||''};"
        "const e=v[0];e.scrollIntoView({block:'center',inline:'center'});e.click();"
        "return{clicked:true,count:v.length,text:(e.innerText||e.textContent||'').slice(0,500)}})()`"
        ".replace(JSON.stringify('${TEXT}'),JSON.stringify(t))),"
        "r2=async(e,t)=>await o2(e,`(()=>{const max=${Number.isFinite(Number(t))?Number(t):4000};"
        "const w=window.innerWidth||0;"
        "const nodes=Array.from(document.querySelectorAll('aside,[role=\"complementary\"],section,div')).map(e=>({e,r:e.getBoundingClientRect(),txt:e.innerText||e.textContent||''}))"
        ".filter(e=>e.txt.trim().length>0&&e.r.left>w*.58&&e.r.width>160&&e.r.height>80);"
        "nodes.sort((e,t)=>t.r.width*t.r.height-e.r.width*e.r.height);"
        "return nodes[0]?.txt?.slice(0,max)||''})()`),"
        "p2=async(e,t)=>await o2(e,`(()=>{const n=${JSON.stringify('${TEXT}')};"
        "const w=window.innerWidth||0;"
        "const v=Array.from(document.querySelectorAll('button,[role=\"button\"],a,li,div,span')).filter(e=>{const t=e.getBoundingClientRect();return t.left>w*.58&&t.width>5&&t.height>5&&(e.innerText||e.textContent||'').includes(n)});"
        "if(v.length===0)return{clicked:false,count:0,text:''};"
        "v.sort((e,t)=>{const n=e.getBoundingClientRect(),r=t.getBoundingClientRect();return n.width*n.height-r.width*r.height});"
        "const e=v[0];e.scrollIntoView({block:'center',inline:'center'});e.click();"
        "return{clicked:true,count:v.length,text:(e.innerText||e.textContent||'').slice(0,500)}})()`"
        ".replace(JSON.stringify('${TEXT}'),JSON.stringify(t))),"
        "c=async t=>{let n=s.default.join(r,t),c=JSON.parse(await d.default.readFile(n,`utf8`)),u=String(c.id??l.randomUUID()),f=s.default.join(r,`processing-${u}.json`);"
        "try{await d.default.rename(n,f)}catch{return}"
        "try{let n=a(),r=null,p=null,m=c.cwd??o.default.homedir();"
        "if(c.action===`ping`)r={pong:!0,marker:t};"
        "else if(c.action===`start-ui-thread`){let t=await n.startThread({cwd:m,threadSource:c.threadSource??`user`,ephemeral:c.ephemeral??!1});let a=t.thread.id;"
        "c.title&&await n.updateThreadTitle(a,c.title).catch(()=>{});"
        "if(c.navigate!==!1){let t=e.windowManager.getPrimaryWindow();t!=null&&!t.isDestroyed()&&(e.windowManager.sendMessageToWindow(t,{type:`navigate-to-route`,path:`/local/${a}`}),t.isMinimized?.()&&t.restore(),t.show(),t.focus())}"
        "if(c.startTurn!==!1&&c.prompt!=null){let e={threadId:a,input:[{type:`text`,text:String(c.prompt),text_elements:[]}],cwd:m};p=await n.startTurn(e,null)}"
        "r={threadId:a,sessionId:t.thread.sessionId,cwd:t.cwd??t.thread.cwd??m,turnId:p?.turn?.id??null,thread:t.thread,turn:p?.turn??null};}"
        "else if(c.action===`start-turn`){let e={threadId:c.threadId,input:[{type:`text`,text:String(c.prompt??``),text_elements:[]}],cwd:c.cwd??m};p=await n.startTurn(e,null);r={turnId:p?.turn?.id??null,turn:p?.turn??null}}"
        "else if(c.action===`list-background-terminals`){let e=await n.sendInternalRequest({id:`thread/backgroundTerminals/list:${l.randomUUID()}`,method:`thread/backgroundTerminals/list`,params:{threadId:c.threadId,cursor:c.cursor??null,limit:c.limit??50}});if(e.error)throw Error(e.error.message??`thread/backgroundTerminals/list failed`);r=e.result}"
        "else if(c.action===`terminate-background-terminal`){let e=await n.sendInternalRequest({id:`thread/backgroundTerminals/terminate:${l.randomUUID()}`,method:`thread/backgroundTerminals/terminate`,params:{threadId:c.threadId,processId:c.processId}});if(e.error)throw Error(e.error.message??`thread/backgroundTerminals/terminate failed`);r=e.result}"
        "else if(c.action===`clean-background-terminals`){let e=await n.sendInternalRequest({id:`thread/backgroundTerminals/clean:${l.randomUUID()}`,method:`thread/backgroundTerminals/clean`,params:{threadId:c.threadId}});if(e.error)throw Error(e.error.message??`thread/backgroundTerminals/clean failed`);r=e.result}"
        "else if(c.action===`navigate`){let t=e.windowManager.getPrimaryWindow();if(t!=null&&!t.isDestroyed()){e.windowManager.sendMessageToWindow(t,{type:`navigate-to-route`,path:c.path??`/`});t.isMinimized?.()&&t.restore();t.show();t.focus();r={navigated:!0,path:c.path??`/`}}else r={navigated:!1}}"
        "else if(c.action===`renderer-dom-text`)r=await o2(e,`(()=>document.body?.innerText?.slice(0,${Number.isFinite(Number(c.maxChars))?Number(c.maxChars):4000})||'')()`);"
        "else if(c.action===`renderer-right-panel-text`)r=await r2(e,c.maxChars);"
        "else if(c.action===`renderer-click-text`)r=await s2(e,String(c.text??``));"
        "else if(c.action===`renderer-click-right-panel-text`)r=await p2(e,String(c.text??``));"
        "else throw Error(`unknown app control action: ${c.action}`);"
        "await i(`response-${u}.json`,{ok:!0,id:u,action:c.action,result:r})}"
        "catch(e){await i(`response-${u}.json`,{ok:!1,id:u,action:c.action,errorMessage:e instanceof Error?e.message:String(e),stack:e instanceof Error?e.stack:null})}"
        "finally{await d.default.unlink(f).catch(()=>{})}},"
        "u2=async()=>{if(n)return;n=!0;try{await d.default.mkdir(r,{recursive:!0});let e=await d.default.readdir(r);for(let t of e)if(t.startsWith(`request-`)&&t.endsWith(`.json`))await c(t)}catch(e){}finally{n=!1}};"
        "u2();let f=setInterval(u2,500);f.unref?.()}"
    )


def apply_app_control_bridge_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> dict[str, Any]:
    original = read_asar_file(asar_path, header, data_offset, APP_CONTROL_MAIN_REL)
    text = original.decode("utf-8")
    substeps: list[dict[str, Any]] = []
    bridge = app_control_bridge_js()
    definition_anchor = "var IF=class{"
    if APP_CONTROL_MARKER in text:
        if (
            "renderer-right-panel-text" in text
            and "renderer-click-right-panel-text" in text
            and "a,li,div,span" in text
        ):
            substeps.append(
                {
                    "name": "app-control-bridge-definition",
                    "target": APP_CONTROL_MAIN_REL,
                    "alreadyApplied": True,
                    "matchedVariant": "already-applied",
                }
            )
        else:
            start = text.find("function __cbtAppControl(e){")
            end = text.find(definition_anchor)
            if start < 0 or end < 0 or start >= end:
                raise ControllerError(
                    "patch-match-failed",
                    "Expected an existing app-control bridge definition before the app controller class.",
                    details={
                        "target": APP_CONTROL_MAIN_REL,
                        "start": start,
                        "end": end,
                        "step": "app-control-bridge-definition-upgrade",
                    },
                )
            text = text[:start] + bridge + text[end:]
            substeps.append(
                {
                    "name": "app-control-bridge-definition",
                    "target": APP_CONTROL_MAIN_REL,
                    "alreadyApplied": False,
                    "matchedVariant": "upgrade-existing",
                }
            )
    else:
        count = text.count(definition_anchor)
        if count != 1:
            raise ControllerError(
                "patch-match-failed",
                "Expected exactly one main-process app controller class anchor.",
                details={"target": APP_CONTROL_MAIN_REL, "beforeCount": count, "step": "app-control-bridge-definition"},
            )
        text = text.replace(definition_anchor, bridge + definition_anchor, 1)
        substeps.append(
            {
                "name": "app-control-bridge-definition",
                "target": APP_CONTROL_MAIN_REL,
                "alreadyApplied": False,
                "matchedVariant": 0,
            }
        )

    call_before = (
        "this.petInstallManager=new EP({appServerClient:this.appServerClient,preferWsl:vF}),"
        "t.s({refresh:!1,preferWsl:vF,bundledRepoRoot:this.bundledSkillsRoot,appServerClient:this.appServerClient})"
        ".catch(e=>{bF().warning(`Failed to warm recommended skills cache`,{safe:{},sensitive:{error:e}})})}dispose(){"
    )
    call_after = (
        "this.petInstallManager=new EP({appServerClient:this.appServerClient,preferWsl:vF}),"
        "t.s({refresh:!1,preferWsl:vF,bundledRepoRoot:this.bundledSkillsRoot,appServerClient:this.appServerClient})"
        ".catch(e=>{bF().warning(`Failed to warm recommended skills cache`,{safe:{},sensitive:{error:e}})}),"
        "__cbtAppControl(this)}dispose(){"
    )
    if "__cbtAppControl(this)" in text:
        substeps.append(
            {
                "name": "app-control-bridge-constructor-call",
                "target": APP_CONTROL_MAIN_REL,
                "alreadyApplied": True,
                "matchedVariant": "already-applied",
            }
        )
    else:
        count = text.count(call_before)
        if count != 1:
            raise ControllerError(
                "patch-match-failed",
                "Expected exactly one main-process constructor call anchor.",
                details={"target": APP_CONTROL_MAIN_REL, "beforeCount": count, "step": "app-control-bridge-constructor-call"},
            )
        text = text.replace(call_before, call_after, 1)
        substeps.append(
            {
                "name": "app-control-bridge-constructor-call",
                "target": APP_CONTROL_MAIN_REL,
                "alreadyApplied": False,
                "matchedVariant": 0,
            }
        )

    updated = text.encode("utf-8")
    syntax_check = javascript_syntax_check(APP_CONTROL_MAIN_REL, text)
    if syntax_check.get("ok") is not True:
        raise ControllerError(
            "javascript-syntax-check-failed",
            "Patched main-process bundle failed JavaScript syntax validation.",
            details=syntax_check,
        )
    return {
        "name": "app-control-thread-start-bridge",
        "target": APP_CONTROL_MAIN_REL,
        "beforeSha256": hashlib.sha256(original).hexdigest(),
        "afterSha256": hashlib.sha256(updated).hexdigest(),
        "beforeSize": len(original),
        "afterSize": len(updated),
        "alreadyApplied": all(step["alreadyApplied"] for step in substeps),
        "substeps": substeps,
        "syntaxCheck": syntax_check,
        "marker": APP_CONTROL_MARKER,
        "content": updated,
    }


def apply_output_tab_command_header_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> dict[str, Any]:
    rel_path = "webview/assets/app-initial~app-main~automations-page-Bl6HoLGr.js"
    original = read_asar_file(asar_path, header, data_offset, rel_path)
    text = original.decode("utf-8")
    function_before = (
        "function Oce(e){let t=(0,Y6.c)(5),{conversationId:n,terminalId:r}=e,i=Os(Yc,n),a;"
        "t[0]!==r||t[1]!==i?(a=jce(i,r),t[0]=r,t[1]=i,t[2]=a):a=t[2];"
        "let o=a,s=kce(r),c=o?.aggregatedOutput??s?.buffer??``,l;"
        "return t[3]===c?l=t[4]:(l=(0,Z6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:c.length>0?(0,Z6.jsx)(Ece,{output:c}):(0,Z6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Z6.jsx)(H,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=c,t[4]=l),l}"
    )
    function_after = (
        "function Oce(e){let t=(0,Y6.c)(5),{conversationId:n,terminalId:r,command:i,output:a}=e,o=Os(Yc,n),s;"
        "t[0]!==r||t[1]!==o?(s=jce(o,r),t[0]=r,t[1]=o,t[2]=s):s=t[2];"
        "let c=s,l=kce(r),u=c?.aggregatedOutput??l?.buffer??a??``,d=c==null?i??``:Nb(c);"
        "d.length===0&&(d=i??``);let f=d.length>0?`${d}\\n${u}`:u,h;"
        "return t[3]===f?h=t[4]:(h=(0,Z6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:f.length>0?(0,Z6.jsx)(Ece,{output:f}):(0,Z6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Z6.jsx)(H,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=f,t[4]=h),h}"
    )
    function_after_without_empty_command_fallback = (
        "function Oce(e){let t=(0,Y6.c)(5),{conversationId:n,terminalId:r,command:i}=e,a=Os(Yc,n),o;"
        "t[0]!==r||t[1]!==a?(o=jce(a,r),t[0]=r,t[1]=a,t[2]=o):o=t[2];"
        "let s=o,c=kce(r),l=s?.aggregatedOutput??c?.buffer??``,u=s==null?i??``:Nb(s),d=u.length>0?`${u}\\n${l}`:l,f;"
        "return t[3]===d?f=t[4]:(f=(0,Z6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:d.length>0?(0,Z6.jsx)(Ece,{output:d}):(0,Z6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Z6.jsx)(H,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=d,t[4]=f),f}"
    )
    function_after_command_only_fallback = (
        "function Oce(e){let t=(0,Y6.c)(5),{conversationId:n,terminalId:r,command:i}=e,a=Os(Yc,n),o;"
        "t[0]!==r||t[1]!==a?(o=jce(a,r),t[0]=r,t[1]=a,t[2]=o):o=t[2];"
        "let s=o,c=kce(r),l=s?.aggregatedOutput??c?.buffer??``,u=s==null?i??``:Nb(s);"
        "u.length===0&&(u=i??``);let d=u.length>0?`${u}\\n${l}`:l,f;"
        "return t[3]===d?f=t[4]:(f=(0,Z6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:d.length>0?(0,Z6.jsx)(Ece,{output:d}):(0,Z6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Z6.jsx)(H,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=d,t[4]=f),f}"
    )
    props_before = "props:{conversationId:n,terminalId:t.id},id:`background-terminal:${n}:${t.id}`"
    props_command_after = "props:{conversationId:n,terminalId:t.id,command:t.command},id:`background-terminal:${n}:${t.id}`"
    props_after = "props:{conversationId:n,terminalId:t.id,command:t.command,output:t.output??``},id:`background-terminal:${n}:${t.id}`"
    command_before = (
        '"interrupt-conversation":Q7(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_ctrl_b_after = (
        command_before
        + f',"background-active-terminal":Q7(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
    )
    command_ctrl_b_terminate_after = (
        command_before
        + f',"background-active-terminal":Q7(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
        + f',"terminate-background-terminal":Q7(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
    )
    command_after = (
        command_before
        + f',"background-active-terminal":Q7(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
        + f',"{LIST_BG_ACTION}":Q7(async(e,{{conversationId:t,cursor:n,limit:r}})=>{{return await e.listBackgroundTerminals(t,n,r)}})'
        + f',"terminate-background-terminal":Q7(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
    )
    substeps: list[dict[str, Any]] = []
    text, function_step = replace_text_variants_in_text(
        text,
        rel_path,
        [
            (function_before, function_after),
            (function_after_without_empty_command_fallback, function_after),
            (function_after_command_only_fallback, function_after),
        ],
        step_name="output-tab-command-line-header",
    )
    substeps.append(function_step)
    text, props_step = replace_text_variants_in_text(
        text,
        rel_path,
        [(props_before, props_after), (props_command_after, props_after)],
        step_name="output-tab-command-prop",
    )
    substeps.append(props_step)
    text, command_step = replace_text_variants_in_text(
        text,
        rel_path,
        [
            (command_before, command_after),
            (command_ctrl_b_after, command_after),
            (command_ctrl_b_terminate_after, command_after),
        ],
        step_name="output-tab-preserve-background-terminal-host-commands",
    )
    substeps.append(command_step)

    updated = text.encode("utf-8")
    syntax_check = javascript_syntax_check(rel_path, text)
    if syntax_check.get("ok") is not True:
        raise ControllerError(
            "javascript-syntax-check-failed",
            "Patched output tab bundle failed JavaScript syntax validation.",
            details=syntax_check,
        )
    return {
        "name": "output-tab-command-header",
        "target": rel_path,
        "beforeSha256": hashlib.sha256(original).hexdigest(),
        "afterSha256": hashlib.sha256(updated).hexdigest(),
        "beforeSize": len(original),
        "afterSize": len(updated),
        "alreadyApplied": all(step["alreadyApplied"] for step in substeps),
        "substeps": substeps,
        "syntaxCheck": syntax_check,
        "content": updated,
    }


def update_info_plist_asar_integrity(app: Path, asar_header_sha256: str) -> dict[str, Any]:
    info_path = app_paths(app)["info"]
    info = read_plist(info_path)
    integrity = info.setdefault("ElectronAsarIntegrity", {})
    if not isinstance(integrity, dict):
        raise ControllerError("plist-integrity-invalid", "ElectronAsarIntegrity is not a dictionary.")
    entry = integrity.setdefault("Resources/app.asar", {})
    if not isinstance(entry, dict):
        raise ControllerError("plist-integrity-invalid", "Resources/app.asar integrity entry is not a dictionary.")
    previous = entry.get("hash")
    entry["algorithm"] = "SHA256"
    entry["hash"] = asar_header_sha256
    with info_path.open("wb") as handle:
        plistlib.dump(info, handle)
    return {
        "infoPlist": str(info_path),
        "previousAsarHash": previous,
        "newAsarHash": asar_header_sha256,
    }


def apply_patch_to_user_copy(*, yes: bool) -> dict[str, Any]:
    if not yes:
        raise ControllerError("confirmation-required", "Pass --yes to patch the configured Codex.app target.")
    app = DEFAULT_USER_APP
    if not path_is_user_writable_app(app):
        raise ControllerError("patch-target-not-supported", "Patch target must be the configured Codex.app target.")
    target_before = analyze_app(app, role="patch-target-before")
    if not target_before.get("exists"):
        raise ControllerError("patch-target-missing", "Configured Codex.app target does not exist.", details=target_before)
    if target_before.get("oldPatchMarkers"):
        raise ControllerError("old-patch-not-removed", "Old patch markers are still present.", details=target_before)
    build_step = build_native_binary()
    stop_report = stop_user_app(app, timeout=10)
    if stop_report.get("remainingPids"):
        raise ControllerError("app-stop-failed", "Configured Codex.app target could not be stopped before patch.", details=stop_report)
    native_step = install_native_binary(app, build_step)
    if not native_step.get("ok"):
        raise ControllerError("native-install-failed", "Patched native binary was not installed correctly.", details=native_step)
    asar_path = app_paths(app)["asar"]
    header, _header_size, data_offset = read_asar_header(asar_path)
    package_step = apply_package_json_framework_marker(asar_path, header, data_offset)
    ctrl_b_steps = apply_ctrl_b_ui_patch(asar_path, header, data_offset)
    task005_steps = apply_task005_ui_patch(asar_path, header, data_offset)
    app_control_step = apply_app_control_bridge_patch(asar_path, header, data_offset)
    output_tab_step = apply_output_tab_command_header_patch(asar_path, header, data_offset)
    replacements = {package_step["target"]: package_step["content"]}
    for step in ctrl_b_steps:
        replacements[step["target"]] = step["content"]
    for step in task005_steps:
        replacements[step["target"]] = step["content"]
    replacements[app_control_step["target"]] = app_control_step["content"]
    replacements[output_tab_step["target"]] = output_tab_step["content"]
    repack = write_asar_archive(asar_path, header, data_offset, replacements)
    plist_update = update_info_plist_asar_integrity(app, str(repack["asarHeaderSha256"]))
    quarantine = run(["xattr", "-dr", "com.apple.quarantine", str(app)], timeout=30)
    codesign_adhoc = run(["codesign", "--force", "--deep", "--sign", "-", str(app)], timeout=180)
    target_after = analyze_app(app, role="patch-target-after")
    marker_ok = PATCH_FRAMEWORK_MARKER in target_after.get("newPatchMarkers", [])
    ctrl_b_marker_ok = (
        CTRL_B_ACTION in target_after.get("newPatchMarkers", [])
        and CTRL_B_NATIVE_METHOD in target_after.get("newPatchMarkers", [])
    )
    list_marker_ok = (
        LIST_BG_ACTION in target_after.get("newPatchMarkers", [])
        and LIST_BG_NATIVE_METHOD in target_after.get("newPatchMarkers", [])
    )
    task005_marker_ok = (
        TERMINATE_BG_ACTION in target_after.get("newPatchMarkers", [])
        and TERMINATE_BG_NATIVE_METHOD in target_after.get("newPatchMarkers", [])
    )
    app_control_marker_ok = APP_CONTROL_MARKER in target_after.get("newPatchMarkers", [])
    task005_ui_after = scan_task005_ui_bindings(app)
    app_control_after = scan_app_control_bridge(app)
    native_ok = target_after.get("nativePatchMarkersOk") is True and target_after.get("codexHash") == native_step.get("afterSha256")
    return {
        "ok": target_after.get("exists") is True
        and target_after.get("isUserWritableTarget") is True
        and target_after.get("asarIntegrityOk") is True
        and marker_ok
        and ctrl_b_marker_ok
        and list_marker_ok
        and task005_marker_ok
        and app_control_marker_ok
        and task005_ui_after.get("ok") is True
        and app_control_after.get("ok") is True
        and codesign_adhoc.returncode == 0
        and native_ok,
        "changeId": CHANGE_ID,
        "generatedAtMs": current_millis(),
        "patchPlan": build_patch_plan(app),
        "steps": [
            build_step,
            native_step,
            {
                "name": "package-json-framework-marker",
                "ok": package_step["beforeSha256"] != package_step["afterSha256"] or package_step["alreadyApplied"],
                "target": package_step["target"],
                "beforeSha256": package_step["beforeSha256"],
                "afterSha256": package_step["afterSha256"],
                "beforeSize": package_step["beforeSize"],
                "afterSize": package_step["afterSize"],
                "alreadyApplied": package_step["alreadyApplied"],
                "marker": PATCH_FRAMEWORK_MARKER,
            },
            *[
                {
                    "name": step["name"],
                    "ok": step["beforeSha256"] != step["afterSha256"] or step["alreadyApplied"],
                    "target": step["target"],
                    "beforeSha256": step["beforeSha256"],
                    "afterSha256": step["afterSha256"],
                    "beforeSize": step["beforeSize"],
                    "afterSize": step["afterSize"],
                    "alreadyApplied": step["alreadyApplied"],
                    "action": CTRL_B_ACTION,
                    "method": CTRL_B_NATIVE_METHOD,
                }
                for step in ctrl_b_steps
            ],
            *[
                {
                    "name": step["name"],
                    "ok": step["beforeSha256"] != step["afterSha256"] or step["alreadyApplied"],
                    "target": step["target"],
                    "beforeSha256": step["beforeSha256"],
                    "afterSha256": step["afterSha256"],
                    "beforeSize": step["beforeSize"],
                    "afterSize": step["afterSize"],
                    "alreadyApplied": step["alreadyApplied"],
                    "substeps": step.get("substeps", []),
                    "syntaxCheck": step.get("syntaxCheck"),
                    "action": TERMINATE_BG_ACTION,
                    "method": TERMINATE_BG_NATIVE_METHOD,
                }
                for step in task005_steps
            ],
            {
                "name": app_control_step["name"],
                "ok": app_control_step["beforeSha256"] != app_control_step["afterSha256"]
                or app_control_step["alreadyApplied"],
                "target": app_control_step["target"],
                "beforeSha256": app_control_step["beforeSha256"],
                "afterSha256": app_control_step["afterSha256"],
                "beforeSize": app_control_step["beforeSize"],
                "afterSize": app_control_step["afterSize"],
                "alreadyApplied": app_control_step["alreadyApplied"],
                "substeps": app_control_step.get("substeps", []),
                "syntaxCheck": app_control_step.get("syntaxCheck"),
                "marker": APP_CONTROL_MARKER,
            },
            {
                "name": output_tab_step["name"],
                "ok": output_tab_step["beforeSha256"] != output_tab_step["afterSha256"]
                or output_tab_step["alreadyApplied"],
                "target": output_tab_step["target"],
                "beforeSha256": output_tab_step["beforeSha256"],
                "afterSha256": output_tab_step["afterSha256"],
                "beforeSize": output_tab_step["beforeSize"],
                "afterSize": output_tab_step["afterSize"],
                "alreadyApplied": output_tab_step["alreadyApplied"],
                "substeps": output_tab_step.get("substeps", []),
                "syntaxCheck": output_tab_step.get("syntaxCheck"),
            },
        ],
        "repack": repack,
        "plistUpdate": plist_update,
        "quarantineClear": quarantine.as_dict(),
        "codesignAdhoc": codesign_adhoc.as_dict(),
        "stopUserApp": stop_report,
        "targetBefore": target_before,
        "targetAfter": target_after,
        "task005UiBindingsAfter": task005_ui_after,
        "appControlBridgeAfter": app_control_after,
    }


def command_status(args: argparse.Namespace) -> int:
    payload = status_report()
    if args.write_report:
        payload["reportPath"] = str(write_report("status", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 2


def command_self_test(args: argparse.Namespace) -> int:
    payload = self_test()
    if args.write_report:
        payload["reportPath"] = str(write_report("self-test", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 1


def command_prepare_user_copy(args: argparse.Namespace) -> int:
    try:
        payload = prepare_user_copy(yes=args.yes)
    except ControllerError as exc:
        payload = {"ok": False, "reason": exc.reason, "message": str(exc), "details": exc.details}
    if args.write_report:
        payload["reportPath"] = str(write_report("prepare-user-copy", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 2


def command_launch_verify(args: argparse.Namespace) -> int:
    payload = launch_verify(timeout=args.launch_timeout)
    if args.write_report:
        payload["reportPath"] = str(write_report("launch-verify", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 2


def command_analyze_native(args: argparse.Namespace) -> int:
    try:
        payload = analyze_native()
    except ControllerError as exc:
        payload = {"ok": False, "reason": exc.reason, "message": str(exc), "details": exc.details}
    if args.write_report:
        payload["reportPath"] = str(write_report("analyze-native", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 2


def command_apply_patch(args: argparse.Namespace) -> int:
    try:
        payload = apply_patch_to_user_copy(yes=args.yes)
    except ControllerError as exc:
        payload = {"ok": False, "reason": exc.reason, "message": str(exc), "details": exc.details}
    if args.write_report:
        payload["reportPath"] = str(write_report("apply-patch", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 2


def command_scenario(args: argparse.Namespace) -> int:
    try:
        payload = scenario_report(args.scenario)
    except ControllerError as exc:
        payload = {"ok": False, "reason": exc.reason, "message": str(exc), "details": exc.details}
    if args.write_report:
        payload["reportPath"] = str(write_report(f"scenario-{args.scenario}", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 2


def command_full_verify(args: argparse.Namespace) -> int:
    payload = full_verify(launch_timeout=args.launch_timeout)
    if args.write_report:
        payload["reportPath"] = str(write_report("full-verify", payload))
    print_payload(payload, as_json=args.json)
    return 0 if payload.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex App background shell patch controller")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    parser.add_argument("--write-report", action="store_true", help="Write a report under ignored artifacts")
    parser.add_argument("--yes", action="store_true", help="Allow modifying the configured Codex.app target")
    parser.add_argument("--launch-timeout", type=float, default=60.0, help="Seconds to wait for the configured Codex.app target")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="Inspect clean source and configured Codex.app target state")
    actions.add_argument("--self-test", action="store_true", help="Run controller self-tests")
    actions.add_argument("--prepare-user-copy", action="store_true", help="Validate the configured official Codex.app target from clean source")
    actions.add_argument("--launch-verify", action="store_true", help="Verify launched app/window ownership")
    actions.add_argument("--analyze-native", action="store_true", help="Analyze native Codex interfaces and ASAR layout")
    actions.add_argument("--apply-patch", action="store_true", help="Apply audited ASAR patch steps to the configured Codex.app target")
    actions.add_argument("--scenario", choices=sorted(SCENARIO_TESTS), help="Run a background-terminal scenario verification")
    actions.add_argument("--full-verify", action="store_true", help="Run the full fail-closed verification matrix")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return command_self_test(args)
    if args.prepare_user_copy:
        return command_prepare_user_copy(args)
    if args.launch_verify:
        return command_launch_verify(args)
    if args.analyze_native:
        return command_analyze_native(args)
    if args.apply_patch:
        return command_apply_patch(args)
    if args.scenario:
        return command_scenario(args)
    if args.full_verify:
        return command_full_verify(args)
    return command_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
