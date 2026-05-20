import sqlite3
import os
import json
import queue
import threading
import datetime
import time
from pathlib import Path
from contextlib import contextmanager
from astrbot.api import logger

# Resolve default relative path under plugin directory
PLUGIN_DIR = Path(__file__).resolve().parent

def _expand_path(path_value: str, base_dir: Path | None = None) -> Path:
    """Expand ~, environment variables and relative paths in a cross-platform way."""
    expanded = os.path.expandvars(str(path_value)).strip()
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = (base_dir or DATA_DIR) / path
    return path.resolve()


def _get_data_dir() -> Path:
    env_data_dir = os.environ.get("ARCHIVE_DATA_DIR", "").strip()
    if env_data_dir:
        return _expand_path(env_data_dir, PLUGIN_DIR)
    try:
        from astrbot.api.star import StarTools
        return Path(StarTools.get_data_dir()).expanduser().resolve()
    except Exception:
        # Fallback for standalone decoupling execution or tests
        return (PLUGIN_DIR / "data").resolve()


DATA_DIR = _get_data_dir()

DEFAULT_DB_PATH = str(DATA_DIR / "chat_history.db")

# logger is imported from astrbot.api to meet framework standards

class Database:
    def execute(self, sql, params=None):
        raise NotImplementedError
    def executemany(self, sql, seq_of_params):
        raise NotImplementedError
    def fetchone(self):
        raise NotImplementedError
    def fetchall(self):
        raise NotImplementedError
    def commit(self):
        raise NotImplementedError
    def rollback(self):
        raise NotImplementedError
    def close(self):
        raise NotImplementedError

class SQLiteConnectionPool:
    def __init__(self, db_path, max_connections=5):
        self.db_path = db_path
        self.max_connections = max_connections
        self.pool = queue.Queue(max_connections)
        self._lock = threading.Lock()
        self._allocated = 0

    def _dict_factory(self, cursor, row):
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def _create_connection(self):
        # Allow cross-thread connection usage under safe queue-based reuse
        conn = sqlite3.connect(self.db_path, timeout=20, check_same_thread=False)
        conn.row_factory = self._dict_factory
        journal_mode = _load_sqlite_journal_mode()
        conn.execute(f"PRAGMA journal_mode={journal_mode};")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def get_connection(self):
        try:
            return self.pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._allocated < self.max_connections:
                    self._allocated += 1
                    try:
                        return self._create_connection()
                    except Exception as e:
                        self._allocated -= 1
                        raise e
            # Block until a connection is released
            return self.pool.get(timeout=10)

    def release_connection(self, conn):
        if conn:
            try:
                self.pool.put_nowait(conn)
            except queue.Full:
                logger.warning("SQLite connection pool is full; closing returned connection.")
                try:
                    conn.close()
                finally:
                    with self._lock:
                        self._allocated = max(0, self._allocated - 1)

    def close_all(self):
        closed = 0
        while not self.pool.empty():
            try:
                conn = self.pool.get_nowait()
                conn.close()
                closed += 1
            except queue.Empty:
                break
        if closed:
            with self._lock:
                self._allocated = max(0, self._allocated - closed)


def _get_config_path() -> Path:
    env_config_path = os.environ.get("ARCHIVE_CONFIG_PATH", "").strip()
    if env_config_path:
        return _expand_path(env_config_path, PLUGIN_DIR)

    config_path = DATA_DIR.parent.parent / "config" / "astrbot_plugin_chat_archive_config.json"
    if not config_path.exists():
        config_path = PLUGIN_DIR.parent.parent / "config" / "astrbot_plugin_chat_archive_config.json"
    return config_path


def _load_db_path() -> str:
    env_db_path = os.environ.get("ARCHIVE_DB_PATH", "").strip()
    if env_db_path:
        return str(_expand_path(env_db_path, DATA_DIR))

    config_path = _get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                custom_path = data.get("basic", {}).get("db_path", "")
                if custom_path:
                    return str(_expand_path(custom_path, DATA_DIR))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read custom db path from config: {e}")
    return str(_expand_path(DEFAULT_DB_PATH, DATA_DIR))


def _load_sqlite_journal_mode() -> str:
    allowed_modes = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}
    mode = os.environ.get("ARCHIVE_SQLITE_JOURNAL_MODE", "").strip().upper()
    if not mode:
        config_path = _get_config_path()
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                    mode = str(data.get("basic", {}).get("sqlite_journal_mode", "WAL")).strip().upper()
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read sqlite journal mode from config: {e}")
                mode = "WAL"
    mode = mode or "WAL"
    if mode not in allowed_modes:
        logger.warning(f"Unsupported SQLite journal mode '{mode}', fallback to WAL.")
        mode = "WAL"
    return mode

