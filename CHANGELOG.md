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