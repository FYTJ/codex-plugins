---
description: `/rewind` 在 Codex App 中由 App 发送前拦截并打开本地选择窗口；CLI 下由 UserPromptSubmit hook fallback。
argument-hint: "[targets|preview <target>|gui|rewind <target> --mode session|code|both]"
allowed-tools: [Bash]
---

# Codex Rewind

## Arguments

用户调用参数：`$ARGUMENTS`

默认交互：

- 用户在 Codex App 中直接输入纯 `/rewind` 时，App 会在 `turn/start` 前拦截该 prompt，不把它交给 LLM。
- App 会运行 `~/.codex/bin/codex-rewind gui --no-apply` 选择目标；随后在 App 进程内调用 live `thread/rollback` 回滚会话，并按需调用脚本回滚代码。
- CLI 下仍由 `UserPromptSubmit` hook 拦截纯 `/rewind`，并直接运行 `~/.codex/plugins/codex-rewind/scripts/codex_rewind.py hook-user-prompt`。
- 脚本会优先打开 Swift/AppKit 原生本地窗口：第一层选择历史对话，第二层选择 `仅对话`、`仅代码`、`对话和代码`；AppKit helper 无法编译或运行时才回退到 Tk 窗口。
- 第一层可用数字键或点击按钮选择；第二层可用 `1/2/3` 或点击按钮选择。
- 第二层左上角有返回键；`Esc` 在第二层返回第一层，在第一层 dismiss。
- 关闭窗口表示 `rewind session dismissed`。

CLI fallback：

- 空参数或 `targets`：列出当前 Codex thread 中可回退的用户对话点，并显示是否能匹配工作区 checkpoint。
- `preview <target> --mode session|code|both`：预览回退到指定用户消息提交前会发生什么。
- `gui`：手动打开同一个本地选择窗口。
- `rewind <target> --mode session|code|both`：用户明确确认后执行回退。
- `gc` / `prune`：预览 rewind 存储清理；只有带 `--yes` 时才删除文件。

兼容旧接口：

- `list`：列出当前工作区的文件 checkpoint。
- `diff <checkpoint>`：预览恢复到指定文件 checkpoint 会影响哪些文件。
- `apply <checkpoint>`：只恢复文件 checkpoint。

`mode` 含义：

- `session`：只回滚 Codex 会话和上下文记忆，需要 Codex 原生 CLI/App 或当前 thread 所属的 live app-server control socket 调用 `thread/rollback`。
- `code`：只回滚工作区文件，当前 hook+script 可直接执行。实现参考 Claude Code 的 file-history：在用户 prompt 提交前建立 checkpoint，在工具写入前记录文件 preimage。对没有 hook checkpoint 的 Codex App 既有会话，会从 thread rollout 中的成功 `apply_patch` 元数据反向恢复 add/update/delete。
- `both`：同时回滚工作区和会话；没有当前 thread 所属的 live app-server control socket 时不要执行，只报告需要原生接入。

## Preflight

1. 确认脚本存在并可执行：

```bash
~/.codex/bin/codex-rewind --help
```

2. 确认当前目录：

```bash
pwd
```

3. 代码回退不依赖 Git。恢复前后只需要报告 `codex-rewind preview/rewind` 输出里的 file-history 动作。

## Plan

- 如果用户输入纯 `/rewind`，正常情况下不会进入这里；应由 hook 直接处理。
- 如果参数为空或为 `targets`，只列出历史对话点。
- 如果参数是 `preview <target>`，只运行 preview。
- 如果参数是 `gui`，打开本地窗口。
- 如果参数是 `rewind <target>`，先运行 preview，展示会回滚的对话 turn 数和文件动作，然后问用户是否继续。
- 如果用户选择 `session` 或 `both`，先确认当前环境是否提供当前 thread 所属的 live app-server control socket；没有时停止，不要把它降级成只回滚代码，也不要用新启动的 app-server 直接改当前会话。
- 如果参数是旧接口 `diff <checkpoint>`，只运行 dry-run。
- 如果参数是旧接口 `apply <checkpoint>`，先运行 dry-run，展示会恢复/删除的文件，然后问用户是否继续。
- 没有用户明确同意时，不运行带 `--yes` 的恢复或 GC 命令。

## Commands

### Targets

默认只读：

