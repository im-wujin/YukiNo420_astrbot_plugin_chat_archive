import ast
import datetime
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class JSONResponse:
    def __init__(self, content=None, status_code=200):
        self.content = content
        self.status_code = status_code
        self.body = json.dumps(content, ensure_ascii=False).encode("utf-8")


class HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Logger:
    def error(self, *args, **kwargs):
        pass


def Query(default, **kwargs):
    return default


def load_api_functions():
    server_path = Path(__file__).resolve().parents[1] / "web" / "server.py"
    tree = ast.parse(server_path.read_text(encoding="utf-8"))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"get_stats", "get_members"}
    ]
    for node in functions:
        node.decorator_list = []

    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "datetime": datetime,
        "get_db_connection": None,
        "HTTPException": HTTPException,
        "JSONResponse": JSONResponse,
        "logger": Logger(),
        "Query": Query,
    }
    exec(compile(module, str(server_path), "exec"), namespace)
    return namespace


class MemberApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "chat.db"
        self._init_db()
        self.api = load_api_functions()
        self.api["get_db_connection"] = self.connect

    def tearDown(self):
        self.tmpdir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE chat_history (
                id INTEGER PRIMARY KEY,
                user_id TEXT,
                sender_name TEXT,
                message TEXT,
                timestamp INTEGER,
                session_id TEXT,
                message_type TEXT
            )
            """
        )
        rows = [
            (1, "1001", "旧昵称", "hello", 100, "group:1", "group"),
            (2, "1001", "其他群昵称", "hello", 300, "group:2", "group"),
            (3, "1001", "当前群新昵称", "hello", 200, "group:1", "group"),
            (4, "1002", "第二个人", "hello", 150, "group:1", "group"),
            (5, "", "空用户", "ignored", 400, "group:1", "group"),
        ]
        conn.executemany(
            "INSERT INTO chat_history VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

    def decode(self, response):
        return json.loads(response.body.decode("utf-8"))

    def test_members_use_latest_name_within_filtered_session(self):
        response = self.api["get_members"](
            session_id="group:1",
            keyword="",
            time_start=0,
            time_end=0,
            limit=10,
            offset=0,
        )
        data = self.decode(response)["data"]
        first = data["members"][0]

        self.assertEqual(first["user_id"], "1001")
        self.assertEqual(first["sender_name"], "当前群新昵称")
        self.assertEqual(data["total"], 2)

    def test_stats_top_users_use_latest_name_and_skip_blank_user(self):
        response = self.api["get_stats"](
            session_id="group:1",
            user_id="",
            time_start=0,
            time_end=0,
            is_private=0,
        )
        top_users = self.decode(response)["data"]["top_users"]

        self.assertEqual([user["user_id"] for user in top_users], ["1001", "1002"])
        self.assertEqual(top_users[0]["sender_name"], "当前群新昵称")


if __name__ == "__main__":
    unittest.main()