DB_PATH = _load_db_path()

_POOL = None
_POOL_LOCK = threading.Lock()

def get_connection_pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = SQLiteConnectionPool(DB_PATH)
    return _POOL


class SQLiteDatabase(Database):
    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None
        self._cursor = None
        self._pool = get_connection_pool()

    def _get_connection(self):
        if self._conn is None:
            self._conn = self._pool.get_connection()
        return self._conn

    def execute(self, sql, params=None):
        try:
            conn = self._get_connection()
            self._cursor = conn.execute(sql, params or ())
            return self
        except Exception as e:
            logger.error(f"SQL execution failed: {sql} | Error: {e}")
            raise e

    def executemany(self, sql, seq_of_params):
        retries = 3
        delays = [0.5, 1.0, 2.0]
        for attempt in range(retries):
            try:
                conn = self._get_connection()
                self._cursor = conn.executemany(sql, seq_of_params)
                return self
            except sqlite3.OperationalError as e:
                if attempt < retries - 1:
                    logger.warning(f"SQL executemany operational error: {e}. Retrying in {delays[attempt]}s...")
                    time.sleep(delays[attempt])
                else:
                    logger.error(f"SQL executemany failed after retries: {sql} | Error: {e}")
                    raise e
            except Exception as e:
                logger.error(f"SQL executemany failed: {sql} | Error: {e}")
                raise e

    def fetchone(self):
        if self._cursor:
            return self._cursor.fetchone()
        return None

    def fetchall(self):
        if self._cursor:
            return self._cursor.fetchall()
        return []

    def commit(self):
        if self._conn:
            self._conn.commit()

    def rollback(self):
        if self._conn:
            self._conn.rollback()

    def close(self):
        if self._conn:
            if self._pool:
                self._pool.release_connection(self._conn)
            self._conn = None
            self._cursor = None
            logger.debug("SQLiteDatabase connection closed and released to pool.")

    @property
    def row_factory(self):
        return self._get_connection().row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._get_connection().row_factory = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            try:
                self.rollback()
            except Exception as e:
                logger.error(f"SQLiteDatabase rollback failed: {e}")
        self.close()

def get_db_connection() -> Database:
    return SQLiteDatabase(DB_PATH)


