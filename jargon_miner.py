"""黑话学习：候选提取 + 双路对照推断。

双路对照的原理（独立实现，思路参考 MaiBot）：
    带上下文推断一次含义  ->  若答 no_info 则放弃，等下次阈值（不瞎猜）
    只给裸词推断一次含义
    比较两次结果：
        相似   -> 说明含义明确，是通用词，不是黑话
        不相似 -> 说明含义依赖圈内语境，是黑话

阈值调度（MaiBot 原值）：count 达到 4 / 8 / 25 / 100 时各推断一次，
count >= 100 标记完成不再推断。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from astrbot.api import logger

from .jargon_store import INFERENCE_THRESHOLDS, JargonStore

# ---------------------------------------------------------------- Prompt

EXTRACT_PROMPT = """请从下面这段群聊记录中，提取「可能是黑话」的候选项。

提取规则：
- 必须是对话中真实出现过的短词或短语
- 必须是你无法从字面直接理解、或者需要本群语境才能理解的词
- 不要选含义清晰的普通词语
- 排除：人名、@提及、表情包描述、纯标点、常规功能词（的、了、呢、啊等）
- 每个词条 2-8 个字符为宜
- 最多提取 {extract_max} 个

黑话通常属于以下几类：
- 拼音首字母缩写，例如 nb、yyds、xswl
- 英文缩写，例如 CPU、API
- 中文词语缩略，例如 社死、内卷
- 本群反复使用、但脱离上下文不容易理解的短词

输出要求：只输出 JSON 数组，不要任何其他文字。
每个元素格式：{{"content": "词条", "source_id": "12"}}
source_id 是该词所在消息的编号（即聊天记录中 <message id="3"> 的数字）

聊天记录：
{chat_str}

输出 JSON："""

INFER_CONTEXT_PROMPT = """你是网络流行语与亚文化专家。
请根据下面的群聊上下文，推断词条「{content}」这个词在本群语境下的含义。

上下文证据：
{evidence}
{previous_section}
要求：
- 只依据上下文推断，不要凭空臆测
- 如果上下文不足以判断含义，把 no_info 设为 true

请只输出 JSON：{{"meaning": "推断的含义", "no_info": false}}"""

INFER_BARE_PROMPT = """请只根据词条本身，推断「{content}」最可能的通用含义。
从网络流行语、拼音缩写、英文缩写、中文缩略的角度考虑，不要参考任何聊天上下文。

请只输出 JSON：{{"meaning": "推断的含义"}}"""

COMPARE_PROMPT = """下面是同一个词条的两次含义推断：

推断A（结合群聊上下文得出）：
{inference1}

推断B（仅凭词条本身得出）：
{inference2}

请判断这两次推断的含义是否基本一致。

- 如果基本一致 → 说明这是通用网络用语，含义明确，不是本群黑话
- 如果有明显差异 → 说明它的含义依赖特定语境，是本群黑话

