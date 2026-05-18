# Codex Rewind

这个本地插件提供 `/rewind`：选择历史用户消息，然后回退 Codex 会话、工作区代码，或同时回退两者。

主要文件：

- `scripts/codex_rewind.py`：checkpoint、target 列表、preview、回退执行和 GUI fallback。
- `scripts/codex_rewind_appkit.swift`：macOS AppKit 原生选择窗口。
- `scripts/codex_rewind_patch_app.py`：修改 Codex.app 的 `app.asar`，让 App 在发送前拦截纯 `/rewind`。
- `bin/codex-rewind` 和 `bin/codex-rewind-patch-app`：可符号链接到 `~/.codex/bin` 的入口。

常用命令：

```bash
~/.codex/bin/codex-rewind --help
~/.codex/bin/codex-rewind targets --cwd "$PWD"
~/.codex/bin/codex-rewind gui --cwd "$PWD"
~/.codex/bin/codex-rewind-patch-app --dry-run
~/.codex/bin/codex-rewind-patch-app
```

Codex App 更新后，重新运行 `codex-rewind-patch-app --dry-run`。如果需要 patch，脚本会备份 `app.asar`，重新打包，并对 `/Applications/Codex.app` 做 ad-hoc `codesign`。
