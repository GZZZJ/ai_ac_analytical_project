"""Question-led investigations assembled from reusable checks over the local data.

The query chooses a decision to make; observed data decides which explanations
survive. Unsupported premises and missing source fields are reported explicitly.
"""
from __future__ import annotations

import re
import math
from datetime import date, timedelta

from . import data
from .analyzers import Blocked, cost


def goal(query: str) -> str | None:
    q = query.replace(" ", "")
    if re.search(r"PSI|KS|区分度|模型分|模型稳定", q, re.I):
        return "model_impact"
    if "回传" in q and any(x in q for x in ("规则", "链路", "故障", "异常")):
        return "callback"
    if "媒体后台" in q or ("媒体" in q and "我们自己" in q):
        return "reconciliation"
    if any(x in q for x in ("值得继续投", "该不该关", "还值得")):
        return None
    if any(x in q for x in ("低成本", "高质量", "规模空间", "继续放大", "优先尝试")) and any(x in q for x in ("流量", "放大", "规模")):
        return "opportunity"
    if ("大盘" in q and any(x in q for x in ("部分", "局部", "账户组", "版位", "出价方式"))) or ("连续多日" in q and any(x in q for x in ("账户组", "版位", "策略"))):
        return "subgroup"
    if "CPS" in q.upper() and "安全过件率" in q and any(x in q for x in ("同时", "都异常", "双异常", "一起", "均异常")):
        return "joint"
    if "动支率" in q and any(x in q for x in ("腰斩", "突然", "不愿借", "风控", "放款")):
        return "draw"
    if "安全过件率" in q and any(x in q for x in ("下降", "下滑", "变差", "恶化", "异常", "越线", "低于")):
        return "quality"
    if any(x in q for x in ("数据出了问题", "数据问题", "数据异常", "真业务异常")):
        return "integrity"
    if "CPS" in q and any(x in q for x in ("突然", "连续", "每天", "报警", "告警", "趋势")):
        return "trend"
    return None


def _pct(before: float | None, after: float | None) -> str:
    return f"{after / before - 1:+.1%}" if before not in (None, 0) and after is not None else "不可比"


def _pp(before: float | None, after: float | None) -> str:
    return f"{(after - before) * 100:+.1f} pp" if before is not None and after is not None else "不可比"


def _value(metric: str, value: float | None) -> str:
    if value is None:
        return "无有效分母"
    if metric in {"安全过件率", "动支率", "发起率", "发起动支率", "CTR", "额度使用率"}:
        return f"{value:.1%}"
    if metric in {"CPS", "cpcl"}:
        return f"{value:.4f}"
    return f"{value:,.0f}"


def _claim(status: str, text: str, confidence: str = "中") -> dict:
    return {"status": status, "text": text, "confidence": confidence}


def _step(title: str, method: str, evidence: list[str], finding: str, boundary: str) -> dict:
    return {"title": title, "method": method, "evidence": evidence, "finding": finding, "boundary": boundary}


def _action(owner: str, action: str, expected: str, observe: str, rollback: str = "若口径或分组对账失败，撤回基于本次分析的投放调整") -> dict:
    return {"owner": owner, "when": "下一投放日", "action": action, "expected": expected, "observe": observe, "rollback": rollback}


def _group_label(media: str, dimension: str, name: str) -> str:
    label = "账户组" if dimension == "账号" else dimension
    return f"{media}{label} {name}" if not name.startswith(media + label) else name


def _window(media: str, days: int, end: date) -> dict:
    p0, p1, c0, c1 = data.windows(days, end)
    before, after = data.rows("media", p0, p1, media=media), data.rows("media", c0, c1, media=media)
    if len(before) != days or len(after) != days or len({r["date"] for r in before}) != days or len({r["date"] for r in after}) != days:
        raise Blocked(f"{media}在 {p0} 至 {c1} 的日级数据不完整，无法比较两个等长窗口")
    return {"media": media, "bounds": (p0, p1, c0, c1), "before_rows": before, "after_rows": after,
            "before": data.metrics(data.aggregate(before)), "after": data.metrics(data.aggregate(after))}


def _append_metric(ctx: dict, metric: str, facts: list[str]) -> tuple[float | None, float | None]:
    a, b = ctx["before"][metric], ctx["after"][metric]
    facts.append(f"{ctx['media']}{metric} {_value(metric, a)} → {_value(metric, b)}（{_pp(a, b) if metric in {'安全过件率', '动支率', '发起率', '发起动支率'} else _pct(a, b)}）")
    return a, b


