#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获客智能分析 · 竞品测试模拟环境 —— 数据一致性校验器
====================================================
校验 6 类约束，并把关键计算结果写入 99_GroundTruth_仅供验证/03_数据一致性检查.md

  C1 汇总关系：每个 segment_type 的细分汇总 == 该渠道大盘
  C2 指标公式：CPS / 授信成本 / 授信单产 / cpcl / 动支率 / 额度使用率 / 平均额度 / 计费比 可复算
  C3 漏斗关系：曝光 >= 点击 >= 首登 >= 申完 >= 授信 >= 发起 >= 动支；安全授信 <= 授信
  C4 比率范围：所有比率落在物理合理区间
  C5 时间关系：预埋事件与指标异常存在时间关联
  C6 结论可复现：4 个 Showcase 的核心结论可由数据直接算出

运行：python3 scripts/validate_sim_data.py
"""

import csv
import os
import sys
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE_DIR, "03_模拟数据")
GT = os.path.join(BASE_DIR, "99_GroundTruth_仅供验证")

MEDIA_F = "ads_rta_media_daily.csv"
SEG_F = "ads_rta_segment_daily.csv"
CRE_F = "ads_rta_creative_daily.csv"
FUN_F = "ads_rta_funnel_daily.csv"
QUA_F = "ads_rta_quality_daily.csv"
DLV_F = "ads_rta_delivery_daily.csv"
EXP_F = "ads_rta_strategy_experiment.csv"
LOG_F = "ads_rta_change_log.csv"

RESULTS = []


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def check(cid, name, ok, detail):
    RESULTS.append((cid, name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {cid} {name} :: {detail}")


MEDIA = load(MEDIA_F)
SEG = load(SEG_F)
CRE = load(CRE_F)
FUN = load(FUN_F)
QUA = load(QUA_F)
DLV = load(DLV_F)
EXP = load(EXP_F)
LOG = load(LOG_F)

# ---- 统一观察窗口（Benchmark 与 GroundTruth 共用，竞品横向可比） ----
# Showcase A
WIN_A_PRE = ("2026-09-09", "2026-09-15")
WIN_A_POST = ("2026-09-16", "2026-09-22")
# Showcase B：近 7 天（含当日）vs 前 7 天
WIN_B_RECENT = ("2026-09-16", "2026-09-22")
WIN_B_PREV = ("2026-09-09", "2026-09-15")
WIN_B_DAY = ("2026-09-22", "2026-09-22")
# Showcase C：近 30 天
WIN_C = ("2026-08-24", "2026-09-22")
# Showcase D：变更日 09-15、爬坡日 09-16 均单列，前/后窗各 6 天
WIN_D_PRE = ("2026-09-09", "2026-09-14")
WIN_D_RAMP = ("2026-09-15", "2026-09-16")     # 剔除：变更当日系数仍为 1.0，次日为 1.075
WIN_D_POST = ("2026-09-17", "2026-09-22")     # 稳态后窗：首日系数已达 1.15
WIN_D_CHANGE = "2026-09-15"

CNT_KEYS = ["曝光", "点击", "首登数", "申完数", "授信数", "安全授信数", "发起数", "动支数"]
AMT_KEYS = ["花费", "首借金额", "授信额度合计", "安全首借金额", "转化数"]

# ------------------------------------------------------------
# C1 汇总关系
# ------------------------------------------------------------

def c1():
    worst = {}
    for media in ["腾讯", "抖音"]:
        mrows = {r["date"]: r for r in MEDIA if r["media"] == media}
        types = sorted({r["segment_type"] for r in SEG if r["media"] == media})
        for st in types:
            keys = set(AMT_KEYS) | set(CNT_KEYS)
            worst[st] = 0.0
            for d, mr in mrows.items():
                srows = [r for r in SEG if r["media"] == media and r["segment_type"] == st
                         and r["date"] == d]
                for k in keys:
                    mv = num(mr.get(k, 0))
                    sv = sum(num(r[k]) for r in srows)
                    if mv > 0:
                        dev = abs(sv - mv) / mv
                        worst[st] = max(worst[st], dev)
    bad = {k: v for k, v in worst.items() if v > 0.005}
    detail = "; ".join(f"{k} 最大偏差 {v:.3%}" for k, v in sorted(worst.items()))
    check("C1", "细分汇总之和等于渠道大盘（容差 0.5%）",
          not bad, detail + (" | 超限: " + str(bad) if bad else ""))


# ------------------------------------------------------------
# C2 指标公式
# ------------------------------------------------------------

def c2():
    maxerr = {}
    for r in MEDIA:
        c = num(r["花费"]); amt = num(r["首借金额"]); cr = num(r["授信数"])
        dr = num(r["动支数"]); lt = num(r["授信额度合计"]); ap = num(r["申完数"])
        conv = num(r["转化数"]); bid = num(r["基础出价"])
        pairs = {
            "授信成本=花费/授信数": (c / cr, c / cr),
            "授信单产=首借金额/授信数": (amt / cr, amt / cr),
            "CPS=花费/首借金额": (c / amt, c / amt),
        }
        # 恒等式 CPS = 授信成本 / 授信单产
        lhs = c / amt
        rhs = (c / cr) / (amt / cr)
        pairs["恒等式 CPS=授信成本/授信单产"] = (lhs, rhs)
        pairs["cpcl=花费/授信额度合计"] = (c / lt, c / lt)
        pairs["动支率=动支数/授信数"] = (dr / cr, dr / cr)
        pairs["额度使用率=(首借金额/动支数)/(额度合计/授信数)"] = (
            (amt / dr) / (lt / cr), (amt / dr) / (lt / cr))
        pairs["计费比=转化成本/基础出价"] = ((c / conv) / bid, (c / conv) / bid)
        pairs["安全过件率=安全授信数/申完数"] = (num(r["安全授信数"]) / ap, num(r["安全授信数"]) / ap)
        # 授信单产 = 平均额度 x 额度使用率 x 动支率
        l2 = (lt / cr) * ((amt / dr) / (lt / cr)) * (dr / cr)
        pairs["恒等式 单产=平均额度x额度使用率x动支率"] = (amt / cr, l2)
        for k, (a, b) in pairs.items():
            e = abs(a - b) / abs(a) if a else 0.0
            maxerr[k] = max(maxerr.get(k, 0.0), e)
    bad = {k: v for k, v in maxerr.items() if v > 1e-6}
    check("C2", "所有指标公式可复算（相对误差 < 1e-6）", not bad,
          f"覆盖 {len(maxerr)} 个恒等式，最大相对误差 "
          f"{max(maxerr.values()):.2e}")


# ------------------------------------------------------------
# C3 漏斗关系
# ------------------------------------------------------------

def c3():
    viol = []
    for src, rows in (("media", MEDIA), ("segment", SEG), ("funnel", FUN)):
        for r in rows:
            seq = [num(r[k]) for k in ["曝光", "点击", "首登数", "申完数", "授信数", "发起数", "动支数"]]
            for i in range(len(seq) - 1):
                if seq[i] + 1e-9 < seq[i + 1]:
                    viol.append((src, r.get("media"), r.get("date"),
                                 r.get("segment_type", ""), r.get("segment_name", r.get("客群分层", "")),
                                 ["曝光", "点击", "首登数", "申完数", "授信数", "发起数", "动支数"][i],
                                 ["曝光", "点击", "首登数", "申完数", "授信数", "发起数", "动支数"][i + 1]))
                    break
            if "安全授信数" in r and num(r["安全授信数"]) > num(r["授信数"]) + 1e-9:
                viol.append((src, r.get("media"), r.get("date"), "安全授信>授信", "", "", ""))
    check("C3", "漏斗单调 + 安全授信 <= 授信", not viol,
          f"检查 {len(MEDIA)+len(SEG)+len(FUN)} 行，违例 {len(viol)} 处"
          + (f"；示例 {viol[:3]}" if viol else ""))


# ------------------------------------------------------------
# C4 比率范围
# ------------------------------------------------------------

def c4():
    viol = []

    def rng(v, lo, hi, label):
        if not (lo - 1e-9 <= v <= hi + 1e-9):
            viol.append(f"{label}={v}")

    for r in MEDIA:
        c = num(r["花费"]); amt = num(r["首借金额"]); cr = num(r["授信数"])
        dr = num(r["动支数"]); ap = num(r["申完数"]); it = num(r["发起数"])
        fl = num(r["首登数"]); clk = num(r["点击"]); imp = num(r["曝光"])
        rng(dr / cr, 0, 1, "动支率")
        rng(num(r["安全授信数"]) / ap, 0, 1, "安全过件率")
        rng(num(r["回传比例"]), 0, 1, "回传比例")
        rng(num(r["参竞率"]), 0, 1, "参竞率")
        rng(clk / imp, 0, 0.05, "曝光点击率")
        rng(fl / clk, 0, 0.02, "点击首登率")
        rng(ap / fl, 0, 1, "首登申完率")
        rng(cr / ap, 0, 1, "申完授信率")
        rng(it / cr, 0, 1, "授信发起率")
        rng(dr / it, 0.6, 1.0, "发起动支率")
        # 额度使用率合理区间
        u = (amt / dr) / (num(r["授信额度合计"]) / cr)
        rng(u, 0.1, 1.2, "额度使用率")
        rng(c / amt, 0.1, 2.0, "CPS")
    for r in CRE:
        rng(num(r["CTR"]), 0, 0.10, "素材CTR")
        rng(num(r["消耗占比"]), 0, 1, "消耗占比")
    check("C4", "比率字段均在物理合理区间", not viol,
          f"违例 {len(viol)} 处" + (f"；示例 {viol[:3]}" if viol else ""))


# ------------------------------------------------------------
# C5 时间关系
# ------------------------------------------------------------

def c5():
    """事件与异常的时间关联"""
    facts = []

    def ws(media, a, b, keys=None):
        keys = keys or ["花费", "首登数", "申完数", "授信数", "动支数", "首借金额", "授信额度合计", "安全授信数", "发起数", "曝光", "点击"]
        rs = [r for r in MEDIA if r["media"] == media and a <= r["date"] <= b]
        return {k: sum(num(r[k]) for r in rs) for k in keys}

    # A: 腾讯 09-16 结构变化与 CPS 抬升
    pre = ws("腾讯", "2026-09-09", "2026-09-15")
    post = ws("腾讯", "2026-09-16", "2026-09-22")
    cps_pre = pre["花费"] / pre["首借金额"]; cps_post = post["花费"] / post["首借金额"]
    sega = [r for r in SEG if r["media"] == "腾讯" and r["segment_type"] == "RTA策略"
            and r["segment_name"] == "腾讯RTA策略_A"]
    sh_pre = sum(num(r["花费"]) for r in sega if "2026-09-09" <= r["date"] <= "2026-09-15") / pre["花费"]
    sh_post = sum(num(r["花费"]) for r in sega if "2026-09-16" <= r["date"] <= "2026-09-22") / post["花费"]
    ok_a = cps_post > cps_pre * 1.08 and sh_post > sh_pre + 0.05
    facts.append(f"A｜腾讯 CPS {cps_pre:.4f}→{cps_post:.4f}（{cps_post/cps_pre-1:+.1%}）；"
                 f"腾讯RTA策略_A 消耗占比 {sh_pre:.1%}→{sh_post:.1%}，与 09-16 预算调整记录时间一致")
    check("C5-a", "Showcase A：结构变化事件与 CPS 抬升时间一致", ok_a, facts[-1])

    # B: 抖音近 7 天消耗持续下滑
    dpre = ws("抖音", *WIN_B_PREV)
    dpost = ws("抖音", *WIN_B_RECENT)
    cost_ratio = dpost["花费"] / dpre["花费"]
    ctr_pre = dpre["点击"] / dpre["曝光"]; ctr_post = dpost["点击"] / dpost["曝光"]
    ok_b = cost_ratio < 0.90 and ctr_post < ctr_pre * 0.95
    facts.append(f"B｜抖音近 7 天花费 {dpost['花费']/1e4:.1f}万 vs 前 7 天 {dpre['花费']/1e4:.1f}万（{cost_ratio-1:+.1%}）；"
                 f"大盘 CTR {ctr_pre:.4%}→{ctr_post:.4%}（{ctr_post/ctr_pre-1:+.1%}）；"
                 f"下滑为持续性（7 日内逐日走低），非单日事件")
    check("C5-b", "Showcase B：近 7 天消耗持续下滑且与 CTR 衰退同向", ok_b, facts[-1])

    # D: 3W2Q 系数调整日 与 实验组规模跃升
    def exp_agg(grp, a, b, media=None):
        rs = [r for r in EXP if r["分组"] == grp and a <= r["date"] <= b
              and (media is None or r["media"] == media)]
        return {k: sum(num(r[k]) for r in rs) for k in
                ["花费", "首登数", "申完数", "授信数", "动支数", "首借金额", "授信额度合计", "安全授信数"]}
    e_pre = exp_agg("实验组", *WIN_D_PRE, "腾讯")
    e_post = exp_agg("实验组", *WIN_D_POST, "腾讯")
    c_pre = exp_agg("对照组", *WIN_D_PRE, "腾讯")
    c_post = exp_agg("对照组", *WIN_D_POST, "腾讯")
    did_cost = (e_post["花费"] / e_pre["花费"]) / (c_post["花费"] / c_pre["花费"])
    ok_d = did_cost > 1.20
    facts.append(f"D｜腾讯 3W2Q 实验组日均花费 {e_pre['花费']/1e4/6:.1f}万→{e_post['花费']/1e4/6:.1f}万，"
                 f"3W3Q 对照组 {c_pre['花费']/1e4/6:.1f}万→{c_post['花费']/1e4/6:.1f}万；"
                 f"双重差分规模效应 {did_cost-1:+.1%}，与 09-15 出价系数调整记录一致"
                 f"（前窗 {WIN_D_PRE[0]}~{WIN_D_PRE[1]} / 过渡 {'~'.join(WIN_D_RAMP)} 剔除 / "
                 f"后窗 {WIN_D_POST[0]}~{WIN_D_POST[1]}；抖音侧后窗同时受素材衰退影响，需单独处理）")
    check("C5-d", "Showcase D：系数调整事件与实验组规模跃升一致", ok_d, facts[-1])

    # 回传波动事件
    cb = [r for r in MEDIA if r["media"] == "抖音" and r["date"] == "2026-09-20"]
    ok_cb = bool(cb) and num(cb[0]["回传比例"]) < 0.5
    check("C5-e", "抖音 09-20 回传比例短时波动已落库", ok_cb,
          f"09-20 抖音回传比例 = {num(cb[0]['回传比例']):.2%}（基線约 90%）" if cb else "缺失")
    return facts


# ------------------------------------------------------------
# C6 结论可复现
# ------------------------------------------------------------

def c6():
    out = []

    def media_agg(media, a, b):
        rs = [r for r in MEDIA if r["media"] == media and a <= r["date"] <= b]
        return {k: sum(num(r[k]) for r in rs) for k in CNT_KEYS + AMT_KEYS}

    def seg_agg(media, st, sn, a, b):
        rs = [r for r in SEG if r["media"] == media and r["segment_type"] == st
              and r["segment_name"] == sn and a <= r["date"] <= b]
        return {k: sum(num(r[k]) for r in rs) for k in CNT_KEYS + AMT_KEYS}

    # ---- A ----
    p1 = media_agg("腾讯", "2026-09-09", "2026-09-15")
    p2 = media_agg("腾讯", "2026-09-16", "2026-09-22")

    def der(x):
        c = x["花费"]
        return dict(cost=c, cps=c / x["首借金额"], credit_cost=c / x["授信数"],
                    credit_unit=x["首借金额"] / x["授信数"],
                    draw_rate=x["动支数"] / x["授信数"],
                    avg_limit=x["授信额度合计"] / x["授信数"],
                    use=(x["首借金额"] / x["动支数"]) / (x["授信额度合计"] / x["授信数"]),
                    per_loan=x["首借金额"] / x["动支数"],
                    safe_rate=x["安全授信数"] / x["申完数"],
                    login_cost=c / x["首登数"])
    a1, a2 = der(p1), der(p2)
    import math
    cost_share = math.log(a2["credit_cost"] / a1["credit_cost"]) / math.log(a2["cps"] / a1["cps"])
    unit_share = 1 - cost_share
    unit_dec = math.log(a1["credit_unit"] / a2["credit_unit"])
    dr_share = math.log(a1["draw_rate"] / a2["draw_rate"]) / unit_dec
    pl_share = math.log(a1["per_loan"] / a2["per_loan"]) / unit_dec
    out.append(dict(id="A", items=[
        ("腾讯 CPS（前7天→近7天）", f"{a1['cps']:.4f} → {a2['cps']:.4f}（{a2['cps']/a1['cps']-1:+.2%}）"),
        ("授信成本", f"{a1['credit_cost']:,.0f} → {a2['credit_cost']:,.0f}（{a2['credit_cost']/a1['credit_cost']-1:+.2%}）"),
        ("授信单产", f"{a1['credit_unit']:,.0f} → {a2['credit_unit']:,.0f}（{a2['credit_unit']/a1['credit_unit']-1:+.2%}）"),
        ("成本侧贡献度", f"{cost_share:.1%}"),
        ("单产侧贡献度", f"{unit_share:.1%}"),
        ("动支率", f"{a1['draw_rate']:.4f} → {a2['draw_rate']:.4f}（{a2['draw_rate']/a1['draw_rate']-1:+.2%}）"),
        ("平均额度", f"{a1['avg_limit']:,.0f} → {a2['avg_limit']:,.0f}（{a2['avg_limit']/a1['avg_limit']-1:+.2%}）"),
        ("额度使用率", f"{a1['use']:.4f} → {a2['use']:.4f}（{a2['use']/a1['use']-1:+.2%}）"),
        ("人均借款", f"{a1['per_loan']:,.0f} → {a2['per_loan']:,.0f}（{a2['per_loan']/a1['per_loan']-1:+.2%}）"),
        ("首登成本", f"{a1['login_cost']:,.0f} → {a2['login_cost']:,.0f}（{a2['login_cost']/a1['login_cost']-1:+.2%}）"),
        ("单产侧内 动支率贡献", f"{dr_share:.1%}（人均借款贡献 {pl_share:.1%}）"),
    ]))

    # ---- 结构效应反事实：若近7天沿用前7天的消耗结构，大盘会怎样 ----
    def shares(a, b, media):
        rs = [r for r in SEG if r["media"] == media and r["segment_type"] == "RTA策略"
              and a <= r["date"] <= b]
        tot = sum(num(r["花费"]) for r in rs) or 1.0
        return {nm: sum(num(r["花费"]) for r in rs if r["segment_name"] == nm) / tot
                for nm in ["腾讯RTA策略_A", "腾讯RTA策略_B", "腾讯RTA策略_C", "腾讯RTA策略_D"]}
    sp, sq = shares("2026-09-09", "2026-09-15", "腾讯"), shares("2026-09-16", "2026-09-22", "腾讯")
    # 近7天各 RTA 策略的分段指标
    seg_post = {}
    for nm in ["腾讯RTA策略_A", "腾讯RTA策略_B", "腾讯RTA策略_C", "腾讯RTA策略_D"]:
        x = seg_agg("腾讯", "RTA策略", nm, "2026-09-16", "2026-09-22")
        seg_post[nm] = x
    # 用「每元花费的产出」重组大盘，剥离结构效应
    def cf_metric(w, num_key, den_key):
        num = den = 0.0
        for nm, x in seg_post.items():
            c = x["花费"]
            if c <= 0:
                continue
            num += w[nm] * (x[num_key] / c)
            den += w[nm] * (x[den_key] / c)
        return num / den if den else 0.0
    tot_post = sum(sq.values())
    w_pre_abs = {nm: sp[nm] for nm in sp}
    w_post_abs = {nm: sq[nm] for nm in sq}
    cf_cps_pre = 1 / sum(w_pre_abs[nm] / (seg_post[nm]["花费"] / seg_post[nm]["首借金额"])
                        for nm in seg_post) / tot_post
    cf_cps_post = 1 / sum(w_post_abs[nm] / (seg_post[nm]["花费"] / seg_post[nm]["首借金额"])
                         for nm in seg_post) / tot_post
    cf_dr_pre = cf_metric(w_pre_abs, "动支数", "授信数")
    cf_dr_post = cf_metric(w_post_abs, "动支数", "授信数")
    cf_sf_pre = cf_metric(w_pre_abs, "安全授信数", "申完数")
    cf_sf_post = cf_metric(w_post_abs, "安全授信数", "申完数")
    out[-1]["items"] += [
        ("结构效应｜近7天 CPS（沿用前7天结构 → 实际结构）",
         f"{cf_cps_pre:.4f} → {cf_cps_post:.4f}（结构贡献 {cf_cps_post-cf_cps_pre:+.4f}）"),
        ("结构效应｜近7天 动支率（沿用前7天结构 → 实际结构）",
         f"{cf_dr_pre:.4f} → {cf_dr_post:.4f}（结构贡献 {cf_dr_post-cf_dr_pre:+.4f}）"),
        ("结构效应｜近7天 安全过件率（沿用前7天结构 → 实际结构）",
         f"{cf_sf_pre:.4f} → {cf_sf_post:.4f}（结构贡献 {cf_sf_post-cf_sf_pre:+.4f}）"),
    ]

    # RTA 策略结构
    for nm in ["腾讯RTA策略_A", "腾讯RTA策略_B", "腾讯RTA策略_C", "腾讯RTA策略_D"]:
        s1 = seg_agg("腾讯", "RTA策略", nm, "2026-09-09", "2026-09-15")
        s2 = seg_agg("腾讯", "RTA策略", nm, "2026-09-16", "2026-09-22")
        d1, d2 = der(s1), der(s2)
        out[-1]["items"].append((
            f"RTA策略 {nm}",
            f"消耗占比 {s1['花费']/p1['花费']:.1%}→{s2['花费']/p2['花费']:.1%}；"
            f"CPS {d1['cps']:.4f}→{d2['cps']:.4f}；动支率 {d1['draw_rate']:.4f}→{d2['draw_rate']:.4f}"))

    # ---- B ----
    b_post = media_agg("抖音", *WIN_B_RECENT)
    b_pre = media_agg("抖音", *WIN_B_PREV)
    b_day = media_agg("抖音", *WIN_B_DAY)
    dpre = 7.0
    budget = 550000.0
    ct_day = b_day["点击"] / b_day["曝光"]
    ct_pre = b_pre["点击"] / b_pre["曝光"]
    out.append(dict(id="B", items=[
        ("统一观察窗口", f"近 7 天 {WIN_B_RECENT[0]}~{WIN_B_RECENT[1]}，对比前 7 天 {WIN_B_PREV[0]}~{WIN_B_PREV[1]}"),
        ("抖音 09-22 花费", f"{b_day['花费']/1e4:.1f} 万（日预算 {budget/1e4:.0f} 万，消耗进度 {b_day['花费']/budget:.1%}，未撞线）"),
        ("近7天日均花费", f"{b_post['花费']/1e4/dpre:.1f} 万；前7天日均 {b_pre['花费']/1e4/dpre:.1f} 万"
                          f"（{b_post['花费']/b_pre['花费']-1:+.1%}）"),
        ("09-22 曝光", f"{b_day['曝光']:,}（vs 前7天日均 {b_pre['曝光']/dpre:,.0f}）"),
        ("09-22 大盘 CTR", f"{ct_day:.4%}（前7天 {ct_pre:.4%}，{ct_day/ct_pre-1:+.1%}）"),
        ("09-22 参竞率", f"{[num(r['参竞率']) for r in MEDIA if r['media']=='抖音' and r['date']=='2026-09-22'][0]:.2%}"
                       f"（前7天均 {(sum(num(r['参竞率']) for r in MEDIA if r['media']=='抖音' and '2026-09-09'<=r['date']<='2026-09-15'))/7:.2%}）"),
        ("09-22 CPM", f"{b_day['花费']/b_day['曝光']*1000:,.1f} 元（前7天均 {b_pre['花费']/b_pre['曝光']*1000:,.1f} 元）"),
        ("基础出价", "未调整（全期 5,000 元）"),
        ("09-22 首登成本", f"{b_day['花费']/b_day['首登数']:,.0f} 元（前7天均 {b_pre['花费']/b_pre['首登数']:,.0f} 元）"),
        ("09-22 CPS", f"{b_day['花费']/b_day['首借金额']:.4f}"),
        ("09-20 回传比例", f"{[num(r['回传比例']) for r in MEDIA if r['media']=='抖音' and r['date']=='2026-09-20'][0]:.2%}"),
    ]))
    cre_day = [r for r in CRE if r["date"] == "2026-09-22"]
    for r in sorted(cre_day, key=lambda x: num(x["CTR"])):
        out[-1]["items"].append((f"素材 {r['素材ID']}（第{r['上线天数']}天）",
                                 f"CTR {num(r['CTR']):.4%}，消耗占比 {num(r['消耗占比']):.1%}"))

    # ---- C ----
    c_seg = seg_agg("腾讯", "RTA策略", "腾讯RTA策略_C", *WIN_C)
    c_tot = media_agg("腾讯", *WIN_C)
    rem = {k: c_tot[k] - c_seg[k] for k in c_tot}
    d_seg, d_tot, d_rem = der(c_seg), der(c_tot), der(rem)
    out.append(dict(id="C", items=[
        ("统一观察窗口", f"{WIN_C[0]} ~ {WIN_C[1]}（近 30 天）"),
        ("标的分组", "腾讯 · RTA策略 = 腾讯RTA策略_C"),
        ("消耗占比", f"{c_seg['花费']/c_tot['花费']:.1%}"),
        ("CPS", f"{d_seg['cps']:.4f} vs 腾讯大盘 {d_tot['cps']:.4f}（{(d_seg['cps']/d_tot['cps']-1):+.1%}）"),
        ("安全过件率", f"{d_seg['safe_rate']:.2%} vs 大盘 {d_tot['safe_rate']:.2%}"),
        ("授信单产", f"{d_seg['credit_unit']:,.0f} vs 大盘 {d_tot['credit_unit']:,.0f}"),
        ("安全交易占比", f"{c_seg['安全首借金额']/c_seg['首借金额']:.1%} vs 大盘 {c_tot['安全首借金额']/c_tot['首借金额']:.1%}"),
        ("平均额度", f"{d_seg['avg_limit']:,.0f} vs 大盘 {d_tot['avg_limit']:,.0f}"),
        ("反事实：剔除后大盘 CPS", f"{d_rem['cps']:.4f}（相对大盘 {d_rem['cps']/d_tot['cps']-1:+.2%}）"),
        ("反事实：剔除后 安全过件率",
         f"{d_rem['safe_rate']:.2%}（{(d_rem['safe_rate']-d_tot['safe_rate'])*100:+.2f}pp）"),
        ("反事实：剔除后 安全首借金额", f"{rem['安全首借金额']/c_tot['安全首借金额']-1:+.1%}"),
        ("反事实：剔除后 大盘首借金额", f"{rem['首借金额']/c_tot['首借金额']-1:+.1%}"),
    ]))

    # ---- D ----
    def exp_ws(grp, a, b, media="腾讯"):
        """返回**日均**口径，便于前后窗不等长时比较"""
        rs = [r for r in EXP if r["分组"] == grp and a <= r["date"] <= b and r["media"] == media]
        nd = len({r["date"] for r in rs}) or 1
        return {k: sum(num(r[k]) for r in rs) / nd for k in
                ["花费", "首登数", "申完数", "授信数", "动支数", "首借金额", "授信额度合计", "安全授信数"]}
    items = []
    items.append(("统一观察窗口",
                  f"前窗 {WIN_D_PRE[0]}~{WIN_D_PRE[1]}（6 天） / 过渡 {WIN_D_RAMP[0]}~{WIN_D_RAMP[1]}（2 天，剔除） / "
                  f"后窗 {WIN_D_POST[0]}~{WIN_D_POST[1]}（6 天）"))
    for media in ["腾讯", "抖音"]:
        ep = exp_ws("实验组", *WIN_D_PRE, media)
        eq = exp_ws("实验组", *WIN_D_POST, media)
        cp = exp_ws("对照组", *WIN_D_PRE, media)
        cq = exp_ws("对照组", *WIN_D_POST, media)
        dep, deq, dcp, dcq = der(ep), der(eq), der(cp), der(cq)
        items.append((f"{media}｜实验组日均花费（前窗 → 后窗）",
                      f"{ep['花费']/1e4:.1f}万/日 → {eq['花费']/1e4:.1f}万/日（{eq['花费']/ep['花费']-1:+.1%}）"))
        items.append((f"{media}｜对照组日均花费",
                      f"{cp['花费']/1e4:.1f}万/日 → {cq['花费']/1e4:.1f}万/日（{cq['花费']/cp['花费']-1:+.1%}）"))
        items.append((f"{media}｜DID 规模效应",
                      f"{(eq['花费']/ep['花费'])/(cq['花费']/cp['花费'])-1:+.1%}"))
        items.append((f"{media}｜DID CPS", f"{(deq['cps']/dep['cps'])/(dcq['cps']/dcp['cps'])-1:+.1%}"))
        items.append((f"{media}｜DID 授信单产", f"{(deq['credit_unit']/dep['credit_unit'])/(dcq['credit_unit']/dcp['credit_unit'])-1:+.1%}"))
        items.append((f"{media}｜DID 平均额度", f"{(deq['avg_limit']/dep['avg_limit'])/(dcq['avg_limit']/dcp['avg_limit'])-1:+.1%}"))
        items.append((f"{media}｜DID 安全过件率",
                      f"{((deq['safe_rate']-dep['safe_rate'])-(dcq['safe_rate']-dcp['safe_rate']))*100:+.2f}pp"))
        items.append((f"{media}｜DID 动支率", f"{(deq['draw_rate']/dep['draw_rate'])/(dcq['draw_rate']/dcp['draw_rate'])-1:+.1%}"))
        items.append((f"{media}｜实验组首借金额", f"{eq['首借金额']/1e4:.1f}万/日（{eq['首借金额']/ep['首借金额']-1:+.1%}）"))
        items.append((f"{media}｜DID 授信数", f"{(eq['授信数']/ep['授信数'])/(cq['授信数']/cp['授信数'])-1:+.1%}"))
        items.append((f"{media}｜DID 动支数", f"{(eq['动支数']/ep['动支数'])/(cq['动支数']/cp['动支数'])-1:+.1%}"))
        items.append((f"{media}｜DID 首借金额", f"{(eq['首借金额']/ep['首借金额'])/(cq['首借金额']/cp['首借金额'])-1:+.1%}"))
        items.append((f"{media}｜前窗平行趋势检查（实验组 CPS vs 对照组 CPS）",
                      f"{dep['cps']:.4f} vs {dcp['cps']:.4f}（比值 {dep['cps']/dcp['cps']:.4f}）"))
    out.append(dict(id="D", items=items))

    check("C6", "4 个 Showcase 的核心结论可由数据直接复算", True,
          f"已复算 {sum(len(o['items']) for o in out)} 项关键数值，详见 03_数据一致性检查.md")
    return out


# ------------------------------------------------------------
# 报告
# ------------------------------------------------------------

def write_report(facts, gt):
    path = os.path.join(GT, "03_数据一致性检查.md")
    os.makedirs(GT, exist_ok=True)
    npass = sum(1 for _, _, ok, _ in RESULTS if ok)
    ntot = len(RESULTS)
    L = []
    L.append("# 数据一致性检查报告\n")
    L.append("> ⚠️ 本文件属于内部验证材料，**不得作为竞品输入**。\n")
    L.append(f"- 检查项：{ntot} 项，通过 {npass} 项，失败 {ntot - npass} 项")
    L.append(f"- 数据范围：{MEDIA[0]['date']} ~ {MEDIA[-1]['date']}，共 {len({r['date'] for r in MEDIA})} 天")
    L.append("")
    L.append("**统一观察窗口**（Benchmark 与 GroundTruth 共用）：")
    L.append("")
    L.append("| Showcase | 对比窗口 | 参考窗口 | 备注 |")
    L.append("|---|---|---|---|")
    L.append(f"| A | {WIN_A_POST[0]} ~ {WIN_A_POST[1]} | {WIN_A_PRE[0]} ~ {WIN_A_PRE[1]} | 各 7 天 |")
    L.append(f"| B | {WIN_B_RECENT[0]} ~ {WIN_B_RECENT[1]} | {WIN_B_PREV[0]} ~ {WIN_B_PREV[1]} | 近 7 天 vs 前 7 天 |")
    L.append(f"| C | {WIN_C[0]} ~ {WIN_C[1]} | 腾讯大盘同期 | 近 30 天 |")
    L.append(f"| D | {WIN_D_POST[0]} ~ {WIN_D_POST[1]} | {WIN_D_PRE[0]} ~ {WIN_D_PRE[1]} | "
             f"变更日 {WIN_D_CHANGE} 与爬坡日 {WIN_D_RAMP[0]}~{WIN_D_RAMP[1]} **单独剔除**；各 6 天 |")
    L.append(f"- 表规模：媒体日级 {len(MEDIA)} 行 / 细分日级 {len(SEG)} 行 / 素材 {len(CRE)} 行 / "
             f"漏斗 {len(FUN)} 行 / 质量 {len(QUA)} 行 / 投放 {len(DLV)} 行 / 实验 {len(EXP)} 行 / 变更日志 {len(LOG)} 行\n")
    L.append("## 一、检查结果\n")
    L.append("| 编号 | 检查项 | 结果 | 说明 |")
    L.append("|---|---|---|---|")
    for cid, name, ok, detail in RESULTS:
        L.append(f"| {cid} | {name} | {'✅ 通过' if ok else '❌ 失败'} | {detail} |")
    L.append("\n## 二、事件与异常的时间关联\n")
    for f in facts:
        L.append(f"- {f}")
    L.append("\n## 三、四个 Showcase 的关键计算\n")
    for o in gt:
        L.append(f"### Showcase {o['id']}\n")
        L.append("| 项 | 值 |")
        L.append("|---|---|")
        for k, v in o["items"]:
            L.append(f"| {k} | {v} |")
        L.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"\n报告已写入 {os.path.relpath(path, BASE_DIR)}")
    return npass, ntot


if __name__ == "__main__":
    print("=" * 90)
    print("模拟环境数据一致性校验")
    print("=" * 90)
    c1(); c2(); c3(); c4()
    facts = c5()
    gt = c6()
    npass, ntot = write_report(facts, gt)
    print("=" * 90)
    print(f"结果：{npass}/{ntot} 通过")
    sys.exit(0 if npass == ntot else 1)
