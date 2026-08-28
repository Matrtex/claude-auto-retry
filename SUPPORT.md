# 支持说明

## 本项目可以协助的问题

请在 GitHub Issues 中报告与 `claude-auto-retry` 本身有关的问题，例如：

- 安装或 CLI 参数解析失败；
- `doctor` 的兼容性检查异常；
- 瞬时错误分类或副作用 barrier 行为不符合文档；
- 不同操作系统上的 supervisor 进程管理问题。

提交前请先运行：

```bash
claude-auto-retry doctor
```

Issue 中请提供操作系统、Python 版本、Claude Code CLI 版本、安装方式、完整命令结构和最小复现步骤。只附上已脱敏的诊断信息；不要提交 API key、OAuth token、完整 prompt、session ID、session transcript、私有代码或未脱敏的 stdout/stderr。

## 不属于本项目的问题

以下问题应使用 Anthropic 官方支持与文档渠道：

- Claude 账号、登录、订阅、账单和 API 配额；
- Claude Code CLI 自身的安装、认证或内置 retry；
- Anthropic 服务状态、模型质量和内容策略；
- Claude Desktop、Claude.ai 或其他 Anthropic 产品的会话。

`claude-auto-retry` 是非官方项目，与 Anthropic 无隶属或背书关系。社区支持按维护者可用时间提供，不承诺 SLA。

疑似安全漏洞不要提交公开 Issue，请遵循 [SECURITY.md](SECURITY.md)。隐私边界见 [PRIVACY.md](PRIVACY.md)。
