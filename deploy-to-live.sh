#!/bin/bash
# 把仓库脚本同步到活体，并强制套用部署差异（默认路径 ~/.openclaw）。
#
# 为什么需要它：直接 cp 会把仓库约定的 ~/.coremem 带到线上（或反过来把线上值带回仓库）。
# 2026-10-22 和 2026-10-26 各犯过一次。这里把差异显式化。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)/src"
LIVE="$HOME/.openclaw/workspace/scripts"
for f in remember.py memory_write_buffer.py; do
    sed 's|Path.home() / ".coremem"|Path.home() / ".openclaw"|g' "$REPO/$f" > "$LIVE/$f"
    chmod +x "$LIVE/$f"
    echo "  deployed $f → 默认 ~/.openclaw"
done
python3 - "$LIVE" <<'PY'
import sys, pathlib
live = pathlib.Path(sys.argv[1])
for f in ("remember.py", "memory_write_buffer.py"):
    import ast
    ast.parse((live/f).read_text())   # 语法自检
print("  语法 OK")
PY
