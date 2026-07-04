# 功能用法

本文按功能说明当前仓库中每个扩展的安装、配置、常用命令和排障方式。默认 Codex 配置目录是 `~/.codex`，默认 Codex App 路径是 `/Applications/Codex.app`。

## 1. 命令审批 Hook

位置：`command-approval-hook/`

这个功能通过 Codex `PreToolUse` hook 拦截 Bash 工具调用。命令命中黑名单短语时，会弹出 macOS 审批窗口；选择 `Allow` 才会继续执行，选择 `Deny`、关闭窗口、弹窗失败或脚本内部等待超时都会返回 deny。

### 安装

```bash
mkdir -p "$HOME/.codex/hooks"
cp command-approval-hook/block_blacklisted_commands.py "$HOME/.codex/hooks/"
chmod +x "$HOME/.codex/hooks/block_blacklisted_commands.py"
```

### 配置 `hooks.json`

```json
{
  "hooks": {
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
      }
    ]
  }
}
```

如果同时启用 `/rewind`，把这个 `PreToolUse` 条目和 rewind 的 `PreToolUse` 条目放在同一个数组里，不要互相覆盖。

### 黑名单规则来源

默认读取 Claude Code 风格配置：

```text
~/.claude/settings.json 的 permissions.ask
```

也可以用普通文本文件指定规则：

```bash
export CODEX_BLACKLIST_RULES_FILE="$HOME/.codex/blacklist-rules.txt"
```

规则文件每行一条，支持两种格式：

```text
Bash(git reset --hard*)
Bash(rm -rf*)
pkill -f
```

示例文件在 `command-approval-hook/blacklist-rules.example.txt`。

### 行为细节

- 规则会被拆成短语匹配，而不是执行 shell glob。
- 命中后弹窗展示项目名、命中的短语和完整命令。
- hook 外层 timeout 建议设为 `86400`；脚本内部会在更早时间返回 deny，避免外层超时后误放行。
- 当 Codex 以完全绕过审批和沙箱的模式运行时，脚本会跳过这层审批逻辑。

### 验证

```bash
python3 -m py_compile "$HOME/.codex/hooks/block_blacklisted_commands.py"
```

然后在测试线程中执行一条命中规则的 Bash 命令，确认会出现审批窗口。

## 2. Codex Rewind

位置：`codex-rewind/`

`/rewind` 提供两类能力：

- 会话回退：通过 Codex App/CLI 的 thread rollback 能力丢弃目标之后的对话 turn。
- 代码回退：通过 file-history checkpoint 恢复工具写入前的文件 preimage。

用户输入纯 `/rewind` 时，会打开本地选择窗口。第一层选择历史用户消息，第二层选择回退模式：仅对话、仅代码、对话和代码。

### 安装

```bash
mkdir -p "$HOME/.codex/bin" "$HOME/.codex/plugins"
rm -rf "$HOME/.codex/plugins/codex-rewind"
cp -R codex-rewind "$HOME/.codex/plugins/codex-rewind"
ln -sf "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind" "$HOME/.codex/bin/codex-rewind"
ln -sf "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind-patch-app" "$HOME/.codex/bin/codex-rewind-patch-app"
chmod +x "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind"
chmod +x "$HOME/.codex/plugins/codex-rewind/bin/codex-rewind-patch-app"
```

### 配置 `hooks.json`

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

`UserPromptSubmit` 负责拦截纯 `/rewind`。`PreToolUse` 负责在写文件前记录 preimage，是代码回退能力的关键。

### 常用命令

列出可回退目标：

```bash
~/.codex/bin/codex-rewind targets --cwd "$PWD"
```

预览回退影响：

```bash
~/.codex/bin/codex-rewind preview <target> --cwd "$PWD" --mode both
~/.codex/bin/codex-rewind preview <target> --cwd "$PWD" --mode session
~/.codex/bin/codex-rewind preview <target> --cwd "$PWD" --mode code
```

执行回退需要显式确认：

```bash
~/.codex/bin/codex-rewind rewind <target> --cwd "$PWD" --mode both --yes
```

手动打开 GUI：

```bash
~/.codex/bin/codex-rewind gui --cwd "$PWD"
```

只调试选择窗口、不执行回退：

```bash
~/.codex/bin/codex-rewind gui --cwd "$PWD" --no-apply
```

