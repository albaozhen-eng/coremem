#!/usr/bin/env python3
"""One-shot, read-only OpenClaw history backfill through the normal capture pipeline.

Examples:
  python3 migrate_openclaw_archive.py --since 2026-08-12 --until '2026-08-12 23:59' --dry-run
  python3 migrate_openclaw_archive.py --hours 24 --max-sessions 3

The archive itself is never modified. Use the same bounded windows and dry-run
review as normal capture; processed-session state prevents accidental repeats.
"""
import sys
import memory_capture


def main():
    if "--sources" in sys.argv:
        raise SystemExit("migrate_openclaw_archive.py 固定使用 --sources openclaw，请勿重复指定")
    sys.argv.extend(["--sources", "openclaw"])
    memory_capture.main()


if __name__ == "__main__":
    main()
