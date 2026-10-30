#!/bin/bash
# pre-delete-openclaw.sh — 删 ~/.openclaw 之前跑一遍。
#
# 只读检查，不修改任何东西。退出码：
#   0  未发现依赖，可以删除
#   1  发现活跃依赖，先处理再删
#
# 2026-10-30 建立：coremem 已与 OpenClaw 解耦（记忆数据/脚本/CHROMA_PATH 全部
# 在 ~/.coremem），本脚本用来确认没有残留的读者/写者。

set -uo pipefail
OC="$HOME/.openclaw"
problems=0

say() { printf '%s\n' "$*"; }
warn() { printf '  ✗ %s\n' "$*"; problems=$((problems + 1)); }
ok() { printf '  ✓ %s\n' "$*"; }

say "== 1. 进程是否还打开着 $OC 下的文件"
hits=$(lsof -nP 2>/dev/null | awk -v p="$OC" 'index($0, p) == 0 { next } $0 !~ /pre-delete-openclaw/ { print }' | head -20)
if [ -n "$hits" ]; then
  warn "以下进程仍持有 .openclaw 下的文件："
  printf '%s\n' "$hits" | sed 's/^/      /'
else
  ok "没有进程持有 .openclaw 文件"
fi

say "== 2. 进程工作目录 / 命令行仍在 .openclaw"
running=$(ps -Axo pid,command | awk -v p="$OC" 'index($0, p) > 0 { print }' | grep -v pre-delete-openclaw | head -20)
if [ -n "$running" ]; then
  warn "仍有进程在跑 .openclaw 里的东西："
  printf '%s\n' "$running" | sed 's/^/      /'
else
  ok "没有进程在跑 .openclaw 里的脚本"
fi

say "== 3. launchd 里引用 .openclaw 的任务"
plists=$(grep -lF "$OC" "$HOME/Library/LaunchAgents"/*.plist 2>/dev/null)
if [ -n "$plists" ]; then
  warn "这些 launchd 仍指向 .openclaw（迁移或卸载后再删）："
  printf '%s\n' "$plists" | while read -r f; do
    printf '      %s\n' "$(basename "$f")"
  done
else
  ok "launchd 中没有 .openclaw 引用"
fi

say "== 4. crontab 引用"
if crontab -l 2>/dev/null | grep -qF "$OC"; then
  warn "crontab 仍引用 .openclaw"
else
  ok "crontab 干净"
fi

say "== 5. 24 小时内仍被写入的文件（活跃写者信号）"
recent=$(find "$OC" -type f -mtime -1 -not -path '*/backups/*' -not -path '*/logs/*' 2>/dev/null | head -15)
if [ -n "$recent" ]; then
  warn "最近 24h 内有写入："
  printf '%s\n' "$recent" | sed 's/^/      /'
else
  ok "24h 内无业务写入（日志/备份除外）"
fi

say "== 6. 外部配置里对 .openclaw 的引用"
configs=(
  "$HOME/.pi/agent/settings.json"
  "$HOME/.pi/agent/models.json"
  "$HOME/.pi/agent/mcp.json"
  "$HOME/.claude.json"
  "$HOME/.codex/config.toml"
  "$HOME/.paseo/config.json"
  "$HOME/.plugmem/config/config.json"
  "$HOME/coremem/coremem.env"
)
found=0
for f in "${configs[@]}"; do
  [ -f "$f" ] || continue
  if grep -qF "$OC" "$f" 2>/dev/null; then
    warn "$f 仍引用 .openclaw"
    found=1
  fi
done
[ "$found" -eq 0 ] && ok "常用配置文件无引用"

say "== 7. coremem 自身是否仍依赖 .openclaw"
if grep -rqF "$OC" "$HOME/.coremem/scripts" 2>/dev/null; then
  warn "~/.coremem/scripts 里仍有 .openclaw 字面量（注释除外请核对）"
else
  ok "~/.coremem/scripts 无 .openclaw 依赖"
fi

say ""
if [ "$problems" -eq 0 ]; then
  say "结论：未发现依赖，可以删除 $OC。建议先 mv 成 .openclaw.parked 观察 48h，再真删。"
  exit 0
fi
say "结论：发现 $problems 处活跃依赖，先处理再删。"
exit 1
