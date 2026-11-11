"""SQLite persistence for sessions, task checkpoints, evidence and feedback."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.getenv("AGENT_MVP_DB", str(Path(__file__).resolve().parent.parent / "agent_mvp.sqlite3")))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT, user_renamed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), intent TEXT, status TEXT NOT NULL, state_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), task_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL, feedback_type TEXT NOT NULL, reason TEXT, text TEXT, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS errors(id TEXT PRIMARY KEY, task_id TEXT, intent TEXT, step TEXT, category TEXT, message TEXT, retry_count INTEGER, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL, progress_json TEXT NOT NULL, response_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), status TEXT NOT NULL, plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
    if "title" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
    if "user_renamed" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN user_renamed INTEGER NOT NULL DEFAULT 0")
    return db


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session() -> str:
    session_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("INSERT INTO sessions(id,created_at) VALUES (?,?)", (session_id, now()))
    return session_id


def list_sessions() -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY created_at DESC")]


def rename_session(session_id: str, title: str):
    title = title.strip()
    if not title or len(title) > 80:
        raise ValueError("会话标题长度须为 1–80 字")
    with connect() as db:
        if not db.execute("UPDATE sessions SET title=?,user_renamed=1 WHERE id=?", (title, session_id)).rowcount:
            raise ValueError("Session not found")


def delete_session(session_id: str):
    with connect() as db:
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise ValueError("Session not found")
        if db.execute("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running')", (session_id,)).fetchone():
            raise ValueError("请先停止正在运行的分析任务")
        ids = [row[0] for row in db.execute("SELECT id FROM tasks WHERE session_id=?", (session_id,))]
        ids += [row[0] for row in db.execute("SELECT id FROM requests WHERE session_id=?", (session_id,))]
        ids += [row[0] for row in db.execute("SELECT id FROM jobs WHERE session_id=?", (session_id,))]
        for task_id in ids:
            db.execute("DELETE FROM errors WHERE task_id=?", (task_id,))
        for table in ("feedback", "messages", "jobs", "requests", "tasks"):
            db.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def session_exists(session_id: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is not None


def history(session_id: str) -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid", (session_id,))]


def latest_task(session_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["state"] = json.loads(value.pop("state_json"))
    value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
    return value


def tasks_for_session(session_id: str) -> list[dict]:
    with connect() as db:
        ids = [row[0] for row in db.execute("SELECT id FROM tasks WHERE session_id=? ORDER BY created_at, rowid", (session_id,))]
    return [task_by_id(task_id) for task_id in ids]


def task_by_id(task_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["state"] = json.loads(value.pop("state_json"))
    value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
    return value


def save_task(task_id: str, session_id: str, intent: str, status: str, state: dict, result: dict | None = None):
    ts = now()
    with connect() as db:
        db.execute("INSERT INTO tasks(id,session_id,intent,status,state_json,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET intent=excluded.intent,status=excluded.status,state_json=excluded.state_json,result_json=excluded.result_json,updated_at=excluded.updated_at",
                   (task_id, session_id, intent, status, json.dumps(state, ensure_ascii=False), json.dumps(result, ensure_ascii=False) if result else None, ts, ts))
        if status == "completed" and result and result.get("title"):
            db.execute("UPDATE sessions SET title=? WHERE id=? AND title IS NULL AND user_renamed=0", (result["title"][:80], session_id))


def add_message(session_id: str, task_id: str | None, role: str, content: str):
    with connect() as db:
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), session_id, task_id, role, content, now()))


def replace_task_assistant_message(task_id: str, content: str):
    """Replace the failed answer when retrying the same task, without adding a new turn."""
    with connect() as db:
        row = db.execute("SELECT id FROM messages WHERE task_id=? AND role='assistant' ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
        if not row:
            raise ValueError("Task has no assistant message to replace")
        db.execute("UPDATE messages SET content=? WHERE id=?", (content, row["id"]))


def add_error(task_id: str, intent: str, step: str, category: str, message: str, retry_count: int = 0):
    with connect() as db:
        db.execute("INSERT INTO errors VALUES (?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), task_id, intent, step, category, message, retry_count, now()))


def add_feedback(task_id: str, feedback_type: str, reason: str = "", text: str = "") -> str:
    task = task_by_id(task_id)
    if not task or not task["result"]:
        raise ValueError("Feedback requires a completed result")
    feedback_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?)", (feedback_id, task["session_id"], task_id, feedback_type, reason, text,
                   json.dumps({"task": task["state"], "result": task["result"], "evidence_refs": task["result"].get("evidence", []), "tool_result_refs": task["state"].get("tool_result_refs", [])}, ensure_ascii=False), now()))
    return feedback_id


def list_feedback() -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM feedback ORDER BY created_at DESC")]


def save_job(job_id: str, session_id: str, status: str, progress: list, response: dict | None = None, error: str | None = None):
    ts = now()
    with connect() as db:
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,progress_json=excluded.progress_json,response_json=excluded.response_json,error=excluded.error,updated_at=excluded.updated_at",
                   (job_id, session_id, status, json.dumps(progress, ensure_ascii=False), json.dumps(response, ensure_ascii=False) if response else None, error, ts, ts))


def save_request(request_id: str, session_id: str, status: str, plan: dict):
    ts = now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT plan_json FROM requests WHERE id=?", (request_id,)).fetchone()
        if current and json.loads(current[0]).get("plan_version", 0) > plan.get("plan_version", 0):
            return False
        db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,plan_json=excluded.plan_json,updated_at=excluded.updated_at",
                   (request_id, session_id, status, json.dumps(plan, ensure_ascii=False), ts, ts))
    return True


def requests_for_session(session_id: str) -> list[dict]:
    with connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM requests WHERE session_id=? ORDER BY created_at,rowid", (session_id,))]
    for row in rows:
        row["plan"] = json.loads(row.pop("plan_json"))
    return rows


def update_request_task(session_id: str, task_id: str, status: str):
    """Resolve a waiting subtask without changing sibling tasks or their results."""
    for request in requests_for_session(session_id):
        plan = request["plan"]
        matching = [step for step in plan["steps"] if step.get("task_id") == task_id]
        if not matching:
            continue
        matching[0]["status"] = status
        plan["plan_version"] += 1
        final = "completed" if all(step["status"] == "COMPLETED" for step in plan["steps"]) else "partial"
        save_request(request["id"], session_id, final, plan)
        return


def replace_request_task(session_id: str, old_task_id: str, new_task_id: str, status: str):
    for request in requests_for_session(session_id):
        plan = request["plan"]
        matching = [step for step in plan["steps"] if step.get("task_id") == old_task_id]
        if not matching:
            continue
        matching[0]["task_id"] = new_task_id
        matching[0]["status"] = status
        matching[0]["supersedes_task_id"] = old_task_id
        plan["plan_version"] += 1
        final = "completed" if all(step["status"] == "COMPLETED" for step in plan["steps"]) else "partial"
        save_request(request["id"], session_id, final, plan)
        return


def job_by_id(job_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["progress"] = json.loads(value.pop("progress_json"))
    value["response"] = json.loads(value.pop("response_json")) if value.get("response_json") else None
    return value
