# claude-auto-retry

[中文](README.md)

An unofficial, conservatively designed local supervisor for the Claude Code CLI.

## Bottom line

Claude Code CLI **already has built-in API retries**. `claude-auto-retry` does not replace them, and it is not a generic loop that replays a failed prompt:

- `launch` (alias: `interactive`) only configures and starts Claude Code's official retry behavior. It never performs an outer session resume.
- `supervise` (alias: `run`) lets the built-in retry budget finish first. Only after that budget is exhausted—and only for an explicitly recognized transient failure, a resumable CLI session, and a clear side-effect barrier—can it recover the same session with `claude --resume <session-id>`.
- The original prompt is submitted once. An outer recovery **never replays the original prompt**.
- Ambiguous conditions stop for human review. Unknown errors are never guessed to be transient.

> [!WARNING]
> Resuming a session does not provide exactly-once execution. Claude, tools, and external services may have completed irreversible work before a connection failed. The conservative side-effect barrier reduces duplicate-execution risk; it cannot eliminate it.

This project supports local Claude Code CLI sessions only. It cannot read or resume Claude Desktop sessions.

## Modes

| Command | Purpose | Outer recovery |
| --- | --- | --- |
| `doctor` | Check the Claude Code CLI path, version, and authentication status | No |
| `launch` / `interactive` | Start interactive Claude Code with official retry settings | No |
| `supervise` / `run` | Run a non-interactive task and, when safe, resume the same session after built-in retries are exhausted | Yes; at most 2 resumes by default |

The recovery path always targets the same session:

```text
submit the original prompt once
  -> Claude Code built-in retries
  -> success: exit
  -> built-in retry budget exhausted
  -> validate transient error, session ID, side-effect barrier, and budgets
  -> any check fails: stop
  -> every check passes: wait for backoff, then recover with --resume
```

`supervise` does not create a new session with a duplicate prompt, and it does not use `--continue` to guess which recent session to resume.

## Safety model

### Official retries come first

