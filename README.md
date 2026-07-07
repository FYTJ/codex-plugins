# Codex Config

这个仓库保存可发布的本地 Codex 扩展配置。当前仓库包含这些功能：

- `command-approval-hook/`：在 Bash 工具执行前检查命令黑名单，命中时弹出 macOS 审批窗口。
- `codex-rewind/`：提供 `/rewind`，支持选择历史对话点并回退会话、工作区文件，或两者同时回退。
- `background-shell/`：提供 Codex App background shell patch 控制器和 `openai/codex` Rust/native patch 文件。

详细用法见 [docs/USAGE.md](docs/USAGE.md)。

## 适用环境

- macOS。
- Python 3。
- Codex 配置目录默认是 `~/.codex`。
- Codex App 默认安装在 `/Applications/Codex.app`。如果不在这个位置，运行 patch 脚本时传入 `--app`。
- `codex-rewind-patch-app` 需要 `asar`；脚本会优先使用系统 `asar`，否则尝试 `npx --yes @electron/asar`。

仓库中的配置示例不包含本机用户名、私有服务器、凭据、token 或真实环境路径。

## 目录结构

```text
.
├── command-approval-hook/
│   ├── block_blacklisted_commands.py
│   ├── blacklist-rules.example.txt
│   ├── config.toml.snippet
│   └── hooks.example.json
├── background-shell/
│   ├── bin/
│   ├── scripts/
│   └── README.md
├── codex-rewind/
│   ├── bin/
│   ├── commands/
│   ├── scripts/
│   ├── config.toml.snippet
│   ├── hooks.example.json
│   └── README.md
└── docs/
    └── USAGE.md
```

## 安装

在仓库根目录运行：

```bash
mkdir -p "$HOME/.codex/hooks" "$HOME/.codex/bin" "$HOME/.codex/plugins"

cp command-approval-hook/block_blacklisted_commands.py "$HOME/.codex/hooks/"
chmod +x "$HOME/.codex/hooks/block_blacklisted_commands.py"

rm -rf "$HOME/.codex/plugins/codex-rewind"
cp -R codex-rewind "$HOME/.codex/plugins/codex-rewind"
ln -sf "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind" "$HOME/.codex/bin/codex-rewind"
ln -sf "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind-patch-app" "$HOME/.codex/bin/codex-rewind-patch-app"
chmod +x "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind"
chmod +x "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind-patch-app"

rm -rf "$HOME/.codex/plugins/background-shell"
cp -R background-shell "$HOME/.codex/plugins/background-shell"
ln -sf "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-app" "$HOME/.codex/bin/codex-background-shell-patch-app"
ln -sf "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-current" "$HOME/.codex/bin/codex-background-shell-patch-current"
chmod +x "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-app"
chmod +x "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-current"
```

如果你不想覆盖已有的 `~/.codex/plugins/codex-rewind` 或 `~/.codex/plugins/background-shell`，先手动备份对应目录。

## `config.toml`

把下面片段合并到 `~/.codex/config.toml`：

```toml
[features]
hooks = true
unified_exec = false

[plugins."codex-rewind@local-personal"]
enabled = true
```

`unified_exec = false` 是 hook 兼容设置。如果你的 Codex 版本在 `unified_exec = true` 下也能稳定触发 hooks，可以按实际情况调整。

`codex-rewind@local-personal` 是本地插件源示例。如果你的 Codex 使用了不同的插件来源名称，把这一项改成本机显示的实际插件 key。只使用 hook 和 App patch 时，核心依赖是 `hooks.json` 与 `~/.codex/bin/codex-rewind`。

## `hooks.json`

把下面内容合并到 `~/.codex/hooks.json`。如果已有 hooks，不要整文件覆盖，按事件名合并数组。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.codex/bin/codex-rewind hook-user-prompt",
            "statusMessage": "Checking rewind command",
            "timeout": 3600
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.codex/hooks/block_blacklisted_commands.py",
            "statusMessage": "Checking command blacklist",
            "timeout": 86400
          }
        ]
      },
      {
        "matcher": "Bash|Write|Edit|apply_patch|ApplyPatch",
        "hooks": [
          {
            "type": "command",
            "command": "~/.codex/bin/codex-rewind hook-pre-tool",
            "statusMessage": "Capturing rewind preimage"
          }
        ]
      }
    ]
  }
}
```

`hooks.state` 和 `trusted_hash` 不需要手写。Codex 首次发现 hook 命令时会要求信任；确认后会写入本机 `config.toml`。

## 快速验证

```bash
python3 -m py_compile "$HOME/.codex/hooks/block_blacklisted_commands.py"
"$HOME/.codex/bin/codex-rewind" --help
"$HOME/.codex/bin/codex-rewind-patch-app" --help
"$HOME/.codex/bin/codex-background-shell-patch-app" --self-test --json
```

如果需要在 Codex App 里直接拦截 `/rewind`，先退出正在运行的 Codex App，然后执行：

```bash
"$HOME/.codex/bin/codex-rewind-patch-app" --dry-run
"$HOME/.codex/bin/codex-rewind-patch-app"
```

`--dry-run` 只检查是否已注入、asar integrity 和签名状态；第二条命令才会修改 `/Applications/Codex.app`。

## 更新

从仓库拉取新版本后，重新复制 `codex-rewind/`、`background-shell/` 和 hook 脚本即可：

```bash
cp command-approval-hook/block_blacklisted_commands.py "$HOME/.codex/hooks/"
rm -rf "$HOME/.codex/plugins/codex-rewind"
cp -R codex-rewind "$HOME/.codex/plugins/codex-rewind"
rm -rf "$HOME/.codex/plugins/background-shell"
cp -R background-shell "$HOME/.codex/plugins/background-shell"
```

Codex App 每次更新后都可能覆盖 `app.asar`，需要重新运行：

```bash
"$HOME/.codex/bin/codex-rewind-patch-app" --dry-run
```

若显示需要 patch，再运行不带 `--dry-run` 的命令。

background shell patch 需要重新检查并按需应用：

```bash
"$HOME/.codex/bin/codex-background-shell-patch-app" --status --json
```

若当前 Codex App bundle 已更新导致通用 ASAR 定位失效，改用当前版本兼容 wrapper，并保持 Codex 运行：

```bash
"$HOME/.codex/bin/codex-background-shell-patch-current"
```

## 发布边界

- 不提交真实 `~/.codex/config.toml`、真实 `hooks.state`、token、cookie、浏览器 profile 或 `.env`。
- 文档中只使用 `~/.codex`、`$HOME` 和 `/Applications/Codex.app` 这类可移植路径。
- `codex-rewind/` 的发布包默认自包含：`bin/` wrapper 会调用同目录下的 `scripts/`。
- `background-shell/` 不包含上游 `openai/codex` checkout、Rust `target/`、验证报告、截图、App bundle 或 DMG。
