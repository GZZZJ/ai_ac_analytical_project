"""Deterministic calculations; causal language is deliberately constrained."""
from __future__ import annotations

import math
from datetime import date, timedelta

from .data import AS_OF, aggregate, load, maturity, metrics, ratio, rows, source, windows


class Blocked(Exception):
    pass


def change_events(start: date, end: date, media: str | None = None) -> tuple[list[dict], dict]:
    found = rows("change_log", start, end)
    if media:
        found = [r for r in found if media in r["变更对象"]]
    return found, source("change_log", found, start, end, {"media": media} if media else {})


def change(before: float | None, after: float | None) -> float | None:
    return after / before - 1 if before not in (None, 0) and after is not None else None


def signed(before: float | None, after: float | None) -> str:
    value = change(before, after)
    return f"{value:+.1%}" if value is not None else "无法计算"


def daily_average(items: list[dict], key: str) -> float:
    days = len({r["date"] for r in items})
    return sum(float(r[key]) for r in items) / days if days else 0.0


def base(intent: str, title: str, end: date, evidence: list, facts: list, claims: list, actions: list, charts: list, questions: list, *, sample: bool = True, strength: str = "中", limitations: list | None = None, complete: bool = True, sample_count: int | None = None, sample_min: int | None = None) -> dict:
    limitations = list(limitations or [])
    mature = maturity(end)
    if sample_count is not None and sample_min is not None:
        sample = sample_count >= sample_min
        if not sample:
            limitations.append(f"有效样本 {sample_count}，低于业务口径最低样本量 {sample_min}；不应按异常阈值做投放动作。")
    if not mature:
        limitations.append(f"{end} 首登 cohort 的 T7 终值在 {AS_OF} 尚未成熟；以下为离线构造终值，不能当作当日实时可见结果。")
    dimensions = {"数据完整性": "通过" if complete else "部分缺失", "样本充分度": f"{sample_count}/{sample_min} · {'通过' if sample else '不足'}" if sample_count is not None and sample_min is not None else "通过" if sample else "不足", "指标成熟度": "通过" if mature else "离线终值", "证据强度": strength}
    confidence = "低" if not sample or not complete or strength == "低" else "中" if not mature or strength == "中" else "高"
    return {"intent": intent, "title": title, "facts": facts, "claims": claims, "actions": actions, "charts": charts[:3], "next_questions": questions[:3], "evidence": evidence,
            "reliability": {"level": confidence, "dimensions": dimensions, "limitations": limitations}, "causal_claim_allowed": False}


