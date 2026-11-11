#!/bin/bash
# ============================================
# L3 派生规则层（每周一次）
#
# 设计要点（不重演 wiki-autocommit 的静默假绿）：
#   * 启动即写 started 状态；结束写心跳。心跳缺失 = 任务没跑。
#   * 退出码原样透传：0=正常 1=有规则被拒 2=配置/语料问题
#   * 结果落 health/l3-schema.json，供看板/日报消费
# ============================================
set -uo pipefail
# 加载 coremem 配置：脚本的路径靠 MEM_* 环境变量决定，不加载就会用默认值。
# 2026-11-11 迁移时正是因为没加载 + 默认值指旧根，导致「取近 7 天：0 条事实」却 rc=0。
[ -f "$HOME/.coremem/coremem.env" ] && . "$HOME/.coremem/coremem.env"
# 跑活体副本，避免把仓库里未提交的改动直接带进生产（复核 2026-10-26 指出）
CHECKER="$HOME/.coremem/scripts/l3_schema_induct.py"
HEALTH="$HOME/.coremem/health"
LOG="$HOME/.coremem/logs/l3-schema.log"
HB="$HEALTH/l3-schema.heartbeat"
JSON="$HEALTH/l3-schema.json"
mkdir -p "$HEALTH" "$(dirname "$LOG")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
printf '%s started\n' "$(ts)" > "$HB"

if [ ! -f "$CHECKER" ]; then
    echo "[$(ts)] 找不到 $CHECKER" >>"$LOG"
    printf '%s rc=2 checker-missing\n' "$(ts)" > "$HB"
    exit 2
fi

DRAFT="$HOME/.coremem/memory/golden/l3-draft.md"
python3 "$CHECKER" --days 7 --threshold 0.65 --draft-out "$DRAFT" --limit-clusters 60 --json "$JSON" >>"$LOG" 2>&1
RC=$?

if [ -f "$JSON" ]; then
    SUMMARY=$(python3 -c "
import json
try:
    d=json.load(open('$JSON'))
    print(f\"通过 {d['passed']} / 拒绝 {d['rejected']}（{d['facts']} 条事实 / {d['clusters']} 簇）\")
except Exception:
    print('结果读取失败')
" 2>/dev/null)
else
    SUMMARY="无 JSON 产物"
fi

printf '%s rc=%s %s\n' "$(ts)" "$RC" "$SUMMARY" > "$HB"
echo "[$(ts)] rc=$RC $SUMMARY" >>"$LOG"
exit $RC
