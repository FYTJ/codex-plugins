# Codex Background Shell

这个目录保存 Codex App background shell hook 的可发布实现。它包含安装/验证控制器和针对 `openai/codex` Rust 源码的 native patch；不包含本机验证报告、App bundle、上游源码 checkout 或 Rust 构建产物。

## 当前兼容基线

- Codex App：`26.814.41407 (6720)`
- 默认安装路径：`/Applications/ChatGPT.app`
- Bundle ID：`com.openai.codex`
- bundled CLI：`codex-cli 0.148.0-alpha.15`
- `openai/codex` 源码提交：`a9ed4f154a4fad64acf538d6418d3ed012aeab86`
- Rust 工具链：`1.95.0-aarch64-apple-darwin`

这一版复用 App build 6720 自带的后台终端 UI，只替换 `Contents/Resources/codex` native binary。安装前后会验证 ASAR 哈希不变、ASAR integrity、native 标记、CLI 版本和 codesign。脚本不会停止或重启 Codex App；如果 App 正在运行，当前进程继续使用旧 inode，新 binary 在用户下次手动完整重启 App 后生效。

## 主要文件

- `scripts/codex_background_terminal_patch_current.py`：build 6720 的主入口；识别内置后台终端 UI，并以原子替换方式安装 native hook。
- `scripts/codex_background_terminal_patch_app.py`：fail-closed 基础控制器，负责源码校验、构建、备份、签名、状态和场景验证。
- `scripts/openai-codex-background-shell.patch`：基于上述固定 `openai/codex` 提交生成的 Rust/native patch。
- `bin/codex-background-shell-patch-current`：调用当前 build 兼容 wrapper 的命令入口。
- `bin/codex-background-shell-patch-app`：调用基础控制器的命令入口。
- `.gitignore`：忽略验证报告、源码 checkout、构建产物和 Python 缓存。

## 安装发布目录

在本仓库根目录运行：

```bash
mkdir -p "$HOME/.codex/plugins" "$HOME/.codex/bin"
cp -R background-shell "$HOME/.codex/plugins/background-shell"
ln -sf "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-app" "$HOME/.codex/bin/codex-background-shell-patch-app"
ln -sf "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-current" "$HOME/.codex/bin/codex-background-shell-patch-current"
chmod +x "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-app"
chmod +x "$HOME/.codex/plugins/background-shell/bin/codex-background-shell-patch-current"
```

如目标目录已经存在，请先自行备份或移走旧目录，再复制新版；不要把旧版 `external-sources/` 或验证报告混入发布提交。

## 准备 native source

控制器不会把上游源码 vendoring 到发布仓库。首次使用前，在插件目录中准备固定提交并应用随附 patch：

```bash
cd "$HOME/.codex/plugins/background-shell"
mkdir -p external-sources
git clone https://github.com/openai/codex external-sources/openai-codex-current
git -C external-sources/openai-codex-current checkout a9ed4f154a4fad64acf538d6418d3ed012aeab86
git -C external-sources/openai-codex-current apply --check ../../scripts/openai-codex-background-shell.patch
git -C external-sources/openai-codex-current apply ../../scripts/openai-codex-background-shell.patch
```

控制器固定使用：

```text
~/.rustup/toolchains/1.95.0-aarch64-apple-darwin/bin/cargo
~/.rustup/toolchains/1.95.0-aarch64-apple-darwin/bin/rustc
```

源码目录必须保持在提交 `a9ed4f154a4fad64acf538d6418d3ed012aeab86`，且工作区差异必须与随附 patch 完全一致；控制器会 fail closed 校验这两项。

## 常用命令

自测控制器：

```bash
"$HOME/.codex/bin/codex-background-shell-patch-current" --self-test --json
```

检查当前 App 与 hook 状态：

```bash
"$HOME/.codex/bin/codex-background-shell-patch-current" --status --json
```

应用当前兼容 hook：

```bash
"$HOME/.codex/bin/codex-background-shell-patch-current"
```

等价的显式命令是：

```bash
"$HOME/.codex/bin/codex-background-shell-patch-current" --apply-patch --yes --allow-running --json --write-report
```

默认目标为 `/Applications/ChatGPT.app`。如需操作其他副本，可显式传入 `--app /path/to/ChatGPT.app`。

## 功能范围

- `exec_command` 支持 `run_in_background=true`，并立即返回后台会话。
- 前台 shell 可原位转入后台；达到 `300s` 自动阈值时不会 kill 后重跑。
- timeout 路径优先保留并转移原进程。
- `Ctrl+B` 可将当前可消费的前台 shell 放入后台。
- App 内置摘要和后台终端页使用 native `thread/backgroundTerminals/*` 接口展示、清理和控制任务。
- stop/restart 使用 native process id，不依赖会话历史推断残留进程。
- 后台任务完成后支持 busy/idle 唤醒，并以模型消费任务 ID 作为 delivered 判定。
- 完成通知携带命令、工作目录、退出码、输出路径和输出摘要；非零退出与 stderr 会保留。
- live process reclaim 被禁用，避免恢复会话时错误接管仍在运行的终端。

## 已验证链路

build 6720 上已经验证：

- `cargo check -p codex-core -p codex-app-server-protocol -p codex-app-server`
- `RUST_MIN_STACK=8388608 cargo test -p codex-core --lib unified_exec`（86/86）
- release binary 构建、版本检查、native 标记、ASAR integrity 和 `codesign --verify --deep --strict`
- 重启后的真实后台成功任务：退出码 `0`，自动唤醒并完整回传 stdout
- 重启后的真实后台失败任务：退出码 `7`，自动唤醒并完整回传 stderr

## 不进入仓库的内容

- `external-sources/openai-codex-current/` checkout
- `codex-rs/target/` 构建产物
- `background-terminal/reports/` 验证报告、截图和备份
- Codex App bundle、DMG、profile、会话、认证和本机配置

这些内容均可由固定源码提交、随附 patch 和控制器重建。
