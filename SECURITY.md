# 安全策略

`claude-auto-retry` 是非官方 Claude Code CLI supervisor。项目只处理自身代码中的安全问题；Claude Code、Anthropic 服务、账号、计费或模型行为问题不在本项目的安全响应范围内。

## 支持范围

安全修复仅面向仓库当前维护的最新代码。旧快照、第三方 fork 和自行修改的构建不提供安全支持。

## 报告漏洞

请优先使用 GitHub 仓库 **Security** 页面中的 **Report a vulnerability** 私密报告功能。不要在公开 Issue 中披露尚未修复的漏洞。

报告中请包含：

- 受影响的 commit 或版本；
- 操作系统、Python 与 Claude Code CLI 版本；
- 最小复现步骤、预期结果与实际结果；
- 影响说明，以及已知的缓解方式；
- 已脱敏的诊断信息。

请勿提交 API key、OAuth token、完整 prompt、session transcript、session ID、私有仓库内容或其他敏感数据。如果私密报告功能不可用，请通过仓库所有者的 GitHub 公开联系方式先请求安全联系渠道，不要直接公开漏洞细节。

维护者会尽力确认和处理有效报告，但本项目不承诺固定响应时限或修复时限。未经维护者同意，请不要对真实第三方系统进行测试。

## 安全边界

- supervisor 不会把失败的原始 prompt 作为新请求重放；恢复只能针对 Claude Code CLI 已创建的同一 session。
- supervisor 只能降低重复执行风险，不能为 Claude 的工具调用或外部系统提供 exactly-once 保证。
- 如果无法确认 session、错误类型或副作用边界，默认停止并交还人工处理。
- Claude Code CLI 自身的本地会话记录、网络请求、身份验证和数据处理不由本项目控制。