Claude Code retries retryable API failures with its own backoff. Its behavior can be tuned with `CLAUDE_CODE_MAX_RETRIES`, `API_TIMEOUT_MS`, and `CLAUDE_CODE_RETRY_WATCHDOG`. See the official [Error reference](https://code.claude.com/docs/en/errors#tune-retry-behavior) and [Programmatic usage](https://code.claude.com/docs/en/headless#handle-api-retries).

This project considers outer recovery only after the Claude Code process reports a terminal failure. `launch` contains no outer recovery path.

### Only explicit transient failures are eligible

Outer recovery uses an allowlist. Eligible evidence is limited to:

- `overloaded` / `overloaded_error` or HTTP 529;
- `server_error` or an HTTP 5xx status;
- HTTP 429 / `rate_limit`, unless billing, quota, usage, spend, credit, or extra-usage evidence marks it permanent;
- with no HTTP status, explicit timeout, connection reset/broken pipe, temporary DNS, or transient TLS-handshake/SSL-syscall failures.

Permanent evidence wins before transient evidence. Authentication, OAuth organization restrictions, billing or exhausted quota, invalid requests, missing models, maximum-output-token failures, other 4xx responses, certificate validation/hostname/self-signed-certificate failures, user interruption, and unknown errors stop without an outer resume. Error text is only supporting evidence; an unclassifiable failure is treated as permanent. Refer to Claude Code's [Error reference](https://code.claude.com/docs/en/errors) for upstream semantics.

### Same session, never the same prompt again

The initial task runs through Claude Code's non-interactive interface. The supervisor generates a session ID up front and verifies that Claude's structured init event returns the same ID; a missing or mismatched ID stops recovery. When every condition passes, that same ID is used with `--resume`, consistent with the official [CLI reference](https://code.claude.com/docs/en/cli-usage) and [session documentation](https://code.claude.com/docs/en/sessions).

A resume round sends only a fixed, built-in safety continuation instruction. It tells Claude to inspect the conversation, tool results, workspace, and relevant external state from the last durable point, and to stop when the outcome of a side effect cannot be verified. It does not contain the user's original prompt. The original prompt is absent from both the resume command and stdin and is never submitted as a new retry request.

### Side-effect barrier

The read-only allowlist is deliberately small: `Read`, `Glob`, `Grep`, `WebSearch`, `WebFetch`, and `ToolSearch`. Any other tool—including `Edit`, `Write`, `Bash`, `PowerShell`, MCP tools, and agents—latches the side-effect barrier. Executed custom hooks, background tasks, file-persistence events, and missing or conflicting tool names also latch it. The supervisor explicitly requests Claude hook lifecycle events for this decision. Once latched for the run, later read-only events do not clear it, and there is no bypass switch.

This is a conservative safety boundary, not a transaction protocol. A network failure can happen after an external system commits an operation but before the local process receives confirmation. The project therefore makes no exactly-once, idempotency, or rollback guarantee. For deployments, payments, messages, destructive actions, or production changes, callers must provide idempotency keys, transactions, or human confirmation.

### StopFailure is not a continuation controller

Claude Code's [`StopFailure` hook](https://code.claude.com/docs/en/hooks#stopfailure) runs when an API failure ends a turn, but the official reference says its output and exit code have no decision control. This project does not use that hook to replay prompts; `supervise` makes a conservative decision from the non-interactive process's structured result.

## Requirements

- Python 3.10 or later.
- An installed, executable, and authenticated Claude Code CLI.
- Claude Code 2.1.223 or later is recommended for every mode. `supervise` treats it as a hard requirement, and `doctor` reports earlier versions as unsupported; plain `launch` can still transparently start an older release. Starting with 2.1.223, resume-by-session-ID can search other project directories on the same machine.
- `launch --watchdog` has a hard requirement of Claude Code 2.1.239 or later. This project will not enable it on earlier releases, preventing their indefinite retries for spend/credit-related 429 responses.

See Claude Code's [Advanced setup](https://code.claude.com/docs/en/installation) for upstream system requirements and installation options.

## Installation

Install Claude Code CLI from its official documentation first, then install this project from a checkout:

```bash
git clone https://github.com/Matrtex/claude-auto-retry.git
cd claude-auto-retry
python -m pip install .
```

Or install directly from the Git repository with `pipx`:

```bash
pipx install git+https://github.com/Matrtex/claude-auto-retry.git
```

This documentation does not claim a PyPI release. Do not rely on `pip install claude-auto-retry` as an installation method.

## Quick start

### 1. Check the environment

```bash
claude-auto-retry doctor
```

Machine-readable output:

```bash
claude-auto-retry doctor --json
```

Select a Claude executable explicitly:

```bash
claude-auto-retry doctor --claude /path/to/claude
```

### 2. Launch interactive Claude Code

```bash
claude-auto-retry launch
```

Configure Claude Code's official retry settings and pass remaining arguments through to `claude`:

```bash
claude-auto-retry launch \
  --max-retries 10 \
  --api-timeout-ms 600000 \
  -- --model sonnet
```

Enable Claude Code's official unattended retry watchdog:

```bash
claude-auto-retry launch --watchdog
```

`--watchdog` is upstream Claude Code behavior, not an unlimited outer-recovery loop implemented by this project. It keeps retrying recoverable 429/529 capacity failures in unattended sessions, while current releases stop immediately for permanent 429 conditions such as spend limits or exhausted credits. Run `doctor` first to confirm the required Claude Code version. Add `--dry-run` to validate the settings without starting Claude.

Full form:

```text
claude-auto-retry launch
  [--claude PATH]
  [--max-retries 0..15]
  [--api-timeout-ms N]
  [--watchdog]
  [--dry-run]
  [-- CLAUDE_ARGS...]
```

### 3. Supervise a non-interactive task

Provide a prompt directly:

```bash
claude-auto-retry supervise --cwd . "Inspect this project and fix clear type errors"
```

Read the prompt from a file:

```bash
claude-auto-retry supervise --prompt-file task.md --max-resumes 2
```

If neither a positional prompt nor `--prompt-file` is present, non-TTY stdin can provide it:

```bash
cat task.md | claude-auto-retry supervise --cwd .
```

The positional prompt and `--prompt-file` are mutually exclusive. Non-TTY stdin is read only when neither is present. The user's original prompt is used for the initial submission only and is not sent again when resuming the session; resume rounds use the fixed safety continuation instruction.

Put arguments for Claude Code after `--`:

```bash
claude-auto-retry supervise \
  --max-resumes 2 \
  --claude-max-retries 10 \
  --api-timeout-ms 600000 \
  "Review the changes and explain the risks" \
  -- --model sonnet
```

`supervise` rejects passthrough options that could break its safety contract, including `--print`, `--resume`, `--continue`, `--session-id`, `--no-session-persistence`, stream/hook-output controls, and remote, background, or initialization-only options. The supervisor owns those settings. It also stops before any model call when `CLAUDE_CODE_SKIP_PROMPT_HISTORY` or `CLAUDE_CODE_CHILD_SESSION` would disable resumable session history. Invoke `claude` directly when you need full control, or use `launch` for an interactive session.

Full form:

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

## Defaults

| Option | Default | Meaning |
| --- | ---: | --- |
| `--max-resumes` | `2` | Maximum same-session resumes after built-in retries are exhausted |
| `--base-delay` | `15` seconds | Base wait before the first outer recovery |
| `--max-delay` | `120` seconds | Cap for the exponential-plus-jitter calculation; a trusted `Retry-After` may be higher |
| `--backoff-factor` | `2` | Exponential backoff multiplier |
| `--jitter` | `3` seconds | Random `±3`-second jitter range around a wait |
| `--max-elapsed` | `1800` seconds | Window, measured from the initial run, in which another resume may start |
| `--claude-max-retries` | `10` | Claude Code's official retry count under `supervise` |
| `--api-timeout-ms` | `600000` | Per-request timeout passed to Claude Code |

The default `launch --max-retries` value is also `10`. Set `supervise --max-resumes 0` to disable outer recovery without disabling Claude Code's own retries. `--max-elapsed` only decides whether another recovery may start; it does not forcibly terminate a Claude process that is already running.

## Output and privacy

- The project contains no telemetry or remote log reporting.
- By default, it writes no prompt, session ID, Claude child-process stdout/stderr, or supervisor state file to disk.
- `--raw-stream` writes Claude Code's raw stream only to the current terminal. The project does not persist it, although a terminal, shell redirection, or CI system still might.
- During a run, the supervisor must process the session ID and required structured events in memory to make recovery and barrier decisions.
- The supervisor sends prompts to the Claude child process through stdin rather than placing them in the Claude child's argv. A positional prompt may still appear in shell history and the supervisor process's own argv; prefer controlled stdin for sensitive input. A file passed with `--prompt-file` is created and managed by the user.
- The project cannot control how the operating system, terminal, shell, CI system, or Claude CLI handles runtime data.
- Claude Code CLI independently handles network requests and local session transcripts according to its own configuration. This project does not control that behavior. Because `supervise` needs a resumable Claude CLI session, it rejects passthrough of `--no-session-persistence`. See [PRIVACY.md](PRIVACY.md) and the official [session documentation](https://code.claude.com/docs/en/sessions).

Never attach raw output containing secrets, full prompts, session IDs, transcripts, or private source code to a public issue.

## Explicitly unsupported

- Taking over or resuming Claude Desktop, Claude.ai, IDE, or cloud sessions.
- Guessing or resuming a “most recent” session when the session ID is unavailable.
- Automatically replaying the original prompt.
- Outer recovery for authentication, billing, permissions, invalid requests, or unknown failures.
- Bypassing the side-effect barrier.
- Exactly-once semantics, distributed transactions, or rollback of external side effects.
- Replacing Claude Code's official retries, account support, or service-status diagnostics.

Claude Desktop is a separate interface with its own session model; see the official [Desktop quickstart](https://code.claude.com/docs/en/desktop-quickstart).

## About port 10808

`10808` may be used only by a maintainer's local repository proxy, for example while running `git push`. It is not a runtime port, dependency, or default setting of `claude-auto-retry`. The program does not require users to open, listen on, or connect to port `10808`; do not configure it unless your own Git/network environment requires it.

## Official references

- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
- [CLI reference](https://code.claude.com/docs/en/cli-usage)
- [Error reference](https://code.claude.com/docs/en/errors)
- [Hooks reference: StopFailure](https://code.claude.com/docs/en/hooks#stopfailure)
- [Manage sessions](https://code.claude.com/docs/en/sessions)
- [Advanced setup and installation](https://code.claude.com/docs/en/installation)
- [Desktop quickstart](https://code.claude.com/docs/en/desktop-quickstart)

## Contributing, support, and license

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing.
- See [SUPPORT.md](SUPPORT.md) for usage support.
- Follow [SECURITY.md](SECURITY.md) for vulnerabilities; do not disclose sensitive details publicly.
- See [PRIVACY.md](PRIVACY.md) for the privacy boundary.
- Licensed under the [MIT License](LICENSE), Copyright (c) 2026 Matrtex.

`claude-auto-retry` is an unofficial project. It is not affiliated with or endorsed by Anthropic. Claude and Claude Code are trademarks or product names of their respective owners.
