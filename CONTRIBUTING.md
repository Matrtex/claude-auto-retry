# 贡献指南

感谢你改进 `claude-auto-retry`。提交代码前，请先确认改动符合项目的安全边界：这是 Claude Code CLI 的本地 supervisor，不是 prompt 重放器，也不是 Claude Desktop 会话管理器。

## 开发环境

需要 Python 3.10 或更高版本，并在本机安装可执行的 Claude Code CLI。建议所有模式使用 Claude Code 2.1.223 或更高版本，`supervise` 将其作为硬要求；使用官方 retry watchdog 时硬要求 2.1.239 或更高版本。

```bash
git clone <repository-url>
cd claude-auto-retry
python -m venv .venv
```

激活虚拟环境后，以 editable 模式安装开发依赖：

```bash
python -m pip install -e ".[dev]"
```

提交前至少执行仓库 CI 当前使用的静态与打包检查：

```bash
python -m compileall -q src
python -m ruff check .
python -m build
python -m twine check dist/*
```

如仓库后续提供现有测试或类型检查，也应运行对应命令。不要提交虚拟环境、缓存、构建产物、日志、session transcript 或任何 secret。

## 设计约束

任何恢复逻辑都必须维持以下约束：

- 优先使用 Claude Code 官方内置 retry；外层恢复只在内置 retry 已耗尽后介入。
- 只对明确识别的瞬时错误恢复；认证、计费、权限、无效请求和未知错误必须停止。
- 只通过 `claude --resume <session-id>` 恢复同一 session，绝不重新发送原始 prompt。
- 检测到无法证明安全的外部副作用后，必须触发副作用 barrier 并停止自动恢复。
- 不宣称 exactly-once；外部工具和服务仍可能产生重复副作用。
- 默认不新增遥测，也不把 prompt、session ID、stdout 或 stderr 写入磁盘。
- 不读取或恢复 Claude Desktop 会话。

放宽上述边界的 PR 必须先通过 Issue 讨论，并给出威胁模型和失败模式分析。

## 提交改动

1. 先搜索已有 Issue 与 PR，避免重复工作。
2. 为非简单修复创建 Issue，说明问题、边界和建议方案。
3. 保持改动聚焦，不混入无关重构。
4. 更新受影响的中英文文档，确保参数、默认值和安全说明一致。
5. 在 PR 中说明验证环境、执行过的检查和未覆盖风险；示例输出必须脱敏。

commit message 使用 `<类型>: <中文描述>` 格式，例如：

```text
fix: 修复瞬时错误分类边界
docs: 补充 supervisor 安全说明
```

提交贡献即表示你同意按仓库的 [MIT License](LICENSE) 授权该贡献。
