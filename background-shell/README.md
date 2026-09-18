# Codex Background Shell

这个目录保存 Codex App background shell hook 的可发布实现。它包含安装/验证控制器和针对 `openai/codex` Rust 源码的 native patch；不包含本机验证报告、App bundle、上游源码 checkout 或 Rust 构建产物。

## 当前兼容基线

- Codex App：`26.911.61220 (9647)`
- 默认安装路径：`/Applications/ChatGPT.app`
- Bundle ID：`com.openai.codex`
- bundled CLI：`codex-cli 0.155.0-alpha.2.6`
- `openai/codex` 源码提交：`bf6f0a4ec97919bf697cdc532e7b8af4ec482fc6`（tag `rust-v0.155.0-alpha.2.6`）
- Rust 工具链：`1.95.0-aarch64-apple-darwin`

这一版适配 App build 9647：替换 `Contents/Resources/codex` native binary，并更新新版拆分后的命令提取与后台终端输出两个 ASAR bundle。摘要栏使用完整命令作为名称，详情窗口第一行显示完整命令、后续显示输出。安装前后会验证 ASAR integrity、native 标记、CLI 版本、JavaScript 语法和 codesign。脚本不会停止或重启 Codex App；如果 App 正在运行，当前进程继续使用旧 inode，新 binary 和 UI bundle 在用户下次手动完整重启 App 后生效。

## 主要文件

- `scripts/codex_background_terminal_patch_current.py`：build 9647 的主入口；识别新版拆分后的后台终端 UI，并以原子替换方式安装 native hook 与命令 UI 补丁。
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
cd "$HOME/.codex/plugins/background-shell/scripts"
mkdir -p external-sources
git clone https://github.com/openai/codex external-sources/openai-codex-0.155.0-alpha.2.6
git -C external-sources/openai-codex-0.155.0-alpha.2.6 checkout bf6f0a4ec97919bf697cdc532e7b8af4ec482fc6
git -C external-sources/openai-codex-0.155.0-alpha.2.6 apply --check ../../openai-codex-background-shell.patch
git -C external-sources/openai-codex-0.155.0-alpha.2.6 apply ../../openai-codex-background-shell.patch
```

控制器固定使用：

```text
~/.rustup/toolchains/1.95.0-aarch64-apple-darwin/bin/cargo
~/.rustup/toolchains/1.95.0-aarch64-apple-darwin/bin/rustc
```

源码目录必须保持在提交 `bf6f0a4ec97919bf697cdc532e7b8af4ec482fc6`，且工作区差异（除 `Cargo.lock`）必须与随附 patch 完全一致；控制器会 fail closed 校验这两项。

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
- 后台终端单次空 `write_stdin` 持续等待窗口为 `172800000ms`（48 小时），与 `300s` 前台自动转后台阈值相互独立。
- timeout 路径优先保留并转移原进程。
- `Ctrl+B` 可将当前可消费的前台 shell 放入后台。
- App 内置摘要和后台终端页使用 native `thread/backgroundTerminals/*` 接口展示、清理和控制任务。
- 摘要栏优先显示完整原始命令；后台终端详情第一行显示完整命令，从下一行开始显示 stdout/stderr。
- stop/restart 使用 native process id，不依赖会话历史推断残留进程。
- 后台任务完成后的唤醒与用户 follow-up 使用同一 `StartOrSteer` 路径：活动 regular turn 中进入原生 `UserInput` 队列，在当前工具调用/工具批次结束后的下一次模型采样中消费；空闲时直接启动新 turn，不等待整个会话结束。
- wakeup watchdog 只在 follow-up 已从队列取出并写入模型输入后启动；排队等待当前工具调用期间不会被误判为 `guided-message-stuck`。模型响应仍须包含唯一任务 ID，作为 delivered 判定。
- 完成通知携带命令、工作目录、退出码、输出路径和输出摘要；非零退出与 stderr 会保留。
- live process reclaim 被禁用，避免恢复会话时错误接管仍在运行的终端。

## 已验证链路

build 9647 上已经验证：

- `cargo check -p codex-core -p codex-app-server-protocol -p codex-app-server`
- `cargo test -p codex-core background --lib`（14/14）
- `cargo test -p codex-app-server background_terminal --lib`（4/4）
- release binary 构建、版本检查、native 标记、ASAR integrity 和 `codesign --verify --deep --strict`
- 两个目标 ASAR bundle 的离线重打包、幂等应用、Node 语法检查和安装后扫描
- build 9647 的真实后台成功/失败通知验证需在用户手动完整重启 Codex App 后执行

## 不进入仓库的内容

- `external-sources/openai-codex-0.155.0-alpha.2.6/` checkout
- `codex-rs/target/` 构建产物
- `background-terminal/reports/` 验证报告、截图和备份
- Codex App bundle、DMG、profile、会话、认证和本机配置

这些内容均可由固定源码提交、随附 patch 和控制器重建。
