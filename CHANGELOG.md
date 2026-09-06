# Changelog

All notable changes to this project will be documented in this file.

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