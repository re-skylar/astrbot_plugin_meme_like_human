"""黑话的机械匹配与注入格式化。

设计要点（学自 jargon 的优秀做法，代码独立实现）：
- 纯机械子串匹配：归一化后 `in` 判断，不调 LLM、不用向量
- 权重打分：score = count - index*0.01，越早出现的词条权重略低
- 按 count 分档：高频词告诉模型「可以偶尔用」，低频词只让「理解」
"""

from __future__ import annotations

import re

#: 归一化时忽略的字符
_IGNORE_RE = re.compile(r"[\s\u3000,，.。!！?？;；:：'\"“”‘’()（）\[\]【】<>《》~～`]+")

#: 同一个词条最多注入几次（防止长文本重复命中）
DEFAULT_LIMIT = 5

#: count 达到此值，才建议模型「可以偶尔使用」
USABLE_THRESHOLD = 8


def normalize(s: str) -> str:
    return _IGNORE_RE.sub("", str(s or "")).strip().lower()


def match_jargons(
    text: str,
    context_texts: list[str] | None = None,
    records: list[dict] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """在当前文本中匹配黑话。

    Args:
        text: 当前用户消息
        context_texts: 额外的上下文（如最近几条用户消息）
        records: 可注入的黑话记录列表
        limit: 最多返回条数

    Returns:
        命中的记录列表，已按权重排序
    """
    if not records:
        return []

    haystacks = [text or ""]
    if context_texts:
        haystacks.extend(context_texts)

    normalized = [normalize(h) for h in haystacks]
    normalized = [h for h in normalized if h]
    if not normalized:
        return []

    joined = "\n".join(normalized)

    matches: dict[str, dict] = {}
    for index, rec in enumerate(records):
        content = (rec.get("content") or "").strip()
        key = normalize(content)
        if not key or key in matches:
            continue
        if key not in joined:
            continue

        count = int(rec.get("count") or 0)
        score = count - index * 0.01
        matches[key] = {
            "content": content,
            "meaning": (rec.get("meaning") or "").strip(),
            "count": count,
            "is_global": bool(rec.get("is_global")),
            "score": score,
            "first_index": index,
        }

    return sorted(
        matches.values(),
        key=lambda m: (-m["score"], m["first_index"], -len(m["content"]), m["content"]),
    )[: max(1, int(limit))]


def format_injection(matches: list[dict], max_chars: int = 150) -> str:
    """把命中结果格式化成注入 system_prompt 的参考块。

    按 count 分两档：
    - count >= USABLE_THRESHOLD：告诉模型可以偶尔自然使用
    - count <  USABLE_THRESHOLD：只让模型理解，不要主动用
    """
    if not matches:
        return ""

    usable = [m for m in matches if m["count"] >= USABLE_THRESHOLD]
    understand_only = [m for m in matches if m["count"] < USABLE_THRESHOLD]

    lines: list[str] = ["[本群黑话参考]"]

    if usable:
        lines.append("可以偶尔自然使用：")
        for m in usable:
            meaning = _clip(m["meaning"], 28)
            if meaning:
                lines.append(f"- {m['content']}：{meaning}")

    if understand_only:
        lines.append("只需理解，不要主动使用：")
        for m in understand_only:
            meaning = _clip(m["meaning"], 28)
            if meaning:
                lines.append(f"- {m['content']}：{meaning}")

    text = "\n".join(lines)

    if len(text) > max_chars:
        # 逐行裁剪，保证不超限
        out: list[str] = []
        total = 0
        for line in lines:
            if total + len(line) + 1 > max_chars:
                break
            out.append(line)
            total += len(line) + 1
        text = "\n".join(out)

    return text


def _clip(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    if len(s) > n:
        return s[:n].rstrip("　 ，,。") + "…"
    return s
