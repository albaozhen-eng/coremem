# Changelog

All notable changes to this project will be documented in this file.

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