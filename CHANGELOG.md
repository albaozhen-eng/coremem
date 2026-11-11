# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2026-11-11

### Added
- MCP server纳入仓库（`mcp/`）+ 可选安装器 `deploy/install-mcp.sh` + 接口回归 `test-mcp-fix.mjs`；路径支持 PLUGMEM_CLI_DIR / PLUGMEM_CONFIG_FILE 覆盖。
- golden-gate 周更门禁任务（周一 5:30，`golden_eval.py --gate v2`）。

### Changed
- 备份扩容：除 data 外新增 workspace（L1 SoT/事实池/MEMORY.md）与 config；快照目录 chmod 700；git bundle 补上 coremem 仓库（原 memory-ops 路径不存在时不再静默空跑）。
- nightly 改跑 memory_index.py（AUTO 同步已退役，旧行为需 MEM_NIGHTLY=insight-scan）。

### Fixed
- health RED/YELLOW 接飞书告警（notify，MEM_HEALTH_NOTIFY=0 可关），不再只写盘无人知晓。
- MCP 接口漂移：memory_contradictions 改调 mem_crystallize.py scan-contradictions；memory_trends 改为直连 PlugMem API 统计近 N 天 tag 词频（不再依赖已删的 CLI 参数）。

## [Previous] - 2026-11-03

### Changed
- Daily capture defaults to pi sessions only; OpenClaw history is an explicit read-only source with a one-shot backfill entry point.
- Local omlx configuration/wake and Feishu alerting are owned by coremem rather than loaded from OpenClaw scripts.
- Deployment now reads one `deploy/files.manifest` and drift-checks shell files too.

### Fixed
- Memory-outage alerts no longer pass through the recruiting notifier's title blocklist; failed sends remain immediately retryable.
- `--dry-run` no longer marks sessions processed, so a preview cannot suppress the real backfill.

### Added
- Regression runner plus isolated tests for source selection, archive reads, wake, alert delivery/retry and deploy guards.

## [Unreleased] - 2026-11-03 (模型通道配置化)

### Changed
- `src/memory_capture.py::_build_llm_channels` 重写为**配置驱动**（v1.5）：通道取舍此前写死在
  代码里，而「哪家充值了、哪个模型退役、哪个端点老超时」是每周都在变的运维事实，每次调整都
  要改代码 → 部署 → 过漂移检查。现在只改 `~/.coremem/config/providers.json`：
  `capture_channels`（顺序与取舍，支持 `provider` 或 `provider:model`）+ `providers.<名>`
  （baseUrl/apiKey/models/model/timeout/enabled/extra）。未知 provider 名也能直接用，
  不必改代码；环境变量 `<PROVIDER>_API_KEY/_BASE_URL/_MODEL` 优先于文件。
- 本地通道识别从「通道名 == local-omlx-flash」改为 `channel["local"]` 标志位（provider 名
  现在可变）；`MEMORY_CAPTURE_SKIP_CHANNELS` 同时支持 provider 名与通道名。

### Added
- 百炼 Qwen4：`bailian` provider 默认模型 `qwen4-max-preview` + `qwen3.8-flash`
  （2026-11-03 实测均正常出文；2026-10-03 的 403 全线退役已解除）。
  百炼推理模型必须关思考链，故 provider 级 `extra: {"enable_thinking": false}`：
  不关时 `max_tokens` 会被 `reasoning_content` 吃空，triage（max_tokens=100）实测 100% 报
  empty content。实测延迟：flash ≈1.2s，qwen4-max ≈50s 且会 429 限流 —— 因此 qwen4 宜作
  质量兜底、flash 作主力。
- SCNet TokenPlan 模型清单恢复：`Qwen3.8-Max` / `DeepSeek-V4-Flash-0731` / `Kimi-K3`
  （端点 `https://api.scnet.cn/api/llm/v1`）。当前订阅仍过期（HTTP 403
  「Token Plan subscription is expired」），挂链无害：403 属账户级错误不重试、每条秒跳，
  实测三条 scnet 全 403 时总耗时仍 1.0s；续费后改配置即生效。

### Fixed
- `tests/test_flush.py` 未注入 `MEM_SEMANTIC_GRAPH_ID`：`plugmem_cfg()` 在 import 期硬校验
  graph id（防静默写错图），因此在**没装过 coremem 的机器上整个测试直接抛错**。补 setdefault
  占位值，测试与宿主配置解耦。

## [Unreleased] - 2026-11-03 (安装体验)

### Added
- `deploy/install.sh` — **一键安装**：目录/配置/部署/调度器/冒烟一次跑完，重复执行幂等，
  已有 `coremem.env` 与 `providers.json` 不覆盖。
- `deploy/scheduler.py` — **调度器随仓库分发**（此前要每台机器手写 plist）：6 个任务
  （capture 30min / buffer-flush 60min / briefing 08:00 / nightly 03:00 / health 09:00 /
  backup 04:00），macOS 用 launchd、Linux 用用户 crontab，支持 install/status/uninstall。
- `src/memory_capture.py` 通道 1 可关（`providers.omlx.enabled=false` 或 `MEM_OMLX_DISABLED=1`）：
  没有本地 omlx 的机器此前每轮要白等一次唤醒超时（实测 90s）。
- `src/memory_capture.py` DeepSeek 官网通道恢复为配置驱动（`providers.deepseek` / `DEEPSEEK_*`）。

### Fixed
- `src/plugmem_backup.py` 快照源目录可用 `MEM_PLUGMEM_DATA` 覆盖（原写死 `~/.coremem/plugmem`，
  非标准布局的机器会备份到空目录，`memory_health` 的 L2 快照检查永远红）。
- `deploy/patch_deploy.py` 现在也 patch `scripts/*.sh`（此前裸拷贝，shell 里的路径无法随时间迁移）；
  新增 profile `exclude` 支持。
- `deploy/deploy.sh` 语法自检跳过 profile 排除掉的文件。
- `scripts/secret_scan.py` 放行本机隧道端口（18081 / 18089），与既有 8089/9999/7897 同类。

## [0.2.0] - 2026-09-06

### Added
- `src/vault_export.py` — **L5 knowledge projection**: export the PlugMem memory graph to an Obsidian vault (frontmatter for Dataview, `[[wikilinks]]` as edges, tag pages, auto-incremental). Turns agent memory into a human-readable graph.
- `README.zh-CN.md` — bilingual (EN/ZH) positioning, six-layer tables, benchmarks
- `deploy/profiles/` — per-machine deploy profiles (`openclaw-scripts`, `plugmem-suite-cli`); `patch_deploy.py --profile` + `deploy.sh` accept DEST/PROFILE and run a post-deploy smoke test
- Unified `MEM_DATA_DIR` to `~/.plugmem/data` across both runtimes (eliminated dual write-buffer drift)

## [0.1.0] - 2026-08-29

### Added
- Initial skeleton: six-layer memory architecture reference implementation
- `src/remember.py` — unified memory write entry point (three-layer fan-out)
- `src/memory_write_buffer.py` — write buffer manager with auto-retry
- `src/memory_insight_scan.py` — semantic index → compiled truth synchronizer
- `src/morning_briefing.py` — daily memory briefing (deterministic, zero LLM)
- `src/memory_recall.py` — full-layer memory retrieval tool
- `docs/architecture.md` — six-layer data flow documentation
- `scripts/secret_scan.py` — privacy leak scanner for pre-commit
- `.pre-commit-config.yaml` — pre-commit hooks (detect-private-key + secret_scan)
- MIT License