class DatabaseManager:
    """Manages all complex query operations for chat history to keep plugin class slim."""

    @staticmethod
    def get_history(
        user_id: str = None,
        session_id: str = None,
        keyword: str = None,
        since_ts: int = None,
        until_ts: int = None,
        limit: int = 50,
        offset: int = 0,
        asc: bool = True,
        exclude_recalled: bool = True,
    ) -> list[dict]:
        try:
            with get_db_connection() as conn:
                query = (
                    "SELECT id, user_id, sender_name, message, timestamp, "
                    "session_id, message_type, session_name, msg_id, is_recalled "
                    "FROM chat_history WHERE 1=1"
                )
                params: list = []

                if exclude_recalled:
                    query += " AND (is_recalled IS NULL OR is_recalled = 0)"
                if user_id:
                    query += " AND user_id = ?"
                    params.append(str(user_id))
                if session_id:
                    query += " AND session_id = ?"
                    params.append(str(session_id))
                if keyword:
                    query += " AND message LIKE ? ESCAPE '\\'"
                    safe_keyword = (
                        keyword.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                    )
                    params.append(f"%{safe_keyword}%")
                if since_ts is not None:
                    query += " AND timestamp >= ?"
                    params.append(int(since_ts))
                if until_ts is not None:
                    query += " AND timestamp <= ?"
                    params.append(int(until_ts))

                order = "ASC" if asc else "DESC"
                id_order = "ASC" if asc else "DESC"
                query += f" ORDER BY timestamp {order}, id {id_order} LIMIT ? OFFSET ?"
                params.extend([limit, offset])

                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Chat Archive API get_history error: {e}")
            return []

    @staticmethod
    def get_sessions() -> list[dict]:
        try:
            with get_db_connection() as conn:
                cursor = conn.execute(
                    "SELECT session_id, message_type, COUNT(*) as count, "
                    "MAX(timestamp) as last_time "
                    "FROM chat_history GROUP BY session_id "
                    "ORDER BY last_time DESC"
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Chat Archive API get_sessions error: {e}")
            return []

    @staticmethod
    def get_member_rank(
        session_id: str,
        limit: int = 10,
        since_ts: int = None,
        until_ts: int = None,
    ) -> list[dict]:
        try:
            with get_db_connection() as conn:
                query = (
                    "SELECT user_id, sender_name, COUNT(*) as count "
                    "FROM chat_history WHERE session_id = ?"
                )
                params: list = [str(session_id)]

                if since_ts is not None:
                    query += " AND timestamp >= ?"
                    params.append(int(since_ts))
                if until_ts is not None:
                    query += " AND timestamp <= ?"
                    params.append(int(until_ts))

                query += " GROUP BY user_id ORDER BY count DESC LIMIT ?"
                params.append(limit)

                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Chat Archive API get_member_rank error: {e}")
            return []

    @staticmethod
    def get_user_summary(user_id: str, session_id: str = None) -> dict:
        summary = {
            "user_id": str(user_id),
            "total_messages": 0,
            "first_seen": None,
            "last_seen": None,
            "last_nickname": None,
        }
        try:
            with get_db_connection() as conn:
                where = "WHERE user_id = ?"
                params: list = [str(user_id)]
                if session_id:
                    where += " AND session_id = ?"
                    params.append(str(session_id))

                cursor = conn.execute(
                    f"SELECT COUNT(*) as cnt, "
                    f"MIN(timestamp) as first_ts, "
                    f"MAX(timestamp) as last_ts "
                    f"FROM chat_history {where}",
                    params,
                )
                row = cursor.fetchone()
                if row and row["cnt"] > 0:
                    summary["total_messages"] = row["cnt"]
                    summary["first_seen"] = row["first_ts"]
                    summary["last_seen"] = row["last_ts"]

                    name_cursor = conn.execute(
                        f"SELECT sender_name FROM chat_history "
                        f"{where} ORDER BY timestamp DESC LIMIT 1",
                        params,
                    )
                    name_row = name_cursor.fetchone()
                    summary["last_nickname"] = name_row["sender_name"] if name_row else None
        except Exception as e:
            logger.error(f"Chat Archive API get_user_summary error: {e}")
        return summary

    @staticmethod
    def get_message_count(
        user_id: str = None,
        session_id: str = None,
        since_ts: int = None,
        until_ts: int = None,
        exclude_recalled: bool = True,
    ) -> int:
        try:
            with get_db_connection() as conn:
                query = "SELECT COUNT(*) as cnt FROM chat_history WHERE 1=1"
                params: list = []

                if exclude_recalled:
                    query += " AND (is_recalled IS NULL OR is_recalled = 0)"
                if user_id:
                    query += " AND user_id = ?"
                    params.append(str(user_id))
                if session_id:
                    query += " AND session_id = ?"
                    params.append(str(session_id))
                if since_ts is not None:
                    query += " AND timestamp >= ?"
                    params.append(int(since_ts))
                if until_ts is not None:
                    query += " AND timestamp <= ?"
                    params.append(int(until_ts))

                row = conn.execute(query, params).fetchone()
                return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"Chat Archive API get_message_count error: {e}")
            return 0

    @classmethod
    def get_context_messages(
        cls,
        session_id: str,
        user_id: str = None,
        limit: int = 50,
        exclude_recalled: bool = True,
    ) -> list[tuple[str, str, str]]:
        records = cls.get_history(
            session_id=session_id,
            user_id=user_id,
            limit=limit,
            asc=True,
            exclude_recalled=exclude_recalled,
        )
        result = []
        for msg in records:
            ts = msg.get("timestamp", 0)
            ts_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            sender = msg.get("sender_name", "")
            content = msg.get("message", "")
            result.append((ts_str, sender, content))
        return result

    @classmethod
    def execute_query(
        cls,
        query_type: str,
        session_id: str,
        keyword: str = "",
        user_id: str = "",
        limit: int = 20,
        since_ts: int = None,
    ) -> dict:
        if query_type == "recent":
            messages = cls.get_history(
                session_id=session_id,
                limit=limit,
                asc=False,
                since_ts=since_ts,
            )
            messages.reverse()
            formatted = []
            for msg in messages:
                ts = msg.get("timestamp", 0)
                ts_str = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                formatted.append(
                    {
                        "time": ts_str,
                        "sender": msg.get("sender_name", ""),
                        "message": msg.get("message", ""),
                    }
                )
            return {
                "type": "recent",
                "count": len(formatted),
                "messages": formatted,
            }

        elif query_type == "search":
            if not keyword:
                return {"error": "搜索类型需要提供 keyword 参数"}
            messages = cls.get_history(
                session_id=session_id,
                keyword=keyword,
                limit=limit,
                asc=False,
                since_ts=since_ts,
            )
            messages.reverse()
            formatted = []
            for msg in messages:
                ts = msg.get("timestamp", 0)
                ts_str = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                formatted.append(
                    {
                        "time": ts_str,
                        "sender": msg.get("sender_name", ""),
                        "user_id": msg.get("user_id", ""),
                        "message": msg.get("message", ""),
                    }
                )
            return {
                "type": "search",
                "keyword": keyword,
                "count": len(formatted),
                "messages": formatted,
            }

        elif query_type == "rank":
            ranking = cls.get_member_rank(
                session_id=session_id,
                limit=limit,
                since_ts=since_ts,
            )
            formatted = []
            for i, member in enumerate(ranking, 1):
                formatted.append(
                    {
                        "rank": i,
                        "sender": member.get("sender_name", ""),
                        "user_id": member.get("user_id", ""),
                        "message_count": member.get("count", 0),
                    }
                )
            return {
                "type": "rank",
                "total_members": len(formatted),
                "ranking": formatted,
            }

        elif query_type == "user_summary":
            if not user_id:
                return {"error": "用户统计需要提供 user_id 参数"}
            summary = cls.get_user_summary(user_id=user_id, session_id=session_id)
            return {
                "type": "user_summary",
                "user_id": summary["user_id"],
                "total_messages": summary["total_messages"],
                "first_seen": datetime.datetime.fromtimestamp(summary["first_seen"]).strftime("%Y-%m-%d %H:%M") if summary["first_seen"] else "无",
                "last_seen": datetime.datetime.fromtimestamp(summary["last_seen"]).strftime("%Y-%m-%d %H:%M") if summary["last_seen"] else "无",
                "last_nickname": summary["last_nickname"] or "未知",
            }

        elif query_type == "count":
            cnt = cls.get_message_count(session_id=session_id, since_ts=since_ts)
            return {
                "type": "count",
                "session_id": session_id,
                "message_count": cnt,
                "time_limit": f"最近 {int((time.time() - since_ts)/86400)} 天" if since_ts else "全部时间",
            }

        return {"error": f"不支持的查询类型: {query_type}"}


