"""群内黑话的 SQLite 存储层。

表结构设计要点：
- chat_scopes: JSON {umo: 出现次数}，黑话是群特有的
- evidence: JSON list[str]，保留推断用的上下文片段
- last_inference_count: 阶梯阈值调度，防止重启后重复推断
- is_complete: count>=100 后不再推断
- created_by: ai / manual，手动录入的词条 AI 永不覆盖
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

#: count 到达这些阈值时各触发一次含义推断（MaiBot 原值）
INFERENCE_THRESHOLDS = (4, 8, 25, 100)

#: 每个词条最多保留的上下文证据片段数
MAX_EVIDENCE = 6

#: count 达到此值后标记完成，不再推断
COMPLETE_THRESHOLD = 100

#: 单字黑话不缓存
MIN_TERM_LEN = 2

#: 候选词长度上限
MAX_TERM_LEN = 12


class JargonStore:
    def __init__(self, db_path: Path | str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jargon(
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    content               TEXT NOT NULL UNIQUE,
                    meaning               TEXT NOT NULL DEFAULT '',
                    count                 INTEGER NOT NULL DEFAULT 0,
                    is_jargon             INTEGER NOT NULL DEFAULT 0,
                    is_complete           INTEGER NOT NULL DEFAULT 0,
                    is_global             INTEGER NOT NULL DEFAULT 0,
                    created_by            TEXT NOT NULL DEFAULT 'ai',
                    chat_scopes           TEXT NOT NULL DEFAULT '{}',
                    evidence              TEXT NOT NULL DEFAULT '[]',
                    last_inference_count  INTEGER NOT NULL DEFAULT 0,
                    created_at            REAL NOT NULL,
                    updated_at            REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jargon_content ON jargon(content)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jargon_jargon ON jargon(is_jargon)"
            )
            self._conn.commit()

    # ------------------------------------------------------------ 基础

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row else None

    def get_by_content(self, content: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE content=?", (content,)
            ).fetchone()
        return self._row(row)

    def get(self, jid: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE id=?", (jid,)
            ).fetchone()
        return self._row(row)

    # ------------------------------------------------------------ 学习侧

    def upsert_candidate(
        self, content: str, umo: str, evidence: str | None
    ) -> tuple[dict | None, int]:
        """写入或更新一条候选词。

        Returns:
            (记录, 当前 count)
        """
        content = (content or "").strip()
        if not (MIN_TERM_LEN <= len(content) <= MAX_TERM_LEN):
            return None, 0

        now = time.time()
        new_id: int | None = None
        new_count = 0

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE content=?", (content,)
            ).fetchone()

            if row is None:
                scopes = {umo: 1} if umo else {}
                ev = [evidence] if evidence else []
                cur = self._conn.execute(
                    """
                    INSERT INTO jargon(content, count, chat_scopes, evidence,
                                       created_at, updated_at)
                    VALUES (?,1,?,?,?,?)
                    """,
                    (
                        content,
                        json.dumps(scopes, ensure_ascii=False),
                        json.dumps(ev, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                self._conn.commit()
                new_id = cur.lastrowid
                new_count = 1
            else:
                # 已存在：count+1，更新作用域与证据
                scopes = _load_json(row["chat_scopes"], {})
                if umo:
                    scopes[umo] = int(scopes.get(umo, 0)) + 1
                ev = _load_json(row["evidence"], [])
                if evidence and evidence not in ev:
                    ev.append(evidence)
                    ev = ev[-MAX_EVIDENCE:]

                self._conn.execute(
                    """
                    UPDATE jargon
                       SET count=count+1, chat_scopes=?, evidence=?, updated_at=?
                     WHERE id=?
                    """,
                    (
                        json.dumps(scopes, ensure_ascii=False),
                        json.dumps(ev, ensure_ascii=False),
                        now,
                        row["id"],
                    ),
                )
                self._conn.commit()
                new_count = int(row["count"]) + 1

        # 注意：必须在释放锁之后再调用 get()，否则会因 Lock 不可重入而死锁
        if new_id is not None:
            return self.get(new_id), new_count
        return self.get_by_content(content), new_count

    def add_manual(self, content: str, meaning: str, is_global: bool = False) -> dict | None:
        """手动录入。AI 永不覆盖。"""
        content = (content or "").strip()
        if not content:
            return None
        now = time.time()
        with self._lock:
            exist = self._conn.execute(
                "SELECT id FROM jargon WHERE content=?", (content,)
            ).fetchone()
            if exist:
                self._conn.execute(
                    """
                    UPDATE jargon
                       SET meaning=?, created_by='manual', is_jargon=1,
                           is_global=?, is_complete=1, updated_at=?
                     WHERE id=?
                    """,
                    (meaning.strip(), 1 if is_global else 0, now, exist["id"]),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO jargon(content, meaning, count, is_jargon,
                                       is_complete, is_global, created_by,
                                       created_at, updated_at)
                    VALUES (?,?,1,1,1,?,'manual',?,?)
                    """,
                    (content, meaning.strip(), 1 if is_global else 0, now, now),
                )
            self._conn.commit()
        return self.get_by_content(content)

    def delete(self, content: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jargon WHERE content=?", (content.strip(),)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def toggle_global(self, content: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT is_global FROM jargon WHERE content=?", (content.strip(),)
            ).fetchone()
            if not row:
                return None
            new_val = 0 if row["is_global"] else 1
            self._conn.execute(
                "UPDATE jargon SET is_global=?, updated_at=? WHERE content=?",
                (new_val, time.time(), content.strip()),
            )
            self._conn.commit()
        return self.get_by_content(content)

    # ------------------------------------------------------------ 推断调度

    @staticmethod
    def should_infer(rec: dict) -> bool:
        """判断是否需要触发含义推断。"""
        if rec.get("created_by") == "manual":
            return False
        if rec.get("is_complete"):
            return False

        count = int(rec.get("count") or 0)
        last = int(rec.get("last_inference_count") or 0)

        if count < INFERENCE_THRESHOLDS[0]:
            return False
        if count <= last:
            return False

        nxt = next((t for t in INFERENCE_THRESHOLDS if t > last), None)
        if nxt is None:
            return False
        return count >= nxt

    def save_inference(
        self,
        content: str,
        meaning: str,
        is_jargon: bool,
        count: int,
        last_inference_count: int,
    ) -> None:
        """写回推断结果。manual 词条不覆盖。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT created_by FROM jargon WHERE content=?", (content,)
            ).fetchone()
            if not row or row["created_by"] == "manual":
                return

            is_complete = 1 if count >= COMPLETE_THRESHOLD else 0
            self._conn.execute(
                """
                UPDATE jargon
                   SET meaning=?, is_jargon=?, is_complete=?,
                       last_inference_count=?, updated_at=?
                 WHERE content=?
                """,
                (
                    meaning,
                    1 if is_jargon else 0,
                    is_complete,
                    last_inference_count,
                    now,
                    content,
                ),
            )
            self._conn.commit()

    def mark_no_info(self, content: str, current_count: int) -> None:
        """信息不足，更新 last_inference_count 防止同阈值重复尝试。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE jargon SET last_inference_count=?, updated_at=? WHERE content=?",
                (current_count, now, content),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 查询侧

    def list_jargon(
        self, page: int = 1, page_size: int = 10, only_jargon: bool = True
    ) -> tuple[list[dict], int]:
        page = max(1, page)
        offset = (page - 1) * page_size
        where = "WHERE is_jargon=1" if only_jargon else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM jargon {where}"
            ).fetchone()["c"]
            rows = self._conn.execute(
                f"""
                SELECT * FROM jargon {where}
                ORDER BY count DESC, updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def all_for_match(self, umo: str) -> list[dict]:
        """取出可用于注入的词条（本群作用域 或 全局）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM jargon
                 WHERE is_jargon=1 AND meaning!=''
                 ORDER BY count DESC
                 LIMIT 500
                """
            ).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            if d["is_global"]:
                out.append(d)
                continue
            scopes = _load_json(d["chat_scopes"], {})
            if umo and umo in scopes:
                out.append(d)
        return out

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS c FROM jargon").fetchone()["c"]
            learned = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jargon WHERE is_jargon=1"
            ).fetchone()["c"]
            manual = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jargon WHERE created_by='manual'"
            ).fetchone()["c"]
            complete = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jargon WHERE is_complete=1"
            ).fetchone()["c"]
        return {
            "total": int(total),
            "learned": int(learned),
            "manual": int(manual),
            "complete": int(complete),
        }


def _load_json(s: str, default):
    try:
        v = json.loads(s)
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default