def _segment_comparison(ctx: dict, dimension: str) -> tuple[list[dict], bool]:
    p0, p1, c0, c1 = ctx["bounds"]
    old = data.rows("segment", p0, p1, media=ctx["media"], segment_type=dimension)
    new = data.rows("segment", c0, c1, media=ctx["media"], segment_type=dimension)
    aligned = bool(old and new) and all(abs(sum(r[key] for r in period) - sum(r[key] for r in market)) <= max(1, sum(r[key] for r in market) * .005)
                                        for period, market in ((old, ctx["before_rows"]), (new, ctx["after_rows"]))
                                        for key in ("花费", "首借金额", "申完数", "安全授信数"))
    names = sorted({r["segment_name"] for r in old + new})
    compared = []
    for name in names:
        old_total = data.aggregate([r for r in old if r["segment_name"] == name])
        new_total = data.aggregate([r for r in new if r["segment_name"] == name])
        x = data.metrics(old_total) | {"申完数": old_total["申完数"]}
        y = data.metrics(new_total) | {"申完数": new_total["申完数"]}
        if x["CPS"] is not None and y["CPS"] is not None and x["安全过件率"] is not None and y["安全过件率"] is not None:
            compared.append({"name": name, "before": x, "after": y})
    return compared, aligned


def _has_group_contrast(groups: list[dict], metric: str) -> bool:
    changes = [g["after"][metric] / g["before"][metric] for g in groups if g["before"][metric] not in (None, 0) and g["after"][metric] is not None]
    return len(changes) >= 2 and not all(math.isclose(value, changes[0], rel_tol=1e-4, abs_tol=1e-4) for value in changes[1:])


def quality_branch(media: str, current: dict, raw_rows: list[dict], *, external_billing: float | None = None) -> dict:
    """A reusable evidence gate for quality alerts and the independent legacy fixtures."""
    applications = sum(float(r.get("申完数") or 0) for r in raw_rows)
    conversions = sum(float(r.get("转化数") or 0) for r in raw_rows)
    rate = current.get("安全过件率")
    qualified = applications >= 30 and rate is not None
    alert = qualified and rate < .17
    result = {"qualified": qualified, "alert": alert, "applications": applications, "billing_ratio": None,
              "billing_state": "不适用" if media == "抖音" else "无有效数据"}
    if media != "腾讯":
        return result
    bid_cost = sum(float(r.get("转化数") or 0) * float(r.get("基础出价") or 0) for r in raw_rows)
    billing = external_billing if external_billing is not None else data.ratio(sum(float(r.get("花费") or 0) for r in raw_rows), bid_cost)
    result["billing_ratio"] = billing
    if billing is not None and (external_billing is not None or conversions >= 10):
        result["billing_state"] = "偏高" if billing > 1.52 else "正常范围"
    elif billing is not None:
        result["billing_state"] = "样本不足"
    return result


def _add_billing_branch(ctx: dict, branch: dict, facts: list[str], claims: list[dict], limitations: list[str]) -> None:
    media = ctx["media"]
    if media == "抖音":
        facts.append("抖音不适用计费比；改查 CPM、CTR、回传发送成功率与申完到安全授信漏斗")
        for metric in ("CPM", "CTR"):
            _append_metric(ctx, metric, facts)
        claims.append(_claim("排除", "抖音计费比作为质量异常判据", "高"))
        return
    value = branch["billing_ratio"]
    state = branch["billing_state"]
    facts.append(f"腾讯模拟计费比 {value:.3f}（{state}；阈值 1.52）" if value is not None else "腾讯计费比缺有效转化数或基础出价")
    if state == "偏高":
        claims.append(_claim("待验证", "计费比偏高提示媒体投放侧协同风险；需核实际 Bid、转化数及变更", "中"))
    elif state == "正常范围":
        claims.append(_claim("不支持", "计费比超出 1.52 是此次质量变化的线索", "中"))
        limitations.append("计费比正常不能证明媒体侧无问题，仍须核对真实出价、流量和定向。")
    else:
        limitations.append("计费比样本或出价数据不足，不能按腾讯阈值判断。")


