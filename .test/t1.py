import sys, json, gzip, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---- 1. 测试 normalize 与匹配 ----
from jargon_match import match_jargons, format_injection, normalize
recs = [
    {"content":"绝绝子","meaning":"太绝了的加强版","count":30,"is_global":True},
    {"content":"yyds","meaning":"永远的神","count":12,"is_global":True},
    {"content":"冷门词","meaning":"测试用","count":3,"is_global":False},
]
m = match_jargons("这个真的绝绝子，yyds！", records=recs, limit=5)
print("匹配:", [x["content"] for x in m])
assert {x["content"] for x in m} == {"绝绝子","yyds"}, m

inj = format_injection(m, max_chars=150)
print("--- 注入块 ---")
print(inj)
assert len(inj) <= 200
assert "可以偶尔" in inj or "只需理解" in inj

# ---- 2. 阈值调度 ----
from jargon_store import JargonStore, INFERENCE_THRESHOLDS
print("\n阈值:", INFERENCE_THRESHOLDS)
assert INFERENCE_THRESHOLDS == (4,8,25,100)

cases = [
    ({"count":3,"last_inference_count":0,"is_complete":0,"created_by":"ai"}, False, "count<4"),
    ({"count":4,"last_inference_count":0,"is_complete":0,"created_by":"ai"}, True,  "count=4首次"),
    ({"count":4,"last_inference_count":4,"is_complete":0,"created_by":"ai"}, False, "已推断过"),
    ({"count":8,"last_inference_count":4,"is_complete":0,"created_by":"ai"}, True,  "跨到8"),
    ({"count":7,"last_inference_count":4,"is_complete":0,"created_by":"ai"}, False, "未到8"),
    ({"count":25,"last_inference_count":8,"is_complete":0,"created_by":"ai"}, True, "跨到25"),
    ({"count":100,"last_inference_count":25,"is_complete":0,"created_by":"ai"}, True,"跨到100"),
    ({"count":100,"last_inference_count":100,"is_complete":1,"created_by":"ai"}, False,"已完成"),
    ({"count":50,"last_inference_count":0,"is_complete":0,"created_by":"manual"}, False,"手动词条"),
]
ok = True
for rec, expect, label in cases:
    got = JargonStore.should_infer(rec)
    flag = "OK" if got == expect else "FAIL"
    if got != expect: ok = False
    print(f"  [{flag}] {label}: 期望={expect} 实际={got}")
assert ok, "阈值调度有误"

# ---- 3. 存储层 CRUD ----
tmp = Path(tempfile.mkdtemp())
st = JargonStore(tmp/"j.db")
r,c = st.upsert_candidate("测试黑话","group:1","证据A")
print(f"\n新增候选: count={c}")
assert c == 1
r,c = st.upsert_candidate("测试黑话","group:1","证据B")
assert c == 2, c
# 单字不入库
r2, c2 = st.upsert_candidate("啊","group:1",None)
assert r2 is None and c2 == 0, (r2,c2)
print("单字过滤: OK")
# 手动录入不被 AI 覆盖
st.add_manual("测试黑话","人工含义")
st.save_inference("测试黑话","AI含义",True,99,99)
rec = st.get_by_content("测试黑话")
assert rec["meaning"] == "人工含义", rec["meaning"]
assert rec["created_by"] == "manual"
print("manual 保护: OK")
st.close()
print("\n✅ 全部单元测试通过")
