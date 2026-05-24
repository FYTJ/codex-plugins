#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


DEFAULT_APP = Path("/Applications/Codex.app")
DEFAULT_REWIND_BIN = Path.home() / ".codex" / "bin" / "codex-rewind"
PATCH_MARKERS = (
    b"codex-rewind-gui",
    b"codex-rewind-code",
    b"__codexRewindIsPrompt",
)


def run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def app_asar(app: Path) -> Path:
    return app / "Contents" / "Resources" / "app.asar"


def app_version(app: Path) -> str:
    plist = app / "Contents" / "Info.plist"
    if not plist.exists():
        return "unknown"
    try:
        with plist.open("rb") as handle:
            info = plistlib.load(handle)
        short = info.get("CFBundleShortVersionString") or "unknown"
        build = info.get("CFBundleVersion") or "unknown"
        return f"{short} ({build})"
    except Exception:
        return "unknown"


def read_pickle_payload(buf: bytes) -> memoryview:
    if len(buf) < 4:
        raise RuntimeError("Invalid asar pickle: too short")
    payload_size = struct.unpack_from("<I", buf, 0)[0]
    header_size = len(buf) - payload_size
    if header_size < 4 or header_size % 4 != 0 or payload_size < 0:
        raise RuntimeError("Invalid asar pickle header size")
    if header_size + payload_size > len(buf):
        raise RuntimeError("Invalid asar pickle payload size")
    return memoryview(buf)[header_size : header_size + payload_size]


def read_pickle_uint32(buf: bytes) -> int:
    payload = read_pickle_payload(buf)
    if len(payload) < 4:
        raise RuntimeError("Invalid asar pickle uint32 payload")
    return struct.unpack_from("<I", payload, 0)[0]


def read_pickle_string_bytes(buf: bytes) -> bytes:
    payload = read_pickle_payload(buf)
    if len(payload) < 4:
        raise RuntimeError("Invalid asar pickle string payload")
    size = struct.unpack_from("<i", payload, 0)[0]
    if size < 0 or 4 + size > len(payload):
        raise RuntimeError("Invalid asar pickle string length")
    return bytes(payload[4 : 4 + size])


def asar_header_hash(asar_path: Path) -> str:
    with asar_path.open("rb") as handle:
        size_buf = handle.read(8)
        if len(size_buf) != 8:
            raise RuntimeError(f"Cannot read asar header size from {asar_path}")
        header_size = read_pickle_uint32(size_buf)
        header_buf = handle.read(header_size)
        if len(header_buf) != header_size:
            raise RuntimeError(f"Cannot read asar header from {asar_path}")
    header_string = read_pickle_string_bytes(header_buf)
    return hashlib.sha256(header_string).hexdigest()


def expected_asar_header_hash(app: Path, asar_path: Path) -> str | None:
    plist = app / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    with plist.open("rb") as handle:
        info = plistlib.load(handle)
    key = str(asar_path.relative_to(app / "Contents"))
    entry = info.get("ElectronAsarIntegrity", {}).get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("hash")
    return value if isinstance(value, str) else None


def update_asar_integrity(app: Path, asar_path: Path) -> str:
    plist = app / "Contents" / "Info.plist"
    if not plist.exists():
        raise RuntimeError(f"Info.plist not found: {plist}")
    with plist.open("rb") as handle:
        info = plistlib.load(handle)
    key = str(asar_path.relative_to(app / "Contents"))
    actual_hash = asar_header_hash(asar_path)
    integrity = info.setdefault("ElectronAsarIntegrity", {})
    if not isinstance(integrity, dict):
        integrity = {}
        info["ElectronAsarIntegrity"] = integrity
    integrity[key] = {"algorithm": "SHA256", "hash": actual_hash}
    with plist.open("wb") as handle:
        plistlib.dump(info, handle, fmt=plistlib.FMT_XML, sort_keys=False)
    return actual_hash


