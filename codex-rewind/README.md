# Codex Rewind

这个本地插件提供 `/rewind`：在 Codex App 中拦截纯 `/rewind` 输入，打开本地选择窗口，然后按选择回退 Codex 会话、工作区代码，或同时回退两者。

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

Codex App 更新后，重新运行 `codex-rewind-patch-app --dry-run`。如果需要 patch，脚本会备份 `app.asar`，重新打包，更新 Electron 的 asar integrity hash，并对 `/Applications/Codex.app` 做 ad-hoc `codesign`。

## Rewind 数据结构

`/rewind` 的文件回退模型对齐 Claude Code：每个用户 prompt 建立一个 file-history snapshot，工具写入前记录相关文件的 preimage。回退代码时按目标 snapshot 恢复；如果某个文件在目标 snapshot 中不存在但后来被追踪，会使用该文件的最早版本，新建文件因此会被删除。

为了避免长会话打开弹窗变慢，`manifest.json` 只保留 checkpoint 摘要和索引；重型数据被拆到：

- `file-history-state.json`：当前被追踪文件及其版本链。
- `checkpoints/<checkpoint-id>/checkpoint.json`：单个 checkpoint 的完整 snapshot。
- `file-history/<hash>@v<version>`：实际文件备份。

目标选择窗口只读取会话 JSONL 和轻量 manifest；只有用户选择“仅代码”或“对话和代码”后，才加载对应 checkpoint 和 file-history 状态计算恢复动作。

## Codex App 更新后的处理

Codex App 更新通常会替换：

```bash
/Applications/Codex.app/Contents/Resources/app.asar
```

因此 App 侧 `/rewind` 拦截逻辑会丢失。更新 Codex App 后，需要重新执行本地 patch。

1. 退出正在运行的 Codex App。

2. 先检查当前 App 是否已经注入，并检查 asar integrity 和签名状态：

```bash
~/.codex/bin/codex-rewind-patch-app --dry-run
```

如果输出同时包含 `status: already patched`、`asar-integrity: valid` 和 `codesign: valid`，不需要继续操作。

3. 如果输出 `status: patch needed`、`asar-integrity: invalid` 或 `codesign: invalid`，运行：

```bash
~/.codex/bin/codex-rewind-patch-app
```

如果 App 已经注入但 integrity 或签名需要修复，脚本会跳过 asar 重写，只更新 `Info.plist` 中的 `ElectronAsarIntegrity` 并重新执行 ad-hoc codesign。

4. 重新启动 Codex App。

## 脚本位置

App patch 主脚本：

```bash
~/.codex/plugins/codex-rewind/scripts/codex_rewind_patch_app.py
```

命令入口：

```bash
~/.codex/bin/codex-rewind-patch-app
```

`/rewind` 后端脚本：

```bash
~/.codex/plugins/codex-rewind/scripts/codex_rewind.py
```

## 失败处理

- 如果提示 Codex 正在运行，脚本会询问是否 kill 所有 Codex App 相关进程。输入 `y` 后脚本会先尝试正常终止，必要时再强制 kill；输入 `n` 或直接回车会取消 patch。
- 如果在非交互环境运行并提示 Codex 正在运行，请改到终端中运行，或显式传入 `--allow-running`。
- 如果提示找不到 `asar`，脚本会优先使用 `npx --yes @electron/asar`；如果本机没有 `npx`，先安装 Node/npm 环境。
- 脚本会在替换前备份 `app.asar` 和 `app.asar.unpacked`，备份文件位于 `/Applications/Codex.app/Contents/Resources/`。
- patch 后脚本会更新 `ElectronAsarIntegrity` 并对 App 做 ad-hoc codesign；如果其中任一步失败，不要启动 App，先查看脚本错误输出。

## 快速验证

```bash
~/.codex/bin/codex-rewind --help
~/.codex/bin/codex-rewind-patch-app --dry-run
```

随后在 Codex App 中输入纯 `/rewind`。预期行为是直接弹出 Codex Rewind 选择窗口，而不是让 LLM 回复。
