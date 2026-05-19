#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


CLAUDE_SETTINGS = Path(
    os.environ.get(
        "CODEX_BLACKLIST_CLAUDE_SETTINGS",
        str(Path.home() / ".claude/settings.json"),
    )
).expanduser()
RULES_FILE = (
    Path(os.environ["CODEX_BLACKLIST_RULES_FILE"]).expanduser()
    if os.environ.get("CODEX_BLACKLIST_RULES_FILE")
    else None
)
APPROVAL_TIMEOUT_SEC = 86_340


def shell_words(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        # Fall back to a conservative split for incomplete model-generated shell.
        return re.findall(r"[^\s;&|()<>]+", text)


def normalize_word(word: str) -> str:
    return word.strip()


def word_variants(word: str) -> set[str]:
    normalized = normalize_word(word)
    variants = {normalized}
    basename = os.path.basename(normalized)
    if basename and basename != normalized:
        variants.add(basename)
    return {variant for variant in variants if variant}


def flatten_shell_words(text: str) -> list[str]:
    words: list[str] = []
    pending = [text]
    seen: set[str] = set()

    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)

        for word in shell_words(current):
            if re.fullmatch(r"[;&|()<>]+", word):
                continue
            words.append(word)
            if re.search(r"[\s;&|()<>]", word):
                pending.append(word)

    return words


def rule_phrase_from_pattern(pattern: str) -> tuple[str, ...] | None:
    words = []
    for word in shell_words(pattern.replace(":", " ")):
        stripped = word.strip("*")
        if stripped:
            words.append(stripped)
    return tuple(words) or None


def phrase_from_rule(rule: str) -> tuple[str, ...] | None:
    pattern = rule.strip()
    if pattern.startswith("Bash(") and pattern.endswith(")"):
        pattern = pattern[5:-1]
    return rule_phrase_from_pattern(pattern)


def load_rule_strings() -> list[str]:
    rules: list[str] = []

    if CLAUDE_SETTINGS.exists():
        try:
            data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
            ask_rules = data.get("permissions", {}).get("ask", [])
            if isinstance(ask_rules, list):
                rules.extend(rule for rule in ask_rules if isinstance(rule, str))
        except Exception:
            pass

    if RULES_FILE and RULES_FILE.exists():
        for line in RULES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rules.append(line)

    return rules


def load_blacklist_phrases() -> list[tuple[str, ...]]:
    phrases: list[tuple[str, ...]] = []
    for rule in load_rule_strings():
        phrase = phrase_from_rule(rule)
        if phrase:
            phrases.append(phrase)
    # Longest phrases first so the reason is as specific as possible.
    return sorted(set(phrases), key=lambda phrase: (len(phrase), phrase), reverse=True)


def normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", command).strip()


def phrase_text(phrase: tuple[str, ...]) -> str:
    return " ".join(phrase)


def phrases_text(phrases: list[tuple[str, ...]]) -> str:
    return "\n".join(f"- {phrase_text(phrase)}" for phrase in phrases)


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def highlight_range_spec(command: str, phrase: tuple[str, ...]) -> str:
    if not command or not phrase:
        return ""

    delimiters = r"\s;&|()<>'\"`"
    separator = f"[{delimiters}]+"
    token_suffix = f"(?=$|[{delimiters}])"

    word_patterns = []
    for word in phrase:
        escaped = re.escape(word)
        if "/" in word:
            word_patterns.append(escaped)
        else:
            word_patterns.append(f"(?:[^{delimiters}]+/)?{escaped}")

    phrase_pattern = separator.join(word_patterns)
    pattern = re.compile(f"(^|[{delimiters}])(?P<hit>{phrase_pattern}){token_suffix}")

    ranges = []
    for match in pattern.finditer(command):
        start, end = match.span("hit")
        ranges.append(f"{utf16_units(command[:start])}:{utf16_units(command[start:end])}")

    return ",".join(ranges)


def highlight_range_specs(command: str, phrases: list[tuple[str, ...]]) -> str:
    ranges: set[tuple[int, int]] = set()
    for phrase in phrases:
        for range_spec in highlight_range_spec(command, phrase).split(","):
            if not range_spec:
                continue
            start_text, length_text = range_spec.split(":", 1)
            ranges.add((int(start_text), int(length_text)))
    return ",".join(f"{start}:{length}" for start, length in sorted(ranges))


def project_label(payload: dict) -> str:
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return "Codex"
    return Path(cwd).name or "Codex"


