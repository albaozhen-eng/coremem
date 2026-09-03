#!/usr/bin/env python3
"""
secret_scan.py — 隐私泄露扫描器
==============================
扫描整个仓库目录，检测可能泄露的敏感信息：
  - IP 地址（含内网段）
  - 域名（含 .com/.cn/.net/.org 等）
  - 手机号（中国手机号格式）
  - API Key / Token 格式
  - 绝对路径（含 /Users/ 等）

用法：
  python3 scripts/secret_scan.py [目录]
  python3 scripts/secret_scan.py --check  # pre-commit hook 模式

Exit codes:
  0 = 无命中
  1 = 有命中（阻断提交）
"""

import argparse
import os
import re
import sys
from pathlib import Path

# === 敏感模式正则 ===
PATTERNS = {
    "IPv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"),
    "Private Domain": re.compile(r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.(?:cn|net|org|io|ai|dev|app|co|xyz|info|me|cc|top|tech|site|online|store|shop|club|live|world|today|news|blog|wiki|space|media|design|cloud)\b", re.IGNORECASE),
    "Phone": re.compile(r"\b1[3-9]\d{9}\b"),
    "API Key": re.compile(r"\b(sk-[a-zA-Z0-9]{20,}|api[_-]?key[=:]\s*['\"][^'\"]{8,}['\"]|X-API-Key:\s*\S{8,})", re.IGNORECASE),
    "Home Path": re.compile(r"/Users/[a-zA-Z0-9_]+"),
    "Port with localhost": re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d{2,5}\b"),
}

# 排除目录（不扫描）
EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".tox", ".eggs", "*.egg-info",
}

# 排除文件（不扫描）
EXCLUDE_FILE_PATTERNS = [
    "*.pyc", "*.pyo", "*.so", "*.dylib", "*.bin",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.svg",
    "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.zip", "*.tar", "*.gz", "*.bz2", "*.xz",
    "poetry.lock", "package-lock.json", "yarn.lock",
    "LICENSE", "CHANGELOG.md",
    "scripts/secret_scan.py",  # 不扫描自身
]

# 已知无害的"假阳性"允许列表
ALLOWLIST_DOMAINS = {
    "github.com", "gitlab.com", "bitbucket.org",
    "python.org", "pypi.org", "npmjs.com",
    "opensource.org", "apache.org",
    "google.com", "microsoft.com",
    "example.com", "example.org", "example.net",
}

ALLOWLIST_IPS = {
    "127.0.0.1", "0.0.0.0", "localhost",
}

ALLOWLIST_PORTS = {
    "127.0.0.1:8089", "127.0.0.1:8080", "127.0.0.1:3000",
    "0.0.0.0:8080", "0.0.0.0:3000", "0.0.0.0:8089",
    "localhost:8080", "localhost:3000", "localhost:8089",
}


def should_exclude(path: Path) -> bool:
    """检查文件/目录是否应排除"""
    for part in path.parts:
        if part in EXCLUDE_DIRS:
            return True
    for pattern in EXCLUDE_FILE_PATTERNS:
        if path.match(pattern):
            return True
    # 也检查相对路径匹配
    rel = str(path)
    for pattern in EXCLUDE_FILE_PATTERNS:
        if pattern.endswith('/') and rel.startswith(pattern):
            return True
        if '/' in pattern and rel.endswith(pattern):
            return True
    if ".git" in path.parts:
        return True
    return False


def scan_file(filepath: Path) -> list[dict]:
    """扫描单个文件，返回命中列表"""
    hits = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return hits

    for pattern_name, pattern in PATTERNS.items():
        for match in pattern.finditer(content):
            matched_text = match.group(0)
            match_lower = matched_text.lower()
            
            # 允许列表过滤
            if pattern_name == "Private Domain":
                if match_lower in ALLOWLIST_DOMAINS or match_lower.rstrip('.') in ALLOWLIST_DOMAINS:
                    continue
            if pattern_name == "IPv4":
                if matched_text in ALLOWLIST_IPS:
                    continue
            if pattern_name == "Port with localhost":
                if matched_text in ALLOWLIST_PORTS:
                    continue
            
            line_no = content[:match.start()].count("\n") + 1
            hits.append({
                "file": str(filepath),
                "pattern": pattern_name,
                "match": matched_text,
                "line": line_no,
            })

    return hits


def scan_directory(root_dir: str, check_mode: bool = False) -> list[dict]:
    """递归扫描目录"""
    all_hits = []
    root = Path(root_dir).resolve()

    for filepath in sorted(root.rglob("*")):
        if not filepath.is_file():
            continue
        if should_exclude(filepath):
            continue

        rel = filepath.relative_to(root)
        if check_mode:
            print(f"  扫描: {rel}", file=sys.stderr)
        hits = scan_file(filepath)
        all_hits.extend(hits)

    return all_hits


def main():
    parser = argparse.ArgumentParser(
        description="隐私泄露扫描器 — 检测仓库中的敏感信息",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 scripts/secret_scan.py
  python3 scripts/secret_scan.py /path/to/repo
  python3 scripts/secret_scan.py --check
        """,
    )
    parser.add_argument("directory", nargs="?", default=".", help="扫描目录（默认当前目录）")
    parser.add_argument("--check", action="store_true", help="pre-commit hook 模式（静默扫描）")
    parser.add_argument("--tracked", action="store_true", help="只扫描 git 跟踪的文件（尊重 .gitignore，hook 推荐）")
    args = parser.parse_args()

    target = args.directory if args.directory else "."
    if not os.path.isdir(target):
        print(f"❌ 目录不存在: {target}", file=sys.stderr)
        sys.exit(2)

    if args.tracked:
        import subprocess
        out = subprocess.run(["git", "ls-files"], cwd=target, capture_output=True, text=True)
        files = [Path(target) / f for f in out.stdout.splitlines()]
        all_hits = []
        for fp in files:
            if fp.is_file() and not should_exclude(fp.relative_to(Path(target).resolve() if Path(target).is_absolute() else Path(target).resolve())):
                all_hits.extend(scan_file(fp))
        hits = all_hits
    else:
        hits = scan_directory(target, check_mode=args.check)

    if hits:
        print(f"\n❌ 发现 {len(hits)} 条疑似敏感信息:")
        for h in hits:
            print(f"  [{h['pattern']}] {h['file']}:{h['line']} → \"{h['match']}\"")
        print(f"\n⚠️ 请移除敏感信息后重新提交。")
        sys.exit(1)
    else:
        print("✅ 未发现敏感信息")
        sys.exit(0)


if __name__ == "__main__":
    main()