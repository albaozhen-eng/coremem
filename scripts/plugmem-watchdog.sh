#!/bin/bash
# ============================================
# PlugMem 写入健康 watchdog
# 由 launchd 每 30 分钟触发
# 检测：进程存活 + 数据目录最近写入时间
# 超过 6 小时无写入 → 写告警日志（供健康巡检上报）
# ============================================
LOG="$HOME/.coremem/logs/plugmem-watchdog.log"
DATA="$HOME/.coremem/plugmem"
THRESHOLD_SECONDS=21600  # 6h

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"; }

# 1. 进程检查
if ! pgrep -f "plugmem.*uvicorn" > /dev/null 2>&1; then
    log "❌ PlugMem 进程未运行"
    exit 1
fi

# 2. 数据写入检查
if [ -d "$DATA" ]; then
    # 最近写入时间（unix 秒）
    LAST=$(find "$DATA" -type f \( -name "*.json" -o -name "*.jsonl" -o -name "*.bin" -o -name "*.pickle" \) \
        -not -path "*/chroma/*" -newermt "-30 days" -exec stat -f "%m" {} \; 2>/dev/null | sort -rn | head -1)
    if [ -z "$LAST" ]; then
        log "⚠️ 数据目录无近期文件"
        exit 1
    fi
    NOW=$(date +%s)
    AGE=$(( NOW - LAST ))
    if [ "$AGE" -gt "$THRESHOLD_SECONDS" ]; then
        HOURS=$(( AGE / 3600 ))
        log "🔴 PlugMem 停写告警：最近写入 ${HOURS}h 前（超过阈值 6h）"
    else
        MIN=$(( AGE / 60 ))
        log "✅ PlugMem 正常（${MIN} 分钟前有写入）"
    fi
else
    log "❌ 数据目录不存在: $DATA"
    exit 1
fi

# 3. 日志轮转（500 行）
tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
