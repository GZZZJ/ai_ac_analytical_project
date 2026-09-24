#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟环境 —— 关键窗口指标探查（开发期调参用，也可作为人工复核工具）"""
import csv
import os
import sys
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE_DIR, "03_模拟数据")


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


MEDIA = load("ads_rta_media_daily.csv")
SEG = load("ads_rta_segment_daily.csv")


def agg_media(media, a, b):
    rs = [r for r in MEDIA if r["media"] == media and a <= r["date"] <= b]
    keys = ["花费", "曝光", "点击", "首登数", "申完数", "授信数", "安全授信数",
            "发起数", "动支数", "首借金额", "授信额度合计", "安全首借金额"]
    return {k: sum(f(r[k]) for r in rs) for k in keys}


def agg_seg(media, stype, sname, a, b):
    rs = [r for r in SEG if r["media"] == media and r["segment_type"] == stype
          and r["segment_name"] == sname and a <= r["date"] <= b]
    keys = ["花费", "曝光", "点击", "首登数", "申完数", "授信数", "安全授信数",
            "发起数", "动支数", "首借金额", "授信额度合计", "安全首借金额"]
    return {k: sum(f(r[k]) for r in rs) for k in keys}


def m(x):
    c = x["花费"]
    d = x["授信数"]
    return dict(
        cost=c, cps=c / x["首借金额"] if x["首借金额"] else 0,
        credit_cost=c / d if d else 0,
        credit_unit=x["首借金额"] / d if d else 0,
        cpcl=c / x["授信额度合计"] if x["授信额度合计"] else 0,
        safe_rate=x["安全授信数"] / x["申完数"] if x["申完数"] else 0,
        draw_rate=x["动支数"] / d if d else 0,
        init_rate=x["发起数"] / d if d else 0,
        avg_limit=x["授信额度合计"] / d if d else 0,
        use=(x["首借金额"] / x["动支数"]) / (x["授信额度合计"] / d) if x["动支数"] and d else 0,
        per_loan=x["首借金额"] / x["动支数"] if x["动支数"] else 0,
        login_cost=c / x["首登数"] if x["首登数"] else 0,
        conv_cost=c / x["动支数"] if x["动支数"] else 0,
        cpm=c / x["曝光"] * 1000 if x["曝光"] else 0,
        ctr=x["点击"] / x["曝光"] if x["曝光"] else 0,
        safe_amt_ratio=x["安全首借金额"] / x["首借金额"] if x["首借金额"] else 0,
        fl=x["首登数"], ap=x["申完数"], cr=d, dr=x["动支数"],
    )


def line(label, x, keys=("cps", "credit_cost", "credit_unit", "safe_rate", "draw_rate",
                         "avg_limit", "use", "login_cost", "per_loan", "conv_cost")):
    d = m(x)
    parts = []
    for k in keys:
        v = d[k]
        parts.append(f"{k}={v:,.4f}" if abs(v) < 10 else f"{k}={v:,.0f}")
    print(f"{label:30s} 花费={x['花费']/1e4:7.1f}万 " + " ".join(parts))


def compare(label, a1, b1, a2, b2, media, keys):
    x1 = m(agg_media(media, a1, b1))
    x2 = m(agg_media(media, a2, b2))
    parts = []
    for k in keys:
        if x1[k]:
            parts.append(f"{k}: {x1[k]:,.2f}->{x2[k]:,.2f} ({x2[k]/x1[k]-1:+.1%})")
        else:
            parts.append(f"{k}: {x1[k]}->{x2[k]}")
    print(f"--- {label} ---")
    for p in parts:
        print("   ", p)
    return x1, x2


