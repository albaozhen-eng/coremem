#!/usr/bin/env bash
# install-mcp.sh — 安装/升级 PlugMem MCP server（可选组件，pi 等 MCP 客户端用）
# 用法: bash deploy/install-mcp.sh [DEST]
#   DEST 默认 ~/.plugmem/mcp
# 做什么: 备份旧 server.mjs → 拷贝 mcp/ 三件套 → 缺依赖时 npm install → 跑接口回归。
# 为什么: 2026-11-11 审计发现 MCP 不在部署清单，CLI 升级后接口漂移导致 2 个工具静默损坏。
#   现在 MCP 与 CLI 同仓库同源；**每次 deploy.sh 升级 cli 后，建议重跑本脚本或至少跑回归**:
#   node <DEST>/test-mcp-fix.mjs
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/.plugmem/mcp}"
STAMP="$(date +%Y%m%d_%H%M%S)"

command -v node >/dev/null || { echo "✗ 需要 node (>=22)"; exit 1; }
mkdir -p "$DEST"
if [ -f "$DEST/server.mjs" ]; then
  cp -p "$DEST/server.mjs" "$DEST/server.mjs.bak-$STAMP"
  echo "✓ 旧版已备份: server.mjs.bak-$STAMP"
fi
cp "$REPO/mcp/server.mjs" "$REPO/mcp/test-mcp-fix.mjs" "$REPO/mcp/package.json" "$DEST/"

if [ ! -d "$DEST/node_modules/@modelcontextprotocol/sdk" ]; then
  echo "… 安装依赖（npm install --omit=dev）"
  (cd "$DEST" && npm install --omit=dev --no-audit --no-fund)
fi

node --check "$DEST/server.mjs"
echo "… 接口回归（trends / contradictions / stats）"
(cd "$DEST" && node test-mcp-fix.mjs "$DEST/server.mjs")
echo "✓ MCP 安装完成: $DEST"
echo "  提示: pi 等客户端需重启会话才会加载新 server 进程。"
echo "  路径可用环境变量覆盖: PLUGMEM_CLI_DIR / PLUGMEM_CONFIG_FILE"