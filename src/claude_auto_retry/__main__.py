"""允许通过 ``python -m claude_auto_retry`` 启动。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
