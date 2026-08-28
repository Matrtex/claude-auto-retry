# claude-auto-retry

[English](README.en.md)

一个非官方、保守设计的 Claude Code CLI 本地 supervisor。

## 核心结论

Claude Code CLI **本身已经提供 API retry**。`claude-auto-retry` 不替代这套机制，也不是把失败 prompt 不断重放的通用重试器：

- `launch`（别名 `interactive`）只配置并启动 Claude Code 的官方 retry，不做外层 session 恢复。
- `supervise`（别名 `run`）先让 Claude Code 的内置 retry 完整执行；仅当内置 retry 已耗尽、错误被明确识别为瞬时错误、同一 CLI session 可安全恢复，并且没有越过副作用 barrier 时，才使用 `claude --resume <session-id>` 恢复该 session。
- 原始 prompt 只提交一次。外层恢复**绝不重放原始 prompt**。
- 条件不明确时直接停止并交还人工处理，不猜测、不把未知错误当作瞬时错误。

> [!WARNING]
> session 恢复不等于 exactly-once。Claude、工具调用和外部服务可能在连接中断前已经产生不可撤销的副作用。本项目通过保守的副作用 barrier 降低重复执行风险，但无法消除该风险。

本项目仅支持本机 Claude Code CLI session，不支持读取或恢复 Claude Desktop 会话。

## 工作模式

| 命令 | 作用 | 是否执行外层恢复 |
| --- | --- | --- |
| `doctor` | 检查 Claude Code CLI 路径、版本与认证状态 | 否 |
| `launch` / `interactive` | 启动交互式 Claude Code，并配置官方 retry 参数 | 否 |
| `supervise` / `run` | 运行非交互任务；内置 retry 耗尽后，在安全条件满足时恢复同一 session | 是，默认最多 2 次 |

恢复路径固定为同一 session：

```text
提交一次原始 prompt
  -> Claude Code 内置 retry
  -> 成功：结束
  -> 内置 retry 耗尽
  -> 校验瞬时错误、session ID、副作用 barrier、次数与时间预算
  -> 任一条件不满足：停止
  -> 全部满足：等待退避时间，并通过 --resume 恢复同一 session
```

`supervise` 不会为恢复创建一个带相同 prompt 的新 session，也不会通过 `--continue` 猜测最近会话。

## 安全边界

### 官方 retry 优先