def cost(media: str, days: int = 7, end: date = AS_OF) -> dict:
    p0, p1, c0, c1 = windows(days, end)
    previous, current = rows("media", p0, p1, media=media), rows("media", c0, c1, media=media)
    if len(previous) != days or len(current) != days:
        raise Blocked("媒体结果数据不完整，无法比较等长窗口")
    a, b = metrics(aggregate(previous)), metrics(aggregate(current))
    if not all(a[k] and b[k] for k in ("CPS", "授信成本", "授信单产")):
        raise Blocked("授信或首借金额分母为零，无法分解 CPS")
    cost0, cost1 = a["授信成本"], b["授信成本"]
    y0, y1 = a["授信单产"], b["授信单产"]
    contribution_absolute = {"成本侧": (cost1-cost0) * (1/y0+1/y1)/2,
                             "单产侧": (cost0+cost1) * (1/y1-1/y0)/2}
    delta_cps = b["CPS"] - a["CPS"]
    contribution = {key: ratio(value, delta_cps) if abs(delta_cps) >= .00005 else None
                    for key, value in contribution_absolute.items()}
    major = max(contribution_absolute, key=lambda key: abs(contribution_absolute[key]))
    drill = {k: {"前窗": a[k], "本窗": b[k], "变化率": ratio(b[k], a[k]) - 1 if a[k] else None} for k in ("首登成本", "申完成本", "cpcl", "授信成本", "授信单产", "平均授信额度", "额度使用率", "动支率", "发起率", "发起动支率")}
    segment_type = "RTA策略" if rows("segment", c0, c1, media=media, segment_type="RTA策略") else "账号"
    segments = rows("segment", c0, c1, media=media, segment_type=segment_type)
    previous_segments = rows("segment", p0, p1, media=media, segment_type=segment_type)
    names = sorted({r["segment_name"] for r in segments})
    segment_summary = []
    for name in names:
        x = metrics(aggregate([r for r in previous_segments if r["segment_name"] == name]))
        y = metrics(aggregate([r for r in segments if r["segment_name"] == name]))
        w0, w1 = ratio(x["首借金额"], a["首借金额"]), ratio(y["首借金额"], b["首借金额"])
        structural = (w1 - w0) * (x["CPS"] + y["CPS"]) / 2 if all(v is not None for v in (w0, w1, x["CPS"], y["CPS"])) else None
        within = (y["CPS"] - x["CPS"]) * (w0 + w1) / 2 if all(v is not None for v in (w0, w1, x["CPS"], y["CPS"])) else None
        segment_summary.append({"name": name, "CPS前窗": x["CPS"], "CPS本窗": y["CPS"], "首借金额份额前窗": w0, "首借金额份额本窗": w1,
                                "花费份额前窗": ratio(x["花费"], a["花费"]), "花费份额本窗": ratio(y["花费"], b["花费"]),
                                "结构贡献": structural, "组内贡献": within, "CPS变化贡献": structural + within if structural is not None and within is not None else None})
    segment_complete = bool(names) and all(sum(r[k] for r in period) and abs(sum(r[k] for r in period) - total[k]) <= max(1, total[k] * .005)
                                           for period, total in ((previous_segments, aggregate(previous)), (segments, aggregate(current))) for k in ("花费", "首借金额"))
    cost_drivers = {"花费": math.log(b["花费"] / a["花费"]), "授信数": -math.log(b["授信数"] / a["授信数"])}
    unit_drivers = {"平均授信额度": -math.log(b["平均授信额度"] / a["平均授信额度"]), "额度使用率": -math.log(b["额度使用率"] / a["额度使用率"]), "动支率": -math.log(b["动支率"] / a["动支率"])}
    draw_drivers = {"发起率": -math.log(b["发起率"] / a["发起率"]), "发起动支率": -math.log(b["发起动支率"] / a["发起动支率"])}
    drivers = cost_drivers if major == "成本侧" else unit_drivers
    top_driver = max(drivers, key=lambda key: abs(drivers[key]))
    top_segment = max(segment_summary, key=lambda item: abs(item["CPS变化贡献"] or 0)) if segment_summary else None
    events, event_source = change_events(p0, c1, media)
    facts = [f"{media} CPS {a['CPS']:.4f} → {b['CPS']:.4f}（{b['CPS']/a['CPS']-1:+.1%}）", f"授信成本 {a['授信成本']:.0f} → {b['授信成本']:.0f} 元；授信单产 {a['授信单产']:.0f} → {b['授信单产']:.0f} 元", f"CPS 绝对变化 {delta_cps:+.4f}：成本侧 {contribution_absolute['成本侧']:+.4f}，单产侧 {contribution_absolute['单产侧']:+.4f}。"]
    if contribution["成本侧"] is not None:
        facts.append(f"对应净变化占比：成本侧 {contribution['成本侧']:+.1%}，单产侧 {contribution['单产侧']:+.1%}；相互抵消时占比可超过 100%。")
    facts.append("完整环节核查：" + "；".join(f"{key} {a[key]:.4f} → {b[key]:.4f}（{signed(a[key], b[key])}）" for key in ("cpcl", "授信成本", "授信单产", "平均授信额度", "额度使用率")))
    applications = sum(r["申完数"] for r in current)
    quality_alert = applications >= 30 and b["安全过件率"] is not None and b["安全过件率"] < .17
    safety_before = f"{a['安全过件率']:.1%}" if a["安全过件率"] is not None else "无有效分母"
    safety_after = f"{b['安全过件率']:.1%}" if b["安全过件率"] is not None else "无有效分母"
    facts.append(f"同步核查质量：安全过件率 {safety_before} → {safety_after}；本窗申完 {applications:.0f}；"
                 + ("低于 17% 阈值" if quality_alert else "未低于 17% 阈值" if applications >= 30 else "样本不足，不能判定越线"))
    if quality_alert:
        if media == "腾讯":
            conversions = sum(r["转化数"] for r in current)
            bid_cost = sum(r["转化数"] * r["基础出价"] for r in current)
            billing = ratio(sum(r["花费"] for r in current), bid_cost)
            if billing is not None and conversions >= 10:
                facts.append(f"质量分支：腾讯模拟计费比 {billing:.3f}（{'高于' if billing > 1.52 else '未高于'} 1.52）；仅作线索，责任待核实际 Bid 与转化数")
            else:
                facts.append("质量分支：腾讯计费比样本或出价不足，不能判定")
        else:
            facts.append("质量分支：抖音不适用计费比；改查 CPM、CTR、回传及申完到安全授信漏斗")
    if major == "成本侧":
        facts.append(f"成本侧继续拆解：花费 {a['花费']:,.0f} → {b['花费']:,.0f} 元（{signed(a['花费'], b['花费'])}），授信数 {a['授信数']:,.0f} → {b['授信数']:,.0f}（{signed(a['授信数'], b['授信数'])}）；主要算术项为{top_driver}。")
    else:
        facts.append(f"单产侧继续拆解：平均额度 {signed(a['平均授信额度'], b['平均授信额度'])}、额度使用率 {signed(a['额度使用率'], b['额度使用率'])}、动支率 {signed(a['动支率'], b['动支率'])}；主要算术项为{top_driver}。")
        if top_driver == "动支率":
            facts.append(f"动支率再拆解：发起率 {signed(a['发起率'], b['发起率'])}，发起→动支率 {signed(a['发起动支率'], b['发起动支率'])}。")
    claims = [{"status": "支持", "text": f"{major}为本窗口 CPS 变化的主要算术贡献，沿恒等式下钻指向{top_driver}", "confidence": "高"}, {"status": "待验证", "text": "同期变更与 CPS 变化的因果关系；仅凭时间吻合不能确认", "confidence": "低"}]
    if quality_alert:
        claims.append({"status": "支持", "text": "安全过件率同时低于 17% 阈值，应与 CPS 一起排查；目前不能证明共同根因", "confidence": "中"})
    if top_segment and segment_complete:
        facts.append(f"{segment_type}中 {top_segment['name']} 对大盘 CPS 变化贡献 {top_segment['CPS变化贡献']:+.4f}，其中结构项 {top_segment['结构贡献']:+.4f}、组内项 {top_segment['组内贡献']:+.4f}。")
        display_dimension = "账户组" if segment_type == "账号" else segment_type
        claims.append({"status": "支持", "text": f"{top_segment['name']} 是该切分下绝对贡献最大的{display_dimension}；份额权重按首借金额计算", "confidence": "中"})
    if not segment_complete:
        claims.append({"status": "待验证", "text": "分组切分未与渠道大盘对齐，结构贡献暂不用于决策", "confidence": "低"})
    segment_dimensions = {segment_type: {"aligned": segment_complete, "groups": segment_summary}}
    segment_evidence = [source("segment", previous_segments + segments, p0, c1, {"media": media, "segment_type": segment_type})]
    if media == "腾讯":
        for dimension in ("版位",):
            old_rows = rows("segment", p0, p1, media=media, segment_type=dimension)
            new_rows = rows("segment", c0, c1, media=media, segment_type=dimension)
            aligned = bool(old_rows and new_rows) and all(abs(sum(r[key] for r in period) - total[key]) <= max(1, total[key] * .005)
                for period, total in ((old_rows, aggregate(previous)), (new_rows, aggregate(current)))
                for key in ("花费", "首借金额", "申完数", "安全授信数"))
            groups = []
            for name in sorted({r["segment_name"] for r in old_rows + new_rows}):
                old = metrics(aggregate([r for r in old_rows if r["segment_name"] == name]))
                new = metrics(aggregate([r for r in new_rows if r["segment_name"] == name]))
                groups.append({"name": name, "CPS前窗": old["CPS"], "CPS本窗": new["CPS"],
                    "安全过件率前窗": old["安全过件率"], "安全过件率本窗": new["安全过件率"],
                    "申完数本窗": sum(r["申完数"] for r in new_rows if r["segment_name"] == name)})
            movements = [g["CPS本窗"] / g["CPS前窗"] for g in groups if g["CPS前窗"] not in (None, 0) and g["CPS本窗"] is not None]
            separable = len(movements) >= 2 and not all(math.isclose(value, movements[0], rel_tol=1e-4, abs_tol=1e-4) for value in movements[1:])
            segment_dimensions[dimension] = {"aligned": aligned, "separable": separable, "groups": groups}
            segment_evidence.append(source("segment", old_rows + new_rows, p0, c1, {"media": media, "segment_type": dimension}))
            if aligned and not separable:
                facts.append("腾讯版位切分虽与大盘对齐，但各版位 CPS 变动几乎同幅，无法定位某个版位独立拖累。")
            elif aligned:
                worsening = [g for g in groups if g["CPS前窗"] and g["CPS本窗"] and g["CPS本窗"] > g["CPS前窗"]]
                worsening.sort(key=lambda g: g["CPS本窗"] - g["CPS前窗"], reverse=True)
                if worsening:
                    g = worsening[0]
                    facts.append(f"版位下钻：{g['name']} CPS {g['CPS前窗']:.4f} → {g['CPS本窗']:.4f}；安全过件率 {g['安全过件率前窗']:.1%} → {g['安全过件率本窗']:.1%}。")
            else:
                claims.append({"status": "待验证", "text": "腾讯版位切分未与大盘对齐，不能用于定位", "confidence": "低"})
    if events:
        facts.append(f"比较期同渠道有 {len(events)} 条变更记录；需逐项核对实际生效与影响范围")
    owner = "风险 / 额度策略" if top_driver == "平均授信额度" else "产品 / 资金 / 研发" if top_driver in {"动支率", "发起率", "发起动支率"} else "获客运营 / 获客策略"
    target = top_segment["name"] if top_segment and segment_complete else media
    actions = [{"owner": owner, "when": "下一投放日", "action": f"先复核 {target} 的{top_driver}原始口径和同期变更；仅在核验后对该对象做小范围可逆调整", "expected": f"确认{top_driver}能否解释 CPS 异动，同时保留其他流量规模", "observe": "调整后连续 7 个成熟 cohort", "rollback": "CPS 未回到前窗或安全过件率下降时撤回调整"}]
    if b["动支数"] < 8:
        actions = [{"owner": "数据分析", "when": "下一成熟窗口", "action": "核对首借金额与动支样本，待动支达到 8 户后重算 CPS 和分组贡献", "expected": "避免小样本 CPS 波动引起误调价", "observe": "下一个成熟 cohort 窗口", "rollback": "若样本仍不足则维持现有配置"}]
    return (base("COST_ANOMALY", f"{media}近 {days} 天 CPS 分析", c1,
                [source("media", previous + current, p0, c1, {"media": media}), *segment_evidence, event_source],
                facts, claims, actions,
                [{"type": "bar", "title": "CPS 绝对变化贡献", "labels": list(contribution_absolute), "values": list(contribution_absolute.values())}, {"type": "bar", "title": "CPS 窗口比较", "labels": ["前窗", "本窗"], "values": [a["CPS"], b["CPS"]]}],
                [f"哪些{'账户组' if segment_type == '账号' else segment_type}拖累了 CPS？", "授信单产具体是额度还是动支率变化？", "那另一个渠道呢？"], strength="高", complete=segment_complete, sample_count=int(b["动支数"]), sample_min=8, limitations=["对数分解及分组结构拆分为算术贡献，不等于变更的因果贡献。"])
                | {"periods": {"previous": [str(p0), str(p1)], "current": [str(c0), str(c1)]}, "metrics": {"previous": a, "current": b}, "contribution": contribution, "contribution_absolute": contribution_absolute, "factor_contribution": {"成本侧": cost_drivers, "单产侧": unit_drivers, "动支率": draw_drivers}, "primary_factor": top_driver, "drilldown": drill, "quality_alert": quality_alert, "segments": segment_summary, "segment_type": segment_type, "segment_dimensions": segment_dimensions, "events": events})