def analyze(query: str, slots: dict) -> dict:
    decision = slots["analysis_goal"]
    days = slots["days"]
    end = date.fromisoformat(slots["end"]) if slots.get("end") else data.AS_OF
    channels = [slots["media"]] if slots.get("media") else ["腾讯", "抖音"]
    contexts = [_window(media, days, end) for media in channels]
    facts: list[str] = []
    claims: list[dict] = []
    steps: list[dict] = []
    actions: list[dict] = []
    limitations: list[str] = []
    evidence = [data.source("media", c["before_rows"] + c["after_rows"], c["bounds"][0], end, {"media": c["media"]}) for c in contexts]
    headline: list[str] = []

    if decision == "reconciliation":
        for c in contexts:
            a, b = _append_metric(c, "CPS", facts)
            headline.append(f"{c['media']}内部 CPS {_pct(a, b)}")
        facts.append("内部 CPS＝业务花费÷首借金额；本数据集没有媒体后台同范围的成本、转化数、归因窗或返点口径")
        steps.append(_step("两套报表口径对账", "先统一渠道、日期、归因窗口、费用及转化分母，再比较数值差异；只对内部口径做可复算检查。", facts.copy(),
                           "当前只能确认内部 CPS 的变化，无法在缺少媒体后台明细时判定谁的报表有误。", "媒体优化目标可能与内部首借金额不同，两边数字不能直接互斥。"))
        claims.append(_claim("待验证", "媒体后台与内部 CPS 的归因窗、费用口径和转化分母是否一致", "低"))
        limitations.append("缺少媒体后台同日同账户的曝光、转化、费用和归因窗口数据。")
        actions.append(_action("获客运营、数据研发", "导出同账户同日期的媒体花费、优化目标转化与归因窗，和内部花费、首借金额逐项对账", "定位差异出在费用、转化定义还是归因时间", "对齐后复核最近 7 个完整日"))

    elif decision == "trend":
        for c in contexts:
            a, b = _append_metric(c, "CPS", facts)
            series = [(r["date"], data.metrics(data.aggregate([r]))["CPS"]) for r in sorted(c["after_rows"], key=lambda row: row["date"])]
            changes = [(series[i][0], series[i][1] / series[i - 1][1] - 1) for i in range(1, len(series)) if series[i - 1][1] and series[i][1]]
            positive = sum(delta > 0 for _, delta in changes)
            largest = max(changes, key=lambda pair: pair[1]) if changes else None
            facts.append(f"{c['media']}本窗 {len(series)} 日中，相邻日 CPS 上涨 {positive}/{len(changes)} 次" + (f"；最大单日涨幅 {largest[1]:+.1%}（{largest[0]}）" if largest else ""))
            if "连续" in query and positive != len(changes):
                claims.append(_claim("不支持", f"{c['media']} CPS 在所选窗口每天连续上涨"))
                headline.append(f"{c['media']} CPS 并非每天连续上涨")
            else:
                claims.append(_claim("支持" if positive == len(changes) and changes else "待验证", f"{c['media']}存在持续上行形态" if positive == len(changes) and changes else f"{c['media']}需区分单日跳变与窗口整体抬升"))
                headline.append(f"{c['media']} CPS {_pct(a, b)}")
            steps.append(_step(f"{c['media']}逐日形态", "计算每天 CPS，并分别检查连续上涨次数、最大单日变化与等长窗口变化。", facts[-2:],
                               f"相邻日上涨 {positive}/{len(changes)} 次" + (f"，最大单日涨幅 {largest[1]:+.1%}（{largest[0]}）" if largest else ""),
                               "告警阈值及报警日志不在数据集中，不能断言系统当时应当报警。"))
        limitations.append("缺少实际告警日志和阈值配置；近期 T7 离线终值不能用于当日真实告警回放。")
        actions.append(_action("获客运营、数据分析", "用成熟 cohort 重放逐日 CPS 与连续 3/7 日累计变化，并核对告警阈值", "区分突变和缓涨漏报", "最近 7 个成熟 cohort"))

    elif decision == "draw":
        for c in contexts:
            a, b = _append_metric(c, "动支率", facts)
            _append_metric(c, "发起率", facts)
            _append_metric(c, "发起动支率", facts)
            halved = a and b is not None and b <= a * .5
            if "腰斩" in query and not halved:
                headline.append(f"{c['media']}动支率没有腰斩，实际变化 {_pct(a, b)}")
                claims.append(_claim("不支持", f"{c['media']}动支率腰斩", "高"))
            else:
                headline.append(f"{c['media']}动支率变化 {_pct(a, b)}")
            init = _pct(c["before"]["发起率"], c["after"]["发起率"])
            post = _pct(c["before"]["发起动支率"], c["after"]["发起动支率"])
            steps.append(_step(f"{c['media']}动支链路", "动支率＝授信后发起率×发起后动支率；分别比较两个环节。", facts[-3:],
                               f"发起率 {init}，发起后动支率 {post}；可定位到变化环节，不能据此直接认定用户意愿、风控或放款故障。", "缺少发起失败原因、二次风控拒绝码和资金路由日志。"))
        limitations.append("日级转化为离线 T7 终值，不能据此定位盘中故障时点。")
        actions.append(_action("产品、风控、资金与研发", "先核对发起事件和发起后拒绝码，再按异常环节调取资金路由与放款失败日志", "区分用户未发起、风控拦截与放款失败", "下一完整日及成熟 cohort"))

    elif decision == "joint":
        for c in contexts:
            qa = quality_branch(c["media"], c["after"], c["after_rows"])
            ca, cb = _append_metric(c, "CPS", facts)
            sa, sb = _append_metric(c, "安全过件率", facts)
            if not qa["qualified"]:
                headline.append(f"{c['media']}申完样本 {qa['applications']:.0f} < 30，双异常判断暂不成立")
                limitations.append(f"{c['media']}安全过件率样本不足，不能按阈值调价。")
                continue
            cost_rise = bool(ca and cb and cb > ca)
            safety_drop = bool(sa is not None and sb is not None and sb < sa)
            headline.append(f"{c['media']}CPS {_pct(ca, cb)}、安全过件率 {_pp(sa, sb)}；"
                            + ("两项同向恶化且安全过件率低于 17%" if cost_rise and safety_drop and qa["alert"]
                               else "两项同向恶化，但安全过件率未低于 17%" if cost_rise and safety_drop
                               else "所问双异常前提未获同窗数据支持"))
            if not (cost_rise and safety_drop):
                claims.append(_claim("不支持", f"{c['media']}当前窗口 CPS 上升且安全过件率下降同时成立", "高"))
            detail = cost(c["media"], days, end)
            for metric in ("cpcl", "授信成本", "授信单产", "平均授信额度", "额度使用率"):
                _append_metric(c, metric, facts)
            terms = detail["contribution_absolute"]
            facts.append(f"{c['media']}CPS 绝对变化：成本侧 {terms['成本侧']:+.4f}，单产侧 {terms['单产侧']:+.4f}；两项合计 {sum(terms.values()):+.4f}")
            evidence.extend(detail["evidence"])
            dims = ["RTA策略", "版位"] if c["media"] == "腾讯" else ["账号"]
            for dim in dims:
                groups, aligned = _segment_comparison(c, dim)
                if not aligned:
                    limitations.append(f"{c['media']}{dim}分组未与大盘对齐，不能归因到分组。")
                    continue
                if len(groups) <= 1:
                    limitations.append(f"{c['media']}{dim}只有一个取值，与大盘相同，无法定位该维度的局部异常。")
                    continue
                if not _has_group_contrast(groups, "CPS"):
                    limitations.append(f"{c['media']}{dim}各组 CPS 变动几乎同幅，不能按规模最大的组认定局部拖累。")
                    continue
                both = [g for g in groups if g["after"]["CPS"] > g["before"]["CPS"] and
                        g["after"]["安全过件率"] < g["before"]["安全过件率"] and g["after"]["申完数"] >= 30]
                both.sort(key=lambda g: (g["after"]["CPS"] - g["before"]["CPS"]) * g["after"]["首借金额"], reverse=True)
                if both:
                    g = both[0]
                    facts.append(f"{_group_label(c['media'], dim, g['name'])} 同时变差：CPS {g['before']['CPS']:.4f} → {g['after']['CPS']:.4f}；安全过件率 {g['before']['安全过件率']:.1%} → {g['after']['安全过件率']:.1%}；申完 {g['after']['申完数']:.0f}")
                evidence.append(data.source("segment", data.rows("segment", c["bounds"][0], end, media=c["media"], segment_type=dim), c["bounds"][0], end, {"media": c["media"], "segment_type": dim}))
            _add_billing_branch(c, qa, facts, claims, limitations)
            headline[-1] += (f"；模拟计费比 {qa['billing_ratio']:.3f}（{qa['billing_state']}）" if c["media"] == "腾讯" and qa["billing_ratio"] is not None else "；抖音不适用计费比" if c["media"] == "抖音" else "；计费比无有效数据")
            steps.append(_step(f"{c['media']}双指标联查", "同窗核验 CPS 与安全过件率；CPS 按授信成本和授信单产分解，再看 cpcl、额度与动支及分组。", facts[-9:],
                               f"CPS 成本侧 {terms['成本侧']:+.4f}、单产侧 {terms['单产侧']:+.4f}；安全过件率 {_pp(sa, sb)}。",
                               "两项同向变化和分组重合不证明同一个根因；近期 T7 为离线终值。"))
            if c["after"]["动支数"] >= 8 and cost_rise and safety_drop:
                actions.append(_action("获客运营、风险策略", "核对双异常分组的定向、出价、模型分档及同期变更；完成对账后做小范围可逆实验", "同时验证 CPS 回落和安全过件率恢复", "后续 3–7 个成熟 cohort", "任一指标继续恶化或样本门槛失效即回滚"))
            elif c["after"]["动支数"] < 8:
                limitations.append(f"{c['media']}动支 {c['after']['动支数']:.0f} < 8，不能按 CPS 变化调整投放。")

    elif decision == "quality":
        for c in contexts:
            a, b = _append_metric(c, "安全过件率", facts)
            cps_a, cps_b = _append_metric(c, "CPS", facts)
            qa = quality_branch(c["media"], c["after"], c["after_rows"])
            facts.append(f"{c['media']}本窗申完 {qa['applications']:.0f}；安全过件率 {'低于 17% 阈值' if qa['alert'] else '未低于 17% 阈值' if qa['qualified'] else '样本不足，不能判定越线'}")
            if not qa["qualified"]:
                limitations.append(f"{c['media']}申完不足 30，安全过件率阈值判断无效。")
            daily = [(r["date"], data.metrics(data.aggregate([r]))["安全过件率"]) for r in sorted(c["after_rows"], key=lambda row: row["date"])]
            day_changes = [(daily[i][0], daily[i][1] - daily[i - 1][1]) for i in range(1, len(daily)) if daily[i][1] is not None and daily[i - 1][1] is not None]
            if day_changes:
                lowest = min(day_changes, key=lambda item: item[1])
                facts.append(f"{c['media']}本窗安全过件率最大单日变化 {lowest[1] * 100:+.1f} pp（{lowest[0]}）；未设定可直接判定‘突然’的业务阈值")
            if "CPS" in query and any(x in query for x in ("没明显变差", "没有明显变差", "暂时还没")) and cps_a and cps_b and cps_b > cps_a * 1.05:
                claims.append(_claim("不支持", f"{c['media']} CPS 尚未明显变差：当前同窗已 {_pct(cps_a, cps_b)}", "高"))
                headline.append(f"{c['media']}安全过件率 {_pp(a, b)}，且 CPS 已 {_pct(cps_a, cps_b)}" + (f"；最大单日变化 {lowest[1] * 100:+.1f} pp" if day_changes else ""))
            else:
                headline.append(f"{c['media']}安全过件率 {_pp(a, b)}" + (f"；最大单日变化 {lowest[1] * 100:+.1f} pp" if day_changes else ""))
            headline[-1] += ("；本窗低于 17% 阈值" if qa["alert"] else "；本窗未低于 17% 阈值" if qa["qualified"] else "；申完不足 30，越线判断无效")
            dim = "RTA策略" if c["media"] == "腾讯" else "账号"
            groups, aligned = _segment_comparison(c, dim)
            if aligned and groups:
                worst = min(groups, key=lambda g: g["after"]["安全过件率"] - g["before"]["安全过件率"])
                facts.append(f"{_group_label(c['media'], dim, worst['name'])} 安全过件率 {_value('安全过件率', worst['before']['安全过件率'])} → {_value('安全过件率', worst['after']['安全过件率'])}")
                evidence.append(data.source("segment", data.rows("segment", c["bounds"][0], end, media=c["media"], segment_type=dim), c["bounds"][0], end, {"media": c["media"], "segment_type": dim}))
            _add_billing_branch(c, qa, facts, claims, limitations)
            headline[-1] += (f"；模拟计费比 {qa['billing_ratio']:.3f}（{qa['billing_state']}）" if c["media"] == "腾讯" and qa["billing_ratio"] is not None else "；抖音不适用计费比" if c["media"] == "抖音" else "；计费比无有效数据")
            steps.append(_step(f"{c['media']}质量前置信号", "先验证安全过件率与 CPS 的同窗变化，再检查逐日形态及对齐后的质量分组。", facts[-4:], headline[-1], "分组下降是定位线索，不证明风控模型或流量来源的因果作用。"))
            if qa["qualified"]:
                actions.append(_action("风险策略、获客运营", "复核安全授信与申完口径，并在下降最大的分组核对模型分档和同期定向变更", "确定质量下降是否先于成本传导", "下一 3–7 个成熟 cohort"))

    elif decision in {"subgroup", "opportunity"}:
        candidates = []
        quality_candidates = []
        for c in contexts:
            _append_metric(c, "CPS", facts)
            _append_metric(c, "安全过件率", facts)
            dims = ["RTA策略", "版位"] if c["media"] == "腾讯" else ["账号"]
            for dim in dims:
                groups, aligned = _segment_comparison(c, dim)
                if not aligned:
                    limitations.append(f"{c['media']}{dim}分组与大盘未对齐，不能据此排序。")
                    continue
                if len(groups) <= 1:
                    limitations.append(f"{c['media']}{dim}只有一个取值，不能定位该维度的局部差异。")
                    continue
                if decision == "subgroup" and not _has_group_contrast(groups, "CPS"):
                    limitations.append(f"{c['media']}{dim}各组 CPS 变动几乎同幅，不能定位某一组单独拖累大盘。")
                    continue
                evidence.append(data.source("segment", data.rows("segment", c["bounds"][0], end, media=c["media"], segment_type=dim), c["bounds"][0], end, {"media": c["media"], "segment_type": dim}))
                if decision == "subgroup":
                    ranked = sorted((g for g in groups if g["after"]["CPS"] > g["before"]["CPS"] and g["after"]["动支数"] >= 8),
                                    key=lambda g: (g["after"]["CPS"] - g["before"]["CPS"]) * g["after"]["首借金额"], reverse=True)
                    if ranked:
                        top = ranked[0]
                        daily = data.rows("segment", c["bounds"][2], end, media=c["media"], segment_type=dim, segment_name=top["name"])
                        daily = sorted(daily, key=lambda row: row["date"])
                        run = 0
                        for row in reversed(daily):
                            day_cps = data.metrics(data.aggregate([row]))["CPS"]
                            if day_cps is not None and day_cps > top["before"]["CPS"]:
                                run += 1
                            else:
                                break
                        facts.append(f"{_group_label(c['media'], dim, top['name'])} CPS {_value('CPS', top['before']['CPS'])} → {_value('CPS', top['after']['CPS'])}；最近连续 {run} 日高于该组前窗 CPS；本窗首借金额 {top['after']['首借金额']:,.0f} 元")
                        candidates.append((c["media"], dim, top, run))
                        if len(ranked) > 1:
                            other = ranked[1]
                            facts.append(f"同一{('账户组' if dim == '账号' else dim)}切分的次要候选 {_group_label(c['media'], dim, other['name'])}：CPS {_value('CPS', other['before']['CPS'])} → {_value('CPS', other['after']['CPS'])}；本窗动支 {other['after']['动支数']:.0f}")
                    quality_ranked = sorted(groups, key=lambda g: (g["after"]["安全过件率"] - g["before"]["安全过件率"]) * g["after"]["申完数"])
                    if quality_ranked and quality_ranked[0]["after"]["安全过件率"] < quality_ranked[0]["before"]["安全过件率"]:
                        qtop = quality_ranked[0]
                        daily = sorted(data.rows("segment", c["bounds"][2], end, media=c["media"], segment_type=dim, segment_name=qtop["name"]), key=lambda row: row["date"])
                        qrun = 0
                        for row in reversed(daily):
                            day_rate = data.metrics(data.aggregate([row]))["安全过件率"]
                            if day_rate is not None and day_rate < qtop["before"]["安全过件率"]:
                                qrun += 1
                            else:
                                break
                        facts.append(f"{_group_label(c['media'], dim, qtop['name'])} 安全过件率 {_value('安全过件率', qtop['before']['安全过件率'])} → {_value('安全过件率', qtop['after']['安全过件率'])}；最近连续 {qrun} 日低于该组前窗安全过件率")
                        if qrun >= 3:
                            quality_candidates.append((c["media"], dim, qtop, qrun))
                else:
                    for g in groups:
                        if g["after"]["CPS"] <= c["after"]["CPS"] * .95 and g["after"]["安全过件率"] >= c["after"]["安全过件率"] + .01 and g["after"]["花费"] > 0:
                            candidates.append((c["media"], dim, g))
        if decision == "subgroup":
            headline = [f"{_group_label(m, dim, g['name'])} 连续 {run} 日高于自身前窗 CPS" for m, dim, g, run in candidates]
            headline += [f"{_group_label(m, dim, g['name'])} 连续 {run} 日低于自身前窗安全过件率" for m, dim, g, run in quality_candidates]
            for c in contexts:
                if c["before"]["CPS"] and c["after"]["CPS"] > c["before"]["CPS"] * 1.05 and "大盘正常" in query:
                    claims.append(_claim("不支持", f"按 CPS 判断{c['media']}大盘正常：实际 {_pct(c['before']['CPS'], c['after']['CPS'])}", "高"))
                    headline.insert(0, f"按 CPS 看，{c['media']}大盘已 {_pct(c['before']['CPS'], c['after']['CPS'])}")
            steps.append(_step("大盘与局部拆分", "按媒体分别比较大盘，再以各自分母重算 RTA 策略、版位或账户组的 CPS 与安全过件率；逐日比较该组自身前窗，分组须与大盘对齐。", facts.copy(),
                               "已逐日核对候选分组相对自身前窗的连续超出天数；仍需结合样本量和同期变更判断。" if candidates else "未取得可对齐的分组证据。", "局部指标与大盘可同时朝不同方向变化；不能将分组均值直接平均。"))
            actions.append(_action("获客运营、数据分析", "对列出的候选分组逐日复核 CPS、花费和首借金额，连续恶化后再核对定向及出价变更", "确认是否存在被大盘掩盖的局部异常", "连续 3–7 个完整日"))
        else:
            candidates.sort(key=lambda item: item[2]["after"]["首借金额"], reverse=True)
            for m, dim, g in candidates[:3]:
                facts.append(f"候选 {_group_label(m, dim, g['name'])}：CPS {g['after']['CPS']:.4f} < 渠道大盘；安全过件率 {g['after']['安全过件率']:.1%} > 大盘；首借金额 {g['after']['首借金额']:,.0f} 元")
            labels = [_group_label(m, dim, g['name']) for m, dim, g in candidates[:3]]
            headline = [f"优先核查{'、'.join(labels)}；{'该候选' if len(labels) == 1 else '这些候选'}满足本窗低 CPS、高安全过件率，规模空间尚未证实" if candidates else "未筛出同时满足低 CPS 和高安全过件率的候选流量"]
            steps.append(_step("候选流量筛选", "同渠道同窗初筛：CPS 至少低于渠道大盘 5%，安全过件率至少高 1 pp；按首借金额排序。这是候选筛选条件，不是放量阈值。", facts[-min(3, len(candidates)):] if candidates else facts[-2:], headline[0],
                               "现有预算字段不是账户级真实可用额度；缺竞得、请求量和预算余量，不能断言有放量空间。"))
            limitations.append("规模空间未证实：缺账户级实际预算余量、竞得率及增量实验。")
            actions.append(_action("获客运营、获客策略", "对候选分组核对真实预算余量、参竞请求和竞得率；证实余量后做小流量增量实验", "验证增量首借金额与 CPS、安全过件率保护指标", "实验后 3–7 个成熟 cohort", "CPS 超出同渠道基线或安全过件率下降时停止扩量"))

    elif decision == "callback":
        for c in contexts:
            p0, p1, c0, c1 = c["bounds"]
            dim = "RTA策略" if c["media"] == "腾讯" else "账号"
            before = data.rows("delivery", p0, p1, media=c["media"], segment_type=dim)
            after = data.rows("delivery", c0, c1, media=c["media"], segment_type=dim)
            if not before or not after:
                limitations.append(f"{c['media']}缺少完整回传发送率明细。")
                continue
            weighted = lambda items: sum(r["回传比例"] * r["消耗"] for r in items) / sum(r["消耗"] for r in items)
            a, b = weighted(before), weighted(after)
            facts.append(f"{c['media']}回传发送成功率 {a:.1%} → {b:.1%}（{_pp(a, b)}）")
            daily = sorted(c["after_rows"], key=lambda row: row["date"])
            day_changes = [(daily[i]["date"], daily[i]["回传比例"] - daily[i - 1]["回传比例"]) for i in range(1, len(daily))]
            lowest = min(day_changes, key=lambda item: item[1]) if day_changes else None
            if lowest:
                facts.append(f"{c['media']}本窗最大单日回传发送率变化 {lowest[1] * 100:+.1f} pp（{lowest[0]}）")
            headline.append(f"{c['media']}回传发送成功率 {_pp(a, b)}" + (f"，最大单日变化 {lowest[1] * 100:+.1f} pp" if lowest else ""))
            evidence.append(data.source("delivery", before + after, p0, c1, {"media": c["media"], "segment_type": dim}))
            events = [r for r in data.rows("change_log", p0, c1) if c["media"] in r["变更对象"] and "回传" in r["变更类型"]]
            facts.append(f"{c['media']}对比期有 {len(events)} 条回传相关变更记录；时间吻合不证明原因")
            evidence.append(data.source("change_log", events, p0, c1, {"media": c["media"], "topic": "回传"}))
            steps.append(_step(f"{c['media']}回传机制辨别", "先比较回传发送成功率和逐日变化，再分开检查应回传覆盖率、发送失败码和规则命中量。", facts[-3:],
                               "只能观察发送成功率的变化；无法用它区分链路故障与规则收紧。", "表中没有应回传人数、符合规则的候选数和错误码。"))
            claims.append(_claim("待验证", f"{c['media']}链路故障或回传规则改变导致变化", "低"))
        limitations.append("回传比例＝发送成功数÷应回传数，不等于规则覆盖率。")
        actions.append(_action("数据研发、获客策略", "核对发送失败码与应回传量，同时对照规则版本、候选授信数及命中数", "分别定位传输故障与规则覆盖变化", "问题发生日前后逐日及下一完整日"))

    elif decision == "model_impact":
        for c in contexts:
            for metric in ("安全过件率", "CPS", "授信单产"):
                _append_metric(c, metric, facts)
            p0, _, _, _ = c["bounds"]
            events = [r for r in data.rows("change_log", p0, end) if c["media"] in r["变更对象"] and "模型" in r["变更类型"]]
            facts.append(f"{c['media']}对比期模型相关变更记录 {len(events)} 条；事件表没有 PSI／区分度监控值")
            evidence.append(data.source("change_log", events, p0, end, {"media": c["media"], "topic": "模型"}))
            headline.append(f"{c['media']}安全过件率 {_pp(c['before']['安全过件率'], c['after']['安全过件率'])}、CPS {_pct(c['before']['CPS'], c['after']['CPS'])}、授信单产 {_pct(c['before']['授信单产'], c['after']['授信单产'])}；模型影响不可判定")
        facts.append("当前表没有 PSI、KS、模型分数分布或模型异常的准确起点")
        steps.append(_step("模型异常影响核对", "先验证模型异常时间和影响人群，再比较受影响与未受影响人群的业务指标和前趋势。", facts.copy(),
                           "可以报告业务指标同期变化；缺少模型监控明细与可比对照，不能判定变化由模型异常造成。", "同期变化不等于模型因果效应。"))
        limitations.append("缺模型 PSI／区分度、分数分布、版本和受影响样本标识。")
        claims.append(_claim("待验证", "模型异常已传导至安全过件率、CPS 或授信单产", "低"))
        actions.append(_action("模型团队、风险策略、数据分析", "提供模型版本、PSI／KS 日序列与受影响样本标识，核对上线时点及未受影响对照人群", "检验业务指标变化是否与模型异常同步且集中于受影响人群", "异常前后各 7 个成熟 cohort"))

    elif decision == "integrity":
        for c in contexts:
            for metric in ("花费", "CPS", "安全过件率"):
                _append_metric(c, metric, facts)
            dim = "RTA策略" if c["media"] == "腾讯" else "账号"
            _, aligned = _segment_comparison(c, dim)
            facts.append(f"{c['media']}{'账户组' if dim == '账号' else dim}汇总与媒体表花费、首借金额、申完数、安全授信数{'对齐' if aligned else '不对齐'}")
            p0, _, _, _ = c["bounds"]
            funnel = data.rows("funnel", p0, end, media=c["media"])
            media_total = data.aggregate(c["before_rows"] + c["after_rows"])
            funnel_total = data.aggregate(funnel)
            funnel_ok = bool(funnel) and all(abs(funnel_total[key] - media_total[key]) <= max(1, media_total[key] * .005) for key in ("首登数", "申完数", "授信数", "发起数", "动支数"))
            facts.append(f"{c['media']}客群漏斗与媒体表首登、申完、授信、发起、动支{'对齐' if funnel_ok else '不对齐'}")
            evidence.append(data.source("funnel", funnel, p0, end, {"media": c["media"]}))
            if c["media"] == "抖音":
                creative = data.rows("creative", p0, end, media="抖音")
                creative_total = data.aggregate(creative)
                creative_ok = bool(creative) and all(abs(creative_total[key] - media_total[key]) <= max(1, media_total[key] * .005) for key in ("花费", "曝光", "点击"))
                facts.append(f"抖音素材表与媒体表花费、曝光、点击{'对齐' if creative_ok else '不对齐'}")
                evidence.append(data.source("creative", creative, p0, end, {"media": "抖音"}))
            else:
                creative_ok = True
            headline.append(f"{c['media']}花费 {_pct(c['before']['花费'], c['after']['花费'])}，分组{'对齐' if aligned else '不对齐'}")
            if "大幅下降" in query and c["after"]["花费"] >= c["before"]["花费"]:
                claims.append(_claim("不支持", f"{c['media']}消耗在所选窗口下降", "高"))
                headline[-1] += "，不支持两渠道都掉量的前提"
            if not all((aligned, funnel_ok, creative_ok)):
                claims.append(_claim("支持", f"{c['media']}存在跨表汇总不一致，应先排查数据", "高"))
        steps.append(_step("先查数据再判业务", "检查两个等长窗口的日记录覆盖及分组与媒体大盘的可加总字段，再比较多指标方向。", facts.copy(),
                           "日记录和分组对账是基础校验；即使通过，也不能排除埋点、归因和上游共同错误。", "模拟数据没有原始请求、埋点日志和实时入仓延迟记录。"))
        limitations.append("缺原始埋点和入仓日志，不能最终排除源头数据问题。")
        actions.append(_action("数据研发、获客运营", "核对原始埋点行数、去重主键、入仓延迟与媒体账单，再逐渠道检查异常环节", "区分采集问题与业务链路变化", "异常前后各 3 个完整日"))

    if not headline:
        headline = ["现有数据不足以完成该判断"]
    if not data.maturity(end):
        limitations.append(f"截至 {data.AS_OF}，{end} 的 T7 cohort 尚未成熟；转化数据为离线终值，不能视为当时实时可见。")
    title = {"reconciliation": "媒体与内部指标口径对账", "trend": "CPS 逐日形态调查", "draw": "动支链路调查", "quality": "安全过件率调查", "joint": "CPS 与安全过件率联查", "subgroup": "局部分组异常调查", "opportunity": "流量放量候选筛选", "callback": "回传异常调查", "model_impact": "模型异常业务影响核对", "integrity": "业务与数据异常辨别"}[decision]
    messages = [part.rstrip("。") for part in headline]
    messages += [step["finding"].rstrip("。") for step in steps if step["finding"].rstrip("。") not in messages]
    if decision == "joint":
        messages += [fact.rstrip("。") for fact in facts if any(marker in fact for marker in ("cpcl ", "授信成本 ", "授信单产 ", "平均授信额度 ", "额度使用率 ", "同时变差："))]
    elif decision == "subgroup":
        messages += [fact.rstrip("。") for fact in facts if "最近连续" in fact and (" CPS " in fact or " 安全过件率 " in fact)]
    elif decision == "quality":
        messages += [fact.rstrip("。") for fact in facts if "安全过件率" in fact and " → " in fact and any(label in fact for label in ("RTA策略", "账户组"))]
    if decision in {"joint", "subgroup"}:
        messages += [note.rstrip("。") for note in limitations if "只有一个取值" in note or "变动几乎同幅" in note]
    answer = "；".join(messages) + "。"
    return {"intent": "INVESTIGATION", "analysis_goal": decision, "title": title, "answer": answer,
            "facts": facts, "claims": claims, "analysis_steps": steps, "actions": actions, "charts": [], "next_questions": [],
            "evidence": evidence, "reliability": {"level": "中" if not limitations else "低", "dimensions": {"数据覆盖": "两个等长窗口完整", "指标成熟度": "通过" if data.maturity(end) else "离线终值", "因果判断": "仅可列候选"}, "limitations": limitations},
            "causal_claim_allowed": False, "verification_status": "PASS_WITH_WARNING" if limitations else "PASS",
            "scope": {"channels": channels, "analysis_period": {"start": str(contexts[0]["bounds"][2]), "end": str(end)},
                      "baseline": {"start": str(contexts[0]["bounds"][0]), "end": str(contexts[0]["bounds"][1])}, "source_as_of": str(data.AS_OF)}}
