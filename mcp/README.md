# plugmem-mcp

PlugMem 记忆系统的 MCP server（stdio），给 pi / OpenClaw / Codex 等 MCP 客户端提供
9 个记忆工具（search / capture / stats / briefing / rate / contradictions / crystallize / trends）。

## 安装

```bash
bash deploy/install-mcp.sh            # 默认装到 ~/.plugmem/mcp
bash deploy/install-mcp.sh /自定义/目录
```

依赖：node ≥ 22、本机 PlugMem 服务（默认 127.0.0.1:8089）、已部署的 coremem CLI。

## 路径约定（可用环境变量覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PLUGMEM_CLI_DIR` | `~/.plugmem/cli` | coremem CLI 脚本目录 |
| `PLUGMEM_CONFIG_FILE` | `~/.plugmem/config/config.json` | PlugMem 服务地址/key/图名 |

## 防漂移纪律

server.mjs 通过子进程调 CLI 脚本，**CLI 升级可能删改子命令/参数**
（2026-11-11 事故：contradictions/trends 静默损坏数天）。因此：

1. MCP 与 CLI 同仓库同源，不再手改生产文件；
2. 每次 `deploy.sh` 升级 CLI 后跑回归：`node ~/.plugmem/mcp/test-mcp-fix.mjs`；
3. 新增工具时同步在 test-mcp-fix.mjs 里加用例。