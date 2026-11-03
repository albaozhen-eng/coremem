#!/usr/bin/env bash
# deploy/install.sh — coremem 一键安装：目录 → 配置 → 部署 → 调度器 → 冒烟
#
# 目标：**新机器不需要改任何路径**。coremem 的默认布局就是 ~/.coremem/{scripts,memory,
# data,logs,config}，deploy.sh 不套任何 profile 时（coremem-local）即生产默认值。
# 只有"宿主平台老目录"（例如历史 OpenClaw 机器）才需要 deploy/profiles/*.json。
#
# 用法：
#   bash deploy/install.sh                      # 交互最少：全默认 + 自动挑模型通道
#   bash deploy/install.sh --deepseek-key sk-x  # 云端通道（本机没有本地 omlx 时用这个）
#   bash deploy/install.sh --local-model        # 用本机 omlx（默认 127.0.0.1:9999/v1）
#   bash deploy/install.sh --no-scheduler       # 只装脚本，不装定时任务
#   bash deploy/install.sh --prefix ~/mymem     # 改根目录（高级用法）
#
# 幂等：可重复执行，会覆盖脚本、保留已有配置（coremem.env / providers.json 不覆盖）。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${MEM_SCRIPTS_DIR:-$HOME/.coremem/scripts}"
ROOT="$(dirname "$PREFIX")"
PY=""
GRAPH_ID=""
OMLX_BASE_URL=""
OMLX_MODEL=""
DEEPSEEK_KEY=""
LOCAL_MODEL=0
WITH_SCHEDULER=1
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; ROOT="$(dirname "$PREFIX")"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --graph-id) GRAPH_ID="$2"; shift 2 ;;
    --omlx-base-url) OMLX_BASE_URL="$2"; shift 2 ;;
    --omlx-model) OMLX_MODEL="$2"; shift 2 ;;
    --deepseek-key) DEEPSEEK_KEY="$2"; shift 2 ;;
    --local-model) LOCAL_MODEL=1; shift ;;
    --no-scheduler) WITH_SCHEDULER=0; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  ✓ %s\n' "$*"; }
warn(){ printf '  ⚠ %s\n' "$*"; }
die() { printf '  ✗ %s\n' "$*" >&2; exit 1; }

say "1/5 环境检查"
[ -n "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "找不到 python3（需要 >= 3.9）"
"$PY" - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 9):
    sys.exit("✗ 需要 Python 3.9+，当前 %s" % sys.version.split()[0])
print(f"  ✓ python {sys.version.split()[0]} @ {sys.executable}")
EOF
command -v git >/dev/null || warn "没有 git（不影响运行，但无法自动更新）"

say "2/5 建目录（${ROOT}）"
for d in scripts memory data logs config backups; do
  [ "$DRY" = 1 ] && { echo "  [dry] mkdir -p $ROOT/$d"; continue; }
  mkdir -p "$ROOT/$d"
done
ok "$ROOT/{scripts,memory,data,logs,config,backups}"

say "3/5 配置"
ENV_FILE="$ROOT/coremem.env"
if [ -f "$ENV_FILE" ] || [ -f "$REPO/coremem.env" ]; then
  ok "coremem.env 已存在，保留不动（要改就手动编辑 ${ENV_FILE}）"
else
  [ "$DRY" = 1 ] && echo "  [dry] 生成 coremem.env（MYHOME=${ROOT}）" || {
    sed -e "s#\$HOME/.coremem#$ROOT#g" "$REPO/coremem.env.example" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  }
  ok "生成 ${ENV_FILE}（机器专属值都放这里，已 gitignore）"
fi

PROVIDERS="$ROOT/config/providers.json"
if [ -f "$PROVIDERS" ]; then
  ok "providers.json 已存在，保留不动"