if __name__ == "__main__":
    print("=" * 100)
    print("腾讯：前7天 09-09~09-15  vs  近7天 09-16~09-22")
    print("=" * 100)
    compare("腾讯", "2026-09-09", "2026-09-15", "2026-09-16", "2026-09-22", "腾讯",
            ["cost", "cps", "credit_cost", "credit_unit", "safe_rate", "draw_rate",
             "avg_limit", "use", "per_loan", "login_cost", "conv_cost", "cpcl"])

    print()
    print("=" * 100)
    print("抖音：前7天 09-09~09-15  vs  近7天 09-16~09-22 ; 以及 09-22 单日")
    print("=" * 100)
    compare("抖音", "2026-09-09", "2026-09-15", "2026-09-16", "2026-09-22", "抖音",
            ["cost", "cps", "credit_cost", "credit_unit", "safe_rate", "draw_rate",
             "avg_limit", "use", "per_loan", "login_cost", "conv_cost", "ctr", "cpm"])

    print()
    print("抖音单日 09-22 vs 前7天均值:")
    line("09-22", agg_media("抖音", "2026-09-22", "2026-09-22"))
    line("近7天", agg_media("抖音", "2026-09-16", "2026-09-22"))
    line("前7天", agg_media("抖音", "2026-09-09", "2026-09-15"))

    print()
    print("=" * 100)
    print("腾讯 RTA策略 分渠道（前7天 / 近7天）")
    print("=" * 100)
    for name in ["腾讯RTA策略_A", "腾讯RTA策略_B", "腾讯RTA策略_C", "腾讯RTA策略_D"]:
        x1 = agg_seg("腾讯", "RTA策略", name, "2026-09-09", "2026-09-15")
        x2 = agg_seg("腾讯", "RTA策略", name, "2026-09-16", "2026-09-22")
        t1 = agg_media("腾讯", "2026-09-09", "2026-09-15")
        t2 = agg_media("腾讯", "2026-09-16", "2026-09-22")
        a = m(x1); b = m(x2)
        print(f"{name:14s} 消耗占比 {x1['花费']/t1['花费']:.1%} -> {x2['花费']/t2['花费']:.1%} | "
              f"CPS {a['cps']:.4f} -> {b['cps']:.4f} | 动支率 {a['draw_rate']:.4f} -> {b['draw_rate']:.4f} | "
              f"安全率 {a['safe_rate']:.4f} -> {b['safe_rate']:.4f} | 单产 {a['credit_unit']:,.0f} -> {b['credit_unit']:,.0f}")

    print()
    print("=" * 100)
    print("Showcase C：腾讯 腾讯RTA策略_C vs 腾讯大盘（近 30 天）")
    print("=" * 100)
    x = agg_seg("腾讯", "RTA策略", "腾讯RTA策略_C", "2026-08-24", "2026-09-22")
    t = agg_media("腾讯", "2026-08-24", "2026-09-22")
    a, b = m(x), m(t)
    print(f"  腾讯RTA策略_C: 消耗占比={x['花费']/t['花费']:.1%} CPS={a['cps']:.4f} 安全率={a['safe_rate']:.4f} "
          f"单产={a['credit_unit']:,.0f} 安全交易占比={a['safe_amt_ratio']:.2%} 平均额度={a['avg_limit']:,.0f}")
    print(f"  腾讯大盘      : 消耗占比=100%  CPS={b['cps']:.4f} 安全率={b['safe_rate']:.4f} "
          f"单产={b['credit_unit']:,.0f} 安全交易占比={b['safe_amt_ratio']:.2%} 平均额度={b['avg_limit']:,.0f}")
    # 反事实：剔除
    rem = {k: t[k] - x[k] for k in t}
    r = m(rem)
    print(f"  剔除后大盘    : CPS={r['cps']:.4f} 安全率={r['safe_rate']:.4f} 单产={r['credit_unit']:,.0f} "
          f"安全交易占比={r['safe_amt_ratio']:.2%} 安全首借金额占比变化={rem['安全首借金额']/t['安全首借金额']-1:+.1%}")

    print()
    print("=" * 100)
    print("抖音 素材 09-22")
    print("=" * 100)
    CRE = load("ads_rta_creative_daily.csv")
    for r in CRE:
        if r["date"] == "2026-09-22":
            print(f"  {r['素材ID']} 上线{r['上线日期']} 第{r['上线天数']}天 CTR={f(r['CTR']):.4%} "
                  f"消耗占比={f(r['消耗占比']):.1%} 花费={f(r['花费'])/1e4:.1f}万")

    print()
    print("=" * 100)
    print("策略实验：3W2Q 实验组 vs 3W3Q 对照组")
    print("统一窗口：前窗 2026-09-09~09-14（6 天）/ 过渡 09-15~09-16（剔除）/ 后窗 09-17~09-22（6 天）")
    print("下列数值均为**日均口径**")
    print("=" * 100)
    EXP = load("ads_rta_strategy_experiment.csv")
    KS = ["花费", "曝光", "点击", "首登数", "申完数", "授信数", "安全授信数", "发起数",
          "动支数", "首借金额", "授信额度合计", "安全首借金额"]
    for grp in ["实验组", "对照组"]:
        for lbl, (a_, b_) in {"前窗 09-09~09-14": ("2026-09-09", "2026-09-14"),
                              "后窗 09-17~09-22": ("2026-09-17", "2026-09-22")}.items():
            for ch in ["腾讯", "抖音", "两渠道合计"]:
                rs = [r for r in EXP if r["分组"] == grp and a_ <= r["date"] <= b_
                      and (ch == "两渠道合计" or r["media"] == ch)]
                nd = len({r["date"] for r in rs}) or 1
                x = {k: sum(f(r[k]) for r in rs) / nd for k in KS}
                d = m(x)
                print(f"  {grp} {ch:6s} {lbl:14s} 日均花费={x['花费']/1e4:6.2f}万 CPS={d['cps']:.4f} "
                      f"日均授信={x['授信数']:6.1f} 日均动支={x['动支数']:6.1f} 安全率={d['safe_rate']:.4f} "
                      f"单产={d['credit_unit']:,.0f} 日均首借金额={x['首借金额']/1e4:6.2f}万 "
                      f"平均额度={d['avg_limit']:,.0f}")
            print()
