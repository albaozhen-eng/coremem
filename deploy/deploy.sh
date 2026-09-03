#!/usr/bin/env bash
# deploy.sh — 把 ~/coremem（GitHub 源）部署到生产 scripts 目录，带时间戳备份
set -euo pipefail
REPO="${1:-$HOME/coremem}"
DEST="$HOME/.openclaw/workspace/scripts"
WS_MAIN="$HOME/.openclaw/workspace-main"
STAMP="$(date +%Y%m%d_%H%M%S)"
BAK="$DEST/.backup/coremem_predeploy_$STAMP"

mkdir -p "$BAK"
for f in plugmem_config.py remember.py memory_recall.py memory_write_buffer.py memory_insight_scan.py morning_briefing.py; do
  [ -f "$DEST/$f" ] && cp -p "$DEST/$f" "$BAK/" || true
done
echo "✓ 旧版已备份到 $BAK"

python3 "$REPO/deploy/patch_deploy.py" "$REPO" "$DEST"
chmod 600 "$DEST"/plugmem_config.py "$DEST"/remember.py "$DEST"/memory_recall.py "$DEST"/memory_write_buffer.py "$DEST"/memory_insight_scan.py "$DEST"/morning_briefing.py "$DEST"/secret_scan.py 2>/dev/null || true
echo "✓ 生产脚本已更新（默认值=现网路径，env 可覆盖）"

# pre-commit hook → coremem 仓库自身（发布/同步前隐私闸门：拦 /Users/ 路径、真实域名、手机号、key）
if [ -d "$REPO/.git" ]; then
  cat > "$REPO/.git/hooks/pre-commit" <<'HOOK'
#!/bin/sh
# coremem 发布隐私闸门：扫描仓库工作区（排除 deploy/ 本地部署件见 .gitignore）
ROOT="$(git rev-parse --show-toplevel)"
python3 "$ROOT/scripts/secret_scan.py" "$ROOT" --tracked
HOOK
  chmod +x "$REPO/.git/hooks/pre-commit"
  echo "✓ coremem 仓库 pre-commit hook 已安装（隐私泄露将阻断 commit/push 源头）"
fi

# 语法自检
for f in "$DEST"/plugmem_config.py "$DEST"/remember.py "$DEST"/memory_recall.py "$DEST"/memory_write_buffer.py "$DEST"/memory_insight_scan.py "$DEST"/morning_briefing.py; do
  python3 -m py_compile "$f" && echo "✓ syntax ok: $(basename "$f")"
done
