"""SQLite persistence and per-session exclusion for one local process."""

import json
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path


class SessionBusy(Exception):
    pass


class SessionStore:
    # All stores for one canonical DB path share active claims in this process.
    _active: set[tuple[str, str, str]] = set()
    _guard = threading.Lock()

    def __init__(self, path: str):
        if not path or str(path) == ":memory:":
            raise ValueError("请使用文件数据库；逐次连接不支持 :memory:。")
        self.path = os.path.normcase(str(Path(path).resolve()))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT NOT NULL, session_id TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY (user_id, session_id))""")

    @contextmanager
    def claim(self, user_id: str, session_id: str):
        key = (self.path, user_id, session_id)
        with self._guard:
            if key in self._active:
                raise SessionBusy("该会话正在执行，请完成后重试或切换会话。")
            self._active.add(key)
        try:
            yield
        finally:
            with self._guard:
                self._active.remove(key)

    def load(self, user_id: str, session_id: str) -> dict:
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute(
                "SELECT state FROM sessions WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            ).fetchone()
        if row is None:
            return {"history": [], "todos": [], "next_todo_id": 1}
        return json.loads(row[0])

    def save(self, user_id: str, session_id: str, state: dict):
        # History and local todo changes commit together. Never hold a DB
        # transaction while waiting for the model's network request.
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO sessions VALUES (?, ?, ?)
                ON CONFLICT(user_id, session_id) DO UPDATE SET state=excluded.state""",
                (user_id, session_id, payload),
            )
