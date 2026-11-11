"""Evidence driven diagnosis for an open performance question.

The initial comparison covers every requested channel and all three business
dimensions. Subsequent checks are selected from observed changes, not from a
single intent guessed before data is read.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import date, timedelta
from typing import Callable

from . import analyzers, data, investigation, reporting, sql_adapter, store, team


def _movement(before: float | None, after: float | None) -> float | None:
    return after / before - 1 if before not in (None, 0) and after is not None else None


def _fmt_change(before: float | None, after: float | None) -> str:
    delta = _movement(before, after)
    return f"{delta:+.1%}" if delta is not None else "无法比较"


def _quality_check(media: str, start: date, end: date, days: int) -> dict:
    """Check subgroup coverage before naming a subgroup as a quality clue."""
    left, right = start - timedelta(days=days), start - timedelta(days=1)
    dimension = "RTA策略" if media == "腾讯" else "账号"
    sql, records = sql_adapter.execute("segment", str(left), str(end), media)
    before = [r for r in records if r["date"] <= str(right) and r["segment_type"] == dimension]
    after = [r for r in records if r["date"] >= str(start) and r["segment_type"] == dimension]
    market_before = data.aggregate(data.rows("media", left, right, media=media))
    market_after = data.aggregate(data.rows("media", start, end, media=media))
    covered = all(abs(data.aggregate(group)[key] - market[key]) <= max(1, market[key] * .005)
                  for group, market in ((before, market_before), (after, market_after))
                  for key in ("申完数", "安全授信数"))
    if not covered or not before or not after:
        return {"status": "PARTIAL", "finding": f"{media}{dimension}分组与大盘未对齐，无法定位质量变化到分组",
                "query": {"table": "segment", "sql": sql, "parameters": [str(left), str(end), media], "rows": len(records)}}
    names = {row["segment_name"] for row in before + after}
    comparisons = []
    for name in names:
        a = data.aggregate([row for row in before if row["segment_name"] == name])
        b = data.aggregate([row for row in after if row["segment_name"] == name])
        if not a["申完数"] or not b["申完数"]:
            continue
        rate_a, rate_b = a["安全授信数"] / a["申完数"], b["安全授信数"] / b["申完数"]
        comparisons.append((rate_b - rate_a, name, rate_a, rate_b, a["申完数"], b["申完数"]))
    if not comparisons:
        return {"status": "PARTIAL", "finding": f"{media}{dimension}分组无可比较的申完样本",
                "query": {"table": "segment", "sql": sql, "parameters": [str(left), str(end), media], "rows": len(records)}}
    diff, name, rate_a, rate_b, count_a, count_b = min(comparisons)
    return {"status": "READY", "finding":
            f"{media}{dimension}中 {name} 安全过件率 {rate_a:.1%} → {rate_b:.1%}（{diff*100:+.1f} pp；申完 {count_a:,.0f} → {count_b:,.0f}）；这是分组变化线索，不是总体下降的因果归因",
            "evidence": data.source("segment", before + after, left, end, {"media": media, "segment_type": dimension}),
            "query": {"table": "segment", "sql": sql, "parameters": [str(left), str(end), media], "rows": len(records)}}


def _verify_overview(overview: dict, channels: list[str], left: date, right: date,
                     start: date, end: date) -> list[str]:
    """Recompute the published comparison independently from original rows."""
    issues = []
    for media in channels:
        for label, first, last in (("previous", left, right), ("current", start, end)):
            expected = data.metrics(data.aggregate(data.rows("media", first, last, media=media)))
            actual = overview[media][label]
            for key in ("花费", "首借金额", "授信数", "CPS", "安全过件率", "授信单产"):
                a, b = actual[key], expected[key]
                if a is None or b is None or not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8):
                    issues.append(f"{media} {label} {key} 与原始数据复算不符")
    return issues


def analyze(session_id: str, task_id: str, query: str, slots: dict, relation_name: str,
            version: int, model_status: str, on_progress: Callable[[list], None] | None,
            cancelled: Callable[[], bool] | None, planner=None) -> dict:
    days = slots["days"]
    end = date.fromisoformat(slots["end"]) if slots.get("end") else data.AS_OF
    left, right, start, end = data.windows(days, end)
    channels = [slots["media"]] if slots.get("media") in {"腾讯", "抖音"} else ["腾讯", "抖音"]
    scope = {"channels": channels, "analysis_period": {"start": str(start), "end": str(end)},
             "baseline": {"start": str(left), "end": str(right)}, "source_as_of": str(data.AS_OF)}
    progress, decisions, checks = [], [], {}

    def emit(name: str, status: str, detail: str):
        item = {"name": name, "status": status, "detail": detail, "scope": scope}
        prior = next((i for i, step in enumerate(progress) if step["name"] == name), None)
        if prior is None:
            progress.append(item)
        else:
            progress[prior] = item
        if on_progress:
            on_progress(progress.copy())

    def check_cancel():
        if cancelled and cancelled():
            raise InterruptedError("用户停止了本次分析")

    check_cancel()
    emit("核对综合目标与数据", "running", "先检查规模、成本和质量，再按变化选择下钻")
    overview, sources, queries = {}, [], []
    for media in channels:
        sql, records = sql_adapter.execute("media", str(left), str(end), media)
        queries.append({"table": "media", "sql": sql, "parameters": [str(left), str(end), media], "rows": len(records)})
        expected = {str(left + timedelta(days=i)) for i in range(days * 2)}
        observed = [row["date"] for row in records]
        if len(observed) != days * 2 or set(observed) != expected:
            raise analyzers.Blocked(f"{media}媒体日表日期缺失或主键重复，无法进行综合比较")
        previous = [row for row in records if row["date"] <= str(right)]
        current = [row for row in records if row["date"] >= str(start)]
        before, after = data.metrics(data.aggregate(previous)), data.metrics(data.aggregate(current))
        overview[media] = {"previous": before, "current": after}
        sources.append(data.source("media", records, left, end, {"media": media}))
    issues = _verify_overview(overview, channels, left, right, start, end)
    if issues:
        raise analyzers.Blocked("综合概览复核失败：" + "；".join(issues))
    emit("核对综合目标与数据", "completed", "两期、逐渠道日期和主键检查通过")

    facts, claims, limitations, actions = [], [], [], []
    for media in channels:
        before, after = overview[media]["previous"], overview[media]["current"]
        facts.append(f"{media}日均消耗 {before['花费']/days:,.0f} → {after['花费']/days:,.0f} 元（{_fmt_change(before['花费'], after['花费'])}）；"
                     f"CPS {before['CPS']:.4f} → {after['CPS']:.4f}（{_fmt_change(before['CPS'], after['CPS'])}）；"
                     f"安全过件率 {before['安全过件率']:.1%} → {after['安全过件率']:.1%}（{(after['安全过件率']-before['安全过件率'])*100:+.1f} pp）")
        facts.append(f"{media}首借金额 {before['首借金额']:,.0f} → {after['首借金额']:,.0f} 元（{_fmt_change(before['首借金额'], after['首借金额'])}）；"
                     f"授信单产 {before['授信单产']:,.0f} → {after['授信单产']:,.0f} 元（{_fmt_change(before['授信单产'], after['授信单产'])}）")
    if len(channels) == 2 and any(overview[m]["current"]["花费"] >= overview[m]["previous"]["花费"] for m in channels):
        claims.append({"status": "不支持", "text": "按消耗口径判断两个渠道都在掉量", "confidence": "高"})
    if not data.maturity(end):
        limitations.append(f"{end} 首登 cohort 的 T7 在 {data.AS_OF} 尚未成熟；转化指标是离线构造终值，不能当作当日已观测结果。")
    limitations.append("前后窗变化与同期变更只提供线索；缺少对照时不能判定因果。")

    # These cutoffs only choose which check to run. They are not anomaly or
    # intervention thresholds; all overview numbers remain visible.
    candidates = []
    for media in channels:
        before, after = overview[media]["previous"], overview[media]["current"]
        if (_movement(before["CPS"], after["CPS"]) or 0) >= .05:
            candidates.append((media, "COST_ANOMALY", "CPS 上升，检查成本与单产贡献"))
        if (_movement(before["花费"], after["花费"]) or 0) <= -.05:
            candidates.append((media, "SCALE_ANOMALY", "消耗下降，检查拿量链路"))
        applications = sum(r["申完数"] for r in data.rows("media", start, end, media=media))
        if applications >= 30 and (after["安全过件率"] - before["安全过件率"] <= -.01 or after["安全过件率"] < .17):
            candidates.append((media, "QUALITY_CHECK", "安全过件率下降或低于 17%，检查分组与渠道差异"))
    decisions.append({"round": 0, "observation": facts.copy(), "next": [f"{m}·{kind}" for m, kind, _ in candidates],
                      "reason": "依据实际指标变化选择检查；5% 和 1 pp 仅为下钻触发值"})
    emit("选择下一步检查", "completed", "；".join(reason for _, _, reason in candidates) if candidates else "未触发额外下钻；保留全部概览")

    remaining = candidates.copy()
    index = 0
    while remaining:
        check_cancel()
        selected = remaining[0]
        planner_source = "规则降级"
        if planner and planner.client:
            options = [f"{media}·{kind}" for media, kind, _ in remaining]
            proposal = planner.structured("diagnostic_next",
                {"type": "object", "properties": {"action": {"type": "string", "enum": options},
                 "reason": {"type": "string"}}, "required": ["action", "reason"],
                 "additionalProperties": False},
                "从已由数据变化触发的候选检查中选择下一项。只决定顺序，不推断原因；不能省略待检查项。",
                {"question": query, "scope": scope, "overview": facts[:2 * len(channels)],
                 "completed": [{"check": key, "status": value["status"]} for key, value in checks.items()],
                 "candidates": options})
            if proposal and proposal.get("action") in options:
                selected = remaining[options.index(proposal["action"])]
                planner_source = "模型选择"
        remaining.remove(selected)
        media, kind, reason = selected
        index += 1
        label = f"{media}·{kind}"
        display = f"{media}·" + {"COST_ANOMALY": "成本与单产", "SCALE_ANOMALY": "拿量链路", "QUALITY_CHECK": "质量分组"}[kind]
        emit(display, "running", reason)
        if kind == "QUALITY_CHECK":
            detail = _quality_check(media, start, end, days)
            raw = data.rows("media", start, end, media=media)
            branch = investigation.quality_branch(media, overview[media]["current"], raw)
            detail["quality_branch"] = branch
            if media == "腾讯":
                value = branch["billing_ratio"]
                detail["finding"] += (f"；腾讯模拟计费比 {value:.3f}（{branch['billing_state']}，只作初筛线索）" if value is not None else "；腾讯计费比无有效数据")
            else:
                detail["finding"] += "；抖音不适用计费比，需核对 CPM、CTR、回传及漏斗"
            queries.append(detail["query"])
            if detail.get("evidence"):
                sources.append(detail["evidence"])
            if detail["status"] == "READY":
                facts.append(detail["finding"])
                if media == "腾讯" and branch["billing_state"] == "偏高":
                    claims.append({"status": "待验证", "text": f"{media}计费比偏高提示媒体协同风险，不能直接判定责任", "confidence": "中"})
            else:
                limitations.append(detail["finding"])
            checks[label] = detail
        else:
            subslots = dict(slots, intent=kind, media=media)
            spec = team.task_spec(task_id, query, subslots, relation_name, version)
            need = team.initial_need(spec)
            fetched = team.semantic_query(spec, need)
            if fetched["status"] != "READY":
                checks[label] = {"status": "PARTIAL", "reason": "下钻数据缺失或检查未通过", "data_result": fetched}
                limitations.append(f"{label} 下钻数据不足，未据此判断原因。")
            else:
                try:
                    detail = analyzers.run(kind, subslots)
                except (analyzers.Blocked, ValueError, KeyError, ZeroDivisionError) as exc:
                    checks[label] = {"status": "PARTIAL", "reason": str(exc), "data_result": fetched}
                    limitations.append(f"{label} 无法完成下钻：{exc}。")
                else:
                    found = team.findings(spec, fetched, detail)
                    verified = team.verify(spec, fetched, found, detail)
                    checks[label] = {"status": verified["status"], "task_spec": spec, "data_result": fetched,
                                     "finding": found, "verification": verified, "result": detail}
                    if verified["status"] == "BLOCKED":
                        limitations.append(f"{label} 下钻未通过复核：{'；'.join(verified['issues'])}")
                    else:
                        facts.extend(f"{media}：{fact}" for fact in detail["facts"] if fact in detail["facts"][:3] or fact.startswith("完整环节核查："))
                        claims.extend(dict(claim, text=f"{media}：{claim['text']}") for claim in detail["claims"])
                        sources.extend(detail["evidence"])
                        actions.extend(dict(action, action=f"{media}：{action['action']}") for action in detail["actions"])
                        limitations.extend(detail["reliability"]["limitations"])
                        if kind == "COST_ANOMALY" and detail["contribution_absolute"]["成本侧"] > 0:
                            before, after = overview[media]["previous"], overview[media]["current"]
                            quality_fell = after["安全过件率"] < before["安全过件率"]
                            already_scheduled = any(m == media and k == "QUALITY_CHECK" for m, k, _ in remaining) or f"{media}·QUALITY_CHECK" in checks
                            if quality_fell and not already_scheduled:
                                remaining.append((media, "QUALITY_CHECK", "成本拆解显示授信成本增加，且安全过件率下降；追加质量分组核查"))
        decisions.append({"round": index, "observation": checks[label]["status"], "checked": label,
                          "next": [f"{m}·{k}" for m, k, _ in remaining],
                          "reason": reason, "planner": planner_source})
        emit(display, "completed" if checks[label]["status"] not in {"PARTIAL", "BLOCKED"} else "blocked", checks[label]["status"])

    event_sql, events = sql_adapter.execute("change_log", str(left), str(end))
    queries.append({"table": "change_log", "sql": event_sql, "parameters": [str(left), str(end)], "rows": len(events)})
    for media in channels:
        count = sum(media in event["变更对象"] for event in events)
        facts.append(f"{media}对比范围内记录 {count} 条相关变更；时间吻合不构成因果证据")
    sources.append(data.source("change_log", events, left, end))

    if not actions:
        actions.append({"owner": "获客运营、数据分析", "when": "下一投放日", "action": "维持现有配置，核对所选渠道成本、规模及安全过件率的连续日变化；只对有证据的异常对象提出试调",
                        "expected": "确定是否存在持续且可定位的经营问题", "observe": "下一 3–7 个完整日，并单列成熟 cohort", "rollback": "若安全过件率或 CPS 继续恶化，停止任何新增放量"})
    partial_checks = [name for name, check in checks.items() if check["status"] in {"PARTIAL", "BLOCKED"}]
    dimensions = {"数据完整性": "通过" if not partial_checks else "部分下钻缺失", "样本充分度": "见各指标样本量",
                  "指标成熟度": "通过" if data.maturity(end) else "离线终值", "原因因果": "未证实"}
    result = {"intent": "GENERAL_DIAGNOSIS", "title": f"{'与'.join(channels)}近 {days} 天投放综合诊断",
              "facts": facts, "claims": claims, "actions": actions, "charts": [],
              "next_questions": [f"{media}的变化从哪天开始？" for media in channels], "evidence": sources,
              "reliability": {"level": "低" if partial_checks else "中", "dimensions": dimensions,
                              "limitations": list(dict.fromkeys(limitations))}, "causal_claim_allowed": False,
              "overview": overview, "investigation": decisions, "checks": checks,
              "verification_status": "PASS_WITH_WARNING" if limitations else "PASS", "model_status": model_status}
    summary = [f"{media}消耗{_fmt_change(overview[media]['previous']['花费'], overview[media]['current']['花费'])}、"
               f"CPS{_fmt_change(overview[media]['previous']['CPS'], overview[media]['current']['CPS'])}"
               for media in channels]
    if len(channels) == 2:
        result["answer"] = "；".join(summary) + "。两渠道应分别判断；已按实际变化检查成本、规模和质量线索，现有证据不能确定共同原因。"
    else:
        result["answer"] = summary[0] + "。已按实际变化检查成本、规模和质量线索；经营原因仍需结合下钻证据判断。"
    result["result_id"] = hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()[:16]
    result["handoffs"] = {"task_spec": {"object_type": "TaskSpec", "task_id": task_id, "task_version": version,
                                        "original_query": query, "channels": channels, "analysis_period": scope["analysis_period"],
                                        "baseline": scope["baseline"], "source_as_of": str(data.AS_OF), "target_metrics": ["规模", "CPS", "安全过件率", "首借金额"]},
                          "data_need": {"object_type": "DataNeed", "metrics": ["花费", "首借金额", "授信数", "申完数", "安全授信数"]},
                          "data_result": {"object_type": "DataResult", "status": "READY", "query_plan": queries,
                                          "quality_checks": {"covered_days": days * 2, "duplicate_keys": False}},
                          "checks": checks}
    reporting.display_result(result)
    explanations = [f"{step['title'].split('：')[0]}：{step['finding'].rstrip('。')}" for step in result["analysis_steps"] if "CPS 的算术驱动" in step["title"] or "消耗变化" in step["title"] or "质量分组核查" in step["title"]]
    if explanations:
        result["answer"] = "；".join(summary) + "。指标分解和链路检查显示：" + "；".join(explanations) + "。上述分别是可复算的算术驱动与掉量候选机制；同期变更的因果影响仍需验证。"
    emit("综合复核与交付", "completed", result["verification_status"])
    state = {"query": query, "slots": slots, "relation": relation_name, "dual_media": len(channels) == 2,
             "progress": progress, "plan": {"plan_version": len(decisions), "decisions": decisions},
             "handoffs": result["handoffs"], "evidence_refs": sources,
             "tool_result_refs": [result["result_id"]], "error": None,
             "checkpoint": {"current_goal": query, "confirmed_scope": slots, "verified_facts": facts, "pending_work": []}}
    check_cancel()
    store.save_task(task_id, session_id, "GENERAL_DIAGNOSIS", "completed", state, result)
    store.add_message(session_id, task_id, "assistant", result["answer"])
    return {"type": "result", "task_id": task_id, "relation": relation_name,
            "status": "completed", "answer": result["answer"], "result": result, "progress": progress}
