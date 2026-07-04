# 回合完成通知

`notify_turn_ended.py` 是 Codex `notify` 入口脚本，只处理 `agent-turn-complete` 事件，并通过 macOS `osascript` 发送本机通知。

本目录的发布版不包含 `iphone-notify`、Bark 或其他远端推送。配置文件示例：

```toml
notify = ["${HOME}/.codex/hooks/notify_turn_ended.py", "turn-ended"]
```

`notify.example.toml` 可复制到 `~/.codex/notify.toml`，用于控制是否启用通知和去重窗口。
