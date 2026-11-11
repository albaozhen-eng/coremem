#!/usr/bin/env python3
"""凭据闸门回归（2026-11-11 审计：回填把宝塔密码写进记忆的事故防再犯）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from memory_capture import redact_credentials  # noqa: E402

CASES = [
    # (输入, 期望是否脱敏, 期望不包含的子串)
    ("宝塔面板初始密码为 0841d2d4f21d", True, "0841d2d4f21d"),
    ("管理员 password: Hunter2secret", True, "Hunter2secret"),
    ("百炼 key 是 sk-sp-ABCDEFGHIJ1234567890abcdef", True, "sk-sp-ABCDEFGHIJ"),
    ("api_key=abcdef1234567890abcd", True, "abcdef1234567890abcd"),
    ("access_token: eyJhbGciOiJIUzI1NiJ9", True, "eyJhbGciOiJIUzI1NiJ9"),
    # 不该误杀的
    ("本机未保存雨云服务器 root 明文密码，仅存在哈希值", False, None),
    ("SSH root 登录使用密钥文件 ~/.ssh/truedha_rainyun", False, None),
    ("用户要求恢复 scnet max 的配置", False, None),
    ("密码策略要求至少 12 位", False, None),  # 「密码策略」后无 为/是/冒号
]

fails = 0
for text, want_redact, forbidden in CASES:
    out, got = redact_credentials(text)
    ok = (got == want_redact) and (forbidden is None or forbidden not in out)
    print(("✅" if ok else "❌"), text[:40], "→", out[:60])
    if not ok:
        fails += 1
sys.exit(1 if fails else 0)