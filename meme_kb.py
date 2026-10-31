"""全网梗知识库：加载词库、建索引、匹配、查询、兜底搜索。

设计要点：
- 词库打包在 data/memes.json.gz（约 0.9MB），首次加载时建成 SQLite 索引
- 匹配用首字索引（first char -> 词条列表），10k 词库下 <1ms
- 兜底搜索只在「问梗句式」且本地未命中时触发，避免乱花钱
- source 字段区分 builtin / search / manual，词库更新时不会冲掉自学习数据
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

# ---------------------------------------------------------------- 常量

#: 词库更新源，按顺序尝试
DICT_SOURCES = (
    "https://cdn.jsdelivr.net/gh/re-skylar/chinese-meme-dict@main/data/memes.json.gz",
    "https://raw.githubusercontent.com/re-skylar/chinese-meme-dict/main/data/memes.json",
)

DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"

#: 判定「用户在问这个梗是什么意思」的句式
ASK_PATTERN = re.compile(
    r"(?:是|这)?(?:什么|啥|啥子)(?:梗|意思|来头|来历|出处|玩法|玩意儿)"
    r"|(?:怎么|如何)(?:用|接|玩)"
    r"|什么梗|啥梗|什么意思|啥意思"
)

#: 匹配时忽略的字符（保留字母数字汉字）
_IGNORE_RE = re.compile(r"[\s\u3000,，.。!！?？;；:：'\"“”‘’()（）\[\]【】<>《》~～`@#$%^&*+=|/\\-]+")

MIN_TERM_LEN = 2
MAX_MATCH_PER_MSG = 5


class MemeKnowledge:
    """梗知识库。一个实例对应一个插件实例。"""

    def __init__(self, data_dir: Path, bundled_dir: Path, config: dict[str, Any]):
        self.data_dir = data_dir
        self.bundled_dir = bundled_dir
        self.config = config

        self._db_path = data_dir / "memes.db"
        self._conn = None
        self._lock = asyncio.Lock()

        # 内存索引：首字 -> [(term, term_id, meaning, origin, year), ...]
        self._index: dict[str, list[tuple[str, int, str, str, str]]] = defaultdict(list)
        # term_id -> 完整记录
        self._records: dict[int, dict] = {}
        self._total = 0
        self._license_info: dict[str, Any] = {}

        # 限流状态
        self._session_hits: dict[str, list[float]] = defaultdict(list)
        self._daily_count = 0
        self._daily_date = ""

        # 后台任务
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """读取打包词库 -> 建 SQLite -> 建内存索引。"""
        t0 = time.time()
        payload = self._read_bundled()

        if payload is None:
            logger.error("[玩梗] 找不到内置词库，功能A将不可用")
            return

        self._license_info = payload.get("attribution") or {}
        entries = payload.get("entries") or []
        if not entries:
            logger.error("[玩梗] 内置词库为空")
            return

        self._conn = await asyncio.to_thread(self._open_db)
        await asyncio.to_thread(self._sync_builtin, entries)
        await asyncio.to_thread(self._load_index)

        cost = (time.time() - t0) * 1000
        logger.info(
            f"[玩梗] 词库就绪：{self._total} 条，索引构建耗时 {cost:.0f}ms"
        )
        notice = self._license_info.get("required_notice")
        if notice:
            logger.info(f"[玩梗] 词库署名：{notice}")

    async def terminate(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._conn is not None:
            try:
                await asyncio.to_thread(self._conn.close)
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------ 读取内置词库

    def _read_bundled(self) -> dict | None:
        gz = self.bundled_dir / "memes.json.gz"
        raw = self.bundled_dir / "memes.json"

        if gz.exists():
            try:
                with gzip.open(gz, "rt", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[玩梗] 读取 gz 词库失败，尝试未压缩版：{e}")

        if raw.exists():
            try:
                with open(raw, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[玩梗] 读取词库失败：{e}")
        return None

    # ------------------------------------------------------------ SQLite

    def _open_db(self):
        import sqlite3

        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memes(
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                term        TEXT NOT NULL,
                term_key    TEXT NOT NULL UNIQUE,
                meaning     TEXT NOT NULL DEFAULT '',
                origin      TEXT NOT NULL DEFAULT '',
                year        TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '',
                source      TEXT NOT NULL DEFAULT 'builtin',
                hit_count   INTEGER NOT NULL DEFAULT 0,
                last_hit_at REAL NOT NULL DEFAULT 0,
                updated_at  REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_term_key ON memes(term_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON memes(source)")
        conn.commit()
        return conn

    def _sync_builtin(self, entries: list[dict]) -> None:
        """把内置词库同步进 SQLite。

        只覆盖 source='builtin' 的记录；source='search'/'manual' 一律保留。
        """
        now = time.time()
        cur = self._conn.cursor()

        existing = {
            row[0]
            for row in cur.execute("SELECT term_key FROM memes WHERE source='builtin'")
        }

        rows = []
        for e in entries:
            term = (e.get("t") or "").strip()
            if not term:
                continue
            key = _normalize(term)
            if not key or key in existing:
                continue
            rows.append(
                (
                    term,
                    key,
                    (e.get("m") or "").strip(),
                    (e.get("o") or "").strip(),
                    (e.get("d") or "").strip(),
                    ",".join(e.get("tag") or []),
                    "builtin",
                    now,
                )
            )

        if rows:
            cur.executemany(
                """
                INSERT OR IGNORE INTO memes
                    (term, term_key, meaning, origin, year, tags, source, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            self._conn.commit()
            logger.info(f"[玩梗] 词库新增 {len(rows)} 条内置记录")

    def _load_index(self) -> None:
        """把全部词条读进内存，建首字索引。"""
        self._index = defaultdict(list)
        self._records = {}

        cur = self._conn.execute(
            "SELECT id, term, meaning, origin, year FROM memes"
        )
        for rid, term, meaning, origin, year in cur:
            if len(term) < MIN_TERM_LEN:
                continue
            self._records[rid] = {
                "id": rid,
                "term": term,
                "meaning": meaning,
                "origin": origin,
                "year": year,
            }
            self._index[term[0]].append((term, rid, meaning, origin, year))

        # 长词优先，避免短词抢匹配
        for lst in self._index.values():
            lst.sort(key=lambda x: len(x[0]), reverse=True)

        self._total = len(self._records)

    # ------------------------------------------------------------ 匹配

    def match(self, text: str, limit: int = MAX_MATCH_PER_MSG) -> list[dict]:
        """从文本中找出命中的梗。

        策略：最长匹配优先。
        先用索引收集所有「可能命中」的候选，再按词条长度降序逐个确认，
        已占用的字符区间不再参与后续匹配。这样「你人还怪好嘞」不会
        被更短的「好嘞」抢走。
        """
        if not text or not self._records:
            return []

        clean = _IGNORE_RE.sub("", text)
        if not clean:
            return []

        # 1) 收集候选（含所有起始位置）
        candidates: list[tuple[int, int, tuple]] = []  # (起始, 长度, 记录元组)
        n = len(clean)
        for i in range(n):
            for item in self._index.get(clean[i], ()):
                term = item[0]
                if clean.startswith(term, i):
                    candidates.append((i, len(term), item))

        if not candidates:
            return []

        # 2) 长度降序 + 起始位置升序，逐个占位
        candidates.sort(key=lambda x: (-x[1], x[0]))

        used = [False] * n
        found: list[dict] = []
        seen_terms: set[str] = set()

        for start, length, (term, rid, meaning, origin, year) in candidates:
            if len(found) >= limit:
                break
            if term in seen_terms:
                continue
            end = start + length
            if any(used[start:end]):
                continue
            used[start:end] = [True] * length
            seen_terms.add(term)
            found.append(
                {
                    "id": rid,
                    "term": term,
                    "meaning": meaning,
                    "origin": origin,
                    "year": year,
                }
            )

        # 3) 按在原文中出现的位置排序，读起来更自然
        found.sort(key=lambda h: clean.find(h["term"]))
        return found

    # ------------------------------------------------------------ 查询

    async def lookup(self, term: str) -> dict | None:
        """精确查询一个词条。"""
        key = _normalize(term)
        if not key:
            return None

        row = await asyncio.to_thread(self._lookup_sync, key)
        if row:
            await self._touch(row["id"])
            return dict(row)
        return None

    def _lookup_sync(self, key: str):
        return self._conn.execute(
            "SELECT id, term, meaning, origin, year, tags, source, hit_count FROM memes WHERE term_key=?",
            (key,),
        ).fetchone()

    async def _touch(self, rid: int) -> None:
        def _do():
            self._conn.execute(
                "UPDATE memes SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                (time.time(), rid),
            )
            self._conn.commit()

        try:
            await asyncio.to_thread(_do)
        except Exception:
            pass

    # ------------------------------------------------------------ 兜底搜索

    def _get_api_key(self) -> str:
        key = (self.config.get("deepseek_api_key") or "").strip()
        if key:
            return key
        return ""

    async def search_online(self, term: str, umo: str = "") -> dict | None:
        """调 DeepSeek 搜索兜底。成功则写回本地库。"""
        if not self.config.get("search_enabled", True):
            return None
        if not self._allow_search(umo):
            return None

        key = self._get_api_key()
        if not key:
            return None

        query = f"{term} 梗 什么意思 出处"
        body = {"query": query[:128], "count": 2}

        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(
                    DEEPSEEK_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[玩梗] 搜索返回 {resp.status}")
                        return None
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[玩梗] 搜索失败：{e}")
            return None

        results = data.get("results") or []
        if not results:
            return None

        summary = (results[0].get("summary") or "").strip()
        if not summary:
            summary = (results[0].get("snippet") or "").strip()
        if not summary:
            return None

        meaning = _condense(summary, 60)
        origin = (results[0].get("site_name") or "").strip()[:20] or "不详"

        rec = await self._save_search_result(term, meaning, origin)
        self._bump_search_count()
        return rec

    async def _save_search_result(
        self, term: str, meaning: str, origin: str
    ) -> dict | None:
        key = _normalize(term)
        if not key:
            return None

        def _do():
            now = time.time()
            cur = self._conn.execute(
                "SELECT id FROM memes WHERE term_key=?", (key,)
            ).fetchone()
            if cur:
                return cur[0]
            cur = self._conn.execute(
                """
                INSERT INTO memes(term, term_key, meaning, origin, year, tags, source, updated_at)
                VALUES (?,?,?,?,'','','search',?)
                """,
                (term, key, meaning, origin, now),
            )
            self._conn.commit()
            return cur.lastrowid

        try:
            rid = await asyncio.to_thread(_do)
        except Exception as e:
            logger.debug(f"[玩梗] 写回搜索结果失败：{e}")
            return None

        if rid is None:
            return None

        rec = {"id": rid, "term": term, "meaning": meaning, "origin": origin, "year": ""}
        self._records[rid] = rec
        if len(term) >= MIN_TERM_LEN:
            self._index[term[0]].append((term, rid, meaning, origin, ""))
            self._index[term[0]].sort(key=lambda x: len(x[0]), reverse=True)
        return rec

    # ------------------------------------------------------------ 限流

    def _allow_search(self, umo: str) -> bool:
        now = time.time()
        today = time.strftime("%Y-%m-%d")

        if self._daily_date != today:
            self._daily_date = today
            self._daily_count = 0

        daily_max = int(self.config.get("search_daily_max", 200) or 0)
        if daily_max > 0 and self._daily_count >= daily_max:
            return False

        per_min = int(self.config.get("search_per_min", 3) or 0)
        if per_min > 0 and umo:
            hits = self._session_hits[umo]
            hits = [t for t in hits if now - t < 60]
            if len(hits) >= per_min:
                self._session_hits[umo] = hits
                return False
            hits.append(now)
            self._session_hits[umo] = hits
        return True

    def _bump_search_count(self) -> None:
        self._daily_count += 1

    # ------------------------------------------------------------ 词库更新

    def start_update_task(self) -> None:
        if not self.config.get("auto_update_dict", True):
            return
        task = asyncio.create_task(self._update_dict())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _update_dict(self) -> None:
        """后台拉取最新词库。失败静默。"""
        for url in DICT_SOURCES:
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(url) as resp:
                        if resp.status != 200:
                            continue
                        raw = await resp.read()
                payload = json.loads(raw.decode("utf-8"))
                entries = payload.get("entries") or []
                if len(entries) <= self._total:
                    return
                await asyncio.to_thread(self._sync_builtin, entries)
                await asyncio.to_thread(self._load_index)
                logger.info(f"[玩梗] 词库更新完成，现有 {self._total} 条")
                return
            except Exception as e:
                logger.debug(f"[玩梗] 词库更新失败({url[:40]})：{e}")
                continue

    # ------------------------------------------------------------ 信息

    @property
    def total(self) -> int:
        return self._total

    @property
    def attribution(self) -> dict[str, Any]:
        return self._license_info


def _normalize(s: str) -> str:
    """归一化：去空白、去标点、转小写。"""
    return _IGNORE_RE.sub("", str(s or "")).strip().lower()


def _condense(text: str, max_chars: int) -> str:
    """把长文本压缩成一句适合注入的释义。

    词库里的释义本身就是 20-60 字的口语化句子，通常不需要压缩；
    只有从搜索结果拿到的长段落才需要截断。所以这里：
      1. 先清洗空白与百科式开头
      2. 若已足够短，直接返回
      3. 否则取靠前的完整句子，而不是「第一个带句号的片段」
    """
    if not text:
        return ""

    t = re.sub(r"\s+", " ", str(text)).strip()

    # 去掉常见的百科/新闻式开头
    t = re.sub(r"^[#\s]*一分钟了解[^ ]*\s*", "", t)
    t = re.sub(r"^[#\s]*", "", t)

    # 已经够短就直接用
    if len(t) <= max_chars:
        return t.strip("　 ，,。.！!？?")

    # 按句切分，取能装进 max_chars 的前若干句
    parts = re.split(r"(?<=[。！？!?])", t)
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        cand = (out + p) if out else p
        if len(cand) > max_chars:
            break
        out = cand

    if not out:
        # 单句就超长：硬截
        out = t[:max_chars]

    out = out.strip("　 ，,。.！!？?")
    if len(t) > len(out):
        out = out.rstrip("　 ，,。") + "…"
    return out
