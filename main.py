"""像人一样玩梗 —— 让机器人真正听懂中文网络梗与群内黑话。

两条能力线：
  A. 全网梗：内置万条词库，被动注入 + 命令查询 + 兜底搜索
  B. 群内黑话：从聊天记录自动学习，双路对照判定后注入

与 memewiki 插件的区别：不占用 /梗 与 /黑话 命令，定位是「被动理解」而非「主动查词」。
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .jargon_match import format_injection, match_jargons, normalize
from .jargon_miner import JargonMiner, is_valid_term
from .jargon_store import JargonStore
from .limits import RateLimiter
from .meme_kb import ASK_PATTERN, MemeKnowledge, _condense

#: 每个会话的消息缓冲上限
BUFFER_MAX = 200

#: 注入块的边界标记，便于排查与其他插件的冲突
BOUND_BEGIN = "<!-- meme-like-human:BEGIN -->"
BOUND_END = "<!-- meme-like-human:END -->"

#: 常见中文高频词，避免把「因为」「所以」当黑话学
COMMON_WORDS = frozenset(
    """
    因为 所以 但是 如果 然后 现在 今天 明天 昨天 我们 你们 他们 她们 它们
    这个 那个 什么 怎么 为什么 可以 应该 需要 已经 还是 就是 不是 没有 知道
    觉得 认为 喜欢 讨厌 开心 难过 生气 害怕 担心 希望 打算 准备 开始 结束
    时候 地方 东西 事情 问题 办法 方法 情况 样子 意思 感觉 心情 状态 结果
    一个 一些 一下 一样 一起 一直 一定 一般 一边 有点 有些 有的 所有 每个
    大家 自己 别人 朋友 同事 家人 老师 学生 老板 领导 客户 用户 会员
    可能 也许 大概 差不多 基本上 实际上 事实上 其实 确实 真的 特别 非常
    手机 电脑 网络 微信 电话 消息 视频 图片 文件 软件 系统 数据 内容
    工作 学习 生活 时间 地方 城市 国家 世界 社会 公司 学校 医院 银行
    """.split()
)

#: 中文停用字（单字不成词）
STOP_CHARS = frozenset("的了吗呢吧啊呀哦嗯哈呵嘿唉哎诶额嗷咦嘻")


class MemeLikeHumanPlugin(Star):
    """像人一样玩梗。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # ---------- 配置 ----------
        self.meme_enabled = bool(self.config.get("meme_enabled", True))
        self.inject_enabled = bool(self.config.get("inject_enabled", True))
        self.inject_max_memes = int(self.config.get("inject_max_memes", 2) or 2)
        self.inject_max_chars = int(self.config.get("inject_max_chars", 120) or 120)

        self.jargon_enabled = bool(self.config.get("jargon_enabled", True))
        self.learn_frequency = max(5, int(self.config.get("learn_frequency", 40) or 40))
        self.extract_max = int(self.config.get("extract_max", 10) or 10)
        self.learn_daily_max = int(self.config.get("learn_daily_max", 30) or 30)
        self.infer_daily_max = int(self.config.get("infer_daily_max", 20) or 20)
        self.jargon_inject_max = int(self.config.get("inject_max", 5) or 5)
        self.jargon_inject_chars = int(
            self.config.get("jargon_inject_max_chars", 150) or 150
        )
        self.listen_private = bool(self.config.get("listen_private", False))
        self.blacklist: set[str] = self._parse_blacklist(
            self.config.get("blacklist_groups", "")
        )

        # ---------- 数据目录 ----------
        try:
            self.data_dir = StarTools.get_data_dir("astrbot_plugin_meme_like_human")
        except Exception:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            self.data_dir = (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / "astrbot_plugin_meme_like_human"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.bundled_dir = Path(__file__).parent / "data"

        # ---------- 组件 ----------
        self.kb = MemeKnowledge(self.data_dir, self.bundled_dir, self.config)
        self.store: JargonStore | None = None
        self.miner: JargonMiner | None = None

        self._limiter = RateLimiter()

        # 每个会话的消息缓冲：umo -> deque[{id, sender, text}]
        self._buffers: dict[str, deque] = defaultdict(lambda: deque(maxlen=BUFFER_MAX))
        self._msg_seq: dict[str, int] = defaultdict(int)
        self._since_learn: dict[str, int] = defaultdict(int)

        # 群成员昵称集合，用于过滤人名
        self._nicknames: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        logger.info("[玩梗] 插件加载中…")

        if self.meme_enabled:
            try:
                await self.kb.initialize()
                self.kb.start_update_task()
            except Exception as e:
                logger.error(f"[玩梗] 词库初始化失败：{e}")

        if self.jargon_enabled:
            try:
                self.store = JargonStore(self.data_dir / "jargon.db")
                self.miner = JargonMiner(self.context, self.store, self.config)
                st = self.store.stats()
                logger.info(
                    f"[玩梗] 黑话库就绪：总数 {st['total']}，已学会 {st['learned']}"
                )
            except Exception as e:
                logger.error(f"[玩梗] 黑话库初始化失败：{e}")
                self.store = None
                self.miner = None

        notice = self.kb.attribution.get("required_notice") if self.meme_enabled else ""
        if notice:
            logger.info(f"[玩梗] 词库署名：{notice}")

        logger.info("[玩梗] 插件加载完成")

    async def terminate(self) -> None:
        await self.kb.terminate()
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
            self.store = None
        self._buffers.clear()
        self._nicknames.clear()
        logger.info("[玩梗] 插件已卸载")

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _parse_blacklist(raw) -> set[str]:
        if not raw:
            return set()
        if isinstance(raw, (list, tuple, set)):
            items = raw
        else:
            items = re.split(r"[,，;\s]+", str(raw))
        return {str(x).strip() for x in items if str(x).strip()}

    def _session_id(self, event: AstrMessageEvent) -> str:
        try:
            gid = getattr(event.message_obj, "group_id", None)
            if gid:
                return str(gid)
        except Exception:
            pass
        umo = getattr(event, "unified_msg_origin", "") or ""
        return umo.split(":")[-1] if umo else ""

    def _is_blocked(self, event: AstrMessageEvent) -> bool:
        if not self.blacklist:
            return False
        umo = getattr(event, "unified_msg_origin", "") or ""
        sid = self._session_id(event)
        parts = {p for p in re.split(r"[:/]", umo) if p}
        for b in self.blacklist:
            if b and (b == sid or b == umo or b in parts):
                return True
        return False

    def _is_group(self, event: AstrMessageEvent) -> bool:
        try:
            return getattr(event.message_obj, "group_id", None) is not None
        except Exception:
            return "group" in (getattr(event, "unified_msg_origin", "") or "").lower()

    # ------------------------------------------------------------ 学习侧：收集

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def collect_message(self, event: AstrMessageEvent):
        """被动收集消息，用于黑话学习。不产生任何回复。"""
        if not self.jargon_enabled or self.store is None or self.miner is None:
            return
        if self._is_blocked(event):
            return

        is_group = self._is_group(event)
        if not is_group and not self.listen_private:
            return

        # 不学自己发的消息
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:
            pass

        text = (event.message_str or "").strip()
        if not text or len(text) < 2:
            return
        if text.startswith("/") or text.startswith("／"):
            return

        umo = getattr(event, "unified_msg_origin", "") or self._session_id(event)
        if not umo:
            return

        # 记录昵称，用于过滤人名
        try:
            nick = event.get_sender_name()
            if nick and 1 < len(nick) <= 16:
                self._nicknames[umo].add(nick)
        except Exception:
            pass

        self._msg_seq[umo] += 1
        self._buffers[umo].append(
            {
                "id": str(self._msg_seq[umo]),
                "sender": "",
                "text": text[:200],
            }
        )
        self._since_learn[umo] += 1

        if self._since_learn[umo] >= self.learn_frequency:
            self._since_learn[umo] = 0
            self._trigger_learn(umo)

    def _trigger_learn(self, umo: str) -> None:
        """触发一次后台学习。用 create_task 避免阻塞消息处理。"""
        if self.store is None or self.miner is None:
            return
        if not self._limiter.allow(f"learn:{umo}", self.learn_daily_max):
            logger.debug(f"[玩梗] {umo} 今日提取次数已达上限")
            return
        import asyncio

        asyncio.create_task(self._do_learn(umo))

    async def _do_learn(self, umo: str) -> None:
        try:
            buf = self._buffers.get(umo)
            if not buf or len(buf) < 5:
                return

            lines = list(buf)[-self.learn_frequency :]
            lock = self.miner.lock_for(umo)
            if lock.locked():
                return

            async with lock:
                candidates = await self.miner.extract_candidates(lines, umo)
                if not candidates:
                    return

                nicknames = self._nicknames.get(umo, set())
                for cand in candidates:
                    content = cand["content"]
                    if not self._passes_filters(content, nicknames, umo):
                        continue
                    rec, count = self.store.upsert_candidate(
                        content, umo, evidence=self._evidence_for(lines, cand)
                    )
                    if not rec:
                        continue
                    if self.store.should_infer(rec):
                        if not self._limiter.allow(f"infer:{umo}", self.infer_daily_max):
                            logger.debug(f"[玩梗] {umo} 今日推断次数已达上限")
                            continue
                        evidence = self._evidence_list(rec)
                        await self.miner.infer_jargon(rec, evidence, umo)
        except Exception as e:
            logger.warning(f"[玩梗] 学习流程异常：{e}")

    def _passes_filters(self, content: str, nicknames: set[str], umo: str) -> bool:
        """候选词过滤：单字 / 常见词 / 人名 / 停用字。"""
        if not is_valid_term(content):
            return False
        if len(content) < 2:
            return False
        if content in COMMON_WORDS:
            return False
        if all(ch in STOP_CHARS for ch in content):
            return False
        for nick in nicknames:
            if not nick:
                continue
            if content == nick or content in nick or nick in content:
                return False
        return True

    def _evidence_for(self, lines: list[dict], cand: dict) -> str:
        """回溯证据：该消息 + 前后各一条。"""
        sid = cand.get("source_id") or ""
        if not sid:
            return ""
        idx = None
        for i, m in enumerate(lines):
            if str(m.get("id")) == sid:
                idx = i
                break
        if idx is None:
            return ""

        lo = max(0, idx - 1)
        hi = min(len(lines) - 1, idx + 1)
        parts = [f"{m.get('sender','')}：{m.get('text','')}".strip("：") for m in lines[lo : hi + 1]]
        text = " / ".join(p for p in parts if p)
        return text[:200]

    def _evidence_list(self, rec: dict) -> list[str]:
        import json

        try:
            ev = json.loads(rec.get("evidence") or "[]")
            if isinstance(ev, list):
                return [str(x) for x in ev if x]
        except Exception:
            pass
        return []

    # ------------------------------------------------------------ 使用侧：注入

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req) -> None:
        """模型请求前注入梗 / 黑话参考。"""
        try:
            blocks: list[str] = []

            if self.meme_enabled and self.inject_enabled:
                blocks.append(await self._build_meme_block(event, req))

            if self.jargon_enabled and self.store is not None:
                blocks.append(self._build_jargon_block(event, req))

            blocks = [b for b in blocks if b]
            if not blocks:
                return

            current = getattr(req, "system_prompt", "") or ""
            if BOUND_BEGIN in current:
                return  # 已经注入过，避免重复

            addition = "\n\n" + "\n\n".join(blocks) + "\n"
            req.system_prompt = f"{current}{addition}\n{BOUND_BEGIN}\n{BOUND_END}\n"
        except Exception as e:
            logger.debug(f"[玩梗] 注入失败：{e}")

    async def _build_meme_block(self, event: AstrMessageEvent, req) -> str:
        text = (event.message_str or "").strip()
        if not text:
            return ""

        hits = self.kb.match(text, limit=self.inject_max_memes)

        # 本地未命中 + 是问梗句式 → 兜底搜索
        if not hits and ASK_PATTERN.search(text):
            term = self._extract_query_term(text)
            if term:
                rec = await self.kb.search_online(term, getattr(event, "unified_msg_origin", "") or "")
                if rec:
                    hits = [rec]

        if not hits:
            return ""

        lines = ["[梗参考]"]
        total = 0
        for h in hits:
            meaning = _condense(h.get("meaning") or "", 40)
            if not meaning:
                continue
            line = f"- {h['term']}：{meaning}"
            if total + len(line) + 1 > self.inject_max_chars:
                break
            lines.append(line)
            total += len(line) + 1

        if len(lines) == 1:
            return ""

        return BOUND_BEGIN + "\n" + "\n".join(lines) + "\n" + BOUND_END

    def _extract_query_term(self, text: str) -> str:
        """从「XX是什么梗」里抽出 XX。"""
        t = text.strip()
        t = re.sub(r"^(?:请问|问一下|谁知道|有人知道)", "", t)
        t = re.sub(
            r"(?:是|这)?(?:什么|啥|啥子)(?:梗|意思|来头|来历|出处|玩意儿).*$", "", t
        )
        t = re.sub(r"^(?:请问|那个|这个)", "", t)
        t = t.strip("　 ，,。.！!？?、~～")
        return t if 1 < len(t) <= 20 else ""

    def _build_jargon_block(self, event: AstrMessageEvent, req) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""
        records = self.store.all_for_match(umo)
        if not records:
            return ""

        text = (event.message_str or "").strip()
        matches = match_jargons(
            text, records=records, limit=self.jargon_inject_max
        )
        if not matches:
            return ""

        body = format_injection(matches, max_chars=self.jargon_inject_chars)
        if not body:
            return ""
        return BOUND_BEGIN + "\n" + body + "\n" + BOUND_END

    # ------------------------------------------------------------ 指令：查梗

    @filter.command("玩梗")
    async def cmd_meme(self, event: AstrMessageEvent, query: str = ""):
        """查询梗的释义。用法：/玩梗 蚌埠住了"""
        if not self.meme_enabled:
            yield event.plain_result("全网梗功能已关闭。")
            return

        query = (query or "").strip()
        if not query:
            yield event.plain_result(
                "用法：/玩梗 <词>\n例如：/玩梗 蚌埠住了\n统计：/玩梗库"
            )
            return

        rec = await self.kb.lookup(query)
        if not rec:
            rec = await self.kb.search_online(
                query, getattr(event, "unified_msg_origin", "") or ""
            )

        if not rec:
            yield event.plain_result(
                f"没查到「{query}」🤔\n可以试试换个说法，或者确认下这个词是不是梗。"
            )
            return

        lines = [f"【{rec['term']}】", _condense(rec.get("meaning") or "", 200)]
        origin = (rec.get("origin") or "").strip()
        year = (rec.get("year") or "").strip()
        if origin and origin != "不详":
            tag = f"（{year}）" if year else ""
            lines.append(f"出处：{origin}{tag}")
        yield event.plain_result("\n".join(lines))

    @filter.command("玩梗库")
    async def cmd_meme_stats(self, event: AstrMessageEvent):
        """查看词库统计与署名。"""
        if not self.meme_enabled:
            yield event.plain_result("全网梗功能已关闭。")
            return

        notice = self.kb.attribution.get("required_notice") or ""
        lines = [f"词库共收录 {self.kb.total} 条梗。"]
        if notice:
            lines.append("")
            lines.append("词库来源：")
            lines.append(notice)
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 指令：黑话

    @filter.command("黑话库")
    async def cmd_jargon(self, event: AstrMessageEvent, arg: str = ""):
        """查看/管理群内黑话。用法：/黑话库 [页码|add 词 含义|del 词|stat]"""
        if not self.jargon_enabled or self.store is None:
            yield event.plain_result("群内黑话功能已关闭。")
            return

        parts = (arg or "").strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else ""

        # ---------------- 管理类：需管理员 ----------------
        if sub in ("add", "del", "global"):
            if not self._is_admin(event):
                yield event.plain_result("这个操作需要管理员权限。")
                return

            if sub == "add":
                if len(parts) < 3:
                    yield event.plain_result("用法：/黑话库 add <词> <含义>")
                    return
                rec = self.store.add_manual(parts[1], parts[2])
                yield event.plain_result(
                    f"已录入：{parts[1]} = {parts[2]}" if rec else "录入失败。"
                )
                return

            if sub == "del":
                if len(parts) < 2:
                    yield event.plain_result("用法：/黑话库 del <词>")
                    return
                ok = self.store.delete(parts[1])
                yield event.plain_result(
                    f"已删除：{parts[1]}" if ok else f"没找到「{parts[1]}」。"
                )
                return

            if sub == "global":
                if len(parts) < 2:
                    yield event.plain_result("用法：/黑话库 global <词>")
                    return
                rec = self.store.toggle_global(parts[1])
                if not rec:
                    yield event.plain_result(f"没找到「{parts[1]}」。")
                else:
                    state = "全局" if rec["is_global"] else "仅本群"
                    yield event.plain_result(f"「{parts[1]}」作用域已设为：{state}")
                return

        # ---------------- stat ----------------
        if sub == "stat":
            st = self.store.stats()
            yield event.plain_result(
                f"黑话统计\n"
                f"候选总数：{st['total']}\n"
                f"已判定为黑话：{st['learned']}\n"
                f"手动录入：{st['manual']}\n"
                f"已完成推断：{st['complete']}"
            )
            return

        # ---------------- 列表（默认） ----------------
        page = 1
        if sub.isdigit():
            page = int(sub)

        rows, total = self.store.list_jargon(page=page, page_size=10)
        if not rows:
            yield event.plain_result(
                "还没学会任何黑话。\n"
                "我会在群里慢慢积累，也可以在后台调低 learn_frequency 加快学习。"
            )
            return

        pages = max(1, (total + 9) // 10)
        lines = [f"本群黑话（第 {page}/{pages} 页，共 {total} 条）", ""]
        for r in rows:
            mark = "🌐" if r["is_global"] else "•"
            meaning = _condense(r["meaning"] or "", 30) or "（未判定）"
            lines.append(f"{mark} {r['content']}（{r['count']}次）：{meaning}")
        lines.append("")
        lines.append("🌐 = 全局生效；翻页：/黑话库 2")
        yield event.plain_result("\n".join(lines))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            sid = str(event.get_sender_id())
            admins = self.context.get_config().get("admins_id") or []
            if isinstance(admins, str):
                admins = [admins]
            return sid in {str(a) for a in admins}
        except Exception:
            return False