def scale(media: str = "抖音", days: int = 7, end: date = AS_OF) -> dict:
    p0, p1, c0, c1 = windows(days, end)
    prev, curr = rows("media", p0, p1, media=media), rows("media", c0, c1, media=media)
    if len(prev) != days or len(curr) != days:
        raise Blocked("媒体日级数据不完整")
    x, y = aggregate(prev), aggregate(curr)
    a, b = metrics(x), metrics(y)
    daily_budget_a = sum(r["日预算"] for r in prev)
    daily_budget_b = sum(r["日预算"] for r in curr)
    delivery_type = "RTA策略" if media == "腾讯" else "账号"
    delivery_a = rows("delivery", p0, p1, media=media, segment_type=delivery_type)
    delivery_b = rows("delivery", c0, c1, media=media, segment_type=delivery_type)
    delivery_complete = bool(delivery_a and delivery_b) and len({r["date"] for r in delivery_a}) == days and len({r["date"] for r in delivery_b}) == days
    def weighted(items, key):
        weights = sum(r["消耗"] for r in items)
        return ratio(sum(r[key] * r["消耗"] for r in items), weights)
    compare = {key: [weighted(delivery_a, key), weighted(delivery_b, key)] if delivery_complete else [None, None]
               for key in ("基础出价", "pCTR", "pCVR", "参竞率", "回传比例")}
    events, event_source = change_events(p0, c1, media)
    trend = rows("media", max(date(2026, 7, 29), c0 - timedelta(days=7)), c1, media=media)
    baseline_daily = x["花费"] / days
    onset = next((trend[i]["date"] for i in range(len(trend) - 1)
                  if trend[i]["date"] >= c0.isoformat() and trend[i]["花费"] < baseline_daily * .9 and trend[i + 1]["花费"] < baseline_daily * .9), None)
    recent_creative = rows("creative", c0, c1, media=media) if media == "抖音" else []
    previous_creative = rows("creative", p0, p1, media=media) if media == "抖音" else []
    creative_ctr = [ratio(sum(r["点击"] for r in period), sum(r["曝光"] for r in period)) for period in (previous_creative, recent_creative)]
    account_summary = []
    for name in sorted({r["segment_name"] for r in delivery_a + delivery_b}) if delivery_complete else []:
        old = [r for r in delivery_a if r["segment_name"] == name]
        new = [r for r in delivery_b if r["segment_name"] == name]
        if old and new:
            account_summary.append({"name": name, "daily_spend_before": daily_average(old, "消耗"), "daily_spend_after": daily_average(new, "消耗"),
                                    "daily_spend_delta": daily_average(new, "消耗") - daily_average(old, "消耗"),
                                    "pCTR_before": weighted(old, "pCTR"), "pCTR_after": weighted(new, "pCTR"),
                                    "pCVR_before": weighted(old, "pCVR"), "pCVR_after": weighted(new, "pCVR")})
    top_account = min(account_summary, key=lambda item: item["daily_spend_delta"]) if account_summary else None
    ecpm_proxy = [math.prod(compare[key][i] for key in ("基础出价", "pCTR", "pCVR")) for i in (0, 1)] if delivery_complete else [None, None]
    spend_change = ratio(y["花费"], x["花费"]) - 1 if x["花费"] else None
    budget_util = [ratio(x["花费"], daily_budget_a), ratio(y["花费"], daily_budget_b)]
    claims = []
    claims.append({"status": "排除" if budget_util[1] is not None and budget_util[1] < .9 and daily_budget_b >= daily_budget_a else "待验证", "text": "渠道总预算撞线", "confidence": "中"})
    bid_change = ratio(*reversed(compare["基础出价"])) - 1 if all(compare["基础出价"]) else None
    claims.append({"status": "排除" if bid_change is not None and bid_change >= -.02 else "待验证", "text": "基础出价下调", "confidence": "中"})
    for metric in ("pCTR", "pCVR", "参竞率", "回传比例"):
        before, after = compare[metric]
        change = after / before - 1 if before else None
        claims.append({"status": "支持" if change is not None and change < -.05 else "排除" if change is not None else "待验证", "text": f"{metric} 下滑：{before:.4f} → {after:.4f}" if before is not None and after is not None else f"{metric} 数据不足", "confidence": "中"})
    claims.append({"status": "待验证", "text": "媒体全市场流量供给变化；当前没有媒体外部竞价数据", "confidence": "低"})
    facts = [f"{media} 日均消耗 {x['花费']/days:,.0f} → {y['花费']/days:,.0f} 元（{spend_change:+.1%}）", f"渠道预算消耗进度 {budget_util[0]:.1%} → {budget_util[1]:.1%}；曝光 {x['曝光']:,.0f} → {y['曝光']:,.0f}"]
    if delivery_complete:
        facts.append(f"{delivery_type}级消耗加权 pCTR {compare['pCTR'][0]:.4f} → {compare['pCTR'][1]:.4f}（{signed(*compare['pCTR'])}）；pCVR {compare['pCVR'][0]:.4f} → {compare['pCVR'][1]:.4f}（{signed(*compare['pCVR'])}）。")
        facts.append(f"Bid×pCTR×pCVR 竞价能力代理量 {ecpm_proxy[0]:.6f} → {ecpm_proxy[1]:.6f}（{signed(*ecpm_proxy)}）；这不是媒体实际 eCPM。")
    else:
        facts.append(f"{delivery_type}级竞价明细未覆盖完整前后窗口，仍可比较渠道级消耗、预算和曝光；竞价链路原因暂不能判定。")
    if onset:
        facts.append(f"以基期日均消耗的 90% 为判据，连续两日低于该线的起点为 {onset}。")
    if top_account:
        facts.append(f"{delivery_type}下钻：{top_account['name']} 日均消耗 {top_account['daily_spend_before']:,.0f} → {top_account['daily_spend_after']:,.0f} 元，为下降最多的{delivery_type}。")
    if all(v is not None for v in creative_ctr):
        facts.append(f"素材合计 CTR {creative_ctr[0]:.2%} → {creative_ctr[1]:.2%}；需结合上线时间与投放份额判断素材疲劳。")
        if creative_ctr[1] < creative_ctr[0] * .95:
            claims.append({"status": "支持", "text": "素材 CTR 与媒体 pCTR 同向下降，素材吸引力减弱是候选机制", "confidence": "中"})
    budget_events = [r for r in events if r["变更类型"] == "预算调整"]
    if budget_events:
        facts.append(f"变更日志有 {len(budget_events)} 条预算调整；渠道总预算进度不能代替账号实际预算配置。")
    if events:
        facts.append(f"比较期记录 {len(events)} 条相关变更，时间关联不能单独证明影响")
    target = top_account["name"] if top_account else media
    if spend_change is not None and spend_change >= 0:
        action = f"保持 {media} 渠道当前预算与出价；核对 {target} 的消耗变化是否被其他{delivery_type}承接，不按渠道掉量处理"
        expected, observe, rollback = "确认渠道大盘与分组结构变化是否一致", "下一 3–7 个完整日", "若日均消耗转为下降且安全过件率同步走低，再重新评估投放配置"
        owner = "获客运营"
    elif top_account:
        action = f"核对 {target} 的实际预算、素材上线、pCTR 和回传日级变化；证据一致后在该对象做小流量素材替换"
        expected, observe, rollback = "核验原因并恢复受影响对象的 CTR、曝光与日均消耗", "调整后 3–7 天", "CTR 或安全过件率继续下降时撤回替换"
        owner = "获客运营、素材团队"
    else:
        action = f"补齐 {media} 的{delivery_type}级竞价链路明细，并先复核渠道预算、曝光与消耗变化；明细补齐前不作竞价原因归因"
        expected, observe, rollback = "补齐可用的竞价链路证据，确定消耗变化原因", "补数后复核前后等长窗口", "若分组消耗与渠道花费不一致则停止下钻归因"
        owner = "获客运营 / 数据研发"
    charts = [{"type": "line", "title": "日消耗趋势", "labels": [r["date"] for r in trend], "values": [r["花费"] for r in trend]}]
    if delivery_complete:
        charts.append({"type": "bar", "title": "竞价链路前后比较", "labels": ["pCTR 前", "pCTR 后", "pCVR 前", "pCVR 后"], "values": compare["pCTR"] + compare["pCVR"]})
    limitations = ["缺少媒体全市场流量与竞得日志，无法证明媒体流量供给下降。"]
    limitations.append("分组指标为消耗加权均值，只能作为机制线索，不能视为请求级 eCPM。" if delivery_complete else
                       f"{delivery_type}级竞价明细缺失或缺日，无法核查 pCTR、pCVR、参竞率和回传链路。")
    return (base("SCALE_ANOMALY", f"{media}近 {days} 天消耗分析", c1,
                [source("media", prev + curr, p0, c1, {"media": media}), source("delivery", delivery_a + delivery_b, p0, c1, {"media": media, "segment_type": delivery_type}) if delivery_complete else None, source("creative", previous_creative + recent_creative, p0, c1, {"media": media}) if recent_creative else None, event_source],
                facts, claims, [{"owner": owner, "when": "下一投放日", "action": action, "expected": expected, "observe": observe, "rollback": rollback}],
                charts,
                ["哪天开始明显掉量？", f"哪些{'账户组' if delivery_type == '账号' else delivery_type}和素材拖累最多？", f"回传异常发生在哪个{'账户组' if delivery_type == '账号' else delivery_type}？"], sample_count=len(curr), sample_min=3, complete=delivery_complete, limitations=limitations)
                | {"metrics": {"previous": a, "current": b}, "delivery": compare, "budget_utilization": budget_util, "ecpm_proxy": ecpm_proxy, "onset": onset, "accounts": account_summary, "creative_ctr": creative_ctr, "events": events})


