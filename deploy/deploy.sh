#!/usr/bin/env bash
# deploy.sh — 把 ~/coremem（GitHub 源）部署到生产目录，带时间戳备份
# 用法: bash deploy.sh [REPO] [DEST] [PROFILE]
#   REPO    = coremem 仓库根（默认 ~/coremem）
#   DEST    = 部署目标目录（默认 ~/.coremem/scripts；coremem 自持，不依宿主平台）
#   PROFILE = 机器 profile 名（默认 coremem-local；plugmem-suite-cli 用于独立 cli）
set -euo pipefail
FORCE=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
REPO="${ARGS[0]:-$HOME/coremem}"
DEST="${ARGS[1]:-$HOME/.coremem/scripts}"
PROFILE="${ARGS[2]:-coremem-local}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BAK="$DEST/.backup/coremem_predeploy_$STAMP"

mkdir -p "$BAK"
# 清单单一真源：与 deploy/patch_deploy.py 的 FILES + SH_FILES 保持一致（2026-11-02 统一）
DEPLOY_FILES="plugmem_config.py remember.py memory_recall.py memory_write_buffer.py mo_common.py memory_insight_scan.py morning_briefing.py vault_export.py golden_eval.py kis_eval.py memory_index.py plugmem_backup.py memory_health.py memory_heartbeat_sentinel.py memory_capture.py plugmem-watchdog.sh run-plugmem-suite.sh"
for f in $DEPLOY_FILES; do
  [ -f "$DEST/$f" ] && cp -p "$DEST/$f" "$BAK/" || true
done
echo "✓ 旧版已备份到 $BAK"

PATCH_FLAG=""
[ "$PROFILE" = "coremem-local" ] && PATCH_FLAG="--no-patch"
# 部署前漂移检查（2026-10-30 事故护栏）：生产被手改过而仓库没同步时默认拒绝，
# 避免把生产热修（检索算法、退役守卫）无声回退。用 --force 才覆盖。
# 工作区有未提交改动时，差异来自本次编辑而非漂移：跳过检查（提交后再部署最安全）。
if [ "$FORCE" != "1" ] && ! git -C "$REPO" diff --quiet 2>/dev/null; then
  echo "ℹ 仓库有未提交改动，跳过漂移检查（建议先 commit 再部署）"
elif [ "$FORCE" != "1" ]; then
  python3 "$REPO/deploy/patch_deploy.py" "$REPO" "$DEST" --profile "$PROFILE" $PATCH_FLAG --check-drift || {
    echo "✗ 漂移检查未通过，已中止部署（确需覆盖：bash deploy.sh --force）" >&2
    exit 3
  }
fi

python3 "$REPO/deploy/patch_deploy.py" "$REPO" "$DEST" --profile "$PROFILE" $PATCH_FLAG

# 检索别名表：只在缺失时安装，绝不覆盖操作者维护过的版本。
SEED="$REPO/deploy/seeds/search_aliases.json"
TARGET="$(dirname "$DEST")/data/search_aliases.json"
if [ -f "$SEED" ] && [ ! -f "$TARGET" ]; then
  mkdir -p "$(dirname "$TARGET")"
  cp "$SEED" "$TARGET"
  echo "✓ 安装检索别名表 $TARGET"
fi
chmod 600 "$DEST"/plugmem_config.py "$DEST"/remember.py "$DEST"/memory_recall.py "$DEST"/memory_write_buffer.py "$DEST"/memory_insight_scan.py "$DEST"/morning_briefing.py "$DEST"/vault_export.py "$DEST"/secret_scan.py 2>/dev/null || true
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

# 语法自检（对活体已落盘的文件做检查；.py 走 py_compile，.sh 走 bash -n）
for f in $DEPLOY_FILES; do
  case "$f" in
    *.py) python3 -m py_compile "$DEST/$f" && echo "✓ syntax ok: $f" ;;
    *.sh) bash -n "$DEST/$f" && echo "✓ shell syntax ok: $f" ;;
  esac
done

# 冒烟自检
if [ -x "$DEST/remember.py" ] || [ -f "$DEST/remember.py" ]; then
  echo "--- 冒烟自检 ---"
  python3 "$DEST/remember.py" --status 2>&1 | head -5
  python3 "$DEST/memory_write_buffer.py" --check 2>/dev/null && echo "✓ buffer --check 通过" || echo "⚠ buffer --check 非零"
fi