def escape_applescript_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def launch_detached(argv: list[str], *, cwd: Path | None = None) -> None:
    try:
        subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def notify_approval_requested(payload: dict) -> None:
    if os.environ.get("CODEX_BLACKLIST_HOOK_NOTIFY", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return

    title = f"[{project_label(payload)}] Codex"
    body = "Codex is waiting for approval"

    script = (
        f'display notification "{escape_applescript_string(body)}" '
        f'with title "{escape_applescript_string(title)}"'
    )
    launch_detached(["/usr/bin/osascript", "-e", script])


def command_matches_phrase(command_words: list[str], phrase: tuple[str, ...]) -> bool:
    if not phrase or len(phrase) > len(command_words):
        return False

    phrase_len = len(phrase)
    for index in range(len(command_words) - phrase_len + 1):
        if all(phrase_word in word_variants(command_word)
               for phrase_word, command_word in zip(phrase, command_words[index:])):
            return True
    return False


def find_blacklisted_phrases(command: str) -> list[tuple[str, ...]]:
    command_words = flatten_shell_words(command)
    phrases = []
    for phrase in load_blacklist_phrases():
        if command_matches_phrase(command_words, phrase):
            phrases.append(phrase)
    return phrases


def prompt_user_macos_alert(payload: dict, phrases: list[tuple[str, ...]], command: str) -> bool:
    range_spec = highlight_range_specs(command, phrases)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="codex-command-",
        suffix=".txt",
        delete=False,
    ) as command_file:
        command_file.write(command or "<empty command>")
        command_path = command_file.name

    script = r'''
use framework "Foundation"
use framework "AppKit"
use scripting additions

on run argv
    set projectText to item 1 of argv
    set phraseText to item 2 of argv
    set commandPath to item 3 of argv
    set rangeSpec to item 4 of argv
    set commandText to current application's NSString's stringWithContentsOfFile:commandPath encoding:(current application's NSUTF8StringEncoding) |error|:(missing value)
    if commandText is missing value then set commandText to "<failed to read command text>"

    current application's NSApplication's sharedApplication()
    current application's NSApp's setActivationPolicy:(current application's NSApplicationActivationPolicyRegular)
    current application's NSApp's activateIgnoringOtherApps:true
    current application's NSRunningApplication's currentApplication()'s activateWithOptions:(current application's NSApplicationActivateIgnoringOtherApps)

    set alert to current application's NSAlert's alloc()'s init()
    alert's setMessageText:("Project: " & projectText)
    alert's setInformativeText:("Blacklisted command phrases detected." & return & return & "Phrases:" & return & phraseText & return & return & "Command:")
    alert's addButtonWithTitle:"Allow"
    alert's addButtonWithTitle:"Deny"

    set scrollView to current application's NSScrollView's alloc()'s initWithFrame:{{0, 0}, {760, 360}}
    scrollView's setHasVerticalScroller:true
    scrollView's setHasHorizontalScroller:false
    scrollView's setAutohidesScrollers:false

    set textView to current application's NSTextView's alloc()'s initWithFrame:{{0, 0}, {760, 360}}
    textView's setString:commandText
    textView's setEditable:false
    textView's setSelectable:true
    textView's setHorizontallyResizable:false
    textView's setVerticallyResizable:true
    textView's setMaxSize:{760, 100000}
    textView's setFont:(current application's NSFont's userFixedPitchFontOfSize:12)
    textView's setTextColor:(current application's NSColor's textColor())
    textView's setBackgroundColor:(current application's NSColor's textBackgroundColor())
    textView's textContainer()'s setContainerSize:{760, 100000}
    textView's textContainer()'s setWidthTracksTextView:true
    textView's textContainer()'s setLineBreakMode:(current application's NSLineBreakByCharWrapping)

    if rangeSpec is not "" then
        set oldDelimiters to AppleScript's text item delimiters
        set highlightColor to current application's NSColor's colorWithCalibratedRed:1.0 green:0.85 blue:0.0 alpha:0.45
        set AppleScript's text item delimiters to ","
        set rangeItems to text items of rangeSpec
        repeat with rangeItem in rangeItems
            set AppleScript's text item delimiters to ":"
            set rangeParts to text items of (rangeItem as text)
            if (count of rangeParts) is 2 then
                set rangeStart to (item 1 of rangeParts) as integer
                set rangeLength to (item 2 of rangeParts) as integer
                if rangeLength > 0 then
                    textView's textStorage()'s addAttribute:(current application's NSBackgroundColorAttributeName) value:highlightColor range:{rangeStart, rangeLength}
                end if
            end if
        end repeat
        set AppleScript's text item delimiters to oldDelimiters
    end if

    scrollView's setDocumentView:textView
    alert's setAccessoryView:scrollView

    set alertWindow to alert's |window|()
    alertWindow's setLevel:(current application's NSModalPanelWindowLevel)
    alertWindow's setHidesOnDeactivate:false
    alertWindow's makeKeyAndOrderFront:(missing value)
    current application's NSApp's activateIgnoringOtherApps:true
    current application's NSRunningApplication's currentApplication()'s activateWithOptions:(current application's NSApplicationActivateIgnoringOtherApps)

    delay 0.1
    set response to alert's runModal()
    if (response as integer) is 1000 then
        return "Allow"
    else
        return "Deny"
    end if
end run
'''

    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                script,
                project_label(payload),
                phrases_text(phrases),
                command_path,
                range_spec,
            ],
            capture_output=True,
            text=True,
            timeout=APPROVAL_TIMEOUT_SEC,
            check=False,
        )
    finally:
        try:
            Path(command_path).unlink()
        except FileNotFoundError:
            pass

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript approval failed")
    return result.stdout.strip() == "Allow"


def prompt_user(payload: dict, phrases: list[tuple[str, ...]], command: str) -> bool:
    override = os.environ.get("CODEX_BLACKLIST_HOOK_DECISION", "").strip().lower()
    if override in {"deny", "block", "no"}:
        return False

    notify_approval_requested(payload)

    return prompt_user_macos_alert(payload, phrases, command)


def emit_deny(reason: str) -> int:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    return 0


def deny_reason_phrases(phrases: list[tuple[str, ...]]) -> str:
    return ", ".join(repr(phrase_text(phrase)) for phrase in phrases)


def main() -> int:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    normalized = normalize_command(command)
    phrases = find_blacklisted_phrases(normalized)

    if phrases:
        try:
            if prompt_user(payload, phrases, command.strip() or normalized):
                return 0
        except Exception as exc:
            return emit_deny(
                "Failed to show local approval dialog for blacklisted "
                f"command phrases {deny_reason_phrases(phrases)}: {exc}"
            )

        return emit_deny(
            "Denied by local approval dialog for blacklisted command "
            f"phrases {deny_reason_phrases(phrases)}."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
