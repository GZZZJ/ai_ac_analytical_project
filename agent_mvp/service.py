"""Conversation orchestration and task state."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from typing import TypedDict

from . import data, diagnostic, followups, investigation, reporting, store, team
from .analyzers import Blocked, run
from .model import LLMClient, RELATION_SCHEMA, SLOT_SCHEMA, asks_both_channels, broad_performance_query, relation, rule_slots

_subtask_lock = threading.Lock()
_subtask_events: dict[str, threading.Event] = {}


class GraphState(TypedDict, total=False):
    intent: str
    slots: dict
    progress: list
    result: dict
    error: str


def _progress(state: GraphState, name: str, status: str, detail: str = "") -> list:
    return state.get("progress", []) + [{"name": name, "status": status, "detail": detail}]


def _precheck(state: GraphState) -> GraphState:
    slots = state["slots"]
    if slots.get("media") not in {"腾讯", "抖音"}:
        return {"error": "请确认渠道为腾讯或抖音", "progress": _progress(state, "确认口径与数据", "blocked")}
    return {"progress": _progress(state, "确认口径与数据", "completed", "T0–T7 离线终值；花费为业务口径")}


def _analyze(state: GraphState) -> GraphState:
    if state.get("error"):
        return {}
    try:
        result = run(state["intent"], state["slots"])
        return {"result": result, "progress": _progress(state, "计算指标与对照", "completed", result["facts"][0])}
    except (Blocked, ValueError, KeyError, ZeroDivisionError) as exc:
        return {"error": str(exc), "progress": _progress(state, "计算指标与对照", "blocked", str(exc))}


def _evidence(state: GraphState) -> GraphState:
    if state.get("error"):
        return {}
    result = state["result"]
    if not result.get("evidence") or not result.get("facts"):
        return {"error": "缺少可追溯证据", "progress": _progress(state, "核对证据边界", "blocked")}
    result["evidence"].append({"knowledge": "01_知识库/03_核心指标口径.md", "rule": "CPS、授信单产、T7 成熟度等口径"})
    result["evidence"].append({"knowledge": "01_知识库/07_业务分析常用方法.md", "rule": "恒等式分解、对照与因果边界"})
    return {"result": result, "progress": _progress(state, "核对证据边界", "completed")}


def _compose(state: GraphState) -> GraphState:
    if state.get("error"):
        return {}
    result = state["result"]
    result["summary"] = "；".join(result["facts"][:2])
    return {"result": result, "progress": _progress(state, "形成结论与动作", "completed")}


def _graph():
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None
    builder = StateGraph(GraphState)
    builder.add_node("precheck", _precheck)
    builder.add_node("analyze", _analyze)
    builder.add_node("evidence", _evidence)
    builder.add_node("compose", _compose)
    builder.add_edge(START, "precheck")
    builder.add_edge("precheck", "analyze")
    builder.add_edge("analyze", "evidence")
    builder.add_edge("evidence", "compose")
    builder.add_edge("compose", END)
    return builder.compile()


GRAPH = _graph()


def _run_graph(initial: GraphState, on_progress: Callable[[list], None] | None = None, cancelled: Callable[[], bool] | None = None) -> GraphState:
    if cancelled and cancelled():
        raise InterruptedError("用户停止了本次分析")
    if GRAPH:
        last: GraphState = dict(initial)
        shown = 0
        for state in GRAPH.stream(initial, stream_mode="values"):
            if cancelled and cancelled():
                raise InterruptedError("用户停止了本次分析")
            last = state
            progress = state.get("progress", [])
            if len(progress) > shown:
                shown = len(progress)
                if on_progress:
                    on_progress(progress)
        return last
    # Fallback preserves deterministic analyzer tests when LangGraph is unavailable.
    state = dict(initial)
    for fn in (_precheck, _analyze, _evidence, _compose):
        if cancelled and cancelled():
            raise InterruptedError("用户停止了本次分析")
        state.update(fn(state))
        if on_progress:
            on_progress(state["progress"])
    return state


def _clarification(slots: dict, dual_media: bool = False) -> dict:
    missing = []
    if slots["intent"] == "UNKNOWN":
        missing.append({"key": "metric", "label": "分析目标 · 单选", "options": ["综合诊断（规模、效率、质量）", "获客效率 / CPS", "投放量级 / 消耗", "风险质量 / 流量价值"], "default": "综合诊断（规模、效率、质量）"})
    if slots.get("days") is None and slots["intent"] in {"INVESTIGATION", "GENERAL_DIAGNOSIS", "COST_ANOMALY", "SCALE_ANOMALY", "UNKNOWN"}:
        missing.append({"key": "days", "label": "时间范围 · 单选", "options": ["近 3 个完整日", "近 7 个完整日", "近 14 个完整日", "近 30 个完整日"], "default": "近 7 个完整日"})
    if dual_media and not slots.get("channel_confirmed"):
        missing.insert(0, {"key": "media", "label": "渠道 · 单选", "options": ["腾讯", "抖音", "两者分别看"], "default": "两者分别看"})
    elif slots.get("media") is None and not dual_media:
        missing.append({"key": "media", "label": "看哪个渠道？", "options": ["腾讯", "抖音"]})
    return {"questions": missing, "comparison": "前一等长周期", "maturity": "T0–T7 离线终值", "source_as_of": str(data.AS_OF)}


def _apply_clarification(slots: dict, answers: dict, dual_media: bool = False):
    if "media" in answers:
        slots["media"] = answers["media"] if answers["media"] in {"腾讯", "抖音"} else None
        if dual_media and answers["media"] in {"腾讯", "抖音", "两者分别看"}:
            slots["channel_confirmed"] = True
    if "days" in answers:
        m = re.search(r"\d+", str(answers["days"]))
        parsed = int(m.group()) if m else None
        slots["days"] = parsed if parsed is not None and 1 <= parsed <= 30 else None
    if "metric" in answers:
        val = str(answers["metric"])
        slots["intent"] = "GENERAL_DIAGNOSIS" if "综合诊断" in val else "COST_ANOMALY" if "效率" in val or "成本" in val or "CPS" in val else "SCALE_ANOMALY" if "消耗" in val or "量级" in val else "STRATEGY_REVIEW" if "策略" in val else "TRAFFIC_VALUE" if "质量" in val or "价值" in val else "UNKNOWN"
        slots["metric"] = val
    slots["needs_clarification"] = bool(_clarification(slots, dual_media)["questions"])


def _analyze_dual_channel(session_id: str, task_id: str, query: str, slots: dict,
                          relation_name: str, task_version: int, model_status: str,
                          on_progress: Callable[[list], None] | None,
                          cancelled: Callable[[], bool] | None,
                          replace_existing_answer: bool = False) -> dict:
    """Resolve one user question using two channel calculations in the same task."""
    if slots["intent"] not in {"COST_ANOMALY", "SCALE_ANOMALY", "TRAFFIC_VALUE"}:
        raise ValueError("跨渠道问题需要先选择成本、规模或质量指标")
    prior_task = store.task_by_id(task_id) if replace_existing_answer else None
    if replace_existing_answer and (not prior_task or prior_task["session_id"] != session_id or prior_task["status"] not in {"blocked", "completed"} or prior_task["state"].get("query") != query):
        raise ValueError("只能重新分析本会话中同一问题的已保存任务")
    channels = ("腾讯", "抖音")
    specs = {media: team.task_spec(task_id, query, dict(slots, media=media), relation_name, task_version)
             for media in channels}
    spec = dict(specs["腾讯"], channels=list(channels))
    plan = team.initial_plan(spec)
    scope = {"channels": spec["channels"], "analysis_period": spec["analysis_period"],
             "baseline": spec["baseline"], "source_as_of": spec["source_as_of"]}
    progress: list[dict] = []

    def emit(name: str, status: str, detail: str = ""):
        step = {"name": name, "status": status, "detail": detail, "scope": scope}
        previous_index = next((i for i, item in enumerate(progress) if item["name"] == name), None)
        if previous_index is None:
            progress.append(step)
        else:
            progress[previous_index] = step
        if on_progress:
            on_progress(progress.copy())

    def check_cancel():
        if cancelled and cancelled():
            raise InterruptedError("用户停止了本次分析")

    def blocked(error: str, handoffs: dict) -> dict:
        store.add_error(task_id, slots["intent"], "dual_channel", "Blocking", error)
        state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": True,
                 "plan": plan, "handoffs": handoffs, "progress": progress, "error": error}
        check_cancel()
        store.save_task(task_id, session_id, slots["intent"], "blocked", state)
        if replace_existing_answer:
            store.replace_task_assistant_message(task_id, error)
        else:
            store.add_message(session_id, task_id, "assistant", error)
        return {"type": "blocked", "task_id": task_id, "relation": relation_name,
                "status": "blocked", "answer": error, "result": None, "progress": progress}

    check_cancel()
    emit("确认口径与数据", "completed", "同一问题，分别核对腾讯与抖音")
    handoffs = {"task_spec": spec, "channels": {}}
    for media in channels:
        check_cancel()
        channel_spec = specs[media]
        need = team.initial_need(channel_spec)
        data_result = team.semantic_query(channel_spec, need)
        handoffs["channels"][media] = {"task_spec": channel_spec, "data_need": need, "data_result": data_result}
        if data_result["status"] != "READY":
            team.advance(plan, "query", "BLOCKED", [data_result["object_id"]])
            emit("计算指标与对照", "blocked", f"{media}基础数据不完整")
            return blocked(f"{media}基础数据缺失、不完整或主键异常，无法完成双渠道分析", handoffs)
    team.advance(plan, "query", "COMPLETED", [handoffs["channels"][m]["data_result"]["object_id"] for m in channels])
    emit("计算指标与对照", "running", "分别计算两个渠道")
    for media in channels:
        check_cancel()
        channel = handoffs["channels"][media]
        try:
            analyzed = _run_graph({"intent": slots["intent"], "slots": dict(slots, media=media), "progress": []}, cancelled=cancelled)
        except (Blocked, ValueError, KeyError, ZeroDivisionError) as exc:
            team.advance(plan, "analyze", "BLOCKED")
            emit("计算指标与对照", "blocked", f"{media}：{exc}")
            return blocked(f"{media}分析失败：{exc}", handoffs)
        if analyzed.get("error"):
            team.advance(plan, "analyze", "BLOCKED")
            emit("计算指标与对照", "blocked", f"{media}：{analyzed['error']}")
            return blocked(f"{media}分析失败：{analyzed['error']}", handoffs)
        channel["result"] = analyzed["result"]
        channel["findings"] = team.findings(channel["task_spec"], channel["data_result"], channel["result"])
    team.advance(plan, "analyze", "COMPLETED", [f["object_id"] for m in channels for f in handoffs["channels"][m]["findings"]])
    emit("计算指标与对照", "completed", "腾讯、抖音已计算")
    emit("核对证据边界", "running", "逐渠道复算并校验")
    for media in channels:
        check_cancel()
        channel = handoffs["channels"][media]
        verification = team.verify(channel["task_spec"], channel["data_result"], channel["findings"], channel["result"])
        channel["verification"] = verification
        if verification["status"] == "BLOCKED":
            team.advance(plan, "verify", "BLOCKED", [verification["object_id"]])
            emit("核对证据边界", "blocked", f"{media}复核失败")
            return blocked(f"{media}结论校验未通过：{'；'.join(verification['issues'])}", handoffs)
        report = team.report_spec(channel["task_spec"], channel["data_result"], verification, channel["result"])
        answer = team.compose_answer(channel["result"], channel["findings"], verification)
        final_check = team.verify_report(channel["task_spec"], report, verification, channel["findings"], channel["result"], answer)
        channel.update(report_spec=report, report_verification=final_check)
        if final_check["status"] == "BLOCKED":
            team.advance(plan, "verify", "BLOCKED", [final_check["object_id"]])
            emit("核对证据边界", "blocked", f"{media}报告复核失败")
            return blocked(f"{media}报告校验未通过：{'；'.join(final_check['issues'])}", handoffs)
    team.advance(plan, "verify", "COMPLETED", [handoffs["channels"][m]["verification"]["object_id"] for m in channels])
    emit("核对证据边界", "completed", "两个渠道均通过复核")

    results = {media: handoffs["channels"][media]["result"] for media in channels}
    facts = [results[m]["facts"][0] for m in channels]
    if slots["intent"] == "COST_ANOMALY":
        facts += [f"{m}：{results[m]['facts'][2]}" for m in channels]
    elif slots["intent"] == "SCALE_ANOMALY":
        facts += [f"{m}：{results[m]['facts'][1]}" for m in channels]
    facts += [f"{m}：{fact}" for m in channels for index, fact in enumerate(results[m]["facts"][1:], start=1)
              if not (slots["intent"] == "COST_ANOMALY" and index == 2 or slots["intent"] == "SCALE_ANOMALY" and index == 1)]
    claims = [dict(claim, text=f"{m}：{claim['text']}") for m in channels for claim in results[m]["claims"]]
    actions = [dict(action, action=f"{m}：{action['action']}") for m in channels for action in results[m]["actions"]]
    charts = [dict(chart, title=f"{m} · {chart['title']}") for m in channels for chart in results[m]["charts"][:2]]
    limitations = list(dict.fromkeys(text for m in channels for text in results[m]["reliability"]["limitations"]))
    evidence = [item for m in channels for item in results[m]["evidence"]]
    level = "低" if any(results[m]["reliability"]["level"] == "低" for m in channels) else "中" if any(results[m]["reliability"]["level"] == "中" for m in channels) else "高"
    name = {"COST_ANOMALY": "CPS", "SCALE_ANOMALY": "消耗", "TRAFFIC_VALUE": "流量价值"}[slots["intent"]]
    result = {"intent": slots["intent"], "title": f"腾讯与抖音近 {slots['days']} 天{name}分析",
              "facts": facts, "claims": claims, "actions": actions, "charts": charts,
              "next_questions": [f"腾讯的{name}变化主要集中在哪些RTA策略？", f"抖音的{name}变化主要集中在哪些账户组？"],
              "evidence": evidence, "reliability": {"level": level,
              "dimensions": {f"{m}·{key}": value for m in channels for key, value in results[m]["reliability"]["dimensions"].items()},
              "limitations": limitations}, "causal_claim_allowed": False,
              "metrics": {m: results[m].get("metrics") for m in channels},
              "channel_results": results, "handoffs": handoffs,
              "verification_status": "PASS_WITH_WARNING" if any(handoffs["channels"][m]["verification"]["status"] == "PASS_WITH_WARNING" for m in channels) else "PASS",
              "model_status": model_status}
    if slots["intent"] == "COST_ANOMALY":
        conclusions = []
        for media in channels:
            channel_result = results[media]
            contributions = channel_result["contribution_absolute"]
            major = max(contributions, key=lambda key: abs(contributions[key]))
            conclusions.append(f"{channel_result['facts'][0]}，成本侧贡献 {contributions['成本侧']:+.4f}、单产侧贡献 {contributions['单产侧']:+.4f}，主要算术贡献来自{major}")
            full = next((fact for fact in channel_result["facts"] if fact.startswith("完整环节核查：")), None)
            if full:
                conclusions.append(f"{media}：{full}")
            conclusions.extend(f"{media}：{fact}" for fact in channel_result["facts"] if fact.startswith(("同步核查质量：", "质量分支：")))
        result["analysis_steps"] = reporting.analysis_steps(result)
        deeper = "；".join(f"{step['title'].split('：')[0]}：{step['finding'].rstrip('。')}" for step in result["analysis_steps"])
        answer = "；".join(conclusions) + "。方法：CPS＝授信成本÷授信单产，按两因素分解后继续检查业务环节。" + deeper + "。同期变更的因果影响仍需进一步验证。"
    elif slots["intent"] == "SCALE_ANOMALY":
        summary = []
        for media in channels:
            channel_result = results[media]
            before, after = channel_result["metrics"]["previous"]["花费"], channel_result["metrics"]["current"]["花费"]
            movement = "下滑" if after < before else "上升" if after > before else "持平"
            sentence = f"{channel_result['facts'][0]}，{movement}"
            pctr_before, pctr_after = channel_result["delivery"]["pCTR"]
            if pctr_before and pctr_after:
                sentence += f"；{media}{'RTA策略' if media == '腾讯' else '账户组'}级 pCTR {pctr_before:.4f} → {pctr_after:.4f}，只能作为候选机制"
            summary.append(sentence)
        answer = "；".join(summary) + "。两渠道变化不同，不能把同期指标变化直接当作共同原因。"
    else:
        answer = "；".join(facts[:2]) + "。两渠道的变化与候选原因分别列示，不能仅凭同期变化认定共同原因。"
    result["answer"] = answer
    reporting.display_result(result)
    result["result_id"] = hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()[:16]
    emit("形成结论与动作", "completed", "形成一份双渠道报告")
    team.advance(plan, "report", "COMPLETED", [handoffs["channels"][m]["report_verification"]["object_id"] for m in channels])
    emit("独立复核与报告一致性", "completed", result["verification_status"])
    state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": True,
             "plan": plan, "handoffs": handoffs, "progress": progress, "error": None,
             "evidence_refs": evidence, "tool_result_refs": [result["result_id"]],
             "recovered_from_error": (prior_task["state"].get("recovered_from_error") or prior_task["state"].get("error")) if prior_task else None,
             "checkpoint": {"current_goal": query, "confirmed_scope": slots, "verified_facts": facts, "pending_work": []}}
    check_cancel()
    store.save_task(task_id, session_id, slots["intent"], "completed", state, result)
    if replace_existing_answer:
        store.replace_task_assistant_message(task_id, answer)
    else:
        store.add_message(session_id, task_id, "assistant", answer)
    return {"type": "result", "task_id": task_id, "relation": relation_name,
            "status": "completed", "answer": answer, "result": result, "progress": progress}


def _split_questions(query: str) -> list[str]:
    parts = [part.strip(" 。？?，,\n") for part in re.split(r"[；;\n]+|另外[，,]", query)]
    return [part for part in parts if part]


def send_batch(session_id: str, questions: list[str], use_model: bool,
               on_progress: Callable[[list], None] | None, cancelled: Callable[[], bool] | None) -> dict:
    request_id = str(uuid.uuid4())
    plan = {"request_id": request_id, "plan_version": 1, "steps": [
        {"step_id": str(uuid.uuid4()), "question": question, "owner": "分析主控", "status": "PENDING",
         "depends_on": [], "completion_condition": "该问题获得明确处理状态", "task_id": str(uuid.uuid4())}
        for question in questions]}
    store.save_request(request_id, session_id, "running", plan)
    lock = threading.Lock()
    results = [None] * len(questions)
    events = {step["task_id"]: threading.Event() for step in plan["steps"]}
    with _subtask_lock:
        _subtask_events.update(events)
    if on_progress:
        on_progress([{"name": step["question"], "status": "pending", "task_id": step["task_id"]} for step in plan["steps"]])

    def execute(index: int, question: str):
        task_id = plan["steps"][index]["task_id"]
        with lock:
            plan["steps"][index]["status"] = "RUNNING"
            store.save_request(request_id, session_id, "running", plan)
        return send(session_id, question, use_model=use_model,
                    cancelled=lambda: events[task_id].is_set() or bool(cancelled and cancelled()),
                    force_new=True, task_id_override=task_id)

    try:
        with ThreadPoolExecutor(max_workers=min(3, len(questions))) as pool:
            futures = {pool.submit(execute, i, question): i for i, question in enumerate(questions)}
            for future in as_completed(futures):
                i = futures[future]
                if cancelled and cancelled():
                    raise InterruptedError("用户停止了本次分析")
                try:
                    results[i] = future.result()
                except InterruptedError:
                    task_id = plan["steps"][i]["task_id"]
                    if not store.task_by_id(task_id):
                        store.save_task(task_id, session_id, "UNKNOWN", "cancelled", {"query": questions[i], "progress": [], "error": "用户取消此分项"})
                    results[i] = {"type": "cancelled", "status": "cancelled", "answer": "此分项已取消", "result": None,
                                  "progress": [], "task_id": task_id}
                except Exception as exc:
                    store.add_error(request_id, "UNKNOWN", "batch", type(exc).__name__, str(exc))
                    results[i] = {"type": "blocked", "status": "failed", "answer": "此分项执行失败，请查看任务记录或重试", "result": None,
                                  "progress": [], "task_id": plan["steps"][i]["task_id"]}
                with lock:
                    plan["steps"][i]["status"] = ("WAITING_USER" if results[i]["type"] == "clarification" else
                                                    "COMPLETED" if results[i]["type"] == "result" else
                                                    "CANCELLED" if results[i]["type"] == "cancelled" else "FAILED")
                    store.save_request(request_id, session_id, "running", plan)
                    if on_progress:
                        on_progress([{"name": step["question"], "status": step["status"].lower(), "task_id": step["task_id"]} for step in plan["steps"]])
    except InterruptedError:
        for step in plan["steps"]:
            if step["status"] in {"PENDING", "RUNNING", "WAITING_USER"}:
                step["status"] = "CANCELLED"
        store.save_request(request_id, session_id, "cancelled", plan)
        raise
    finally:
        with _subtask_lock:
            for task_id in events:
                _subtask_events.pop(task_id, None)
    status = "completed" if all(item["type"] == "result" for item in results) else "partial"
    store.save_request(request_id, session_id, status, plan)
    return {"type": "batch", "request_id": request_id, "status": status, "plan": plan,
            "items": results, "progress": [{"name": step["question"], "status": step["status"].lower(), "task_id": step["task_id"]} for step in plan["steps"]]}


def cancel_subtask(task_id: str) -> dict:
    with _subtask_lock:
        event = _subtask_events.get(task_id)
        if event:
            event.set()
            return {"status": "stopping"}
    task = store.task_by_id(task_id)
    if task and task["status"] == "clarifying":
        store.save_task(task_id, task["session_id"], task["intent"], "cancelled", task["state"])
        store.update_request_task(task["session_id"], task_id, "CANCELLED")
        return {"status": "cancelled"}
    if not task:
        raise ValueError("Task not found")
    return {"status": task["status"]}


def send(session_id: str, query: str, answers: dict | None = None, use_model: bool = True,
         on_progress: Callable[[list], None] | None = None, cancelled: Callable[[], bool] | None = None,
         force_new: bool = False, task_id_override: str | None = None,
         target_task_id: str | None = None) -> dict:
    if not store.session_exists(session_id):
        raise ValueError("Session not found")
    query = query.strip()
    if not query and not answers:
        raise ValueError("Empty message")
    questions = _split_questions(query) if not answers and not force_new else []
    if len(questions) > 1:
        return send_batch(session_id, questions, use_model, on_progress, cancelled)
    target_task_id = answers.get("__task_id") if answers else target_task_id
    previous = None if force_new else (store.task_by_id(target_task_id) if target_task_id else store.latest_task(session_id))
    if target_task_id and (not previous or previous["session_id"] != session_id):
        raise ValueError("Clarification task not found in session")
    previous_slots = previous["state"].get("slots") if previous else None
    dual_media = bool(previous["state"].get("dual_media")) if answers and previous else asks_both_channels(query) or investigation.goal(query) == "opportunity" and not any(x in query for x in ("腾讯", "抖音", "巨量"))
    broad_question = bool(broad_performance_query(query)) if not answers else False
    investigation_goal = investigation.goal(query) if not answers else None
    def check_cancel():
        if cancelled and cancelled():
            raise InterruptedError("用户停止了本次分析")
    check_cancel()
    model = LLMClient() if use_model else None
    model_status = "disabled" if not model or not model.client else "active"
    if investigation_goal or answers and previous_slots and previous_slots.get("intent") == "INVESTIGATION":
        model_status = "deterministic"
    if answers:
        if not previous or previous["status"] != "clarifying":
            raise ValueError("No pending clarification")
        task_id = previous["id"]
        slots = dict(previous_slots)
        _apply_clarification(slots, answers, dual_media)
        if dual_media and slots.get("media") in {"腾讯", "抖音"}:
            dual_media = False
        relation_name = previous["state"].get("relation", "CONTINUE")
        query = previous["state"].get("query", "")
    else:
        relation_name = relation(query, previous_slots)
        if target_task_id:
            relation_name = "MODIFY_SCOPE"
        if model and model.client and previous_slots and not investigation_goal:
            remote = model.structured("turn_relation", RELATION_SCHEMA, "判断新消息与当前业务任务的关系。只输出枚举。", {"task": previous_slots, "query": query})
            if remote and remote.get("relation") in {"CONTINUE", "MODIFY_SCOPE", "SIDE_QUERY", "NEW_TASK"}:
                relation_name = remote["relation"]
            else:
                model_status = "fallback"
        if broad_question or dual_media or investigation_goal:
            relation_name = "NEW_TASK"
        check_cancel()
        if relation_name == "SIDE_QUERY":
            definition = "CPS = 业务口径花费 ÷ T0–T7 首借金额。" if "CPS" in query else "安全过件率 = 安全授信数 ÷ 申完数。" if "安全过件率" in query else "授信单产 = 首借金额 ÷ 授信数。"
            check_cancel()
            store.add_message(session_id, previous["id"], "user", query)
            store.add_message(session_id, previous["id"], "assistant", definition)
            return {"relation": relation_name, "type": "side_query", "answer": definition, "task_id": previous["id"]}
        inherited = previous_slots if relation_name in {"CONTINUE", "MODIFY_SCOPE"} else None
        slots = rule_slots(query, inherited)
        rule_media = slots.get("media")
        rule_days = slots.get("days")
        rule_intent = slots.get("intent")
        if model and model.client and not investigation_goal:
            remote = model.structured("intent_slots", SLOT_SCHEMA, "提取信贷获客分析意图与渠道、时间、指标。media 只能为腾讯、抖音或 null；RTA 是投放方式，不是渠道。已有明确条件不能遗失。", {"query": query, "previous": inherited})
            if remote and remote.get("intent") in {"GENERAL_DIAGNOSIS", "COST_ANOMALY", "SCALE_ANOMALY", "TRAFFIC_VALUE", "STRATEGY_REVIEW", "UNKNOWN"}:
                for key in ("intent", "media", "days", "metric"):
                    if remote.get(key) is not None:
                        slots[key] = remote[key]
            else:
                model_status = "fallback"
        # Explicitly broad or two-channel questions cannot become a strategy
        # review through a model guess or inherited task context.
        if broad_question:
            if model and model.client and (slots.get("intent") != "GENERAL_DIAGNOSIS" or slots.get("media") != rule_media or slots.get("days") != rule_days):
                model_status = "fallback"
            slots.update(intent="GENERAL_DIAGNOSIS", media=rule_media, days=rule_days,
                         metric="综合诊断", needs_clarification=rule_days is None)
        if dual_media:
            if rule_intent != "UNKNOWN" and (slots.get("intent") != rule_intent or slots.get("days") != rule_days):
                model_status = "fallback"
                slots["intent"] = rule_intent
                slots["days"] = rule_days
            slots["media"] = None
        if investigation_goal:
            slots["intent"] = "INVESTIGATION"
            slots["analysis_goal"] = investigation_goal
            slots["metric"] = investigation_goal
            slots["media"] = rule_media
            slots["days"] = rule_days
        check_cancel()
        aliases = {"巨量": "抖音", "巨量引擎": "抖音", "Douyin": "抖音", "Tencent": "腾讯"}
        slots["media"] = aliases.get(slots.get("media"), slots.get("media"))
        if slots.get("media") not in {"腾讯", "抖音", None}:
            slots["media"] = rule_media if rule_media in {"腾讯", "抖音"} else None
            model_status = "fallback"
        if slots.get("days") is not None and (not isinstance(slots["days"], int) or isinstance(slots["days"], bool) or not 1 <= slots["days"] <= 30):
            slots["days"] = None
            model_status = "fallback"
        if relation_name == "MODIFY_SCOPE" and previous_slots:
            slots["intent"] = previous_slots["intent"]
        if slots["intent"] == "TRAFFIC_VALUE" and slots.get("days") is None:
            slots["days"] = 30
        if slots["intent"] == "STRATEGY_REVIEW" and slots.get("media") is None:
            slots["media"] = "腾讯"
        task_id = task_id_override or str(uuid.uuid4())
        store.add_message(session_id, task_id, "user", query)
    clarification = _clarification(slots, dual_media)
    clarification_rounds = previous["state"].get("clarification_rounds", 0) + 1 if answers and previous else 0
    task_version = (previous["state"].get("pending_version", 1) if answers else
                    previous["state"].get("handoffs", {}).get("task_spec", {}).get("task_version", 1) + 1
                    if previous and relation_name == "MODIFY_SCOPE" else 1)
    supersedes_task_id = (previous["state"].get("supersedes_task_id") if answers else
                          previous["id"] if previous and relation_name == "MODIFY_SCOPE" else None)
    if clarification["questions"]:
        if clarification_rounds >= 2:
            error = "两轮澄清后仍缺少必要条件；可重新提问，例如“腾讯近 7 天 CPS 为什么上涨？”"
            state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": dual_media,
                     "clarification_rounds": clarification_rounds, "progress": [], "error": error}
            store.save_task(task_id, session_id, slots["intent"], "blocked", state)
            store.add_message(session_id, task_id, "assistant", error)
            if target_task_id:
                store.update_request_task(session_id, task_id, "FAILED")
            return {"type": "blocked", "task_id": task_id, "relation": relation_name,
                    "status": "blocked", "answer": error, "result": None, "progress": []}
        state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": dual_media, "pending_version": task_version,
                 "supersedes_task_id": supersedes_task_id, "clarification_rounds": clarification_rounds,
                 "progress": [], "evidence_refs": [], "tool_result_refs": [], "error": None}
        check_cancel()
        store.save_task(task_id, session_id, slots["intent"], "clarifying", state)
        if target_task_id and not answers and task_id != target_task_id:
            store.replace_request_task(session_id, target_task_id, task_id, "WAITING_USER")
        return {"type": "clarification", "task_id": task_id, "relation": relation_name, "clarification": clarification}
    if slots.get("end") and slots["end"] > str(data.AS_OF):
        error = f"所问日期 {slots['end']} 尚无数据；当前数据最新完整日期为 {data.AS_OF}。请指定已有日期或改问截至 {data.AS_OF} 的历史复盘。"
        state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": dual_media, "progress": [], "error": error}
        store.save_task(task_id, session_id, slots["intent"], "blocked", state)
        store.add_message(session_id, task_id, "assistant", error)
        return {"type": "blocked", "task_id": task_id, "relation": relation_name, "status": "blocked", "answer": error, "result": None, "progress": []}
    if slots["intent"] == "INVESTIGATION":
        try:
            result = investigation.analyze(query, slots)
            scope = result.pop("scope")
            result["handoffs"] = {"task_spec": {"object_type": "TaskSpec", "task_id": task_id, "task_version": task_version,
                "original_query": query, "channels": scope["channels"], "analysis_period": scope["analysis_period"],
                "baseline": scope["baseline"], "source_as_of": scope["source_as_of"], "target_metrics": [slots["analysis_goal"]]}}
            result["model_status"] = model_status
            result["result_id"] = hashlib.sha256(json.dumps(result["facts"], ensure_ascii=False).encode()).hexdigest()[:16]
            progress = [{"name": "确认口径与数据", "status": "completed", "detail": "核对日期、渠道和数据覆盖"},
                        {"name": "计算指标与对照", "status": "completed", "detail": "按问题选择分析方法并验证前提"},
                        {"name": "核对证据边界", "status": "completed", "detail": "区分可判定和待验证"},
                        {"name": "形成结论与动作", "status": "completed", "detail": result["title"]}]
            state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": dual_media, "progress": progress,
                     "handoffs": result["handoffs"], "evidence_refs": result["evidence"], "tool_result_refs": [result["result_id"]],
                     "checkpoint": {"current_goal": query, "confirmed_scope": slots, "verified_facts": result["facts"], "pending_work": []}}
            store.save_task(task_id, session_id, slots["intent"], "completed", state, result)
            if target_task_id:
                store.update_request_task(session_id, task_id, "COMPLETED")
            store.add_message(session_id, task_id, "assistant", result["answer"])
            return {"type": "result", "task_id": task_id, "relation": relation_name, "status": "completed", "answer": result["answer"], "result": result, "progress": progress}
        except (Blocked, ValueError, KeyError, ZeroDivisionError) as exc:
            error = str(exc)
            state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": dual_media, "progress": [], "error": error}
            store.save_task(task_id, session_id, slots["intent"], "blocked", state)
            store.add_message(session_id, task_id, "assistant", error)
            return {"type": "blocked", "task_id": task_id, "relation": relation_name, "status": "blocked", "answer": error, "result": None, "progress": []}
    if slots["intent"] == "GENERAL_DIAGNOSIS":
        try:
            return diagnostic.analyze(session_id, task_id, query, slots, relation_name,
                                      task_version, model_status, on_progress, cancelled, model)
        except (Blocked, ValueError, KeyError, ZeroDivisionError) as exc:
            error = str(exc)
            state = {"query": query, "slots": slots, "relation": relation_name,
                     "dual_media": dual_media, "progress": [], "error": error}
            store.save_task(task_id, session_id, slots["intent"], "blocked", state)
            store.add_error(task_id, slots["intent"], "diagnostic", "Blocking", error)
            store.add_message(session_id, task_id, "assistant", error)
            return {"type": "blocked", "task_id": task_id, "relation": relation_name,
                    "status": "blocked", "answer": error, "result": None, "progress": []}
    if dual_media:
        return _analyze_dual_channel(session_id, task_id, query, slots, relation_name,
                                     task_version, model_status, on_progress, cancelled)
    spec = team.task_spec(task_id, query, slots, relation_name, task_version)
    if on_progress:
        raw_progress = on_progress
        scope = {"channels": spec["channels"], "analysis_period": spec["analysis_period"],
                 "baseline": spec["baseline"], "source_as_of": spec["source_as_of"]}
        def on_progress(steps):
            raw_progress([dict(step, scope=scope) for step in steps])
    plan = team.initial_plan(spec)
    need = team.initial_need(spec)
    followup = followups.answer(previous, query) if previous and relation_name == "CONTINUE" and not answers else None
    prior_data = previous["state"].get("handoffs", {}).get("data_result") if previous and followup else None
    if prior_data and previous["state"].get("slots") == slots and prior_data["status"] == "READY":
        data_result = deepcopy(prior_data)
        data_result.update(object_id=str(uuid.uuid4()), task_id=task_id, task_version=task_version,
                           input_refs=[need["object_id"], prior_data["object_id"]], generated_at=store.now(),
                           reused_from=prior_data["object_id"])
    else:
        data_result = team.semantic_query(spec, need)
    if data_result["status"] != "READY":
        team.advance(plan, "query", "BLOCKED", [data_result["object_id"]])
        error = "请求范围内基础数据缺失、不完整或主键异常，无法继续分析"
        state = {"query": query, "slots": slots, "relation": relation_name, "plan": plan, "handoffs": {"task_spec": spec, "data_need": need, "data_result": data_result}, "progress": [{"name": "核对数据", "status": "blocked", "detail": error}], "error": error}
        if on_progress:
            on_progress(state["progress"])
        store.save_task(task_id, session_id, slots["intent"], "blocked", state)
        store.add_message(session_id, task_id, "assistant", error)
        store.add_error(task_id, slots["intent"], "semantic_query", "DataUnavailable", error)
        return {"type": "blocked", "task_id": task_id, "relation": relation_name, "status": "blocked", "answer": error, "result": None, "progress": state["progress"]}
    team.advance(plan, "query", "COMPLETED", [data_result["object_id"]])
    if followup:
        output: GraphState = {"result": followup, "progress": [{"name": "读取前次分析证据", "status": "completed", "detail": previous["id"]}, {"name": "完成定向下钻", "status": "completed", "detail": followup["title"]}]}
        if on_progress:
            on_progress(output["progress"])
    else:
        initial: GraphState = {"intent": slots["intent"], "slots": slots, "progress": []}
        output = _run_graph(initial, on_progress, cancelled)
    result = output.get("result")
    error = output.get("error")
    if error:
        team.advance(plan, "analyze", "BLOCKED")
        store.add_error(task_id, slots["intent"], "analyzer", "Blocking", error)
        status = "blocked"
        answer = error
    else:
        found = team.findings(spec, data_result, result)
        team.advance(plan, "analyze", "COMPLETED", [finding["object_id"] for finding in found])
        verification = team.verify(spec, data_result, found, result)
        if verification["status"] == "BLOCKED":
            team.advance(plan, "verify", "BLOCKED", [verification["object_id"]])
            error = "结论校验未通过：" + "；".join(verification["issues"])
            status, answer, result = "blocked", error, None
            store.add_error(task_id, slots["intent"], "verification", "Blocking", error)
        else:
            team.advance(plan, "verify", "COMPLETED", [verification["object_id"]])
            status = "completed"
            report = team.report_spec(spec, data_result, verification, result)
            answer = team.compose_answer(result, found, verification)
            final_check = team.verify_report(spec, report, verification, found, result, answer)
            if final_check["status"] == "BLOCKED":
                team.advance(plan, "report", "BLOCKED", [final_check["object_id"]])
                error = "最终报告校验未通过：" + "；".join(final_check["issues"])
                status, answer, result = "blocked", error, None
                store.add_error(task_id, slots["intent"], "report_verification", "Blocking", error)
            else:
                team.advance(plan, "report", "COMPLETED", [report["object_id"], final_check["object_id"]])
                result["answer"] = answer
                result["handoffs"] = {"task_spec": spec, "data_need": need, "data_result": data_result,
                                       "findings": found, "verification": verification, "report_spec": report,
                                       "report_verification": final_check}
                result["verification_status"] = verification["status"]
                result["model_status"] = model_status
                output["progress"].append({"name": "独立复核与报告一致性", "status": "completed", "detail": verification["status"]})
                if on_progress:
                    on_progress(output["progress"])
    if result:
        reporting.display_result(result)
        result["result_id"] = hashlib.sha256(json.dumps(result["facts"], ensure_ascii=False).encode()).hexdigest()[:16]
    state = {"query": query, "slots": slots, "relation": relation_name, "supersedes_task_id": supersedes_task_id, "plan": plan, "progress": output["progress"],
             "evidence_refs": result.get("evidence", []) if result else [], "tool_result_refs": [result["result_id"]] if result else [], "error": error,
             "handoffs": result.get("handoffs") if result else {"task_spec": spec, "data_need": need, "data_result": data_result},
             "checkpoint": {"current_goal": query, "confirmed_scope": slots, "verified_facts": result.get("facts", []) if result else [], "pending_work": []}}
    check_cancel()
    store.save_task(task_id, session_id, slots["intent"], status, state, result)
    if answers and target_task_id:
        store.update_request_task(session_id, task_id, "COMPLETED" if status == "completed" else "FAILED")
    elif target_task_id and previous and task_id != target_task_id:
        store.replace_request_task(session_id, target_task_id, task_id, "COMPLETED" if status == "completed" else "FAILED")
    store.add_message(session_id, task_id, "assistant", answer)
    return {"type": "result" if result else "blocked", "task_id": task_id, "relation": relation_name, "status": status, "answer": answer, "result": result, "progress": output["progress"]}
