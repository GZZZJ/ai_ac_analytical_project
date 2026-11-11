"""Background analysis with persisted progress and cooperative cancellation."""
from __future__ import annotations

import threading
import uuid

from . import service, store

_lock = threading.Lock()
_cancel: dict[str, threading.Event] = {}


def start(session_id: str, query: str, answers: dict | None = None, *, use_model: bool = True,
          target_task_id: str | None = None) -> str:
    if not store.session_exists(session_id):
        raise ValueError("Session not found")
    if target_task_id:
        target = store.task_by_id(target_task_id)
        if not target or target["session_id"] != session_id:
            raise ValueError("Target task not found in session")
    if not query.strip() and not answers:
        raise ValueError("Empty message")
    job_id = str(uuid.uuid4())
    event = threading.Event()
    with _lock:
        _cancel[job_id] = event
    store.save_job(job_id, session_id, "queued", [])

    def run():
        progress: list = []

        def update(steps: list):
            nonlocal progress
            progress = steps
            store.save_job(job_id, session_id, "running", steps)

        try:
            store.save_job(job_id, session_id, "running", progress)
            if target_task_id:
                result = service.send(session_id, query, answers, use_model, update, event.is_set, target_task_id=target_task_id)
            else:
                result = service.send(session_id, query, answers, use_model, update, event.is_set)
            store.save_job(job_id, session_id, "completed", result.get("progress", progress), result)
        except InterruptedError:
            store.save_job(job_id, session_id, "stopped", progress, error="分析已停止")
        except Exception as exc:
            store.add_error(job_id, "UNKNOWN", "job", type(exc).__name__, str(exc))
            store.save_job(job_id, session_id, "failed", progress, error="分析任务执行失败，请重试或查看错误记录")
        finally:
            with _lock:
                _cancel.pop(job_id, None)

    threading.Thread(target=run, daemon=True, name=f"analysis-{job_id[:8]}").start()
    return job_id


def stop(job_id: str) -> dict:
    job = store.job_by_id(job_id)
    if not job:
        raise ValueError("Job not found")
    with _lock:
        event = _cancel.get(job_id)
        if event and job["status"] in {"queued", "running"}:
            event.set()
            return {"status": "stopping"}
    return {"status": job["status"]}
