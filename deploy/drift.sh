#!/usr/bin/env bash
# deploy/drift.sh — 检查"仓库 src/" 与 "活体 ~/.coremem/scripts/" 是否分叉。
#
# 为什么需要：2026-11-06 实测发现三处分叉，方向还各不相同 ——
#   memory_capture.py / plugmem_backup.py：仓库更新（活体落后）
#   memory_heartbeat_sentinel.py：活体更新（我的修复没进仓库）
# 两边的后果相反：活体落后 = 缺功能；仓库落后 = 修复丢失且无人知晓。
#
# 用法：
#   bash deploy/drift.sh            # 只报告（默认）
#   bash deploy/drift.sh --strict   # 有分叉时 exit 1（可挂 CI / 定时检查）
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIVE="${MEM_SCRIPTS_DIR:-$HOME/.coremem/scripts}"
STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

echo "  仓库: $REPO/src"
echo "  活体: $LIVE"
echo

drift=0
for f in "$REPO"/src/*.py; do
    b="$(basename "$f")"
    if [ ! -f "$LIVE/$b" ]; then
        echo "  ⊘ 仅仓库: $b"
        drift=$((drift+1)); continue
    fi
    if ! diff -q "$f" "$LIVE/$b" >/dev/null 2>&1; then
        echo "  ⚠ 分叉: $b"
        diff "$f" "$LIVE/$b" | grep -c '^[<>]' | sed 's/^/      差异行数: /'
        drift=$((drift+1))
    fi
done

for f in "$LIVE"/*.py; do
    b="$(basename "$f")"
    [ -f "$REPO/src/$b" ] || { echo "  ⊘ 仅活体: $b"; drift=$((drift+1)); }
done

echo
if [ "$drift" -eq 0 ]; then
    echo "  ✓ 无分叉"
    exit 0
fi
echo "  共 $drift 处。同步方向要人工判断（别盲目覆盖）："
echo "    仓库→活体: cp $REPO/src/<f> $LIVE/<f>"
echo "    活体→仓库: cp $LIVE/<f> $REPO/src/<f> && git -C $REPO commit"
[ "$STRICT" -eq 1 ] && exit 1 || exit 0
