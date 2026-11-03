# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2026-11-03

### Changed
- Daily capture defaults to pi sessions only; OpenClaw history is an explicit read-only source with a one-shot backfill entry point.
- Local omlx configuration/wake and Feishu alerting are owned by coremem rather than loaded from OpenClaw scripts.
- Deployment now reads one `deploy/files.manifest` and drift-checks shell files too.

### Fixed
- Memory-outage alerts no longer pass through the recruiting notifier's title blocklist; failed sends remain immediately retryable.
- `--dry-run` no longer marks sessions processed, so a preview cannot suppress the real backfill.

### Added
- Regression runner plus isolated tests for source selection, archive reads, wake, alert delivery/retry and deploy guards.

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