"""HTTP API and built frontend."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import data, jobs, reporting, service, store

app = FastAPI(title="获客智能分析 Agent MVP")


class Message(BaseModel):
    query: str = ""
    answers: dict | None = None
    target_task_id: str | None = None


class Feedback(BaseModel):
    task_id: str
    feedback_type: str = Field(pattern="^(positive|negative)$")
    reason: str = ""
    text: str = ""


class SessionRename(BaseModel):
    title: str


@app.get("/api/health")
def health():
    return {"ok": True, "model_configured": bool(service.LLMClient().key)}


@app.get("/api/dataset")
def dataset():
    return {"as_of": data.AS_OF.isoformat(), "type": "模拟数据", "maturity": "T0–T7 离线终值"}


@app.post("/api/sessions")
def new_session():
    return {"session_id": store.create_session()}


@app.get("/api/sessions")
def sessions():
    return store.list_sessions()


@app.get("/api/sessions/{session_id}")
def session(session_id: str):
    if not store.session_exists(session_id):
        raise HTTPException(404, "Session not found")
    task = store.latest_task(session_id)
    tasks = store.tasks_for_session(session_id)
    for item in tasks:
        reporting.display_result(item["result"])
    if task:
        reporting.display_result(task["result"])
    return {"messages": store.history(session_id), "tasks": tasks, "requests": store.requests_for_session(session_id), "task": task,
            "clarifications": [{"task_id": item["id"], "clarification": service._clarification(item["state"]["slots"], item["state"].get("dual_media", False))} for item in tasks if item["status"] == "clarifying"],
            "clarification": service._clarification(task["state"]["slots"], task["state"].get("dual_media", False)) if task and task["status"] == "clarifying" else None}


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, payload: SessionRename):
    try:
        store.rename_session(session_id, payload.title)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    try:
        store.delete_session(session_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/tasks/{task_id}/export.md")
def export_markdown(task_id: str):
    task = store.task_by_id(task_id)
    if not task or not task["result"]:
        raise HTTPException(404, "Result not found")
    result = reporting.display_result(task["result"])
    sections = [f"# {result['title']}", "## 结论", result["answer"], "## 数据依据", *[f"- {text}" for text in result["facts"]],
                "## 分析过程", *[f"### {step['title']}\n方法：{step['method']}\n依据：{'；'.join(step['evidence'])}\n判断：{step['finding']}\n边界：{step['boundary']}" for step in result["analysis_steps"]],
                "## 原因判断", *[f"- {claim['status']}：{claim['text']}（证据强度：{claim['confidence']}）" for claim in result["claims"]],
                "## 限制", *[f"- {text}" for text in result["reliability"]["limitations"]],
                "## 建议动作", *[f"- {action['action']}；责任方：{action['owner']}；时间：{action['when']}；观察：{action['observe']}；回滚：{action['rollback']}" for action in result["actions"]],
                "## 来源", *[f"- {source.get('table') or source.get('knowledge')} · {source.get('range') or source.get('rule')}" for source in result["evidence"]]]
    return PlainTextResponse("\n\n".join(sections) + "\n", media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="analysis-{task_id}.md"'})


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    try:
        return service.cancel_subtask(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/sessions/{session_id}/messages")
def message(session_id: str, payload: Message):
    try:
        return service.send(session_id, payload.query, payload.answers, target_task_id=payload.target_task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/sessions/{session_id}/jobs")
def start_job(session_id: str, payload: Message):
    try:
        return {"job_id": jobs.start(session_id, payload.query, payload.answers, target_task_id=payload.target_task_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    value = store.job_by_id(job_id)
    if not value:
        raise HTTPException(404, "Job not found")
    return value


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    try:
        return jobs.stop(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/feedback")
def feedback(payload: Feedback):
    try:
        return {"feedback_id": store.add_feedback(payload.task_id, payload.feedback_type, payload.reason, payload.text)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/feedback")
def feedback_list():
    return store.list_feedback()


DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
def index():
    if not (DIST / "index.html").exists():
        return {"message": "Frontend not built. Run npm install && npm run build in frontend."}
    return FileResponse(DIST / "index.html")