def value(media: str = "腾讯", days: int = 30, end: date = AS_OF, strategy: str = "腾讯RTA策略_C") -> dict:
    start = end - timedelta(days=days - 1)
    market_rows = rows("media", start, end, media=media)
    strategy_rows = rows("segment", start, end, media=media, segment_type="RTA策略", segment_name=strategy)
    if len(market_rows) != days or len(strategy_rows) != days:
        raise Blocked("价值判断所需的大盘或策略数据缺失")
    market, group = aggregate(market_rows), aggregate(strategy_rows)
    remainder = {k: market[k] - group[k] for k in market}
    if any(v < -1e-5 for v in remainder.values()):
        raise Blocked("策略分组超过媒体大盘，无法做剔除测算")
    a, b, c = metrics(market), metrics(group), metrics(remainder)
    quality = rows("quality", start, end, media=media, segment_type="RTA策略", segment_name=strategy)
    daily_comparison = []
    for market_day in market_rows:
        group_day = next((r for r in strategy_rows if r["date"] == market_day["date"]), None)
        if not group_day:
            continue
        g = metrics(aggregate([group_day]))
        rest = {key: market_day[key] - group_day[key] for key in group}
        r = metrics(rest)
        daily_comparison.append({"date": market_day["date"], "strategy": g, "remainder": r})
    stable = {"CPS更高天数": sum(x["strategy"]["CPS"] > x["remainder"]["CPS"] for x in daily_comparison if x["strategy"]["CPS"] is not None and x["remainder"]["CPS"] is not None),
              "安全率更高天数": sum(x["strategy"]["安全过件率"] > x["remainder"]["安全过件率"] for x in daily_comparison if x["strategy"]["安全过件率"] is not None and x["remainder"]["安全过件率"] is not None),
              "单产更高天数": sum(x["strategy"]["授信单产"] > x["remainder"]["授信单产"] for x in daily_comparison if x["strategy"]["授信单产"] is not None and x["remainder"]["授信单产"] is not None)}
    facts = [f"{strategy} CPS {b['CPS']:.4f}，{media}大盘 {a['CPS']:.4f}", f"安全过件率 {b['安全过件率']:.1%} vs {a['安全过件率']:.1%}；授信单产 {b['授信单产']:,.0f} vs {a['授信单产']:,.0f} 元", f"直接剔除情景：少花费 {group['花费']:,.0f} 元，同时少首借金额 {group['首借金额']:,.0f} 元；剩余大盘 CPS {c['CPS']:.4f}"]
    facts.append(f"与剔除该策略后的其余流量相比：CPS 更高 {stable['CPS更高天数']}/{len(daily_comparison)} 天、安全过件率更高 {stable['安全率更高天数']}/{len(daily_comparison)} 天、授信单产更高 {stable['单产更高天数']}/{len(daily_comparison)} 天。")
    facts.append(f"该策略贡献首借金额 {group['首借金额'] / market['首借金额']:.1%}，安全首借金额 {group['安全首借金额'] / market['安全首借金额']:.1%}；直接剔除不代表其他流量会自动补足。")
    mature_vintages = [r for r in quality if date.fromisoformat(r["date"]) <= AS_OF - timedelta(days=30) and r.get("Vintage30逾期率") not in ("", None)]
    claims = [{"status": "支持", "text": "策略流量的短期成本、质量和交易价值可量化比较", "confidence": "中"}, {"status": "待验证", "text": "剔除后其余流量能否补足规模，以及长期风险收益", "confidence": "低"}]
    if not mature_vintages:
        claims.append({"status": "待验证", "text": "截至模拟观察日没有成熟 Vintage30 cohort；不能判断长期逾期表现", "confidence": "低"})
    return (base("TRAFFIC_VALUE", f"{strategy} 近 {days} 天价值判断", end,
                [source("media", market_rows, start, end, {"media": media}), source("segment", strategy_rows, start, end, {"media": media, "segment_type": "RTA策略", "segment_name": strategy}), source("quality", quality, start, end, {"media": media, "segment_name": strategy})],
                facts, claims, [{"owner": "获客运营、风险策略", "when": "下一次预算评审", "action": "保持当前策略量级，抽取成熟 Vintage30 cohort 与其余 RTA 策略做同口径复核", "expected": "在验证长期质量前保留当前首借规模", "observe": "至少 30 天成熟观察", "rollback": "成熟逾期率显著高于同渠道可比策略时缩量复核"}],
                [{"type": "bar", "title": "CPS 比较", "labels": [strategy, "大盘", "剔除后"], "values": [b["CPS"], a["CPS"], c["CPS"]]}, {"type": "bar", "title": "安全过件率", "labels": [strategy, "大盘", "剔除后"], "values": [b["安全过件率"], a["安全过件率"], c["安全过件率"]]}],
                ["剔除后哪些指标变化最大？", "Vintage 成熟后如何复核？", "这部分流量的优势稳定吗？"], strength="中", sample_count=int(b["动支数"]), sample_min=8, limitations=["剔除测算是静态情景，不包含预算重新分配后的流量替代。", "未成熟 Vintage30 不能支持长期风险收益结论。"])
                | {"metrics": {"market": a, "strategy": b, "without_strategy": c}, "stability": stable, "days_compared": len(daily_comparison), "counterfactual": {"saved_spend": group["花费"], "lost_amount": group["首借金额"], "lost_safe_amount": group["安全首借金额"]}})