### 存储和空间控制

默认数据目录：

```text
~/.codex/rewind
```

文件回退模型对齐 Claude Code：每个用户 prompt 建立 snapshot，工具写入前记录相关文件 preimage。新建文件在目标 snapshot 中不存在，因此回退时会被删除。

空间策略：

- 单个 file-history 实体默认上限是 `16MB`。
- 普通 Bash 命令不解析路径；只有 `_simulatedSedEdit.filePath` 这类可预览 sed edit 会进入 file-history。
- `Write`、`Edit`、`NotebookEdit`、`apply_patch` 按显式路径或 patch header 捕获。
- 默认排除 rewind 存储自身、`.codex/backups`、`.codex/tmp`、App bundle、常见生成目录和二进制/归档后缀。
- `UserPromptSubmit` hook 每天最多自动清理一次 30 天以上的 rewind 数据。

可调环境变量：

```bash
export CODEX_REWIND_MAX_FILE_HISTORY_FILE_BYTES=$((16 * 1024 * 1024))
export CODEX_REWIND_CLEANUP_DAYS=30
export CODEX_REWIND_DISABLE_AUTO_GC=1
export CODEX_REWIND_DISABLE_EXCLUDES=1
export CODEX_REWIND_EXCLUDE_SUBSTRINGS="/some/cache:/another/path"
```

手动预览 GC：

```bash
~/.codex/bin/codex-rewind gc --cwd "$PWD"
~/.codex/bin/codex-rewind gc --all
```

实际清理：

```bash
~/.codex/bin/codex-rewind gc --cwd "$PWD" --yes
~/.codex/bin/codex-rewind gc --all --yes
```

### App 侧 `/rewind` 拦截

hook fallback 可以在 CLI 中处理纯 `/rewind`。如果要在 Codex App 中输入 `/rewind` 后直接弹出窗口，需要 patch Codex App 的 `app.asar`。

默认 App 路径：

```text
/Applications/Codex.app
```

先退出 Codex App，然后检查状态：

```bash
~/.codex/bin/codex-rewind-patch-app --dry-run
```

如果需要注入或修复签名：

```bash
~/.codex/bin/codex-rewind-patch-app
```

Codex App 不在默认位置时：

```bash
~/.codex/bin/codex-rewind-patch-app --app /path/to/Codex.app
```

脚本会：

1. 解包 `app.asar`。
2. 注入 `/rewind` prompt 拦截和 host handler。
3. 重新打包 `app.asar`。
4. 更新 Electron asar integrity。
5. 对 Codex App 做 ad-hoc codesign 并验证签名。

如果 Codex 正在运行，交互式终端会询问是否先终止 Codex 进程；非交互环境不会等待输入。

### 失败处理

- `Cannot find asar`：安装 Node/npm，或用 `--asar-cmd` 指定 asar 可执行文件。
- `codesign: invalid`：重新运行 `codex-rewind-patch-app`，不要加 `--skip-sign`。
- `/rewind` 被当作普通 prompt：检查 App 是否已 patch，或确认 `UserPromptSubmit` hook 已启用并受信任。
- 代码没有回退：说明相关文件没有被 hook 捕获，先用 `preview --mode code` 查看 file-history 动作。

## 3. 两个功能一起启用

合并后的 `hooks.json` 至少需要包含：

- `UserPromptSubmit`：rewind prompt 拦截。
- `PreToolUse` 中的 Bash 黑名单审批。
- `PreToolUse` 中的 rewind preimage 捕获。

示例见顶层 [README.md](../README.md)。Codex 首次读取 hooks 后会要求信任 hook；确认后会在本机 `config.toml` 写入 `hooks.state`，这些状态不应该提交到仓库。

## 4. 卸载

删除安装文件：

```bash
rm -f "$HOME/.codex/hooks/block_blacklisted_commands.py"
rm -f "$HOME/.codex/bin/codex-rewind" "$HOME/.codex/bin/codex-rewind-patch-app"
rm -rf "$HOME/.codex/plugins/codex-rewind"
```

然后从 `~/.codex/hooks.json` 中移除对应 hook 条目。已经 patch 过的 Codex App 会在下一次 Codex App 更新时自然被官方包覆盖；如果需要立即恢复，需要重新安装官方 Codex App。
