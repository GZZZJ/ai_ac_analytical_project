"""Turn verified calculations into a visible analysis chain.

This also presents previously saved results without rewriting their evidence.
"""
from __future__ import annotations


def _relative(before: float | None, after: float | None) -> str:
    return f"{after / before - 1:+.1%}" if before not in (None, 0) and after is not None else "无法比较"


def _cost_step(media: str, detail: dict) -> dict:
    previous, current = detail["metrics"]["previous"], detail["metrics"]["current"]
    terms = detail["contribution_absolute"]
    delta = current["CPS"] - previous["CPS"]
    major = max(terms, key=lambda key: abs(terms[key]))
    movement = "上升" if delta > 0 else "下降" if delta < 0 else "变化"
    share = f"，占 CPS 净变化 {terms[major] / delta:.1%}" if delta else ""
    if major == "单产侧":
        drill = detail["drilldown"]
        driver = detail["primary_factor"]
        deeper = f"{driver}{_relative(drill[driver]['前窗'], drill[driver]['本窗'])}"
        if driver == "动支率":
            deeper += f"，其中发起率{_relative(drill['发起率']['前窗'], drill['发起率']['本窗'])}"
        finding = f"CPS {movement}的主要算术项来自授信单产变化{share}；继续拆解指向{deeper}。"
    else:
        credit_change = _relative(previous['授信数'], current['授信数'])
        spend_change = _relative(previous['花费'], current['花费'])
        driver = "授信数降幅大于花费" if current['授信数'] < previous['授信数'] and current['花费'] < previous['花费'] and current['授信数'] / previous['授信数'] < current['花费'] / previous['花费'] else "授信数变化落后于花费"
        finding = (f"CPS {movement}的主要算术项来自授信成本变化{share}；"
                   f"授信数{credit_change}，花费{spend_change}，{driver}。")
    segment = next((fact for fact in detail["facts"] if "对大盘 CPS 变化贡献" in fact), None)
    evidence = [f"CPS {previous['CPS']:.4f} → {current['CPS']:.4f}（{_relative(previous['CPS'], current['CPS'])}）；"
                f"绝对变化 {delta:+.4f} = 授信成本侧 {terms['成本侧']:+.4f} + 授信单产侧 {terms['单产侧']:+.4f}。",
                f"授信成本 {previous['授信成本']:,.0f} → {current['授信成本']:,.0f} 元；"
                f"授信单产 {previous['授信单产']:,.0f} → {current['授信单产']:,.0f} 元。"]
    if segment:
        evidence.append(segment)
    return {"title": f"{media}：CPS 的算术驱动", "method": "CPS＝授信成本÷授信单产；用两因素对称替换分解绝对变化，再检查业务环节和分组贡献。",
            "evidence": evidence, "finding": finding,
            "boundary": "这是可复算的指标驱动，不能单凭分解证明某项投放变更造成了变化。"}


def _scale_step(media: str, detail: dict) -> dict:
    previous, current = detail["metrics"]["previous"], detail["metrics"]["current"]
    delivery = detail["delivery"]
    supported = [claim["text"].split("：")[0].replace("回传比例", "回传发送成功率").replace(" 下滑", "").strip() for claim in detail["claims"]
                 if claim["status"] == "支持" and "下滑" in claim["text"]]
    excluded = [claim["text"] for claim in detail["claims"] if claim["status"] == "排除" and claim["text"] in {"渠道总预算撞线", "基础出价下调"}]
    evidence = detail["facts"][:3]
    if all(value is not None for value in delivery["参竞率"] + delivery["回传比例"]):
        evidence.append(f"参竞率 {delivery['参竞率'][0]:.1%} → {delivery['参竞率'][1]:.1%}；"
                        f"回传发送成功率 {delivery['回传比例'][0]:.1%} → {delivery['回传比例'][1]:.1%}。")
    mechanism = f"{'、'.join(supported)}等指标同期下滑，是掉量的候选机制" if supported and current['花费'] < previous['花费'] else "竞价链路指标提供消耗变化的候选线索，原因尚不明确"
    finding = (f"消耗{_relative(previous['花费'], current['花费'])}；{mechanism}。"
               + (f"当前数据不支持{'、'.join(excluded)}作为主因。" if excluded else ""))
    return {"title": f"{media}：消耗变化怎么形成", "method": f"先比较消耗、曝光和预算进度，再核对 pCTR、pCVR、参竞率、回传发送成功率及{'RTA策略' if media == '腾讯' else '账户组'}变化。",
            "evidence": evidence, "finding": finding,
            "boundary": "链路指标同向变化不等于因果；缺少媒体竞得与全市场流量日志，不能确定最终掉量原因。"}


def analysis_steps(result: dict) -> list[dict]:
    """Include only branches with a passing numeric verification."""
    steps = []
    if result.get("intent") == "GENERAL_DIAGNOSIS":
        for media in result.get("handoffs", {}).get("task_spec", {}).get("channels", []):
            for kind, builder in (("COST_ANOMALY", _cost_step), ("SCALE_ANOMALY", _scale_step)):
                check = result.get("checks", {}).get(f"{media}·{kind}", {})
                if check.get("status") in {"PASS", "PASS_WITH_WARNING"} and check.get("result"):
                    steps.append(builder(media, check["result"]))
            quality = result.get("checks", {}).get(f"{media}·QUALITY_CHECK", {})
            if quality.get("status") == "READY":
                steps.append({"title": f"{media}：质量分组核查", "method": "先核对分组与渠道大盘的申完数和安全授信数，再比较分组安全过件率。",
                              "evidence": [quality["finding"]], "finding": "已定位到值得继续核查的分组变化；不能据此归因整体质量变化。",
                              "boundary": "分组指标为离线 T7 终值，近期 cohort 尚未成熟。"})
    elif result.get("channel_results"):
        for media, detail in result["channel_results"].items():
            verification = result.get("handoffs", {}).get("channels", {}).get(media, {}).get("verification", {})
            if verification.get("status") not in {"PASS", "PASS_WITH_WARNING"}:
                continue
            if detail.get("intent") == "COST_ANOMALY":
                steps.append(_cost_step(media, detail))
            elif detail.get("intent") == "SCALE_ANOMALY":
                steps.append(_scale_step(media, detail))
    elif result.get("verification_status") in {"PASS", "PASS_WITH_WARNING"}:
        if result.get("intent") == "COST_ANOMALY":
            media = result.get("handoffs", {}).get("task_spec", {}).get("channels", ["渠道"])[0]
            steps.append(_cost_step(media, result))
        elif result.get("intent") == "SCALE_ANOMALY":
            media = result.get("handoffs", {}).get("task_spec", {}).get("channels", ["渠道"])[0]
            steps.append(_scale_step(media, result))
    return steps


def display_result(result: dict | None) -> dict | None:
    """Add a presentation field to an in-memory result, including old tasks."""
    if result:
        if not result.get("analysis_steps"):
            result["analysis_steps"] = analysis_steps(result)
        def public_term(text: str) -> str:
            if "腾讯" in text:
                return text.replace("子组", "RTA策略")
            return text.replace("子组", "账户组")
        result["next_questions"] = [public_term(text) for text in result.get("next_questions", [])]
        for claim in result.get("claims", []):
            claim["text"] = public_term(claim["text"])
    return result
