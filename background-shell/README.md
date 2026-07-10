# Codex Background Shell

这个目录保存 Codex App background shell patch 的可发布实现。它包含 patch 控制器脚本和对 `openai/codex` Rust 源码的补丁文件，不包含本机验证报告、截图、App bundle、DMG、会话、缓存或 Rust `target/` 构建产物。

## 主要文件

- `scripts/codex_background_terminal_patch_app.py`：fail-closed patch/verify 控制器。它负责解析 clean source、验证官方 Codex.app 目标、构建 patched native binary、修改 App ASAR、更新 Electron ASAR integrity、ad-hoc 签名、启动验证和完整场景验证。
- `scripts/codex_background_terminal_patch_current.py`：当前 Codex App bundle 兼容 wrapper，用于适配更新后的 ASAR 入口和 minified symbol。
- `scripts/openai-codex-background-shell.patch`：基于 `openai/codex` 的 `rust-v0.144.0-alpha.4` tag 生成的 Rust/native patch。控制器构建 native binary 时要求该 patch 已应用到对应源码副本。
- `bin/codex-background-shell-patch-app`：可符号链接到 `~/.codex/bin` 的命令入口。
- `bin/codex-background-shell-patch-current`：调用当前 Codex App 兼容 wrapper 的命令入口。
- `.gitignore`：忽略运行时生成的 `background-terminal/` 报告目录、`external-sources/` 源码 checkout 和 Python 缓存。

## 安装

在本仓库根目录运行安装命令。

```bash
mkdir -p ~/.codex/plugins ~/.codex/bin
cp -R background-shell ~/.codex/plugins/background-shell
ln -sf ~/.codex/plugins/background-shell/bin/codex-background-shell-patch-app ~/.codex/bin/codex-background-shell-patch-app
ln -sf ~/.codex/plugins/background-shell/bin/codex-background-shell-patch-current ~/.codex/bin/codex-background-shell-patch-current
chmod +x ~/.codex/plugins/background-shell/bin/codex-background-shell-patch-app
chmod +x ~/.codex/plugins/background-shell/bin/codex-background-shell-patch-current
```

## 准备 native source

控制器不会把上游源码 vendoring 到发布仓库。首次使用前需要在插件目录内 clone `openai/codex`，并应用随附 patch：

```bash
cd ~/.codex/plugins/background-shell
mkdir -p external-sources
git clone --branch rust-v0.144.0-alpha.4 --single-branch https://github.com/openai/codex external-sources/openai-codex-rust-v0.144.0-alpha.4
cd external-sources/openai-codex-rust-v0.144.0-alpha.4
git apply --check ../../scripts/openai-codex-background-shell.patch
git apply ../../scripts/openai-codex-background-shell.patch
```

当前兼容基线为 Codex App `26.707.31428 (5059)`，其 bundled native CLI 为 `codex-cli 0.144.0-alpha.4`。控制器同时兼容 Electron 的 `bootstrap.js` 与 `early-bootstrap.js` 入口，并在 App 正在运行时使用原子替换安装 native binary；新代码在完整退出并重新打开 Codex 后生效。

控制器默认使用 `PATH` 中的 `cargo` 和 `rustc`：

```text
cargo
rustc
```

如果需要固定工具链目录，可在运行前设置 `CODEX_BACKGROUND_SHELL_RUST_TOOLCHAIN` 指向包含 `cargo` 和 `rustc` 的目录；也可以分别设置 `CARGO` 和 `RUSTC`。

## 常用命令

先运行自测：

```bash
~/.codex/bin/codex-background-shell-patch-app --self-test --json
```

检查 clean source 和官方 Codex.app 目标状态：

```bash
~/.codex/bin/codex-background-shell-patch-app --status --json
```

验证官方 Codex.app 目标：

```bash
~/.codex/bin/codex-background-shell-patch-app --prepare-user-copy --yes --json --write-report
```

应用 patch 到官方 Codex.app 目标：

```bash
~/.codex/bin/codex-background-shell-patch-app --apply-patch --yes --allow-running --json --write-report
```

当前 Codex App 版本的 ASAR/minified symbol 变化时，改用 current wrapper 应用兼容 patch：

```bash
~/.codex/bin/codex-background-shell-patch-current
```

运行完整 fail-closed 验证：

```bash
~/.codex/bin/codex-background-shell-patch-app --full-verify --yes --json --write-report
```

## 行为边界

控制器 patch 当前安装的官方 Codex.app：

```text
/Applications/Codex.app
```

`/Applications/Codex.app` 同时作为 clean source 候选和 patch 目标。每次 patch 后由当前 CLI 会话负责停止、签名验证、重启和截图检查，避免 App 自身重启打断调试 loop。

验证报告默认写入：

```text
background-shell/background-terminal/reports/
```

这个目录是运行证据，不进入 Git。完整验证会产生截图、备份和 JSON report，体积可能很大。

## 功能范围

该 patch 实现以下 background shell 能力：

- `shell_command` 支持 `run_in_background=true`。
- 前台运行中的 shell 可以转入后台。
- 前台命令达到 `450s` 自动后台阈值时转后台，不 kill 后重跑。
- timeout 时优先把原进程转后台。
- `Ctrl+B` 可将当前可消费前台 shell 放入后台。
- sleep denylist 对齐 leading `sleep` 规则。
- 摘要栏使用 native `thread/backgroundTerminals/list` 显示后台任务，任务标题来自命令内容。
- 输出视图第一行显示完整命令，后续显示真实输出。
- 摘要栏 stop/restart 通过 native process id 控制后台任务。
- 后台任务完成后支持 busy/idle 唤醒，并以模型消费证据作为 delivered 判定。

## 不进入仓库的内容

发布仓库不包含：

- `external-sources/openai-codex/` checkout。
- `codex-rs/target/` 构建产物。
- `background-terminal/reports/` 验证报告、截图和备份。
- Codex App bundle、DMG、profile、会话、认证和本机配置。

这些内容都可由脚本和 README 中的步骤重建。