def strategy(media: str = "腾讯", end: date = AS_OF) -> dict:
    changes = [r for r in load("change_log") if r["变更类型"] == "RTA出价系数调整" and media in r["变更对象"] and "3W2Q" in r["变更对象"] and r["变更日期"] <= end.isoformat()]
    if not changes:
        raise Blocked("缺少 3W2Q 系数变更记录，无法确认策略是否执行")
    change = changes[-1]
    day = date.fromisoformat(change["变更日期"])
    before = (day - timedelta(days=6), day - timedelta(days=1))
    after = (day + timedelta(days=2), min(end, day + timedelta(days=7)))
    if (after[1] - after[0]).days + 1 < 3:
        raise Blocked("过渡期后观察窗口不足 3 天")
    exp = load("strategy_experiment")
    def group(name, period):
        r = [x for x in exp if x["media"] == media and x["分组"] == name and period[0].isoformat() <= x["date"] <= period[1].isoformat()]
        if len(r) != (period[1] - period[0]).days + 1:
            raise Blocked(f"{name}实验数据缺日")
        return r, metrics(aggregate(r))
    ep, e0 = group("实验组", before)
    ea, e1 = group("实验组", after)
    cp, c0 = group("对照组", before)
    ca, c1 = group("对照组", after)
    coeff_before = {r["RTA出价系数"] for r in ep}
    coeff_after = {r["RTA出价系数"] for r in ea}
    if not coeff_before or not coeff_after or max(coeff_after) <= max(coeff_before):
        raise Blocked("实验表未确认 3W2Q 系数实际提高；停止效果复盘")
    n0, n1 = len(ep), len(ea)
    amount_day = [e0["首借金额"] / n0, e1["首借金额"] / n1]
    control_day = [c0["首借金额"] / len(cp), c1["首借金额"] / len(ca)]
    did = (amount_day[1] - amount_day[0]) - (control_day[1] - control_day[0])
    relative = ratio(ratio(amount_day[1], amount_day[0]), ratio(control_day[1], control_day[0]))
    half = len(ep) // 2
    early_e = daily_average(ep[:half], "首借金额")
    late_e = daily_average(ep[half:], "首借金额")
    early_c = daily_average(cp[:half], "首借金额")
    late_c = daily_average(cp[half:], "首借金额")
    pretrend_gap = (late_e - early_e) - (late_c - early_c)
    events, event_source = change_events(before[0], after[1], media)
    concurrent = [event for event in events if event["变更类型"] != "RTA出价系数调整"]
    facts = [f"{media} 3W2Q 系数 {min(coeff_before):.2f} → {max(coeff_after):.2f}；前窗 {before[0]}–{before[1]}，后窗 {after[0]}–{after[1]}，剔除过渡期", f"实验组日均首借金额 {amount_day[0]:,.0f} → {amount_day[1]:,.0f} 元；对照组 {control_day[0]:,.0f} → {control_day[1]:,.0f} 元", f"加法 DID {did:+,.0f} 元/日；相对变化之比 {relative:.3f}；CPS {e0['CPS']:.4f} → {e1['CPS']:.4f}；安全过件率 {e0['安全过件率']:.1%} → {e1['安全过件率']:.1%}"]
    facts.append(f"变更前各 3 天日均首借金额的组间趋势差变化 {pretrend_gap:+,.0f} 元/日；仅 6 天前窗不足以证实平行趋势。")
    facts.append(f"比较窗口另有 {len(concurrent)} 条同渠道变更；实验组后窗动支 {e1['动支数']:.0f} 户、对照组 {c1['动支数']:.0f} 户。")
    claims = [{"status": "支持", "text": "系数变更记录与实验表生效值一致", "confidence": "高"}, {"status": "待验证", "text": "客群级对照非随机分桶；平行趋势、流量挤出和同期差异化变更尚不能排除", "confidence": "低"}]
    return (base("STRATEGY_REVIEW", f"{media} 3W2Q 提价复盘", after[1],
                [source("strategy_experiment", ep + ea + cp + ca, before[0], after[1], {"media": media, "groups": "实验组/对照组"}), event_source],
                facts, claims, [{"owner": "获客策略与获客运营", "when": "下轮投放前", "action": "保持现有系数，设计设备级随机 Control/Treatment 小流量实验；核心看首借金额，保护 CPS 与安全过件率", "expected": "确认增量是否可归因于系数调整", "observe": "至少 7 个成熟 cohort", "rollback": "Treatment CPS 高于 Control 且安全过件率下降时回滚"}],
                [{"type": "bar", "title": "实验组与对照组日均首借金额", "labels": ["实验前", "实验后", "对照前", "对照后"], "values": amount_day + control_day}],
                ["同期还有哪些变更？", "下一轮随机实验怎么分桶？", "实验前趋势是否可比？"], strength="中", sample_count=int(e1["动支数"]), sample_min=8, limitations=["3W2Q 与 3W3Q 为不同客群，DID 只是观察性对照校正，不能直接解释为随机实验因果效应。"])
                | {"metrics": {"experiment_before": e0, "experiment_after": e1, "control_before": c0, "control_after": c1}, "did_daily_amount": did, "relative_change_ratio": relative, "pretrend_gap_daily_amount": pretrend_gap, "concurrent_events": concurrent, "events": events, "change": change})


ANALYZERS = {"COST_ANOMALY": cost, "SCALE_ANOMALY": scale, "TRAFFIC_VALUE": value, "STRATEGY_REVIEW": strategy}


def run(intent: str, slots: dict) -> dict:
    if intent not in ANALYZERS:
        raise Blocked("无法确定分析任务类型")
    args = {"media": slots.get("media") or ("抖音" if intent == "SCALE_ANOMALY" else "腾讯")}
    if intent in {"COST_ANOMALY", "SCALE_ANOMALY", "TRAFFIC_VALUE"}:
        args["days"] = int(slots.get("days") or (30 if intent == "TRAFFIC_VALUE" else 7))
    if slots.get("end"):
        args["end"] = date.fromisoformat(slots["end"])
    if intent == "TRAFFIC_VALUE" and slots.get("strategy"):
        args["strategy"] = slots["strategy"]
    result = ANALYZERS[intent](**args)
    result["evidence"] = [s for s in result["evidence"] if s]
    return result
