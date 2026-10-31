"""集成检查：验证 main.py 的结构与装饰器用法（不依赖真实 AstrBot）。"""
import sys, types, ast, json
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ---- 用 AST 静态检查，避免真实 import AstrBot ----
src = (ROOT / "main.py").read_text(encoding="utf-8")
tree = ast.parse(src)

checks = []
classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
stars = [c for c in classes if any(
    (isinstance(b, ast.Name) and b.id == "Star") or
    (isinstance(b, ast.Attribute) and b.attr == "Star")
    for b in c.bases)]
checks.append(("定义了 Star 子类", len(stars) == 1, [c.name for c in stars]))

# 检查 @register 未被使用（已废弃）
has_register = "@register(" in src or "register(" in src.split("class ")[0]
checks.append(("@register 未使用（已废弃）", not has_register, None))

# 检查关键方法
cls = stars[0] if stars else None
methods = [m.name for m in ast.walk(cls) if isinstance(m, ast.AsyncFunctionDef)] if cls else []
need = ["initialize", "terminate", "inject_context", "collect_message"]
for n in need:
    checks.append((f"方法 {n}", n in methods, None))

# 检查命令装饰器
cmds = []
for node in ast.walk(cls):
    if isinstance(node, ast.AsyncFunctionDef):
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                if d.func.attr == "command" and d.args:
                    cmds.append(d.args[0].value if isinstance(d.args[0], ast.Constant) else "?")
checks.append(("命令已注册", len(cmds) >= 3, cmds))

# 检查 on_llm_request
has_llm = "on_llm_request" in src
checks.append(("on_llm_request 已使用", has_llm, None))

# 检查边界标记
checks.append(("注入边界标记", "meme-like-human:BEGIN" in src, None))

# ---- metadata.yaml 校验 ----
import re
meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
for field in ["name:", "display_name:", "version:", "author:", "repo:"]:
    checks.append((f"metadata {field.rstrip(':')}", field in meta, None))
m = re.search(r"^name:\s*(\S+)", meta, re.M)
checks.append(("name 以 astrbot_plugin_ 开头", bool(m and m.group(1).startswith("astrbot_plugin_")), m.group(1) if m else None))

# ---- _conf_schema.json 校验 ----
schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
checks.append(("配置项数量", len(schema) >= 15, len(schema)))
bad_types = [k for k, v in schema.items() if v.get("type") not in ("bool", "int", "string", "float", "list", "dict")]
checks.append(("配置项类型均合法", not bad_types, bad_types))
missing_desc = [k for k, v in schema.items() if not v.get("description")]
checks.append(("配置项均有描述", not missing_desc, missing_desc))

# ---- 词库存在 ----
checks.append(("词库 gz 存在", (ROOT/"data"/"memes.json.gz").exists(), None))
checks.append(("LICENSE 存在", (ROOT/"LICENSE").exists(), None))
checks.append(("NOTICE 存在", (ROOT/"NOTICE").exists(), None))
notice = (ROOT/"NOTICE").read_text(encoding="utf-8")
checks.append(("NOTICE 含 sky", "sky" in notice, None))
checks.append(("NOTICE 含 楠", "楠" in notice, None))

# ---- 输出 ----
print("=" * 60)
fail = 0
for name, ok, detail in checks:
    if not ok:
        fail += 1
    d = f"  {detail}" if detail else ""
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}{d}")
print("=" * 60)
print(f"共 {len(checks)} 项，失败 {fail} 项")
assert fail == 0, f"{fail} 项未通过"
print("=== 集成检查全部通过 ===")