def print_asar_integrity(app: Path, asar_path: Path) -> bool:
    actual = asar_header_hash(asar_path)
    expected = expected_asar_header_hash(app, asar_path)
    ok = expected == actual
    print(f"asar-integrity: {'valid' if ok else 'invalid'}")
    if not ok:
        print(f"asar-integrity-expected: {expected or 'missing'}")
        print(f"asar-integrity-actual: {actual}")
    return ok


def is_patched(asar_path: Path) -> bool:
    data = asar_path.read_bytes()
    return all(marker in data for marker in PATCH_MARKERS)


def find_asar_cmd(explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    env = os.environ.get("CODEX_REWIND_ASAR")
    if env:
        return [env]
    found = shutil.which("asar")
    if found:
        return [found]
    if shutil.which("npx"):
        return ["npx", "--yes", "@electron/asar"]
    raise RuntimeError("Cannot find asar. Install it with: npm install -g @electron/asar")


def run_asar(asar_cmd: list[str], args: list[str]) -> None:
    result = run([*asar_cmd, *args], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"asar {' '.join(args)} failed: {detail}")


def unpacked_dir_for(asar_path: Path) -> Path:
    return asar_path.with_name(f"{asar_path.name}.unpacked")


def unpack_dir_patterns(unpacked_dir: Path) -> list[str]:
    if not unpacked_dir.exists():
        return []
    patterns: list[str] = []
    node_modules = unpacked_dir / "node_modules"
    if node_modules.exists():
        for child in sorted(node_modules.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("@"):
                for scoped_child in sorted(child.iterdir()):
                    if scoped_child.is_dir():
                        patterns.append(f"node_modules/{child.name}/{scoped_child.name}")
            else:
                patterns.append(f"node_modules/{child.name}")
    for child in sorted(unpacked_dir.iterdir()):
        if child.name == "node_modules" or not child.is_dir():
            continue
        patterns.append(child.name)
    return patterns


def unpack_dir_expression(patterns: list[str]) -> str | None:
    if not patterns:
        return None
    unscoped = []
    for pattern in patterns:
        prefix = "node_modules/"
        if not pattern.startswith(prefix):
            return "node_modules"
        name = pattern[len(prefix) :]
        if "/" in name:
            return "node_modules"
        unscoped.append(name)
    if len(unscoped) == 1:
        return f"node_modules/{unscoped[0]}"
    return f"node_modules/+({'|'.join(unscoped)})"


def js_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.js") if path.is_file())


def patch_host_handlers(root: Path, rewind_bin: Path) -> Path:
    marker = '"codex-rewind-gui"'
    anchor = ',"projectless-thread-cwd":'
    insertion = (
        ',"codex-rewind-gui":async e=>await new Promise((t,n)=>{'
        f'let r=[`gui`,`--cwd`,e.cwd||process.cwd(),`--thread-id`,e.threadId||`latest`,`--no-apply`];'
        'e.rolloutPath&&r.push(`--rollout-path`,e.rolloutPath);'
        f'require(`node:child_process`).execFile(process.env.CODEX_REWIND_BIN||`{rewind_bin}`,r,'
        '{cwd:e.cwd||process.cwd(),env:{...process.env,CODEX_REWIND_GUI_NO_APPLY:`1`},maxBuffer:10485760},'
        '(e,r,i)=>{if(e){n(Error(i||e.message));return}try{t(JSON.parse(r))}catch(e){n(Error(`Invalid codex-rewind gui output: ${e instanceof Error?e.message:String(e)}`))}})'
        '}),'
        '"codex-rewind-code":async e=>await new Promise((t,n)=>{'
        'let r=[`rewind`,String(e.target||`latest`),`--mode`,`code`,`--yes`,`--cwd`,e.cwd||process.cwd(),`--thread-id`,e.threadId||`latest`];'
        'e.rolloutPath&&r.push(`--rollout-path`,e.rolloutPath);'
        f'require(`node:child_process`).execFile(process.env.CODEX_REWIND_BIN||`{rewind_bin}`,r,'
        '{cwd:e.cwd||process.cwd(),maxBuffer:10485760},'
        '(e,r,i)=>{if(e){n(Error(i||e.message));return}t({status:`applied`,stdout:r})})'
        '})'
    )

    candidates: list[Path] = []
    for path in js_files(root):
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        if marker in text:
            return path
        if '"codex-home":async' in text and anchor in text and "handleVSCodeRequest" in text:
            candidates.append(path)

    if not candidates:
        raise RuntimeError("Cannot locate Electron host handler bundle to inject codex-rewind handlers.")
    if len(candidates) > 1:
        names = ", ".join(str(path.relative_to(root)) for path in candidates[:5])
        raise RuntimeError(f"Ambiguous Electron host handler bundles: {names}")

    path = candidates[0]
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    if anchor not in text:
        raise RuntimeError(f"Cannot locate host handler insertion anchor in {path.relative_to(root)}")
    path.write_text(text.replace(anchor, insertion + anchor, 1), encoding="utf-8", errors="surrogateescape")
    return path


def find_rollback_apply_function(text: str) -> str:
    match = re.search(
        r"function\s+([A-Za-z_$][\w$]*)\([^)]*\{conversationId:[^,]+,conversationState:[^,]+,rollbackResponse:[^}]+\}\)\{",
        text,
    )
    if not match:
        raise RuntimeError("Cannot locate conversation rollback state apply function.")
    return match.group(1)


def patch_conversation_intercept(root: Path) -> Path:
    helper_marker = "__codexRewindIsPrompt"
    submit_pattern = re.compile(
        r"(async function [A-Za-z_$][\w$]*\(([^)]*)\)\{)let(\{beforeSendRequest:[A-Za-z_$][\w$]*,"
        r"inheritThreadSettings:[A-Za-z_$][\w$]*=!0,\.\.\.([A-Za-z_$][\w$]*)\}=([A-Za-z_$][\w$]*)),"
    )

    candidates: list[Path] = []
    for path in js_files(root):
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        if helper_marker in text:
            return path
        if "beforeSendRequest" in text and "thread/rollback" in text and "forkConversationFromLatest" in text:
            candidates.append(path)

    if not candidates:
        raise RuntimeError("Cannot locate app-server conversation bundle to intercept /rewind.")
    if len(candidates) > 1:
        names = ", ".join(str(path.relative_to(root)) for path in candidates[:5])
        raise RuntimeError(f"Ambiguous app-server conversation bundles: {names}")

    path = candidates[0]
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    rollback_fn = find_rollback_apply_function(text)

    fork_anchor = re.search(r"async function [A-Za-z_$][\w$]*\(e,\{sourceConversationId:", text)
    if not fork_anchor:
        raise RuntimeError(f"Cannot locate rewind helper insertion anchor in {path.relative_to(root)}")

    helper = (
        "function __codexRewindIsPrompt(e){return Array.isArray(e)&&e.length===1&&e[0]?.type===`text`&&typeof e[0].text===`string`&&e[0].text.trim()===`/rewind`}"
        "function __codexRewindNumTurns(e){let t=e?.plan?.session?.params?.numTurns??e?.session?.params?.numTurns??e?.plan?.session?.drops_user_turns,n=Number(t);if(!Number.isFinite(n)||n<1)throw Error(`Invalid rewind numTurns`);return Math.trunc(n)}"
        "async function __codexRewindHandle(e,t,n){let r=e.getConversation(t);if(!r)throw Error(`Conversation state not found`);let i=n.cwd??r.cwd??`/`,a=r.rolloutPath??null,o=await e.fetchFromHost(`codex-rewind-gui`,{params:{hostId:e.hostId,cwd:i,threadId:t,rolloutPath:a}});if(!o||o.status===`dismissed`)return{status:`dismissed`};if(o.status!==`selected`)throw Error(o.error||`rewind failed`);let s=String(o.mode||`code`),c=Number(o.target);if(s===`code`||s===`both`)await e.fetchFromHost(`codex-rewind-code`,{params:{hostId:e.hostId,cwd:i,threadId:t,rolloutPath:a,target:c}});if(s===`session`||s===`both`){let n=__codexRewindNumTurns(o),r=e.getConversation(t);if(!r)throw Error(`Conversation state not found after rewind selection`);let i=await e.sendRequest(`thread/rollback`,{threadId:t,numTurns:n});"
        f"{rollback_fn}(e,{{conversationId:t,conversationState:r,rollbackResponse:i}})"
        "}return{status:`applied`}}"
    )
    text = text[: fork_anchor.start()] + helper + text[fork_anchor.start() :]

    submit = submit_pattern.search(text)
    if not submit:
        raise RuntimeError(f"Cannot locate turn/start submit function in {path.relative_to(root)}")
    args = [item.strip() for item in submit.group(2).split(",")]
    if len(args) < 2:
        raise RuntimeError(f"Unexpected turn/start submit function args in {path.relative_to(root)}")
    manager_arg = args[0]
    conversation_arg = args[1]
    destructuring = submit.group(3)
    rest_arg = submit.group(4)
    intercept = f"if(__codexRewindIsPrompt({rest_arg}.input))return await __codexRewindHandle({manager_arg},{conversation_arg},{rest_arg});"
    text = text[: submit.start()] + submit.group(1) + "let" + destructuring + ";" + intercept + "let " + text[submit.end() :]

    path.write_text(text, encoding="utf-8", errors="surrogateescape")
    return path


def codex_is_running(app: Path) -> bool:
    result = run(["pgrep", "-x", "Codex"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        return True
    contents = app / "Contents"
    result = run(["pgrep", "-f", re.escape(str(contents))], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def verify_signature(app: Path) -> tuple[bool, str]:
    result = run(["codesign", "--verify", "--deep", "--strict", str(app)], check=False)
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail


def sign_app(app: Path) -> None:
    result = run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"codesign failed: {detail}")
    valid, detail = verify_signature(app)
    if not valid:
        raise RuntimeError(f"codesign verify failed: {detail}")


def ensure_not_running(app: Path, args: argparse.Namespace) -> None:
    if codex_is_running(app) and not args.allow_running:
        raise SystemExit(
            "Codex is currently running. Quit Codex first, or pass --allow-running if you accept that the current process will keep using the old app.asar until restart."
        )


def maybe_sign_existing_app(app: Path, asar_path: Path, args: argparse.Namespace) -> bool:
    actual_hash = update_asar_integrity(app, asar_path)
    print(f"asar-integrity: updated {actual_hash}")
    if args.skip_sign:
        print("codesign: skipped")
        return False
    ensure_not_running(app, args)
    print("codesign: ad-hoc signing app bundle...")
    sign_app(app)
    return True


def patch_app(args: argparse.Namespace) -> int:
    app = Path(args.app).expanduser().resolve()
    asar_path = app_asar(app)
    rewind_bin = Path(args.rewind_bin).expanduser().resolve()

    if not asar_path.exists():
        raise SystemExit(f"app.asar not found: {asar_path}")
    if not rewind_bin.exists():
        raise SystemExit(f"codex-rewind wrapper not found: {rewind_bin}")

    print(f"Codex.app: {app}")
    print(f"version: {app_version(app)}")
    print(f"app.asar: {asar_path}")

    if is_patched(asar_path) and not args.force:
        print("status: already patched")
        if args.dry_run:
            print_asar_integrity(app, asar_path)
            valid, detail = verify_signature(app)
            print(f"codesign: {'valid' if valid else 'invalid'}")
            if detail:
                print(f"codesign-detail: {detail}")
            print("dry-run: no files changed")
            return 0
        signed = maybe_sign_existing_app(app, asar_path, args)
        print("status: signed" if signed else "status: ready")
        return 0

    if args.dry_run:
        print("status: patch needed" if not is_patched(asar_path) else "status: already patched, --force would repatch")
        print_asar_integrity(app, asar_path)
        valid, detail = verify_signature(app)
        print(f"codesign: {'valid' if valid else 'invalid'}")
        if detail:
            print(f"codesign-detail: {detail}")
        print("dry-run: no files changed")
        return 0

    ensure_not_running(app, args)

    asar_cmd = find_asar_cmd(args.asar_cmd)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = asar_path.with_name(f"app.asar.bak.rewind-postupdate-{timestamp}")

    with tempfile.TemporaryDirectory(prefix="codex-rewind-app-patch-") as tmp:
        tmp_path = Path(tmp)
        extract_dir = tmp_path / "extract"
        patched_asar = tmp_path / "app.asar"
        unpacked_dir = unpacked_dir_for(asar_path)
        generated_unpacked = unpacked_dir_for(patched_asar)
        if not unpacked_dir.exists():
            print(f"warning: sibling unpacked resources not found: {unpacked_dir}")
            print("warning: if extraction fails, copy app.asar together with app.asar.unpacked or run against the full Codex.app bundle.")
        print("extracting app.asar...")
        run_asar(asar_cmd, ["extract", str(asar_path), str(extract_dir)])

        host_file = patch_host_handlers(extract_dir, rewind_bin)
        intercept_file = patch_conversation_intercept(extract_dir)
        print(f"patched host handlers: {host_file.relative_to(extract_dir)}")
        print(f"patched conversation intercept: {intercept_file.relative_to(extract_dir)}")

        print("packing patched app.asar...")
        pack_args = ["pack"]
        unpack_expr = unpack_dir_expression(unpack_dir_patterns(unpacked_dir))
        if unpack_expr:
            pack_args.append(f"--unpack-dir={unpack_expr}")
        pack_args.extend([str(extract_dir), str(patched_asar)])
        run_asar(asar_cmd, pack_args)

        if not is_patched(patched_asar):
            raise RuntimeError("patched app.asar does not contain required rewind markers")

        print(f"backup: {backup}")
        shutil.copy2(asar_path, backup)
        if unpacked_dir.exists():
            unpacked_backup = backup.with_name(f"{backup.name}.unpacked")
            print(f"backup unpacked: {unpacked_backup}")
            if unpacked_backup.exists():
                shutil.rmtree(unpacked_backup)
            shutil.copytree(unpacked_dir, unpacked_backup, symlinks=True)
        os.replace(patched_asar, asar_path)
        if generated_unpacked.exists():
            if unpacked_dir.exists():
                shutil.rmtree(unpacked_dir)
            shutil.move(str(generated_unpacked), str(unpacked_dir))

    actual_hash = update_asar_integrity(app, asar_path)
    print(f"asar-integrity: updated {actual_hash}")

    if args.skip_sign:
        print("codesign: skipped")
    else:
        print("codesign: ad-hoc signing app bundle...")
        sign_app(app)

    print("status: patched")
    print("restart Codex to load the updated /rewind App integration")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reapply the local Codex /rewind App integration after Codex.app updates.")
    parser.add_argument("--app", default=str(DEFAULT_APP), help="Path to Codex.app")
    parser.add_argument("--rewind-bin", default=str(DEFAULT_REWIND_BIN), help="Path to codex-rewind wrapper")
    parser.add_argument("--asar-cmd", default=None, help="Explicit asar executable. Defaults to asar, then npx --yes @electron/asar.")
    parser.add_argument("--dry-run", action="store_true", help="Check patch status without changing files")
    parser.add_argument("--force", action="store_true", help="Repatch even when markers already exist")
    parser.add_argument("--allow-running", action="store_true", help="Allow replacing app.asar while Codex is running")
    parser.add_argument("--skip-sign", action="store_true", help="Do not ad-hoc codesign the app after patching")
    args = parser.parse_args()
    try:
        return patch_app(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
