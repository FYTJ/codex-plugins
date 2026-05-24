# Codex 本地配置

这个目录整理了三组本机 Codex 扩展配置。每组功能都放在独立子目录中，便于直接发布到 GitHub 后按需安装。

## 目录结构

- `command-approval-hook/`：命令检查 hook。拦截命中的 Bash 命令短语，弹出 macOS 确认窗口，用户选择 `Allow` 才放行。
- `turn-complete-notify/`：回合完成通知。Codex agent turn 完成后发送 macOS 通知，不包含 `iphone-notify` 或 Bark 推送。
- `codex-rewind/`：`/rewind` 本地插件。为 Codex 增加会话/代码回退选择窗口，并提供 Codex.app patch 脚本。

## 安装位置

下面假设仓库位于 `~/Documents/codex-config`，Codex 配置目录是 `~/.codex`。
脚本需要 Python 3；`turn-complete-notify` 在 Python 3.10 及更早版本下需要额外安装 `tomli`。

```bash
cd ~/Documents/codex-config
mkdir -p ~/.codex/hooks ~/.codex/bin ~/.codex/plugins

cp command-approval-hook/block_blacklisted_commands.py ~/.codex/hooks/
cp turn-complete-notify/notify_turn_ended.py ~/.codex/hooks/
cp turn-complete-notify/notify.example.toml ~/.codex/notify.toml

cp -R codex-rewind ~/.codex/plugins/codex-rewind
ln -sf ~/.codex/plugins/codex-rewind/bin/codex-rewind ~/.codex/bin/codex-rewind
ln -sf ~/.codex/plugins/codex-rewind/bin/codex-rewind-patch-app ~/.codex/bin/codex-rewind-patch-app

chmod +x ~/.codex/hooks/block_blacklisted_commands.py
chmod +x ~/.codex/hooks/notify_turn_ended.py
chmod +x ~/.codex/plugins/codex-rewind/bin/codex-rewind
chmod +x ~/.codex/plugins/codex-rewind/bin/codex-rewind-patch-app
```

如果 `~/.codex/plugins/codex-rewind` 已存在，先备份旧目录再覆盖，避免把新目录嵌套复制到旧目录里。

## `config.toml` 配置

把下面片段合并到 `~/.codex/config.toml`。`notify` 需要使用真实绝对路径，不要直接复制 `/Users/YOU`。

```toml
notify = ["/Users/YOU/.codex/hooks/notify_turn_ended.py", "turn-ended"]

[features]
hooks = true
unified_exec = false

[plugins."codex-rewind@local-personal"]
enabled = true
```

`unified_exec = false` 是本机实测的 hook 兼容设置。如果你的 Codex 版本在 `unified_exec = true` 下也能稳定触发 hooks，可以按自己的版本调整。

`codex-rewind@local-personal` 是本机本地插件源的名称。如果你的 Codex 显示的插件来源不是 `local-personal`，把这一项改成实际插件 key；只使用 hook/App patch 时，核心依赖仍然是 `hooks.json` 和 `~/.codex/bin/codex-rewind`。

## `hooks.json` 配置

把下面内容合并到 `~/.codex/hooks.json`。如果已有 hooks，不要整文件覆盖，按事件名合并数组。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/YOU/.codex/bin/codex-rewind hook-user-prompt",
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
            "command": "/usr/bin/python3 /Users/YOU/.codex/hooks/block_blacklisted_commands.py",
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
            "command": "/Users/YOU/.codex/bin/codex-rewind hook-pre-tool",
            "statusMessage": "Capturing rewind preimage"
          }
        ]
      }
    ]
  }
}
```

`hooks.state` 和 `trusted_hash` 不需要手写。Codex 首次看到 hook 命令时会要求信任，确认后会写入本机 `config.toml`。

## 功能说明

命令检查 hook 读取黑名单规则，然后检查每次 Bash 工具调用。命中后会打开一个 macOS 弹窗，窗口里展示项目名、命中的短语和完整命令文本；只有选择 `Allow` 才会继续执行，选择 `Deny` 或弹窗失败会向 Codex 返回 deny 决策。示例配置把 Codex 外层 hook timeout 设为 86400 秒，脚本内部会在 86340 秒先返回 deny，避免外层超时后放行。当 Codex 以完全访问权限运行时，脚本会直接跳过黑名单审批逻辑。默认规则来源是 `~/.claude/settings.json` 里的 `permissions.ask`，也可以设置 `CODEX_BLACKLIST_RULES_FILE` 指向普通文本规则文件。

回合完成通知使用 Codex 的 `notify` 入口，只处理 `agent-turn-complete` 事件。脚本会做短窗口去重，并用 `osascript` 发送 macOS 系统通知。这个发布版移除了 iPhone/Bark 推送路径，只保留本机通知。

`/rewind` 由两部分组成：hook fallback 和 App 原生拦截。hook 会在用户输入纯 `/rewind` 时打开本地选择窗口；pre-tool hook 会在写文件前保存 preimage。App patch 会把 Codex.app 的前端拦截逻辑注入到 `app.asar`，从而在 App 内输入 `/rewind` 时不启动 LLM turn，而是直接弹出回退窗口。

## `/rewind` App patch 和签名

Codex App 更新通常会替换：

```bash
/Applications/Codex.app/Contents/Resources/app.asar
```

因此每次 App 更新后都需要重新检查或注入 `/rewind` App 侧拦截。先退出正在运行的 Codex App，然后执行：

```bash
~/.codex/bin/codex-rewind-patch-app --dry-run
```

如果输出同时包含 `status: already patched` 和 `codesign: valid`，不需要继续操作。如果输出 `status: patch needed` 或 `codesign: invalid`，执行：

```bash
~/.codex/bin/codex-rewind-patch-app
```

如果 App 已经注入但签名需要修复，脚本会跳过 asar 重写，只重新执行 ad-hoc codesign。

脚本会完成这些动作：

1. 解包 `/Applications/Codex.app/Contents/Resources/app.asar`。
2. 注入 `codex-rewind-gui`、`codex-rewind-code` 和 `/rewind` prompt 拦截逻辑。
3. 重新打包 `app.asar`，并备份原始 `app.asar` 和 `app.asar.unpacked`。
4. 对 Codex.app 做 ad-hoc 签名：

```bash
codesign --force --deep --sign - /Applications/Codex.app
codesign --verify --deep --strict /Applications/Codex.app
```

脚本默认会自动运行上述签名和验证命令。只有明确知道后果时才使用 `--skip-sign`。如果 Codex.app 不在默认路径，使用：

```bash
~/.codex/bin/codex-rewind-patch-app --app /path/to/Codex.app
```

如果本机没有全局 `asar`，脚本会优先尝试 `npx --yes @electron/asar`。没有 Node/npm 时需要先安装 Node 环境，或者用 `--asar-cmd /path/to/asar` 指定可执行文件。

## 快速验证

```bash
python3 -m py_compile ~/.codex/hooks/block_blacklisted_commands.py
python3 -m py_compile ~/.codex/hooks/notify_turn_ended.py
~/.codex/bin/codex-rewind --help
~/.codex/bin/codex-rewind-patch-app --dry-run
```

随后重启 Codex App，在一个测试线程中输入纯 `/rewind`。预期行为是打开本地回退选择窗口，而不是把 `/rewind` 作为普通 prompt 交给 LLM。
