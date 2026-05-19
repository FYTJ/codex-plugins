# 命令检查 hook

`block_blacklisted_commands.py` 是 Codex `PreToolUse` hook。它检查 Bash 命令是否命中黑名单短语，命中时弹出 macOS 审批窗口。只有用户选择 `Allow` 才会放行；选择 `Deny`、弹窗失败或脚本内部等待超时都会返回 deny。示例 `hooks.json` 把 Codex 外层 hook timeout 设为 86400 秒，脚本内部会在 86340 秒先返回 deny，避免外层超时后放行。

默认读取 `~/.claude/settings.json` 中的 `permissions.ask`。也可以设置：

```bash
export CODEX_BLACKLIST_RULES_FILE=/path/to/blacklist-rules.txt
```

普通规则文件支持每行一个短语，也支持 `Bash(...)` 形式。安装和 `hooks.json` 配置见上级目录 `README.md`。
