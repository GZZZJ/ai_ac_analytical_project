#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获客智能分析 · 竞品测试模拟环境 —— 数据生成器
================================================
用途：生成一套 56 天、双层渠道、多维度、指标自洽的模拟投放数据。
所有数值均为构造，不含任何真实业务数据。

设计约束（生成后由 validate_sim_data.py 校验）：
  1. 每个 segment_type 都是该渠道大盘的一个"独立切分"，汇总后等于大盘
  2. 漏斗单调：曝光 >= 点击 >= 首登 >= 申完 >= 授信 >= 发起 >= 动支；安全授信 <= 授信
  3. CPS = 花费/首借金额 = 授信成本/授信单产
  4. 授信单产 = 平均额度 x 额度使用率 x 动支率
  5. 预埋 4 个 Showcase 的异常与多个业务事件，事件与异常之间存在时间关系

运行：python3 scripts/generate_sim_data.py
输出：03_模拟数据/*.csv
"""

import csv
import math
import os
import random
from datetime import date, timedelta

SEED = 20260922
random.seed(SEED)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("SIM_DATA_OUT_DIR", os.path.join(BASE_DIR, "03_模拟数据"))

START = date(2026, 7, 29)
END = date.fromisoformat(os.environ.get("SIM_DATA_END", "2026-09-22"))


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


DATES = list(daterange(START, END))
N = len(DATES)
D = {d.isoformat(): i for i, d in enumerate(DATES)}

# ---- 关键时点 ----
SHOWCASE_A_SHIFT = "2026-09-16"      # 腾讯 RTA策略_A 预算上调 -> 结构变化
SHOWCASE_D_CHANGE = "2026-09-15"     # 3W2Q 客群 RTA 出价系数上调
SHOWCASE_B_BUDGET = "2026-09-10"     # 抖音预算上调（为 B 提供"未撞线"的反证）
DY_CREATIVE_LAUNCH = {}  # 见下方 DY_CREATIVES

DY_CREATIVES = [
    # 素材ID,          上线日期,      基础份额权重, 素材类型
    ("DY_CRE_01", "2026-07-05", 0.22, "视频-口播"),
    ("DY_CRE_02", "2026-07-11", 0.20, "视频-剧情"),
    ("DY_CRE_03", "2026-07-17", 0.20, "视频-口播"),
    ("DY_CRE_04", "2026-07-23", 0.18, "视频-图文"),
    ("DY_CRE_05", "2026-07-29", 0.20, "视频-剧情"),
    ("DY_CRE_06", "2026-08-04", 0.22, "视频-口播"),
    ("DY_CRE_07", "2026-08-10", 0.21, "视频-剧情"),
    ("DY_CRE_08", "2026-08-16", 0.20, "视频-口播"),
    ("DY_CRE_09", "2026-08-22", 0.21, "视频-图文"),
    ("DY_CRE_10", "2026-08-28", 0.20, "视频-口播"),
    ("DY_CRE_11", "2026-09-03", 0.22, "视频-剧情"),
    ("DY_CRE_12", "2026-09-19", 0.20, "视频-口播"),
    ("DY_CRE_13", "2026-09-24", 0.28, "视频-剧情"),
]
DY_CRE_LIFE = 30           # 素材有效投放天数
DY_CRE_MATURITY_DECAY = 0.055   # 素材相对成熟度衰减系数（指数）
DY_CB_INCIDENT = "2026-09-20"        # 抖音回传比例短时波动（次要问题）

import hashlib


def rnd(key, lo=-1.0, hi=1.0):
    """确定性伪随机：同一天同一 key 结果稳定"""
    h = hashlib.md5(f"{SEED}|{key}".encode()).hexdigest()
    return lo + (hi - lo) * (int(h[:12], 16) / float(16 ** 12))


# ⚠️ 出价系数 / 参竞率 / pCTR / pCVR 等派生字段的随机扰动种子。
#    本环境在业务语义重构前使用过旧实体标识（oCPM-* / 抖音账户组A-D）。
#    为保证重构前后**所有派生字段逐值不变**，种子沿用旧标识；该旧标识不写入任何输出。
_SEED_ALIAS = {
    "腾讯RTA策略_A": "oCPM-首登",
    "腾讯RTA策略_B": "oCPM-授信",
    "腾讯RTA策略_C": "oCPM-深度转化",
    "腾讯RTA策略_D": "CPA-首借",
    "授信账户-常规": "抖音账户组A",
    "授信账户-合作方A": "抖音账户组B",
    "ROI账户-常规": "抖音账户组C",
    "ROI账户-合作方A": "抖音账户组D",
}


def seedkey(name):
    """返回用于随机扰动的稳定种子键（跨重命名不变）"""
    return _SEED_ALIAS.get(name, name)


def noise(key, amp):
    return 1.0 + rnd(key, -amp, amp)


def is_weekend(d):
    return d.weekday() >= 5


def weekend_factor(d, amp=0.045):
    return 1.0 - amp if is_weekend(d) else 1.0


def wavg(pairs):
    num = sum(v * w for v, w in pairs)
    den = sum(w for _, w in pairs)
    return num / den if den else 0.0


COHORT_MUL_3W2Q = dict(cost=1.450, fl=1.300, apply=1.300, credit=1.291, safe=1.365,
                        init=1.291, draw=1.291, amount=1.381, limit_total=1.368, safe_amt=1.450)
COHORT_MUL_DEFAULT = {k: 1.0 for k in COHORT_MUL_3W2Q}


COHORT_RAMP_END = "2026-09-17"     # 出价系数调整后 2 天达到完全生效

# 分层乘数 key -> 表字段名
COHORT_FIELD_MAP = {
    "花费": "cost", "曝光": "cost", "点击": "cost",
    "首登数": "fl", "申完数": "apply", "授信数": "credit", "安全授信数": "safe",
    "发起数": "init", "动支数": "draw", "首借金额": "amount",
    "授信额度合计": "limit_total", "安全首借金额": "safe_amt",
}


def cohort_mul(day_idx, cname):
    """3W2Q 客群自 SHOWCASE_D_CHANGE 起 RTA 出价系数上调带来的分层乘数"""
    if cname != "3W2Q":
        return dict(COHORT_MUL_DEFAULT)
    g = ramp(day_idx, SHOWCASE_D_CHANGE, COHORT_RAMP_END)
    return {k: 1.0 + (v - 1.0) * g for k, v in COHORT_MUL_3W2Q.items()}


def ramp(day_idx, start_s, end_s):
    """线性爬坡：返回 0~1"""
    a, b = D[start_s], D[end_s]
    if day_idx <= a:
        return 0.0
    if day_idx >= b:
        return 1.0
    return (day_idx - a) / float(b - a)


# ============================================================
# 一、腾讯：以「RTA策略」为主切分，大盘由策略汇总而来
# ============================================================

TX_BIDTYPES = ["腾讯RTA策略_A", "腾讯RTA策略_B", "腾讯RTA策略_C", "腾讯RTA策略_D"]

# 消耗份额：09-16 起向 RTA策略_A 倾斜（预算腾挪）
TX_SHARE_COST_P1 = {"腾讯RTA策略_A": 0.15, "腾讯RTA策略_B": 0.36, "腾讯RTA策略_C": 0.09, "腾讯RTA策略_D": 0.40}
TX_SHARE_COST_P2 = {"腾讯RTA策略_A": 0.34, "腾讯RTA策略_B": 0.29, "腾讯RTA策略_C": 0.09, "腾讯RTA策略_D": 0.28}
TX_SHARE_FL_P1 = {"腾讯RTA策略_A": 0.220, "腾讯RTA策略_B": 0.345, "腾讯RTA策略_C": 0.030, "腾讯RTA策略_D": 0.405}
TX_SHARE_FL_P2 = {"腾讯RTA策略_A": 0.395, "腾讯RTA策略_B": 0.285, "腾讯RTA策略_C": 0.030, "腾讯RTA策略_D": 0.290}

# 分段画像：首登->申完, 申完->授信, 申完->安全, 授信->发起, 发起->动支, 平均额度, 额度使用率, 安全金额占比
TX_SEG = {
    "腾讯RTA策略_A": dict(apply=0.300, credit=0.520, safe=0.145, init=0.300, draw=0.951,
                      limit=24500, use=0.395, safe_amt=0.520),
    "腾讯RTA策略_B": dict(apply=0.400, credit=0.650, safe=0.215, init=0.400, draw=0.951,
                      limit=30000, use=0.462, safe_amt=0.630),
    "腾讯RTA策略_C": dict(apply=0.440, credit=0.680, safe=0.340, init=0.430, draw=0.951,
                        limit=43200, use=0.415, safe_amt=0.860),
    "腾讯RTA策略_D": dict(apply=0.400, credit=0.560, safe=0.185, init=0.400, draw=0.951,
                     limit=29700, use=0.472, safe_amt=0.585),
}

# 09-16 起的段内变化（乘数）
TX_POST_MUL = dict(apply=1.015, credit=1.057, safe=0.995, init=0.960, limit=1.015, use=0.968)

TX_BASE = dict(cost=380000.0, fl=730.0, cpm=20.04, ctr=0.00770, r_click_login=0.00500,
               cb_ratio=0.92, budget=520000.0, auction=0.68, base_bid=5000.0)
TX_LOGIN_COST_UP = 0.045   # 09-16 起首登成本抬升幅度


def recent_stage(d):
    """09-23 起构造四日渐进变化；此前 Benchmark 的所有值保持不变。"""
    return {23: 0.25, 24: 0.55, 25: 0.80, 26: 1.0}.get(d.day, 0.0) if d.year == 2026 and d.month == 9 else 0.0


def tx_state(day_idx):
    t = ramp(day_idx, SHOWCASE_A_SHIFT, "2026-09-18")
    cost_sh = {k: TX_SHARE_COST_P1[k] + (TX_SHARE_COST_P2[k] - TX_SHARE_COST_P1[k]) * t
               for k in TX_BIDTYPES}
    fl_sh = {k: TX_SHARE_FL_P1[k] + (TX_SHARE_FL_P2[k] - TX_SHARE_FL_P1[k]) * t
             for k in TX_BIDTYPES}
    stage = recent_stage(DATES[day_idx])
    cost_sh["腾讯RTA策略_A"] += 0.08 * stage
    cost_sh["腾讯RTA策略_D"] -= 0.08 * stage
    fl_sh["腾讯RTA策略_A"] += 0.025 * stage
    fl_sh["腾讯RTA策略_D"] -= 0.025 * stage
    return cost_sh, fl_sh, t


def build_tx(day_idx, d):
    ds = d.isoformat()
    cost_sh, fl_sh, t = tx_state(day_idx)
    stage = recent_stage(d)

    total_cost = TX_BASE["cost"] * (1 + 0.20 * stage) * weekend_factor(d) * noise(f"tx_cost_{ds}", 0.025)

    # 首登成本抬升 -> 同样花费换来的首登减少
    login_up = (1.0 + TX_LOGIN_COST_UP * ramp(day_idx, "2026-09-16", "2026-09-18")) * (1 + 0.13 * stage)
    total_fl = (total_cost / (TX_BASE["cost"] / TX_BASE["fl"]) / login_up
                * noise(f"tx_fln_{ds}", 0.020))

    out = []
    agg = dict(cost=0, impressions=0, clicks=0, fl=0, apply=0, credit=0, safe=0,
               init=0, draw=0, amount=0, limit_total=0, safe_amount=0)
    for name in TX_BIDTYPES:
        p = dict(TX_SEG[name])
        if t > 0:
            for k, m in TX_POST_MUL.items():
                p[k] = p[k] * (1 + (m - 1) * t)
        p["use"] *= 1 - 0.07 * stage

        fl = total_fl * fl_sh[name]
        apply_ = fl * p["apply"]
        credit = apply_ * p["credit"]
        safe = apply_ * p["safe"]
        init_ = credit * p["init"]
        draw = init_ * p["draw"]
        limit = p["limit"]
        use = p["use"]
        amount = draw * limit * use
        limit_total = credit * limit
        out.append(dict(date=ds, media="腾讯", segment_type="RTA策略", segment_name=name,
                        cost=total_cost * cost_sh[name], fl=fl, apply=apply_, credit=credit,
                        safe=safe, init=init_, draw=draw, amount=amount,
                        limit_total=limit_total, safe_amount=amount * p["safe_amt"],
                        limit=limit, use=use))
        for k, v in (("cost", total_cost * cost_sh[name]), ("fl", fl), ("apply", apply_),
                     ("credit", credit), ("safe", safe), ("init", init_), ("draw", draw),
                     ("amount", amount), ("limit_total", limit_total),
                     ("safe_amount", amount * p["safe_amt"])):
            agg[k] += v

    # 曝光 / 点击
    ctr = TX_BASE["ctr"] * noise(f"tx_ctr_{ds}", 0.020)
    clicks = agg["fl"] / (TX_BASE["r_click_login"] * login_up)
    impressions = clicks / ctr
    agg["clicks"] = clicks
    agg["impressions"] = impressions
    for seg in out:
        w = seg["cost"] / agg["cost"]
        seg["clicks"] = clicks * w
        seg["impressions"] = impressions * w

    return out, agg


# ============================================================
# 二、抖音：大盘 pCTR 衰退驱动消耗受限，账号 / 素材为切分
# ============================================================

DY_BASE = dict(cost=412000.0, cpm=42.80, r_click_login=0.00506,
               apply=0.4300, credit=0.6370, safe=0.2180, init=0.3610, draw=0.9510,
               limit=25500.0, use=0.482, safe_amt=0.620,
               cb_ratio=0.90, budget=550000.0, auction=0.72,
               base_bid=5000.0, ref_ctr=1.75)

DY_ACCOUNTS = [("授信账户-常规", 0.31), ("授信账户-合作方A", 0.26),
               ("ROI账户-常规", 0.24), ("ROI账户-合作方A", 0.19)]

DY_CRE_SHARE = {"DY_CRE_01": 0.26, "DY_CRE_02": 0.22, "DY_CRE_03": 0.18,
                "DY_CRE_04": 0.14, "DY_CRE_05": 0.11, "DY_CRE_06": 0.09}
DY_CRE_BASE_CTR = {"DY_CRE_01": 2.90, "DY_CRE_02": 2.75, "DY_CRE_03": 2.60,
                   "DY_CRE_04": 2.50, "DY_CRE_05": 2.85, "DY_CRE_06": 2.70}
DY_CRE_TYPE = {"DY_CRE_01": "视频-口播", "DY_CRE_02": "视频-剧情", "DY_CRE_03": "视频-口播",
               "DY_CRE_04": "视频-图文", "DY_CRE_05": "视频-剧情", "DY_CRE_06": "视频-口播"}
DY_CRE_DECAY = 0.060  # 日衰减系数


DY_CRE_TYPE = {}   # 由 DY_CREATIVES 派生


def dy_channel_ctr(day_idx, d):
    """抖音大盘 CTR(%)：稳态 ~1.75%，09-10 起缓降、09-18 后加速"""
    f = ramp(day_idx, "2026-09-10", "2026-09-22")
    decline = 0.100 * f + 0.157 * (f ** 2)
    base = DY_BASE["ref_ctr"] * (1 - decline)
    # 素材轮换带来的日常水位波动
    base *= noise(f"dy_ctrb_{d.isoformat()}", 0.030)
    # 周末流量结构差异
    if is_weekend(d):
        base *= 0.975
    # 新素材上线后，构造一个逐日恢复段；渠道 CTR 与素材 CTR 同源。
    base *= 1 + {24: 0.04, 25: 0.11, 26: 0.17}.get(d.day, 0.0) if d.year == 2026 and d.month == 9 else 1
    return base


def dy_creative_state(day_idx, d):
    """当日各素材 CTR(%) 与消耗份额；加权 CTR 精确等于大盘 CTR"""
    ch_ctr = dy_channel_ctr(day_idx, d)
    raw = {}
    for cid, launch, w, ctype in DY_CREATIVES:
        ld = date.fromisoformat(launch)
        age = (d - ld).days
        if age < 0 or age >= DY_CRE_LIFE:
            continue
        maturity = math.exp(-DY_CRE_MATURITY_DECAY * age)
        ramp_in = min(1.0, (age + 1) / 3.0)
        raw[cid] = dict(maturity=maturity, share=w * ramp_in, age=age,
                        launch=launch, ctype=ctype)
    tot = sum(v["share"] for v in raw.values())
    for k in raw:
        raw[k]["share"] /= tot
    wavg_mat = sum(v["maturity"] * v["share"] for v in raw.values())
    for k in raw:
        raw[k]["ctr"] = ch_ctr * raw[k]["maturity"] / wavg_mat
    return raw


def dy_regime(day_idx, d):
    """抖音素材衰退显性化程度 0~1"""
    return ramp(day_idx, "2026-09-12", "2026-09-22")


def build_dy(day_idx, d, cre_state):
    ds = d.isoformat()
    sev = dy_regime(day_idx, d)

    ch_ctr = sum(v["ctr"] * v["share"] for v in cre_state.values())
    ratio = ch_ctr / DY_BASE["ref_ctr"]

    # 参竞率随 eCPM 下降而下降
    auction = DY_BASE["auction"] * (ratio ** 0.60) * noise(f"dy_auc_{ds}", 0.030)
    auction = max(0.10, min(0.98, auction))

    # 曝光由「eCPM 竞争力」决定：CTR 越低，拿到的曝光越少
    imp_base = DY_BASE["cost"] / DY_BASE["cpm"] * 1000.0
    impressions = (imp_base * (ratio ** 0.85) * weekend_factor(d)
                   * noise(f"dy_imp_{ds}", 0.030))

    # 剩余可竞得库存需更高出价 -> CPM 结构性上升
    cpm = DY_BASE["cpm"] * (ratio ** 0.30) * noise(f"dy_cpm_{ds}", 0.025)

    cost = impressions * cpm / 1000.0
    cost = min(cost, DY_BASE["budget"])   # 本期不发生预算撞线，仅作保护

    clicks = impressions * ch_ctr / 100.0
    fl = clicks * DY_BASE["r_click_login"] * noise(f"dy_fl_{ds}", 0.030)
    apply_ = fl * DY_BASE["apply"] * noise(f"dy_ap_{ds}", 0.020)
    credit = apply_ * DY_BASE["credit"] * noise(f"dy_cr_{ds}", 0.020)
    safe = apply_ * DY_BASE["safe"] * noise(f"dy_sf_{ds}", 0.020)
    init_ = credit * DY_BASE["init"] * noise(f"dy_in_{ds}", 0.020)
    draw = init_ * DY_BASE["draw"] * noise(f"dy_dr_{ds}", 0.015)

    limit = DY_BASE["limit"] * noise(f"dy_lm_{ds}", 0.008)
    use = DY_BASE["use"] * noise(f"dy_us_{ds}", 0.010)
    amount = draw * limit * use
    limit_total = credit * limit

    return dict(cost=cost, impressions=impressions, clicks=clicks, fl=fl, apply=apply_,
                credit=credit, safe=safe, init=init_, draw=draw, amount=amount,
                limit_total=limit_total, safe_amount=amount * DY_BASE["safe_amt"],
                auction=auction, cpm=cpm, ctr_pct=ch_ctr)


# ============================================================
# 三、派生比率
# ============================================================

def derive(a):
    cost, amt = a["cost"], a["amount"]
    credit, draw, apply_, fl = a["credit"], a["draw"], a["apply"], a["fl"]
    return dict(
        cps=cost / amt if amt else 0,
        credit_cost=cost / credit if credit else 0,
        credit_unit=amt / credit if credit else 0,
        cpcl=cost / a["limit_total"] if a["limit_total"] else 0,
        safe_rate=a["safe"] / apply_ if apply_ else 0,
        draw_rate=draw / credit if credit else 0,
        init_rate=a["init"] / credit if credit else 0,
        limit_use=(amt / draw) / (a["limit_total"] / credit) if draw and credit else 0,
        avg_limit=a["limit_total"] / credit if credit else 0,
        apply_rate=apply_ / fl if fl else 0,
        credit_rate=credit / apply_ if apply_ else 0,
        login_cost=cost / fl if fl else 0,
        apply_cost=cost / apply_ if apply_ else 0,
        conv_cost=cost / draw if draw else 0,
        per_loan=amt / draw if draw else 0,
        safe_amt_ratio=a["safe_amount"] / amt if amt else 0,
        cpm=cost / a["impressions"] * 1000 if a["impressions"] else 0,
        ctr=a["clicks"] / a["impressions"] if a["impressions"] else 0,
        click_login=a["fl"] / a["clicks"] if a["clicks"] else 0,
    )


# ============================================================
# 四、分配工具：把大盘整数口径精确拆分到各细分，保证汇总等于大盘
# ============================================================

CNT_STAGES = ["曝光", "点击", "首登数", "申完数", "授信数", "安全授信数", "发起数", "动支数"]
AMT_STAGES = ["花费", "首借金额", "授信额度合计", "安全首借金额"]
STAGE_ORDER = ["曝光", "点击", "首登数", "申完数", "授信数", "发起数", "动支数"]


def apportion(total, weights):
    """最大余数法：把整数 total 按 weights 分配，和精确等于 total"""
    total = int(total)
    n = len(weights)
    if n == 0:
        return []
    s = sum(weights)
    if s <= 0 or total <= 0:
        return [0] * n
    raw = [total * w / s for w in weights]
    base = [int(math.floor(x)) for x in raw]
    rem = total - sum(base)
    order = sorted(range(n), key=lambda j: -(raw[j] - base[j]))
    for j in order[:max(0, rem)]:
        base[j] += 1
    return base


def alloc_amount(total, weights):
    """金额分配：末段吸收舍入残差，和精确等于 total"""
    n = len(weights)
    if n == 0:
        return []
    s = sum(weights)
    if s <= 0:
        return [0.0] * n
    vals = [round(total * w / s, 2) for w in weights]
    vals[-1] = round(total - sum(vals[:-1]), 2)
    return vals


def _fill_deficit(rows, field, caps, need):
    """把 need 个单位补给仍有余量的行（余量大者优先）"""
    guard = 0
    while need > 0 and guard < 5000:
        guard += 1
        cands = [j for j in range(len(rows)) if rows[j][field] < caps[j]]
        if not cands:
            break
        cands.sort(key=lambda j: -(caps[j] - rows[j][field]))
        rows[cands[0]][field] += 1
        need -= 1


def partition(media_row, stype, names, weights):
    """按 weights 把媒体日级整数口径拆分到各细分，保证：
       - 每个字段之和 == 大盘该字段
       - 每行内部漏斗单调；安全授信数 <= 授信数
    """
    n = len(names)
    rows = [dict(date=media_row["date"], media=media_row["media"],
                 segment_type=stype, segment_name=names[j]) for j in range(n)]

    def W(f):
        return [max(0.0, weights[j].get(f, 0.0)) for j in range(n)]

    for f in CNT_STAGES:
        for j, v in enumerate(apportion(media_row[f], W(f))):
            rows[j][f] = v
    for f in AMT_STAGES:
        for j, v in enumerate(alloc_amount(media_row[f], W(f))):
            rows[j][f] = v

    for stage, prev in zip(STAGE_ORDER[1:], STAGE_ORDER[:-1]):
        caps = [rows[j][prev] for j in range(n)]
        for j in range(n):
            rows[j][stage] = min(rows[j][stage], caps[j])
        _fill_deficit(rows, stage, caps, int(media_row[stage]) - sum(rows[j][stage] for j in range(n)))

    caps = [rows[j]["授信数"] for j in range(n)]
    for j in range(n):
        rows[j]["安全授信数"] = min(rows[j]["安全授信数"], caps[j])
    _fill_deficit(rows, "安全授信数", caps,
                  int(media_row["安全授信数"]) - sum(rows[j]["安全授信数"] for j in range(n)))

    for j in range(n):
        rows[j]["转化数"] = rows[j]["动支数"]
    return rows


# ============================================================
# 四、主流程
# ============================================================

media_rows = []
segment_rows = []
creative_rows = []
funnel_rows = []
quality_rows = []
delivery_rows = []
experiment_rows = []

# 客群分层（腾讯 / 抖音 共用一套定义，份额不同）
COHORTS = [("5W4Q", 0.22), ("4W3Q", 0.29), ("3W2Q", 0.14), ("3W3Q", 0.11), ("2W3Q", 0.24)]
TX_SLOTS = [("微信朋友圈", 0.42), ("腾讯视频", 0.26), ("腾讯新闻", 0.20), ("QQ浏览器", 0.12)]

tx_agg_by_day = {}
dy_agg_by_day = {}

for i, d in enumerate(DATES):
    ds = d.isoformat()

    # ---------- 腾讯 ----------
    tx_segs, tx_agg = build_tx(i, d)
    tx_agg_by_day[ds] = tx_agg
    tx_agg["auction"] = TX_BASE["auction"] * noise(f"tx_auc_{ds}", 0.03)
    tx_agg["cb_ratio"] = TX_BASE["cb_ratio"] * noise(f"tx_cb_{ds}", 0.010)
    tx_der = derive(tx_agg)

    # ---------- 抖音 ----------
    cre_state = dy_creative_state(i, d)
    dy_agg = build_dy(i, d, cre_state)
    dy_agg_by_day[ds] = dy_agg
    # 回传比例：09-20 出现短时波动（次要问题）
    cb = DY_BASE["cb_ratio"] * noise(f"dy_cb_{ds}", 0.012)
    if ds == DY_CB_INCIDENT:
        cb = 0.34
    elif ds == "2026-09-21":
        cb = 0.86
    dy_agg["cb_ratio"] = cb
    dy_der = derive(dy_agg)

    # ---------- 媒体日级（整数口径，作为所有细分的分配基数） ----------
    media_today = {}
    for media, a in (("腾讯", tx_agg), ("抖音", dy_agg)):
        row = dict(
            date=ds, media=media,
            花费=round(a["cost"], 2), 曝光=int(round(a["impressions"])),
            点击=int(round(a["clicks"])), 首登数=int(round(a["fl"])),
            申完数=int(round(a["apply"])), 授信数=int(round(a["credit"])),
            安全授信数=int(round(a["safe"])), 发起数=int(round(a["init"])),
            动支数=int(round(a["draw"])), 首借金额=round(a["amount"], 2),
            授信额度合计=round(a["limit_total"], 2),
            安全首借金额=round(a["safe_amount"], 2), 转化数=int(round(a["draw"])),
            回传比例=round(a["cb_ratio"], 4), 回传比例变化幅度="",
            基础出价=TX_BASE["base_bid"] if media == "腾讯" else DY_BASE["base_bid"],
            参竞率=round(a["auction"], 4),
            日预算=round(TX_BASE["budget"] if media == "腾讯" else DY_BASE["budget"], 2),
        )
        media_today[media] = row
        media_rows.append(row)

    for media in ("腾讯", "抖音"):
        if i > 0:
            prev = [r for r in media_rows
                    if r["date"] == DATES[i - 1].isoformat() and r["media"] == media][0]
            media_today[media]["回传比例变化幅度"] = round(
                media_today[media]["回传比例"] - prev["回传比例"], 4)

    # ---------- 腾讯 细分：RTA策略 ----------
    names, weights = [], []
    for s in tx_segs:
        names.append(s["segment_name"])
        weights.append({"曝光": s["cost"], "点击": s["cost"], "首登数": s["fl"],
                        "申完数": s["apply"], "授信数": s["credit"], "安全授信数": s["safe"],
                        "发起数": s["init"], "动支数": s["draw"], "花费": s["cost"],
                        "首借金额": s["amount"], "授信额度合计": s["limit_total"],
                        "安全首借金额": s["safe_amount"]})
    row_index = {("腾讯", "RTA策略", n): r for n, r in
                 zip(names, partition(media_today["腾讯"], "RTA策略", names, weights))}
    segment_rows.extend(row_index.values())

    # ---------- 腾讯 细分：版位 ----------
    ns = [n for n, _ in TX_SLOTS]
    ws = [{k: sh for k in CNT_STAGES + AMT_STAGES} for _, sh in TX_SLOTS]
    for n, r in zip(ns, partition(media_today["腾讯"], "版位", ns, ws)):
        row_index[("腾讯", "版位", n)] = r
        segment_rows.append(r)

    # ---------- 抖音 细分：账号 ----------
    sev = dy_regime(i, d)
    ns = [n for n, _ in DY_ACCOUNTS]
    ws = []
    for n, sh in DY_ACCOUNTS:
        k = {"授信账户-常规": 0.90, "授信账户-合作方A": 0.92, "ROI账户-常规": 0.99,
             "ROI账户-合作方A": 0.97}[n]
        kk = 1 - (1 - k) * sev
        w = {f: sh for f in CNT_STAGES + AMT_STAGES}
        for f in ("花费", "曝光", "点击"):
            w[f] = sh * kk
        ws.append(w)
    for n, r in zip(ns, partition(media_today["抖音"], "账号", ns, ws)):
        row_index[("抖音", "账号", n)] = r
        segment_rows.append(r)

    # ---------- 客群分层（两个渠道各自的切分） ----------
    for media in ("腾讯", "抖音"):
        ns = [c for c, _ in COHORTS]
        ws = []
        for cname, csh in COHORTS:
            k = cohort_mul(i, cname)
            ws.append({f: csh * k.get(COHORT_FIELD_MAP[f], 1.0)
                       for f in CNT_STAGES + AMT_STAGES})
        for n, r in zip(ns, partition(media_today[media], "客群分层", ns, ws)):
            row_index[(media, "客群分层", n)] = r
            segment_rows.append(r)

    # ---------- 漏斗表（按客群） ----------
    for media in ("腾讯", "抖音"):
        for cname, csh in COHORTS:
            r = row_index[(media, "客群分层", cname)]
            imp, clk = r["曝光"], r["点击"]
            fl, ap, cr = r["首登数"], r["申完数"], r["授信数"]
            it, dr_ = r["发起数"], r["动支数"]
            funnel_rows.append(dict(
                date=ds, media=media, 客群分层=cname,
                曝光=imp, 点击=clk, 首登数=fl, 申完数=ap, 授信数=cr, 发起数=it, 动支数=dr_,
                曝光点击率=round(clk / imp, 6) if imp else 0,
                点击首登率=round(fl / clk, 6) if clk else 0,
                首登申完率=round(ap / fl, 6) if fl else 0,
                申完授信率=round(cr / ap, 6) if ap else 0,
                授信发起率=round(it / cr, 6) if cr else 0,
                发起动支率=round(dr_ / it, 6) if it else 0,
            ))

    # ---------- 素材表（抖音） ----------
    for cid, v in cre_state.items():
        creative_rows.append(dict(
            date=ds, media="抖音", 素材ID=cid, 素材类型=v["ctype"],
            上线日期=v["launch"], 上线天数=v["age"],
            花费=round(media_today["抖音"]["花费"] * v["share"], 2),
            曝光=int(round(media_today["抖音"]["曝光"] * v["share"])),
            点击=int(round(media_today["抖音"]["点击"] * v["share"])),
            CTR=round(v["ctr"] / 100.0, 6),
            完播率3秒=round(min(0.75, max(0.18, v["ctr"] / 100.0 * 22.5)), 4),
            消耗占比=round(v["share"], 4),
        ))

    # ---------- 质量价值表（RTA策略 / 账号） ----------
    for media in ("腾讯", "抖音"):
        stype = "RTA策略" if media == "腾讯" else "账号"
        for sname in (TX_BIDTYPES if media == "腾讯" else [x[0] for x in DY_ACCOUNTS]):
            r = row_index[(media, stype, sname)]
            if media == "腾讯":
                p = TX_SEG[sname]
                safe_amt_ratio = p["safe_amt"]
            else:
                safe_amt_ratio = DY_BASE["safe_amt"]
            cr_ = r["授信数"]
            quality_rows.append(dict(
                date=ds, media=media, segment_type=stype, segment_name=sname,
                申完数=r["申完数"], 授信数=cr_, 安全授信数=r["安全授信数"],
                平均授信额度=round(r["授信额度合计"] / cr_, 2) if cr_ else 0,
                安全过件率=round(r["安全授信数"] / r["申完数"], 6) if r["申完数"] else 0,
                首借金额=r["首借金额"], 安全首借金额=r["安全首借金额"],
                安全交易占比=round(r["安全首借金额"] / r["首借金额"], 4) if r["首借金额"] else 0,
                Vintage30逾期率=round(max(0.004, 0.041 - 0.038 * (safe_amt_ratio - 0.52) / 0.35), 5),
            ))

    # ---------- 投放竞争力表 ----------
    for media, a in (("腾讯", tx_agg), ("抖音", dy_agg)):
        stype = "RTA策略" if media == "腾讯" else "账号"
        for sname in (TX_BIDTYPES if media == "腾讯" else [x[0] for x in DY_ACCOUNTS]):
            r = row_index[(media, stype, sname)]
            # ⚠️ 本字段为"渠道预算按细分个数均分"的**参考值**，不是真实的分策略预算。
            # 真实的预算调整见 ads_rta_change_log（日出价预算）。二者不可混用。
            budget = (TX_BASE["budget"] if media == "腾讯" else DY_BASE["budget"]) / len(
                TX_BIDTYPES if media == "腾讯" else DY_ACCOUNTS)
            cost = r["花费"]
            imp = r["曝光"]
            delivery_rows.append(dict(
                date=ds, media=media, segment_type=stype, segment_name=sname,
                均分预算参考值=round(budget, 2), 消耗=round(cost, 2),
                消耗进度=round(cost / budget, 4) if budget else 0,
                基础出价=TX_BASE["base_bid"] if media == "腾讯" else DY_BASE["base_bid"],
                preA出价系数=round(1.0 + 0.06 * rnd(f"pa_{seedkey(sname)}", -1, 1), 4),
                preG出价系数=round(1.0 + 0.06 * rnd(f"pg_{seedkey(sname)}", -1, 1), 4),
                参竞率=round(a["auction"] * (1 + 0.02 * rnd(f"au_{seedkey(sname)}_{ds}", -1, 1)), 4),
                曝光=imp,
                CPM=round(cost / imp * 1000, 4) if imp else 0,
                pCTR=round(r["点击"] / imp * (1 + 0.02 * rnd(f"pc_{seedkey(sname)}", -1, 1)), 6) if imp else 0,
                pCVR=round(r["授信数"] / r["点击"] * (1 + 0.03 * rnd(f"pv_{seedkey(sname)}", -1, 1)), 6)
                if r["点击"] else 0,
                回传比例=round(a["cb_ratio"], 4),
                回传比例变化幅度="",
            ))

    # ---------- 策略实验表（3W2Q 实验组 / 3W3Q 对照组） ----------
    if d >= date(2026, 8, 25):
        for media in ("腾讯", "抖音"):
            for gname, cohort in (("实验组", "3W2Q"), ("对照组", "3W3Q")):
                g = ramp(i, SHOWCASE_D_CHANGE, COHORT_RAMP_END) if gname == "实验组" else 0.0
                coef = 1.00 + (0.15 if gname == "实验组" else 0.0) * g
                k = cohort_mul(i, cohort)
                if gname == "对照组":
                    k = {kk: 1.0 for kk in k}
                # 实验组 / 对照组直接取该媒体下对应客群分层的细分行，保证与客群表一致
                rs = row_index[(media, "客群分层", cohort)]
                experiment_rows.append(dict(
                    date=ds, media=media, 实验名称="3W2Q客群RTA出价系数上调",
                    分组=gname, 客群=cohort, RTA出价系数=round(coef, 4),
                    花费=rs["花费"], 曝光=rs["曝光"], 点击=rs["点击"], 首登数=rs["首登数"],
                    申完数=rs["申完数"], 授信数=rs["授信数"], 安全授信数=rs["安全授信数"],
                    发起数=rs["发起数"], 动支数=rs["动支数"], 首借金额=rs["首借金额"],
                    授信额度合计=rs["授信额度合计"], 安全首借金额=rs["安全首借金额"],
                ))

# ============================================================
# 五、变更日志
# ============================================================

change_log = [
    # 变更日期, 变更时间, 变更类型, 变更对象, 变更前, 变更后, 操作方, 备注
    ("2026-07-29", "10:20", "素材上线", "抖音-DY_CRE_05", "—", "新素材上线", "素材", "常规素材迭代"),
    ("2026-08-04", "09:40", "素材上线", "抖音-DY_CRE_06", "—", "新素材上线", "素材", "常规素材迭代"),
    ("2026-08-08", "09:15", "排除包更新", "腾讯-全渠道", "排除包 v2026.07", "排除包 v2026.08", "获客策略", "月度黑名单例行更新"),
    ("2026-08-10", "11:00", "素材上线", "抖音-DY_CRE_07", "—", "新素材上线", "素材", "常规素材迭代"),
    ("2026-08-16", "09:30", "素材上线", "抖音-DY_CRE_08", "—", "新素材上线", "素材", "常规素材迭代"),
    ("2026-08-22", "09:30", "素材上线", "抖音-DY_CRE_09", "—", "新素材上线", "素材", "常规素材迭代"),
    ("2026-08-22", "09:30", "素材下线", "抖音-DY_CRE_04", "在投", "已下线", "素材", "投放周期到期"),
    ("2026-08-28", "10:00", "素材上线", "抖音-DY_CRE_10", "—", "新素材上线", "素材", "常规素材迭代"),
    ("2026-09-03", "10:00", "素材上线", "抖音-DY_CRE_11", "—", "新素材上线", "素材", "常规素材迭代"),
    ("2026-09-07", "14:30", "回传规则复核", "腾讯-授信账户", "额度门槛 2.5 万", "额度门槛维持 2.5 万（分箱阈值按周复核）", "策略", "例行复核，未变更实质门槛"),
    ("2026-09-10", "10:00", "预算调整", "抖音-ROI账户-常规", "日预算 11.0 万", "日预算 13.2 万", "获客运营", "为 Q4 冲量预留额度"),
    ("2026-09-10", "10:00", "预算调整", "抖音-授信账户-常规", "日预算 14.0 万", "日预算 17.1 万", "获客运营", "为 Q4 冲量预留额度"),
    ("2026-09-11", "16:45", "定向调整", "抖音-授信账户-合作方A", "人群包 3 个", "人群包 3 个（替换其中 1 个）", "获客策略", "常规人群包轮换"),
    ("2026-09-12", "09:15", "排除包更新", "腾讯-全渠道", "排除包 v2026.08", "排除包 v2026.09", "获客策略", "月度黑名单例行更新"),
    ("2026-09-14", "17:20", "模型上线", "腾讯-额度子分", "v9.2", "v9.3", "获客模型", "额度子模型版本例行迭代"),
    ("2026-09-15", "10:00", "RTA出价系数调整", "腾讯-3W2Q客群", "出价系数 1.00", "出价系数 1.15", "获客策略", "3W2Q 客群提价测试"),
    ("2026-09-15", "10:00", "RTA出价系数调整", "抖音-3W2Q客群", "出价系数 1.00", "出价系数 1.15", "获客策略", "3W2Q 客群提价测试"),
    ("2026-09-16", "08:50", "预算调整", "腾讯RTA策略_A", "日出价预算 5.7 万", "日出价预算 13.0 万", "获客运营", "RTA 策略预算上调"),
    ("2026-09-16", "08:50", "预算调整", "腾讯RTA策略_D", "日出价预算 15.2 万", "日出价预算 10.6 万", "获客运营", "预算在 RTA 策略间腾挪"),
    ("2026-09-19", "09:30", "素材上线", "抖音-DY_CRE_12", "—", "新素材上线", "素材", "补充素材供给"),
    ("2026-09-20", "16:00", "回传链路异常", "抖音-ROI账户", "回传比例 90%", "回传比例 34%", "数据/研发", "短时故障，19:00 后自行恢复"),
    ("2026-09-20", "19:10", "回传链路恢复", "抖音-ROI账户", "回传比例 34%", "回传比例 86%", "数据/研发", "故障恢复，部分数据待补发"),
    ("2026-09-22", "09:00", "定向调整", "抖音-ROI账户-合作方A", "定向人群包 4 个", "定向人群包 3 个", "获客策略", "收缩低效人群包"),
    ("2026-09-23", "09:20", "策略调整", "腾讯RTA策略_A", "原出价与预算", "出价与预算上调", "获客运营", "扩大 A 策略投放测试；效果需用对照验证"),
    ("2026-09-24", "10:00", "素材上线", "抖音-DY_CRE_13", "—", "新剧情素材上线", "素材", "补充素材供给；流量变化不能仅凭时间吻合归因"),
]

# 事件表（业务事件，用于异常与事件的时间关系分析）
# 不写入知识库的结论，只记录"哪天发生了什么"


def write_csv(path, rows, fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"  wrote {os.path.relpath(path, BASE_DIR)}  ({len(rows)} rows)")


def main():
    print("生成模拟数据 …")

    mf = ["date", "media", "花费", "曝光", "点击", "首登数", "申完数", "授信数", "安全授信数",
          "发起数", "动支数", "首借金额", "授信额度合计", "安全首借金额", "转化数",
          "回传比例", "回传比例变化幅度", "基础出价", "参竞率", "日预算"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_media_daily.csv"), media_rows, mf)

    sf = ["date", "media", "segment_type", "segment_name", "花费", "曝光", "点击", "首登数",
          "申完数", "授信数", "安全授信数", "发起数", "动支数", "首借金额", "授信额度合计",
          "安全首借金额", "转化数"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_segment_daily.csv"), segment_rows, sf)

    cf = ["date", "media", "素材ID", "素材类型", "上线日期", "上线天数", "花费", "曝光",
          "点击", "CTR", "完播率3秒", "消耗占比"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_creative_daily.csv"), creative_rows, cf)

    ff = ["date", "media", "客群分层", "曝光", "点击", "首登数", "申完数", "授信数", "发起数",
          "动支数", "曝光点击率", "点击首登率", "首登申完率", "申完授信率", "授信发起率", "发起动支率"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_funnel_daily.csv"), funnel_rows, ff)

    qf = ["date", "media", "segment_type", "segment_name", "申完数", "授信数", "安全授信数",
          "平均授信额度", "安全过件率", "首借金额", "安全首借金额", "安全交易占比", "Vintage30逾期率"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_quality_daily.csv"), quality_rows, qf)

    df = ["date", "media", "segment_type", "segment_name", "均分预算参考值", "消耗", "消耗进度",
          "基础出价", "preA出价系数", "preG出价系数", "参竞率", "曝光", "CPM", "pCTR", "pCVR",
          "回传比例", "回传比例变化幅度"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_delivery_daily.csv"), delivery_rows, df)

    ef = ["date", "media", "实验名称", "分组", "客群", "RTA出价系数", "花费", "曝光", "点击",
          "首登数", "申完数", "授信数", "安全授信数", "发起数", "动支数", "首借金额",
          "授信额度合计", "安全首借金额"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_strategy_experiment.csv"), experiment_rows, ef)

    lf = ["变更日期", "变更时间", "变更类型", "变更对象", "变更前", "变更后", "操作方", "备注"]
    write_csv(os.path.join(OUT_DIR, "ads_rta_change_log.csv"),
              [dict(zip(lf, r)) for r in change_log], lf)

    print("完成。")


if __name__ == "__main__":
    main()