elif [ -n "$DEEPSEEK_KEY" ] || [ -n "$OMLX_BASE_URL" ] || [ "$LOCAL_MODEL" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    echo "  [dry] 生成 providers.json"
  else
    DEEPSEEK_KEY="$DEEPSEEK_KEY" OMLX_BASE_URL="$OMLX_BASE_URL" OMLX_MODEL="$OMLX_MODEL" \
    LOCAL_MODEL="$LOCAL_MODEL" PROVIDERS="$PROVIDERS" GRAPH_ID="$GRAPH_ID" "$PY" - <<'EOF'
import json, os, pathlib
cfg = {"providers": {}}
if os.environ.get("DEEPSEEK_KEY"):
    cfg["providers"]["deepseek"] = {"baseUrl": "https://api.deepseek.com/v1",
                                    "model": "deepseek-chat",
                                    "apiKey": os.environ["DEEPSEEK_KEY"]}
if os.environ.get("LOCAL_MODEL") == "1" or os.environ.get("OMLX_BASE_URL"):
    omlx = {"enabled": True,
            "baseUrl": os.environ.get("OMLX_BASE_URL") or "http://127.0.0.1:9999/v1"}
    if os.environ.get("OMLX_MODEL"):
        omlx["model"] = os.environ["OMLX_MODEL"]
    cfg["providers"]["omlx"] = omlx
if not os.environ.get("LOCAL_MODEL") == "1" and os.environ.get("DEEPSEEK_KEY"):
    cfg["providers"]["omlx"] = {"enabled": False}
p = pathlib.Path(os.environ["PROVIDERS"])
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
os.chmod(p, 0o600)
print(f"  ✓ 写入 {p}（通道: {', '.join(cfg['providers']) or '无'}）")
EOF
  fi
else
  warn "没有指定模型通道：采集会跑但 LLM 提取会失败。加 --deepseek-key 或 --local-model"
fi

# 语义引擎的 graph 配置（PlugMem 兼容服务）
GRAPH_FILE="$ROOT/plugmem/config.json"
GRAPH_ID="${GRAPH_ID:-default}"
if [ ! -f "$GRAPH_FILE" ]; then
  if [ "$DRY" = 1 ]; then echo "  [dry] 生成 plugmem.json"; else
    mkdir -p "$(dirname "$GRAPH_FILE")"
    printf '{\n  "baseUrl": "http://127.0.0.1:8089",\n  "apiKey": "",\n  "defaultGraphId": "%s"\n}\n' "$GRAPH_ID" > "$GRAPH_FILE"
    chmod 600 "$GRAPH_FILE"; ok "生成 plugmem.json（graph=${GRAPH_ID}）"
  fi
elif [ ! -f "$GRAPH_FILE" ]; then
  warn "没有 ${GRAPH_FILE}：语义层检索/写入会报 graph 未配置（可加 --graph-id）"
fi

say "4/5 部署脚本 → ${PREFIX}"
if [ "$DRY" = 1 ]; then
  echo "  [dry] bash deploy/deploy.sh $REPO $PREFIX coremem-local --force"
else
  set +e
  bash "$REPO/deploy/deploy.sh" "$REPO" "$PREFIX" coremem-local --force >/tmp/coremem-deploy.log 2>&1
  DEPLOY_RC=$?
  set -e
  if [ ! -f "$PREFIX/remember.py" ]; then tail -20 /tmp/coremem-deploy.log; die "部署失败（脚本没落盘），详见 /tmp/coremem-deploy.log"; fi
  ok "$(grep -c 'syntax ok' /tmp/coremem-deploy.log) 个文件语法自检通过，已部署"
  if [ "$DEPLOY_RC" != 0 ]; then
    warn "deploy.sh 收尾冒烟有失败项（多半是语义引擎未启动，不影响脚本安装）"
  fi
fi

say "5/5 调度器"
if [ "$WITH_SCHEDULER" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    "$PY" "$REPO/deploy/scheduler.py" install --prefix "$PREFIX" --python "$PY" --dry-run
  else
    "$PY" "$REPO/deploy/scheduler.py" install --prefix "$PREFIX" --python "$PY" || warn "调度器安装有失败项（见上）"
  fi
else
  warn "跳过调度器（--no-scheduler）：记忆不会自动积累"
fi

say "冒烟自检"
if [ "$DRY" != 1 ] && [ -f "$PREFIX/remember.py" ]; then
  "$PY" "$PREFIX/remember.py" --status 2>&1 | head -6 || warn "remember.py --status 非零"
fi

cat <<EOF

完成。下一步：
  1) 写入第一条记忆： $PY $PREFIX/remember.py "我的第一条记忆" --tags demo
  2) 看调度状态：     $PY $REPO/deploy/scheduler.py status
  3) 语义引擎（可选）：需要 PlugMem 兼容服务在 127.0.0.1:8089，或用 MEM_SEMANTIC_* 指到别处
  4) 每日自检：       $PY $PREFIX/memory_health.py
EOF