```bash
~/.codex/bin/codex-rewind targets --cwd "$PWD"
```

### Preview

默认模式是 `both`：

```bash
~/.codex/bin/codex-rewind preview <target> --cwd "$PWD" --mode both
```

仅会话和记忆：

```bash
~/.codex/bin/codex-rewind preview <target> --cwd "$PWD" --mode session
```

仅代码：

```bash
~/.codex/bin/codex-rewind preview <target> --cwd "$PWD" --mode code
```

### Rewind

只有用户明确确认后才运行：

```bash
~/.codex/bin/codex-rewind rewind <target> --cwd "$PWD" --mode both --yes
```

### GUI

手动打开与 hook 相同的窗口：

```bash
~/.codex/bin/codex-rewind gui --cwd "$PWD"
```

仅调试 GUI 交互、不执行任何回滚：

```bash
~/.codex/bin/codex-rewind gui --cwd "$PWD" --no-apply
```

如果是原生 Codex CLI/App 接入，UI 应调用同一个后端计划，并在执行 `session` 或 `both` 时调用：

```json
{"method":"thread/rollback","params":{"threadId":"<thread-id>","numTurns":<n>}}
```

其中 `<n>` 是从选中用户消息所在 turn 开始到当前状态为止需要丢弃的 Codex turn 数；它可能大于可见用户消息数，因为中间可能存在没有用户消息的空 turn。工作区回滚使用同一 target 对应的 hook file-history snapshot。

代码回滚的覆盖范围与 Claude Code 一致地以 Codex 已知的文件修改为核心：每个用户 prompt 建立 snapshot，工具写入前把相关文件 preimage 记录到当前 snapshot；回退时按目标 snapshot 恢复 tracked files，目标 snapshot 中还没有的 tracked file 使用该文件最早版本。新建文件的 preimage 是“文件不存在”，因此回退时会删除它。App 既有会话没有 hook checkpoint 时，fallback 仍只覆盖 rollout 里记录的 `apply_patch`。

Bash 捕获范围对齐 Claude Code：普通 shell 命令不解析路径；只有 `_simulatedSedEdit.filePath` 这类可预览 sed edit 会进入 file-history。`Write`、`Edit`、`NotebookEdit`、`apply_patch` 仍按显式文件路径或 patch header 捕获。单个实体备份默认上限为 16MB，超过上限只记录 skipped 元数据；rewind 存储自身、`.codex/backups`、`.codex/tmp`、App bundle、常见生成目录和二进制/归档后缀默认排除。

### GC / Prune

默认只预览：

```bash
~/.codex/bin/codex-rewind gc --cwd "$PWD"
~/.codex/bin/codex-rewind gc --all
```

只有用户明确确认后才运行：

```bash
~/.codex/bin/codex-rewind gc --cwd "$PWD" --yes
~/.codex/bin/codex-rewind gc --all --yes
```

### Legacy File Checkpoints

```bash
~/.codex/bin/codex-rewind list --cwd "$PWD"
~/.codex/bin/codex-rewind diff <checkpoint> --cwd "$PWD"
~/.codex/bin/codex-rewind apply <checkpoint> --cwd "$PWD" --yes
```

## Verification

恢复后重新读取状态：

```bash
~/.codex/bin/codex-rewind targets --cwd "$PWD"
```

如果恢复失败，报告错误输出，不要继续做额外清理。

## Reapply App Patch After Updates

Codex App 更新后，`/Applications/Codex.app/Contents/Resources/app.asar` 内的 App 侧 `/rewind` 拦截会被新版覆盖。更新后先退出 Codex，再运行：

```bash
~/.codex/bin/codex-rewind-patch-app
```

主脚本位于：

```bash
~/.codex/plugins/codex-rewind/scripts/codex_rewind_patch_app.py
```

只检查当前 App 是否已注入，不改文件：

```bash
~/.codex/bin/codex-rewind-patch-app --dry-run
```

## Summary

用中文汇报：

- 选择的历史对话 target。
- 选择的 mode。
- dry-run 发现的影响文件。
- 是否执行了工作区恢复。
- 是否执行了 `thread/rollback`，或为什么当前环境不能执行会话回滚。
- 恢复后的 file-history 动作。

## Next Steps

如果恢复后仍有非预期修改，说明该文件没有被 hook/rollout 记录，需要用户手动处理。