请只输出 JSON：{{"is_similar": true}}"""

# ---------------------------------------------------------------- 工具

_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```")
_AT_RE = re.compile(r"@\S+")
_URL_RE = re.compile(r"https?://\S+")
_PUNCT_RE = re.compile(r"^[\W_]+$")


def parse_json_loose(text: str) -> Any:
    """宽松解析 LLM 输出：去代码围栏 + 截取首个括号区间。"""
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text).strip()
    for start_ch, end_ch in (("[", "]"), ("{", "}")):
        start = cleaned.find(start_ch)
        end = cleaned.rfind(end_ch)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def is_valid_term(content: str) -> bool:
    """候选词基础过滤。"""
    if not content:
        return False
    content = content.strip()
    if not (2 <= len(content) <= 12):
        return False
    if "@" in content or "http" in content.lower():
        return False
    if _PUNCT_RE.match(content):
        return False
    return True


# ---------------------------------------------------------------- 推断器


class JargonMiner:
    """黑话提取与双路对照推断。"""

    def __init__(self, context, store: JargonStore, config: dict):
        self.context = context
        self.store = store
        self.config = config

        self.extract_max = int(config.get("extract_max", 10) or 10)
        self._infer_sem = asyncio.Semaphore(2)  # 限制并发，避免打爆 LLM
        self._learn_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ LLM

    def _get_provider(self, umo: str | None = None):
        pid = str(self.config.get("provider_id") or "").strip()
        if pid:
            try:
                prov = self.context.get_provider_by_id(pid)
                if prov:
                    return prov
                logger.warning(f"[玩梗] 配置的模型 {pid} 不存在，回退主模型")
            except Exception as e:
                logger.warning(f"[玩梗] 获取指定模型失败：{e}")

        try:
            prov = self.context.get_using_provider(umo)
            if prov:
                return prov
        except Exception:
            pass

        try:
            providers = self.context.get_all_providers()
            return providers[0] if providers else None
        except Exception:
            return None

    async def _chat(self, prompt: str, umo: str | None = None) -> str:
        provider = self._get_provider(umo)
        if not provider:
            logger.warning("[玩梗] 没有可用的 LLM 提供商，跳过")
            return ""
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=None,
                contexts=[],
                system_prompt="你是一个严谨的中文网络用语分析助手，只输出要求的 JSON。",
            )
            return (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning(f"[玩梗] LLM 调用失败：{e}")
            return ""

    # ------------------------------------------------------------ 学习锁

    def lock_for(self, umo: str) -> asyncio.Lock:
        lock = self._learn_locks.get(umo)
        if lock is None:
            lock = asyncio.Lock()
            self._learn_locks[umo] = lock
        return lock

    # ------------------------------------------------------------ 候选提取

    async def extract_candidates(
        self, chat_lines: list[dict], umo: str | None = None
    ) -> list[dict]:
        """从聊天记录里提取候选词。chat_lines: [{id, sender, text}]"""
        if not chat_lines:
            return []

        chat_str = "\n".join(
            f'<message id="{m["id"]}">{m.get("sender","")}：{m.get("text","")}</message>'
            for m in chat_lines[:60]
        )

        prompt = EXTRACT_PROMPT.format(
            extract_max=self.extract_max, chat_str=chat_str
        )
        raw = await self._chat(prompt, umo)
        data = parse_json_loose(raw)
        if not isinstance(data, list):
            return []

        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not is_valid_term(content):
                continue
            out.append(
                {
                    "content": content,
                    "source_id": str(item.get("source_id") or "").strip(),
                }
            )
            if len(out) >= self.extract_max:
                break
        return out

    # ------------------------------------------------------------ 双路对照

    async def infer_jargon(
        self, rec: dict, evidence_texts: list[str], umo: str | None = None
    ) -> dict | None:
        """三步双路对照推断。返回 {is_jargon, meaning, no_info}。"""
        async with self._infer_sem:
            return await self._infer_inner(rec, evidence_texts, umo)

    async def _infer_inner(
        self, rec: dict, evidence_texts: list[str], umo: str | None
    ) -> dict | None:
        content = rec["content"]
        count = int(rec.get("count") or 0)
        previous_meaning = (rec.get("meaning") or "").strip()

        if not evidence_texts:
            logger.info(f"[玩梗] 「{content}」无证据上下文，跳过推断")
            self.store.mark_no_info(content, count)
            return None

        evidence = "\n".join(f"- {t}" for t in evidence_texts[:6])
        previous_section = ""
        if previous_meaning:
            previous_section = (
                f"\n上一次推断的含义（仅供参考）：{previous_meaning}\n"
                "请结合新上下文，给出更准确或更新的推断。\n"
            )

        # ---------- 步骤1：带上下文推断 ----------
        p1 = INFER_CONTEXT_PROMPT.format(
            content=content, evidence=evidence, previous_section=previous_section
        )
        raw1 = await self._chat(p1, umo)
        r1 = parse_json_loose(raw1)
        if not isinstance(r1, dict):
            logger.info(f"[玩梗] 「{content}」推断1解析失败")
            return None

        if r1.get("no_info"):
            logger.info(f"[玩梗] 「{content}」信息不足，放弃本次推断")
            self.store.mark_no_info(content, count)
            return None

        meaning1 = str(r1.get("meaning") or "").strip()
        if not meaning1:
            self.store.mark_no_info(content, count)
            return None

        # ---------- 步骤2：裸词推断 ----------
        p2 = INFER_BARE_PROMPT.format(content=content)
        raw2 = await self._chat(p2, umo)
        r2 = parse_json_loose(raw2)
        if not isinstance(r2, dict):
            logger.info(f"[玩梗] 「{content}」推断2解析失败")
            return None
        meaning2 = str(r2.get("meaning") or "").strip()
        if not meaning2:
            return None

        # ---------- 步骤3：对照 ----------
        p3 = COMPARE_PROMPT.format(inference1=meaning1, inference2=meaning2)
        raw3 = await self._chat(p3, umo)
        r3 = parse_json_loose(raw3)
        if not isinstance(r3, dict):
            logger.info(f"[玩梗] 「{content}」对照解析失败")
            return None

        is_similar = bool(r3.get("is_similar", True))
        is_jargon = not is_similar

        # 不是黑话就保留上一次的含义（若有）
        final_meaning = meaning1 if is_jargon else (previous_meaning or meaning1)

        self.store.save_inference(
            content=content,
            meaning=final_meaning,
            is_jargon=is_jargon,
            count=count,
            last_inference_count=count,
        )

        logger.info(
            f"[玩梗] 「{content}」推断完成："
            f"{'是黑话' if is_jargon else '非黑话'} → {final_meaning[:30]}"
        )
        return {
            "is_jargon": is_jargon,
            "meaning": final_meaning,
            "no_info": False,
        }
