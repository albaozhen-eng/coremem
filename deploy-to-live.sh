#!/bin/bash
# 把仓库脚本同步到活体（默认 ~/.coremem/scripts）。仓库默认值就是现网值，不再套用 .openclaw 补丁。
#
# 2026-10-30：coremem 与 OpenClaw 解耦，活体目录改为 ~/.coremem/scripts；
# 旧版会把 ".coremem" sed 成 ".openclaw"，那是宿主耦合，已删除。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)/src"
LIVE="$HOME/.coremem/scripts"
for f in remember.py memory_write_buffer.py; do
    cp "$REPO/$f" "$LIVE/$f"
    chmod +x "$LIVE/$f"
    echo "  deployed $f → 默认 ~/.coremem"
done
python3 - "$LIVE" <<'PY'
import sys, pathlib
live = pathlib.Path(sys.argv[1])
for f in ("remember.py", "memory_write_buffer.py"):
    import ast
    ast.parse((live/f).read_text())   # 语法自检
print("  语法 OK")
PY
