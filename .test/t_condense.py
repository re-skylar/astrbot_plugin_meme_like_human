import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
fake=types.ModuleType("astrbot"); api=types.ModuleType("astrbot.api")
class L:
    def __getattr__(self,n): return lambda *a,**k: None
api.logger=L(); fake.api=api
sys.modules.setdefault("astrbot",fake); sys.modules.setdefault("astrbot.api",api)
try:
    import aiohttp
except ImportError:
    m=types.ModuleType("aiohttp")
    class T:
        def __init__(s,*a,**k): pass
    class S:
        def __init__(s,*a,**k): pass
    m.ClientTimeout=T; m.ClientSession=S; sys.modules["aiohttp"]=m

from meme_kb import _condense

cases = [
    ("短句直接用", "蚌埠谐音绷不住了，意思是情绪被戳中，想笑到憋不住。", 40),
    ("百科开头清洗", "蚌埠住了，网络流行语。源自安徽蚌埠的谐音。该词指情绪受到冲击，绷不住了的谐音梗。现在多用于表达情绪激动。", 40),
    ("超长截断", "这是一段非常长的百科式描述" * 10, 40),
    ("空串", "", 40),
]
for label, text, mx in cases:
    r = _condense(text, mx)
    ok = len(r) <= mx + 1
    print(f"[{'OK' if ok else 'FAIL'}] {label}: {len(r)}字  {r}")
    assert ok, (label, len(r))

# 核心要求：短句不应被破坏
core = "蚌埠谐音绷不住了，意思是情绪被戳中，想笑到憋不住。"
r = _condense(core, 40)
assert r == core.rstrip("。"), f"短句被破坏: {r!r}"
print("\n[OK] 短句未被破坏")
print("=== 压缩测试通过 ===")
