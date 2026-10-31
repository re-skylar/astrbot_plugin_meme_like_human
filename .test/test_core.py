"""核心算法单元测试（不依赖 AstrBot）。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jargon_match import format_injection, match_jargons, normalize  # noqa: E402
from jargon_store import INFERENCE_THRESHOLDS, JargonStore  # noqa: E402


def test_match():
    recs = [
        {"content": "绝绝子", "meaning": "太绝了的加强版", "count": 30, "is_global": True},
        {"content": "yyds", "meaning": "永远的神", "count": 12, "is_global": True},
        {"content": "冷门词", "meaning": "测试用", "count": 3, "is_global": False},
    ]
    m = match_jargons("这个真的绝绝子，yyds！", records=recs, limit=5)
    got = {x["content"] for x in m}
    assert got == {"绝绝子", "yyds"}, got
    print("[OK] 匹配:", sorted(got))

    inj = format_injection(m, max_chars=150)
    assert inj, "注入块为空"
    assert len(inj) <= 200, len(inj)
    assert "绝绝子" in inj
    print("[OK] 注入块长度:", len(inj))
    print("---- 注入块预览 ----")
    print(inj)
    print("---------------------")


def test_thresholds():
    assert INFERENCE_THRESHOLDS == (4, 8, 25, 100), INFERENCE_THRESHOLDS
    print("[OK] 阈值:", INFERENCE_THRESHOLDS)

    cases = [
        ({"count": 3, "last_inference_count": 0, "is_complete": 0, "created_by": "ai"}, False, "count<4"),
        ({"count": 4, "last_inference_count": 0, "is_complete": 0, "created_by": "ai"}, True, "count=4 首次"),
        ({"count": 4, "last_inference_count": 4, "is_complete": 0, "created_by": "ai"}, False, "同阈值已推断"),
        ({"count": 8, "last_inference_count": 4, "is_complete": 0, "created_by": "ai"}, True, "跨到 8"),
        ({"count": 7, "last_inference_count": 4, "is_complete": 0, "created_by": "ai"}, False, "未到 8"),
        ({"count": 25, "last_inference_count": 8, "is_complete": 0, "created_by": "ai"}, True, "跨到 25"),
        ({"count": 100, "last_inference_count": 25, "is_complete": 0, "created_by": "ai"}, True, "跨到 100"),
        ({"count": 100, "last_inference_count": 100, "is_complete": 1, "created_by": "ai"}, False, "已完成"),
        ({"count": 50, "last_inference_count": 0, "is_complete": 0, "created_by": "manual"}, False, "手动词条"),
    ]
    bad = 0
    for rec, expect, label in cases:
        got = JargonStore.should_infer(rec)
        flag = "OK" if got == expect else "FAIL"
        if got != expect:
            bad += 1
        print(f"  [{flag}] {label}: 期望={expect} 实际={got}")
    assert bad == 0, f"{bad} 项不符合预期"


def test_store():
    tmp = Path(tempfile.mkdtemp())
    st = JargonStore(tmp / "j.db")

    rec, c = st.upsert_candidate("测试黑话", "group:1", "证据A")
    assert c == 1, c
    rec, c = st.upsert_candidate("测试黑话", "group:1", "证据B")
    assert c == 2, c
    print("[OK] 计次:", c)

    rec2, c2 = st.upsert_candidate("啊", "group:1", None)
    assert rec2 is None and c2 == 0, (rec2, c2)
    print("[OK] 单字过滤")

    rec2, c2 = st.upsert_candidate("这个太长了根本不像黑话的句子", "group:1", None)
    assert rec2 is None, "超长词条应被拒绝"
    print("[OK] 超长过滤")

    st.add_manual("测试黑话", "人工含义")
    st.save_inference("测试黑话", "AI含义", True, 99, 99)
    rec = st.get_by_content("测试黑话")
    assert rec["meaning"] == "人工含义", rec["meaning"]
    assert rec["created_by"] == "manual"
    print("[OK] manual 保护")

    st.save_inference("新词", "某含义", True, 4, 4)
    rec, _ = st.upsert_candidate("新词", "group:1", None)
    st.save_inference("新词", "推断含义", True, 8, 8)
    rec = st.get_by_content("新词")
    assert rec["is_jargon"] == 1 and rec["meaning"] == "推断含义", rec
    print("[OK] 推断结果写回")

    st.save_inference("新词", "完成", True, 100, 100)
    rec = st.get_by_content("新词")
    assert rec["is_complete"] == 1, rec
    print("[OK] count>=100 标记完成")

    rows, total = st.list_jargon(page=1, page_size=10)
    print(f"[OK] 列表: {total} 条")
    stt = st.stats()
    print("[OK] 统计:", stt)

    st.close()


def test_normalize():
    assert normalize("绝 绝 子！") == "绝绝子"
    assert normalize("YYDS？") == "yyds"
    print("[OK] 归一化")


if __name__ == "__main__":
    test_normalize()
    test_match()
    test_thresholds()
    test_store()
    print("\n=== 全部测试通过 ===")
