# 隐私说明

生效日期：2026-08-28

## 核心结论

`claude-auto-retry` 不包含遥测、分析 SDK、广告或远程日志上报。默认配置下，本项目不会把 prompt、Claude session ID、Claude 子进程的 stdout 或 stderr 写入磁盘。

## 运行时会处理什么

作为 Claude Code CLI 的本地 supervisor，本项目会在运行期间启动并观察本机 `claude` 子进程，读取恢复判定所需的进程结果和错误信号，并在安全条件满足时使用 session ID 执行 `claude --resume`。用户的原始 prompt 通过 stdin 传给 Claude 子进程，不放入 Claude 子进程 argv；恢复轮次使用不包含原 prompt 的固定安全继续指令。这些数据用于当前进程内的控制流程。

如果使用位置 prompt，文本仍可能出现在 shell history 和 supervisor 进程自身的 argv 中。敏感 prompt 优先通过受控的 non-TTY stdin 提供；使用 `--prompt-file` 时，该文件由用户自行创建、保护和删除。

如果用户显式启用 shell 重定向、CI 日志、终端录制、操作系统诊断或其他第三方工具，相关工具可能保存输出；这不属于本项目的默认持久化行为。

## Claude Code 的数据处理

Claude Code CLI 是独立软件，可能根据其自身配置保存本地 session transcript，并与 Anthropic 或用户配置的模型提供方通信。`claude-auto-retry` 不会关闭、替代或控制 Claude Code 的数据处理机制。`supervise` 依赖可恢复的 Claude CLI session，因此不允许透传 `--no-session-persistence`；“本项目默认不落盘”仅指 supervisor 自身不会另存 prompt、session ID 或子进程输出，并不表示 Claude CLI 不保存其官方 session transcript。请查阅 Claude Code 官方文档和适用于你账号、组织及模型提供方的隐私条款。

本项目不读取或接管 Claude Desktop 会话，也不支持恢复 Claude Desktop 中的会话。

## 用户责任

- 不要在 Issue、日志或诊断输出中提交 secret、token、完整 prompt、session transcript 或私有代码。
- 在启用 shell 重定向、CI 日志或其他持久化功能前，确认其访问权限和保留策略。
- 按照组织的数据治理要求配置 Claude Code CLI、终端、操作系统和模型提供方。

隐私问题请通过仓库 Issue 提交，但不要在公开 Issue 中附带敏感数据。安全漏洞请遵循 [SECURITY.md](SECURITY.md)。
