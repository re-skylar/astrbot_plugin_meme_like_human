"""功能A：词库加载、索引、匹配的验证。

不依赖 AstrBot，用假的 config dict 直接跑 MemeKnowledge。
"""

import gzip
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# MemeKnowledge 里 import 了 astrbot.api.logger，这里做个 stub
import types

fake_astrbot = types.ModuleType("astrbot")
fake_api = types.ModuleType("astrbot.api")


class _Logger:
    def info(self, *a, **k):
        pass

    warning = info
    error = info
    debug = info


fake_api.logger = _Logger()
fake_astrbot.api = fake_api
sys.modules.setdefault("astrbot", fake_astrbot)
sys.modules.setdefault("astrbot.api", fake_api)

# aiohttp 桩：本测试不涉及网络，只需让 import 通过
if "aiohttp" not in sys.modules:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        fake_aiohttp = types.ModuleType("aiohttp")

        class _Timeout:
            def __init__(self, *a, **k):
                pass

        class _Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, *a, **k):
                raise RuntimeError("测试环境无网络")

            def post(self, *a, **k):
                raise RuntimeError("测试环境无网络")

        fake_aiohttp.ClientTimeout = _Timeout
        fake_aiohttp.ClientSession = _Session
        sys.modules["aiohttp"] = fake_aiohttp

import asyncio  # noqa: E402

from meme_kb import MemeKnowledge, _condense, _normalize  # noqa: E402


async def main():
    # ---------- 1. gz 词库可读 ----------
    gz = ROOT / "data" / "memes.json.gz"
    raw = ROOT / "data" / "memes.json"
    print(f"gz 存在: {gz.exists()}  大小: {gz.stat().st_size/1024/1024:.2f} MB" if gz.exists() else "")
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        payload = json.load(f)
    print(f"count={payload['count']}  license={payload['license']}")
    print("署名:", payload.get("attribution", {}).get("required_notice"))
    assert payload["count"] > 10000

    # ---------- 2. 加载并建索引 ----------
    tmp = Path(tempfile.mkdtemp())
    cfg = {"search_enabled": False, "auto_update_dict": False, "search_daily_max": 200}
    kb = MemeKnowledge(tmp, ROOT / "data", cfg)

    t0 = time.time()
    await kb.initialize()
    cost = (time.time() - t0) * 1000
    print(f"\n初始化耗时: {cost:.0f} ms   入库: {kb.total} 条")
    assert kb.total > 10000, kb.total

    # ---------- 3. 匹配测试 ----------
    cases = [
        ("这个真的绝绝子", ["绝绝子"]),
        ("别人说什么都没用，我蚌埠住了", ["蚌埠住了"]),
        # 原文必须与词库条目字面一致才对；「你这个人还怪好嘞」中间多个「这个」不算命中
        ("你人还怪好嘞", ["你人还怪好嘞"]),
        ("今天加班到十点，太离谱了", ["离谱"]),
        ("泰裤辣！这波操作", ["泰裤辣"]),
        ("这简直是yyds", ["yyds"]),
        ("显眼包本包了", ["显眼包"]),
        ("普通的一句话没有任何梗", []),
    ]
    print("\n=== 匹配测试 ===")
    bad = 0
    for text, expect in cases:
        t = time.time()
        hits = kb.match(text)
        dt = (time.time() - t) * 1000
        got = [h["term"] for h in hits]
        ok = all(e in got for e in expect)
        if not ok:
            bad += 1
        print(f"  [{'OK' if ok else 'FAIL'}] {text[:20]:22s} -> {got}  ({dt:.2f}ms)")
    assert bad == 0, f"{bad} 个匹配用例失败"

    # ---------- 3b. 最长优先验证 ----------
    print("\n=== 最长优先验证 ===")
    long_cases = [
        ("离谱他妈给离谱开门", "离谱他妈给离谱开门"),
        ("你人还怪好嘞", "你人还怪好嘞"),
    ]
    for text, expect_longest in long_cases:
        hits = kb.match(text, limit=1)
        got = hits[0]["term"] if hits else None
        ok = got == expect_longest
        print(f"  [{'OK' if ok else 'FAIL'}] {text} -> {got} (期望最长 {expect_longest})")
        assert ok, f"最长优先失效：{got} != {expect_longest}"

    # ---------- 4. 精确查询 ----------
    print("\n=== 精确查询 ===")
    for term in ["绝绝子", "显眼包", "蚌埠住了"]:
        rec = await kb.lookup(term)
        if rec:
            m = (rec["meaning"] or "")[:50]
            print(f"  [OK] {term}: {m}")
        else:
            print(f"  [MISS] {term}: 库中无此条")

    # ---------- 5. 压缩函数 ----------
    print("\n=== 释义压缩 ===")
    long_text = "蚌埠住了，网络流行语。源自安徽蚌埠的谐音。该词指情绪受到冲击，绷不住了的谐音梗。现在多用于表达情绪激动。"
    c = _condense(long_text, 40)
    print(f"  原文 {len(long_text)} 字 -> 压缩 {len(c)} 字")
    print(f"  {c}")
    assert len(c) <= 42, len(c)

    # ---------- 6. 归一化 ----------
    assert _normalize(" 绝 绝 子！ ") == "绝绝子"
    assert _normalize("YYDS??") == "yyds"
    print("\n[OK] 归一化")

    await kb.terminate()
    print("\n=== 功能A 测试全部通过 ===")


asyncio.run(main())
