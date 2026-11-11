"""Answer supported drilldowns from the stored, traceable analysis result."""
from __future__ import annotations

from copy import deepcopy


def answer(previous: dict, query: str) -> dict | None:
    source = previous.get("result")
    if not source:
        return None
    intent = source.get("intent")
    facts: list[str] = []
    title = ""
    if intent == "COST_ANOMALY" and any(x in query for x in ("拖累", "子组", "账号", "账户组", "RTA策略")):
        segments = sorted(source.get("segments", []), key=lambda x: x.get("CPS变化贡献") or 0, reverse=True)
        facts = [f"{x['name']}：CPS {x['CPS前窗']:.4f} → {x['CPS本窗']:.4f}；大盘变化贡献 {x['CPS变化贡献']:+.4f}（结构 {x['结构贡献']:+.4f}，组内 {x['组内贡献']:+.4f}）" for x in segments[:4] if x.get("CPS变化贡献") is not None]
        title = f"{('账户组' if source.get('segment_type') == '账号' else source.get('segment_type', '账户组'))}贡献下钻"
    elif intent == "COST_ANOMALY" and any(x in query for x in ("额度", "动支率", "单产")):
        drill = source.get("drilldown", {})
        facts = [f"{name}：{drill[name]['前窗']:.4f} → {drill[name]['本窗']:.4f}（{drill[name]['变化率']:+.1%}）" for name in ("平均授信额度", "额度使用率", "动支率", "发起率", "发起动支率") if name in drill and drill[name]["变化率"] is not None]
        title = "授信单产恒等式下钻"
    elif intent == "SCALE_ANOMALY" and any(x in query for x in ("哪天", "何时", "开始")):
        onset = source.get("onset")
        facts = [f"连续两日低于前窗日均消耗 90% 的首日：{onset}。" if onset else "当前窗口没有连续两日低于前窗日均消耗 90% 的时点。"]
        title = "掉量起点"
    elif intent == "SCALE_ANOMALY" and any(x in query for x in ("账号", "账户组", "素材")):
        accounts = sorted(source.get("accounts", []), key=lambda x: x["daily_spend_delta"])
        facts = [f"{x['name']}：日均消耗 {x['daily_spend_before']:,.0f} → {x['daily_spend_after']:,.0f} 元，变化 {x['daily_spend_delta']:+,.0f} 元；pCTR {x['pCTR_before']:.4f} → {x['pCTR_after']:.4f}" for x in accounts[:4]]
        ctr = source.get("creative_ctr", [])
        if len(ctr) == 2 and all(v is not None for v in ctr):
            facts.append(f"素材合计 CTR {ctr[0]:.2%} → {ctr[1]:.2%}；缺少逐素材贡献分解，不能确认具体哪条素材拖累。")
        title = "账户组与素材下钻"
    elif intent == "TRAFFIC_VALUE" and any(x in query for x in ("剔除", "去掉")):
        m = source.get("metrics", {})
        c = source.get("counterfactual", {})
        if m and c:
            facts = [f"静态剔除后 CPS {m['market']['CPS']:.4f} → {m['without_strategy']['CPS']:.4f}；安全过件率 {m['market']['安全过件率']:.1%} → {m['without_strategy']['安全过件率']:.1%}。", f"账面可减少花费 {c['saved_spend']:,.0f} 元，同时失去首借金额 {c['lost_amount']:,.0f} 元、安全首借金额 {c['lost_safe_amount']:,.0f} 元。"]
        title = "静态剔除测算"
    elif intent == "TRAFFIC_VALUE" and any(x in query for x in ("稳定", "波动")):
        s = source.get("stability", {})
        n = source.get("days_compared", 0)
        facts = [f"相对其余流量：CPS 更高 {s['CPS更高天数']}/{n} 天，安全过件率更高 {s['安全率更高天数']}/{n} 天，授信单产更高 {s['单产更高天数']}/{n} 天。"] if s and n else []
        title = "价值优势日级稳定性"
    elif intent == "STRATEGY_REVIEW" and any(x in query for x in ("同期", "变更")):
        events = source.get("events", [])
        facts = [f"{x['变更日期']}｜{x['变更类型']}｜{x['变更对象']}" for x in events[:8]] or ["比较期没有记录同渠道变更。"]
        title = "同期变更核对"
    elif intent == "STRATEGY_REVIEW" and any(x in query for x in ("趋势", "可比")):
        gap = source.get("pretrend_gap_daily_amount")
        facts = [f"变更前 6 天分成前后各 3 天，实验与对照的日均首借金额趋势差变化 {gap:+,.0f} 元/日。", "前窗过短，不能据此确认平行趋势；需更长历史和随机分桶。"] if gap is not None else []
        title = "实验前趋势检查"
    elif intent == "STRATEGY_REVIEW" and any(x in query for x in ("分桶", "随机实验")):
        facts = ["建议同一媒体内按设备 ID 稳定哈希随机分成 Control 与 Treatment，组间只改变 3W2Q 出价系数。", "事先锁定核心指标：成熟 cohort 日均首借金额；保护指标：CPS、安全过件率。至少观察 7 个成熟 cohort，并检查组间基线与流量挤出。"]
        title = "下一轮实验设计"
    if not facts:
        return None
    result = deepcopy(source)
    result["title"] = title
    result["facts"] = facts
    result["summary"] = facts[0]
    result["claims"] = [{"status": "支持", "text": "以上为原分析已保存的数据计算结果", "confidence": "中"}, {"status": "待验证", "text": "分组变化与业务事件的因果关系", "confidence": "低"}]
    result["charts"] = []
    result["next_questions"] = []
    result["followup_of"] = previous["id"]
    return result
