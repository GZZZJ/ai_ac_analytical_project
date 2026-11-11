"""Versioned handoffs for the local four-role prototype.

The semantic-query role uses the CSV adapter; it does not claim warehouse SQL access.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, timedelta

from . import data, sql_adapter, store


def obj(object_type: str, task_id: str, version: int, input_refs: list[str], **fields) -> dict:
    return {"object_type": object_type, "object_id": str(uuid.uuid4()), "task_id": task_id,
            "task_version": version, "input_refs": input_refs, "generated_at": store.now(), **fields}


def task_spec(task_id: str, query: str, slots: dict, relation: str, version: int = 1) -> dict:
    days = slots.get("days") or (30 if slots["intent"] == "TRAFFIC_VALUE" else 7)
    end = date.fromisoformat(slots["end"]) if slots.get("end") else data.AS_OF
    start = end - timedelta(days=days - 1)
    baseline_start, baseline_end = start-timedelta(days=days), start-timedelta(days=1)
    if slots["intent"] == "STRATEGY_REVIEW":
        changes = [r for r in data.load("change_log") if r["变更类型"] == "RTA出价系数调整"
                   and slots["media"] in r["变更对象"] and "3W2Q" in r["变更对象"] and r["变更日期"] <= str(end)]
        if changes:
            change_day = date.fromisoformat(changes[-1]["变更日期"])
            baseline_start, baseline_end = change_day-timedelta(days=6), change_day-timedelta(days=1)
            start, end = change_day+timedelta(days=2), min(end, change_day+timedelta(days=7))
    return obj("TaskSpec", task_id, version, [], original_query=query, relation="MODIFY" if relation == "MODIFY_SCOPE" else relation,
               problem_domain=slots["intent"], channels=[slots["media"]],
               timezone="Asia/Shanghai", analysis_period={"start": str(start), "end": str(end)},
               baseline=None if slots["intent"] == "TRAFFIC_VALUE" else {"start": str(baseline_start), "end": str(baseline_end), "aggregation": "period_total"},
               target_metrics=[slots.get("metric") or slots["intent"]], maturity_requirement="T7 cohort 成熟度核对",
               pending_fields=[], ready_for_query=True, source_as_of=str(data.AS_OF))


def initial_need(spec: dict) -> dict:
    intent = spec["problem_domain"]
    metrics = {"COST_ANOMALY": ["花费", "授信数", "首借金额"],
               "SCALE_ANOMALY": ["花费", "曝光", "日预算", "基础出价"],
               "TRAFFIC_VALUE": ["花费", "首借金额", "安全授信数", "申完数"],
               "STRATEGY_REVIEW": ["RTA出价系数", "花费", "首借金额", "安全授信数"]}[intent]
    date_range = {"start": spec["baseline"]["start"] if spec["baseline"] else spec["analysis_period"]["start"],
                  "end": spec["analysis_period"]["end"]}
    if intent == "STRATEGY_REVIEW":
        dates = [r["date"] for r in data.load("strategy_experiment") if r["media"] == spec["channels"][0]
                 and r["date"] <= spec["analysis_period"]["end"]]
        if dates:
            date_range = {"start": min(dates), "end": max(dates)}
    return obj("DataNeed", spec["task_id"], spec["task_version"], [spec["object_id"]],
               question=spec["original_query"], hypothesis="先确认数据事实和可比性", metrics=metrics,
               dimensions=["日期", "渠道"], filters={"media": spec["channels"][0]}, grain="日期×渠道",
               date_range=date_range,
               decision_impact="决定是否可进入分析，以及哪些解释需要更多数据", blocking_for=["核心问题"])


def initial_plan(spec: dict) -> dict:
    stages = [("query", "语义查询", "取得所需数据及质量说明", []),
              ("analyze", "数据分析", "计算变化并检验候选解释", ["query"]),
              ("verify", "结果校验", "复算关键数值及发布边界", ["analyze"]),
              ("report", "分析主控", "只交付已校验的结果", ["verify"])]
    return {"request_id": spec["task_id"], "plan_version": 1,
            "task_bindings": [{"task_id": spec["task_id"], "task_version": spec["task_version"]}],
            "steps": [{"step_id": stage, "step_version": 1, "task_id": spec["task_id"],
                       "owner": owner, "goal": goal, "depends_on": depends_on,
                       "input_refs": [spec["object_id"]] if stage == "query" else [], "output_refs": [],
                       "completion_condition": "交付物经版本及必需字段检查可用", "status": "PENDING", "attempts": 0}
                      for stage, owner, goal, depends_on in stages]}


def advance(plan: dict, stage: str, status: str, output_refs: list[str] | None = None):
    if status not in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}:
        raise ValueError("无效步骤状态")
    steps = {step["step_id"]: step for step in plan["steps"]}
    step = steps[stage]
    if status == "COMPLETED" and any(steps[dep]["status"] != "COMPLETED" for dep in step["depends_on"]):
        raise ValueError("前置依赖未完成")
    step["status"] = status
    step["attempts"] += 1
    step["output_refs"] = output_refs or []
    for child in plan["steps"]:
        if child["status"] == "PENDING" and all(steps[dep]["status"] == "COMPLETED" for dep in child["depends_on"]):
            child["status"] = "READY"


def semantic_query(spec: dict, need: dict) -> dict:
    start, end = (date.fromisoformat(need["date_range"][key]) for key in ("start", "end"))
    media = spec["channels"][0]
    intent = spec["problem_domain"]
    table = "strategy_experiment" if intent == "STRATEGY_REVIEW" else "media"
    needed = {"COST_ANOMALY": ["media", "segment", "change_log"],
              "SCALE_ANOMALY": ["media", "delivery", "change_log"] + (["creative"] if media == "抖音" else []),
              "TRAFFIC_VALUE": ["media", "segment", "quality"],
              "STRATEGY_REVIEW": ["strategy_experiment", "change_log"]}[intent]
    queries, sources = [], []
    fetched = {}
    for name in needed:
        sql, records = sql_adapter.execute(name, str(start), str(end), media if name != "change_log" else None)
        fetched[name] = records
        queries.append({"table": name, "sql": sql, "parameters": [str(start), str(end)] + ([media] if name != "change_log" else []),
                        "rows": len(records)})
        sources.append(str(data.DATA / (f"ads_rta_{name}" + ("" if name in {"strategy_experiment", "change_log"} else "_daily") + ".csv")))
    items = fetched[table]
    covered = sorted({r["date"] for r in items})
    expected = (end-start).days+1
    status = "READY" if len(covered) == expected else "PARTIAL" if items else "BLOCKED"
    check = {"expected_days": expected, "covered_days": len(covered),
             "duplicate_keys": len(items) != len({(r["date"], r.get("media"), r.get("分组")) for r in items}),
             "source": "本地模拟 CSV；无数仓 MCP 或生产 SQL"}
    if check["duplicate_keys"]:
        status = "BLOCKED"
    missing_tables = [name for name in needed if name != "change_log" and not fetched[name]]
    optional_tables = {"delivery", "creative"} if intent == "SCALE_ANOMALY" else set()
    if any(name not in optional_tables for name in missing_tables):
        status = "BLOCKED"
    check["missing_tables"] = missing_tables
    reconciliation_errors = []
    if "media" in fetched:
        media_by_day = {row["date"]: row for row in fetched["media"]}
        for name, amount_key in (("segment", "花费"), ("delivery", "消耗"), ("creative", "花费")):
            if name not in fetched:
                continue
            groups = {}
            for row in fetched[name]:
                key = (row["date"], row.get("segment_type") if name == "segment" else
                       row.get("segment_type") if name == "delivery" else "素材")
                groups[key] = groups.get(key, 0) + float(row[amount_key] or 0)
            for (day, group), total in groups.items():
                expected_amount = float(media_by_day.get(day, {}).get("花费", 0) or 0)
                if expected_amount and abs(total-expected_amount)/expected_amount > .005:
                    reconciliation_errors.append({"table": name, "date": day, "segment_type": group,
                                                  "difference": total-expected_amount})
    check["reconciliation_errors"] = reconciliation_errors[:20]
    if reconciliation_errors:
        status = "BLOCKED"
    return obj("DataResult", spec["task_id"], spec["task_version"], [need["object_id"]],
               status=status, query_plan={"queries": queries, "filters": {"media": media},
                                            "grain": "日期×渠道" if table == "media" else "日期×渠道×分组",
                                            "date_range": need["date_range"], "execution": "SQLITE_CSV_READ_ONLY"},
               metric_definition="01_知识库/03_核心指标口径.md", row_count=len(items),
               sample_count=sum(float(r.get("动支数", 0) or 0) for r in items),
               covered_dates=covered, source_as_of=str(data.AS_OF),
               mature_through=str(data.AS_OF-timedelta(days=7)), quality_checks=check,
               limitations=([f"辅助表 {', '.join(name for name in missing_tables if name in optional_tables)} 无数据；仅支持渠道级结论"] if status == "READY" and any(name in optional_tables for name in missing_tables) else
                            [] if status == "READY" else ["日期覆盖或主键检查未通过，不能将缺失当作业务零值"]),
               source_refs=sources)


def findings(spec: dict, data_result: dict, result: dict) -> list[dict]:
    refs = [data_result["object_id"]]
    output = []
    for fact in result["facts"]:
        output.append(obj("Finding", spec["task_id"], spec["task_version"], refs,
                          finding_version=1, kind="fact", text=fact, method="指标计算／窗口比较", confidence="中"))
    for claim in result["claims"]:
        output.append(obj("Finding", spec["task_id"], spec["task_version"], refs,
                          finding_version=1, kind="explanation", text=claim["text"],
                          hypothesis_status=claim["status"], confidence=claim["confidence"]))
    return output


def verify(spec: dict, data_result: dict, findings_list: list[dict], result: dict) -> dict:
    issues = []
    if data_result["status"] != "READY":
        issues.append("基础数据覆盖或主键检查未通过")
    if not result.get("evidence") or not result.get("facts"):
        issues.append("缺少来源或数据事实")
    queried = {entry["table"] for entry in data_result["query_plan"]["queries"]}
    for evidence in result.get("evidence", []):
        table = evidence.get("table")
        if table and table.removeprefix("ads_rta_").removesuffix("_daily") not in queried:
            issues.append(f"证据表 {table} 未进入查询计划")
    media = spec["channels"][0]
    period = spec["analysis_period"]
    start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
    def matches(actual: dict, expected: dict, keys: tuple[str, ...], label: str):
        for key in keys:
            a, b = actual.get(key), expected.get(key)
            if a is None or b is None or not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8):
                issues.append(f"{label} {key} 与原始数据复算不符")
    if result["intent"] in {"COST_ANOMALY", "SCALE_ANOMALY"}:
        baseline = spec["baseline"]
        old = data.metrics(data.aggregate(data.rows("media", date.fromisoformat(baseline["start"]), date.fromisoformat(baseline["end"]), media=media)))
        new = data.metrics(data.aggregate(data.rows("media", start, end, media=media)))
        matches(old, result["metrics"]["previous"], ("花费", "首借金额", "授信数", "CPS"), "基线")
        matches(new, result["metrics"]["current"], ("花费", "首借金额", "授信数", "CPS"), "当期")
    elif result["intent"] == "TRAFFIC_VALUE":
        market = data.aggregate(data.rows("media", start, end, media=media))
        strategy_name = result["evidence"][1]["filters"]["segment_name"]
        group = data.aggregate(data.rows("segment", start, end, media=media, segment_type="RTA策略", segment_name=strategy_name))
        rest = {key: market[key] - group[key] for key in market}
        for label, raw in (("market", market), ("strategy", group), ("without_strategy", rest)):
            matches(data.metrics(raw), result["metrics"][label], ("花费", "首借金额", "CPS", "安全过件率"), label)
        if not math.isclose(group["花费"], result["counterfactual"]["saved_spend"], abs_tol=1e-8):
            issues.append("静态剔除花费与原始数据不符")
    elif result["intent"] == "STRATEGY_REVIEW":
        baseline = spec["baseline"]
        before_start, before_end = date.fromisoformat(baseline["start"]), date.fromisoformat(baseline["end"])
        groups = {}
        for label, group_name, left, right in (("experiment_before", "实验组", before_start, before_end),
                                                ("experiment_after", "实验组", start, end),
                                                ("control_before", "对照组", before_start, before_end),
                                                ("control_after", "对照组", start, end)):
            group_rows = data.rows("strategy_experiment", left, right, media=media, 分组=group_name)
            groups[label] = (data.aggregate(group_rows), len({row["date"] for row in group_rows}))
            matches(data.metrics(groups[label][0]), result["metrics"][label], ("花费", "首借金额", "CPS", "安全过件率"), label)
        if all(count for _, count in groups.values()):
            daily = {key: values["首借金额"]/count for key, (values, count) in groups.items()}
            did = (daily["experiment_after"]-daily["experiment_before"]) - (daily["control_after"]-daily["control_before"])
            if not math.isclose(did, result["did_daily_amount"], rel_tol=1e-9, abs_tol=1e-8):
                issues.append("DID 与原始数据复算不符")
    if result["intent"] == "COST_ANOMALY":
        m = result["metrics"]
        before, after = m["previous"], m["current"]
        terms = result["contribution_absolute"]
        if not math.isclose(sum(terms.values()), after["CPS"]-before["CPS"], abs_tol=1e-10):
            issues.append("CPS 两因素贡献未对账")
        for period in (before, after):
            if not math.isclose(period["授信成本"]/period["授信单产"], period["CPS"], abs_tol=1e-10):
                issues.append("CPS 恒等式不成立")
    unsupported = [f["object_id"] for f in findings_list if f.get("hypothesis_status") == "待验证"]
    status = "BLOCKED" if issues else "PASS_WITH_WARNING" if unsupported or result["reliability"]["limitations"] else "PASS"
    return obj("VerificationResult", spec["task_id"], spec["task_version"],
               [data_result["object_id"]] + [f["object_id"] for f in findings_list],
               stage="finding", status=status, allowed_finding_ids=[] if issues else [f["object_id"] for f in findings_list if f["object_id"] not in unsupported],
               unsupported_claims=unsupported, issues=issues,
               mandatory_limitations=result["reliability"]["limitations"])


def report_spec(spec: dict, data_result: dict, verification: dict, result: dict) -> dict:
    if verification["status"] not in {"PASS", "PASS_WITH_WARNING"}:
        raise ValueError("未通过的结论不能发布")
    return obj("ReportSpec", spec["task_id"], spec["task_version"], [verification["object_id"]],
               report_version=1, allowed_finding_ids=verification["allowed_finding_ids"],
               data_result_ids=[data_result["object_id"]], verification_id=verification["object_id"],
               mandatory_limitations=verification["mandatory_limitations"], charts=result["charts"], format="markdown")


def compose_answer(result: dict, findings_list: list[dict], verification: dict) -> str:
    answer = "；".join(result["facts"][:2])
    if result.get("intent") == "COST_ANOMALY":
        full = next((fact for fact in result["facts"] if fact.startswith("完整环节核查：")), None)
        if full:
            answer += "；" + full
        answer += "；" + "；".join(fact for fact in result["facts"] if fact.startswith(("同步核查质量：", "质量分支：")))
    allowed = set(verification["allowed_finding_ids"])
    for finding in findings_list:
        if finding["object_id"] in allowed and finding.get("hypothesis_status") == "支持":
            return answer + "；证据支持：" + finding["text"]
    return answer + "；原因尚不能确定" if result.get("claims") else answer


def verify_report(spec: dict, report: dict, verification: dict, findings_list: list[dict], result: dict, answer: str) -> dict:
    issues = []
    if report["verification_id"] != verification["object_id"]:
        issues.append("报告引用了不同版本的校验结果")
    if answer != compose_answer(result, findings_list, verification):
        issues.append("报告摘要出现未校验的新表述")
    if report["mandatory_limitations"] != result["reliability"]["limitations"]:
        issues.append("报告遗漏或修改强制限制")
    return obj("VerificationResult", spec["task_id"], spec["task_version"], [report["object_id"]],
               stage="report", status="BLOCKED" if issues else "PASS", issues=issues,
               allowed_finding_ids=report["allowed_finding_ids"], mandatory_limitations=report["mandatory_limitations"])