def column_exists(db, table, column):
    cursor = db.execute(f"PRAGMA table_info({table});")
    columns = [row["name"] for row in cursor.fetchall()]
    return column in columns

def migrate_v1(db):
    if not column_exists(db, "chat_history", "session_id"):
        db.execute("ALTER TABLE chat_history ADD COLUMN session_id TEXT;")
    if not column_exists(db, "chat_history", "message_type"):
        db.execute("ALTER TABLE chat_history ADD COLUMN message_type TEXT;")

def migrate_v2(db):
    if not column_exists(db, "chat_history", "session_name"):
        db.execute("ALTER TABLE chat_history ADD COLUMN session_name TEXT;")

def migrate_v3(db):
    if not column_exists(db, "chat_history", "msg_id"):
        db.execute("ALTER TABLE chat_history ADD COLUMN msg_id TEXT;")

def migrate_v4(db):
    if not column_exists(db, "chat_history", "is_recalled"):
        db.execute("ALTER TABLE chat_history ADD COLUMN is_recalled INTEGER DEFAULT 0;")

def init_db():
    Path(DB_PATH).expanduser().parent.mkdir(parents=True, exist_ok=True)
    db = get_db_connection()
    try:
        # 检测表是否已存在
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_history';")
        table_exists = cursor.fetchone() is not None

        if not table_exists:
            # 1. 全新数据库：直接建立包含所有最新列的完整表，免去中间迁移步骤
            db.execute('''CREATE TABLE chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                sender_name TEXT,
                message TEXT,
                timestamp INTEGER,
                session_id TEXT,
                message_type TEXT,
                session_name TEXT,
                msg_id TEXT,
                is_recalled INTEGER DEFAULT 0
            )''')
            # 将 user_version 置为当前最高迁移版本
            db.execute("PRAGMA user_version = 4;")
            db.commit()
        else:
            # 2. 存量数据库：基于 PRAGMA user_version 进行有序增量迁移
            cursor = db.execute("PRAGMA user_version;")
            row = cursor.fetchone()
            current_version = row["user_version"] if row else 0

            migrations = [
                (1, migrate_v1),
                (2, migrate_v2),
                (3, migrate_v3),
                (4, migrate_v4),
            ]

            for version, migrate_func in migrations:
                if current_version < version:
                    migrate_func(db)
                    db.execute(f"PRAGMA user_version = {version};")
                    db.commit()

        # 3. 始终确保必要的高性能索引已创建（幂等）
        # 注: idx_user 和 idx_session 尽管被组合索引覆盖，保留它们是为了兼容旧查询可能只过滤单列时的性能
        db.execute("CREATE INDEX IF NOT EXISTS idx_msg_id ON chat_history(msg_id);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_user ON chat_history(user_id);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_session ON chat_history(session_id);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_session_timestamp ON chat_history(session_id, timestamp);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_user_timestamp ON chat_history(user_id, timestamp);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON chat_history(timestamp);")
        db.commit()
    finally:
        db.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