Claude Code 会对可重试 API 错误执行自己的退避和重试，并可通过 `CLAUDE_CODE_MAX_RETRIES`、`API_TIMEOUT_MS` 与 `CLAUDE_CODE_RETRY_WATCHDOG` 调整。详见官方 [Error reference](https://code.claude.com/docs/en/errors#tune-retry-behavior) 和 [Programmatic usage](https://code.claude.com/docs/en/headless#handle-api-retries)。

本项目只在 Claude Code 进程已经给出最终失败后评估外层恢复。`launch` 完全不包含外层恢复。

### 明确瞬时错误才恢复

外层恢复采用 allowlist，仅接受以下证据：

- `overloaded` / `overloaded_error` 或 HTTP 529；
- `server_error` 或任意 HTTP 5xx；
- HTTP 429 / `rate_limit`，但 billing、quota、usage、spend、credits 或 extra usage 等永久信号优先并阻止恢复；
- 没有 HTTP 状态时，明确的 timeout、连接重置或 broken pipe、临时 DNS、TLS handshake 或 SSL syscall 瞬时故障。

永久错误证据优先于瞬时证据。认证失败、OAuth 组织限制、计费或额度耗尽、无效请求、模型不存在、达到最大输出 token、其他 4xx、证书校验/主机名/自签名证书错误、用户中断以及未知错误不会自动恢复。错误文本只是辅助证据；无法可靠分类时按永久失败处理。Claude Code 的完整错误语义以官方 [Error reference](https://code.claude.com/docs/en/errors) 为准。

### 同一 session，不重放 prompt

初始任务使用 Claude Code 的非交互模式运行。supervisor 预先生成 session ID，并验证 Claude 返回的结构化 init 事件与该 ID 一致；不一致或缺失时停止。满足恢复条件时，使用同一 ID 执行 `--resume`。这符合 Claude Code 官方的 [CLI reference](https://code.claude.com/docs/en/cli-usage) 与 [session 管理说明](https://code.claude.com/docs/en/sessions)。

恢复轮次只发送项目内置的固定安全继续指令，要求 Claude 从最后可确认的状态检查会话、工具结果、workspace 和外部状态，并在副作用结果未知时停止。它不包含用户的原始 prompt。原始 prompt 不会在恢复命令或 stdin 中再次出现，也不会写入新的重试请求。

### 副作用 barrier

只读工具 allowlist 严格限定为 `Read`、`Glob`、`Grep`、`WebSearch`、`WebFetch` 和 `ToolSearch`。任何其他工具，包括 `Edit`、`Write`、`Bash`、`PowerShell`、MCP 工具和 Agent，都会锁存副作用 barrier；实际执行的自定义 Hook、后台任务、持久化文件事件、缺少工具名或同一 tool ID 出现名称冲突时也会锁存。supervisor 会显式请求 Claude 输出 hook lifecycle 事件用于这一判定。barrier 一旦锁存，后续只读事件不会解除，且没有绕过开关。

这是一条保守的安全边界，不是事务机制：网络断开可能发生在外部系统已提交操作、但本地尚未收到确认的时刻。因此项目不提供 exactly-once、幂等性或事务回滚保证。涉及发布、付款、发消息、删除资源、修改生产环境等操作时，应由调用方提供幂等键、事务或人工确认。

### StopFailure 不是继续控制器

Claude Code 的 [`StopFailure` hook](https://code.claude.com/docs/en/hooks#stopfailure) 会在 API 错误导致 turn 失败时运行，但官方说明其输出与退出码不具备 decision control。本项目不依赖该 hook 重放 prompt；`supervise` 根据非交互进程的结构化结果做保守判定。

## 要求

- Python 3.10 或更高版本；
- 已安装、可执行且已完成认证的 Claude Code CLI；
- 建议所有模式使用 Claude Code 2.1.223 或更高版本。`supervise` 将其作为硬要求，`doctor` 会把更早版本标记为 unsupported；普通 `launch` 仍可透明启动旧版。该版本起，按 session ID 恢复可以跨本机项目目录查找会话。
- `launch --watchdog` 硬要求 Claude Code 2.1.239 或更高版本。本项目不在更早版本启用它，避免旧版对 spend/credits 类 429 进行无限重试。

Claude Code 的系统要求和官方安装方法见 [Advanced setup](https://code.claude.com/docs/en/installation)。

## 安装

先按照官方文档安装 Claude Code CLI，然后在仓库目录安装本项目：

```bash
git clone https://github.com/Matrtex/claude-auto-retry.git
cd claude-auto-retry
python -m pip install .
```

也可以直接从 Git 仓库通过 `pipx` 安装：

```bash
pipx install git+https://github.com/Matrtex/claude-auto-retry.git
```

本项目当前文档不声明 PyPI 发布；请勿使用 `pip install claude-auto-retry` 作为安装依据。

## 快速开始

### 1. 检查环境

```bash
claude-auto-retry doctor
```

机器可读输出：

```bash
claude-auto-retry doctor --json
```

指定 Claude 可执行文件：

```bash
claude-auto-retry doctor --claude /path/to/claude
```

### 2. 启动交互式 Claude Code

```bash
claude-auto-retry launch
```

配置 Claude Code 官方 retry，并把其余参数原样传给 `claude`：

```bash
claude-auto-retry launch \
  --max-retries 10 \
  --api-timeout-ms 600000 \
  -- --model sonnet
```

启用官方 unattended retry watchdog：

```bash
claude-auto-retry launch --watchdog
```

`--watchdog` 是 Claude Code 官方行为，不是本项目实现的无限外层恢复。它会让无人值守会话持续重试可恢复的 429/529 容量错误，但新版会立即停止 spend limit 或 credits 耗尽等永久 429。先用 `doctor` 确认 Claude Code 版本满足要求。若只想检查配置而不启动 Claude，可加 `--dry-run`。

完整形式：

```text
claude-auto-retry launch
  [--claude PATH]
  [--max-retries 0..15]
  [--api-timeout-ms N]
  [--watchdog]
  [--dry-run]
  [-- CLAUDE_ARGS...]
```

### 3. 监督非交互任务

直接提供 prompt：

```bash
claude-auto-retry supervise --cwd . "检查当前项目并修复明确的类型错误"
```

从文件读取 prompt：

```bash
claude-auto-retry supervise --prompt-file task.md --max-resumes 2
```

未提供位置 prompt 或 `--prompt-file` 时，也可以从 non-TTY stdin 读取：

```bash
cat task.md | claude-auto-retry supervise --cwd .
```

位置 prompt 与 `--prompt-file` 互斥；两者都未提供时才从 non-TTY stdin 读取。用户的原始 prompt 只用于首次提交；恢复同一 session 时不会再次发送，而是使用固定的安全继续指令。

传给 Claude Code 的参数放在 `--` 之后：

```bash
claude-auto-retry supervise \
  --max-resumes 2 \
  --claude-max-retries 10 \
  --api-timeout-ms 600000 \
  "审查变更并说明风险" \
  -- --model sonnet
```

`supervise` 会拒绝可能破坏安全契约的透传参数，例如 `--print`、`--resume`、`--continue`、`--session-id`、`--no-session-persistence`、stream/hook output 控制以及远程、后台、初始化专用参数。这些参数由 supervisor 管理；需要完全自行控制 Claude CLI 时请直接使用 `claude`，或在交互场景使用 `launch`。如果环境中的 `CLAUDE_CODE_SKIP_PROMPT_HISTORY` 或 `CLAUDE_CODE_CHILD_SESSION` 会关闭可恢复 session，`supervise` 也会在调用模型前停止。

完整形式：

```text
claude-auto-retry supervise
  [PROMPT | --prompt-file FILE | non-TTY stdin]
  [--cwd DIR]
  [--claude PATH]
  [--max-resumes 0..5]
  [--base-delay S]
  [--max-delay S]
  [--backoff-factor N]
  [--jitter S]
  [--max-elapsed S]
  [--claude-max-retries 0..15]
  [--api-timeout-ms N]
  [--raw-stream]
  [-- CLAUDE_ARGS...]
```

## 默认值

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `--max-resumes` | `2` | Claude 内置 retry 耗尽后，最多恢复同一 session 的次数 |
| `--base-delay` | `15` 秒 | 第一次外层恢复前的基础等待时间 |
| `--max-delay` | `120` 秒 | 指数退避与 jitter 的单次计算上限；可信 `Retry-After` 可以更高 |
| `--backoff-factor` | `2` | 指数退避倍数 |
| `--jitter` | `3` 秒 | 等待时间的 `±3` 秒随机抖动范围 |
| `--max-elapsed` | `1800` 秒 | 从初始运行开始、允许启动下一次 resume 的时间窗口 |
| `--claude-max-retries` | `10` | `supervise` 中 Claude Code 官方 retry 次数 |
| `--api-timeout-ms` | `600000` | Claude Code 单次 API 请求超时 |

`launch` 的 `--max-retries` 默认也是 `10`。如需禁止外层恢复，可在 `supervise` 中设置 `--max-resumes 0`；这不会关闭 Claude Code 自身的 retry。`--max-elapsed` 只决定能否再启动一轮恢复，不会强行终止正在运行的 Claude 进程。

## 输出与隐私

- 本项目不包含遥测或远程日志上报。
- 默认不把 prompt、session ID、Claude 子进程 stdout 或 stderr 写入磁盘，也不创建自身状态文件。
- `--raw-stream` 只把 Claude Code 的原始 stream 输出到当前终端，不会由本项目另行落盘；终端、shell 重定向或 CI 系统仍可能保存它。
- 运行期间，supervisor 必须在内存中处理 session ID 和必要的结构化事件，才能做恢复与 barrier 判定。
- supervisor 通过 stdin 把 prompt 传给 Claude 子进程，不把 prompt 放入 Claude 子进程 argv。若使用位置 prompt，文本仍可能出现在 shell history 和 supervisor 自身的 argv 中；敏感输入优先使用受控的 stdin。`--prompt-file` 指向的文件由用户自行创建和管理。
- 本项目无法控制操作系统、终端、shell、CI 或 Claude CLI 自身如何处理运行时数据。
- Claude Code CLI 会依据自己的配置处理网络请求和本地 session transcript；这不受本项目控制。`supervise` 依赖 Claude CLI 的可恢复 session，因此不允许向其透传 `--no-session-persistence`。详情见 [PRIVACY.md](PRIVACY.md) 与 Claude Code [session 文档](https://code.claude.com/docs/en/sessions)。

不要把包含 secret、完整 prompt、session ID、session transcript 或私有代码的原始输出提交到公开 Issue。

## 明确不支持的场景

- Claude Desktop、Claude.ai、IDE 或云端会话的接管与恢复；
- 在 session ID 不明确时猜测或恢复“最近一次”会话；
- 对原始 prompt 进行自动重放；
- 对认证、计费、权限、无效请求或未知错误进行外层恢复；
- 绕过副作用 barrier；
- exactly-once、分布式事务或外部副作用回滚；
- 替代 Claude Code 官方 retry、账号支持或服务状态诊断。

Claude Desktop 是独立产品界面，其会话模型见官方 [Desktop quickstart](https://code.claude.com/docs/en/desktop-quickstart)。

## 关于端口 10808

`10808` 只可能用于维护者本地环境中的仓库网络代理，例如执行 `git push`。它不是 `claude-auto-retry` 的运行时端口、依赖或默认配置。本项目运行时不会要求用户开放、监听或连接 `10808`；除非你自己的 Git/网络环境确实需要，否则不要配置它。

## 官方参考

- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
- [CLI reference](https://code.claude.com/docs/en/cli-usage)
- [Error reference](https://code.claude.com/docs/en/errors)
- [Hooks reference: StopFailure](https://code.claude.com/docs/en/hooks#stopfailure)
- [Manage sessions](https://code.claude.com/docs/en/sessions)
- [Advanced setup and installation](https://code.claude.com/docs/en/installation)
- [Desktop quickstart](https://code.claude.com/docs/en/desktop-quickstart)

## 贡献、支持与许可

- 贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 使用问题见 [SUPPORT.md](SUPPORT.md)。
- 安全漏洞请遵循 [SECURITY.md](SECURITY.md)，不要公开敏感细节。
- 隐私边界见 [PRIVACY.md](PRIVACY.md)。
- 本项目采用 [MIT License](LICENSE)，Copyright (c) 2026 Matrtex。

`claude-auto-retry` 是非官方项目，与 Anthropic 无隶属关系，也未获得 Anthropic 背书。Claude 和 Claude Code 是其各自权利人的商标或产品名称。
