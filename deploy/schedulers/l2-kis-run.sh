#!/bin/bash
# 每月跑一次 L2 已知项评测，产出 golden/reports/l2kis-<ts>.json。
# 为什么需要：memory_health 的 l2_recall 探针 >8 天无新报告即判黄。
# 2026-11-11 之前没有任何调度器生产它，于是探针盯着一份 16 天前的文件永远黄。
set -uo pipefail
S="$HOME/.coremem/scripts"; H="$HOME/.coremem/health"; R="$HOME/.coremem/memory/golden/reports"
LOG="$HOME/.coremem/logs/l2-kis.log"; HB="$H/l2-kis.heartbeat"
mkdir -p "$H" "$R" "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
printf '%s started\n' "$(ts)" > "$HB"
/usr/bin/python3 "$S/l2_kis.py" --n 200 --out "$R/l2kis-$(date +%Y%m%d-%H%M).json" >>"$LOG" 2>&1
RC=$?
printf '%s rc=%s\n' "$(ts)" "$RC" > "$HB"
echo "[$(ts)] rc=$RC" >>"$LOG"
exit $RC
