#!/usr/bin/env python3
"""生成小时广告组投放及该小时归因 cohort 的 T0/T3/T7 离线终值。

投放基础量拆分自媒体日表；转化按聚合行模拟，不生成用户明细。
转化的 cohort 是归因触达时段，区别于原媒体日表的首登日 cohort。
"""

import csv
import hashlib
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "03_模拟数据/ads_rta_media_daily.csv"
TARGET = ROOT / "03_模拟数据/ads_rta_adgroup_hourly.csv"
STAGES = ("申完数", "授信数", "安全授信数", "发起数", "动支数", "动支金额")
FIELDS = (
    ["date", "hour", "media", "ad_group", "花费", "曝光", "点击", "首登数",
     "30天以上未申完老户归因触达数", "归因触达人数"]
    + [f"T{day}{stage}" for day in (0, 3, 7) for stage in STAGES]
)
GROUPS = {
    "腾讯": [f"TX_AG_{i:02d}" for i in range(1, 5)],
    "抖音": [f"DY_AG_{i:02d}" for i in range(1, 5)],
}


def unit(*parts):
    digest = hashlib.md5("|".join(map(str, parts)).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / 16**12


def active_groups(groups, date, hour):
    """白天每媒体 3 组，00 点 2 组，01–05 点每媒体 1 组。"""
    shift = int(date[-2:]) + hour
    if hour == 0:
        return [groups[shift % 4], groups[(shift + 1) % 4]]
    if 1 <= hour <= 5:
        return [groups[shift % 4]]
    return [g for i, g in enumerate(groups) if i != shift % 4]


def weight(date, media, hour, group):
    # 行数占比与指标数值占比独立；夜间有记录的组也通常只拿到少量流量。
    if hour == 0:
        time_weight = 0.28
    elif 1 <= hour <= 5:
        time_weight = 0.06
    else:
        time_weight = 0.90 + 0.22 * math.sin((hour - 6) * math.pi / 17)
    group_weight = [1.10, 0.95, 1.20, 0.85][int(group[-2:]) - 1]
    jitter = 0.85 + 0.30 * unit("weight", date, media, hour, group)
    return time_weight * group_weight * jitter


def allocate(total, weights):
    """最大余数法：整数或分的汇总严格等于媒体日表。"""
    weighted = [total * w / sum(weights) for w in weights]
    base = [math.floor(value) for value in weighted]
    remaining = total - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (weighted[i] - base[i], -i), reverse=True)
    for i in order[:remaining]:
        base[i] += 1
    return base


def allocate_capped(total, weights, caps):
    """把总量分到各行，同时不超过上一漏斗节点的行级容量。"""
    assert 0 <= total <= sum(caps)
    result = [0] * len(caps)
    remaining = total
    while remaining:
        spare = [cap - value for cap, value in zip(caps, result)]
        eligible_weights = [w if space else 0 for w, space in zip(weights, spare)]
        proposed = allocate(remaining, eligible_weights)
        accepted = [min(value, space) for value, space in zip(proposed, spare)]
        for index, value in enumerate(accepted):
            result[index] += value
        remaining -= sum(accepted)
    return result


def add_conversions(rows, daily):
    """在聚合行上生成归因 cohort 的累计转化；不声称还原真实触点。"""
    login_total = max(1, int(daily["首登数"]))
    apply_rate = int(daily["申完数"]) / login_total
    credit_rate = int(daily["授信数"]) / max(1, int(daily["申完数"]))
    safe_rate = int(daily["安全授信数"]) / max(1, int(daily["授信数"]))
    initiate_rate = int(daily["发起数"]) / max(1, int(daily["授信数"]))
    draw_rate = int(daily["动支数"]) / max(1, int(daily["发起数"]))
    mean_draw = float(daily["首借金额"]) / max(1, int(daily["动支数"]))

    # 可再次投放的老户仅表示「首登超过 30 天且未申完」的聚合归因人数。
    # 这是模拟假设，不能据此检验真实排除包或跨渠道末次触点规则。
    old_total = round(int(daily["首登数"]) * 0.12)
    old_parts = allocate(old_total, [int(row["点击"]) + 1 for row in rows])

    factors = [
        0.88 + 0.24 * unit("conversion", row["date"], row["media"], row["hour"], row["ad_group"])
        for row in rows
    ]
    populations = [int(row["首登数"]) + old for row, old in zip(rows, old_parts)]
    for row, old, population in zip(rows, old_parts, populations):
        row["30天以上未申完老户归因触达数"] = old
        row["归因触达人数"] = population

    n = sum(populations)
    apply = allocate_capped(round(n * apply_rate),
                            [v * f for v, f in zip(populations, factors)], populations)
    credit = allocate_capped(round(sum(apply) * credit_rate),
                             [v * f for v, f in zip(apply, factors)], apply)
    safe = allocate_capped(round(sum(credit) * safe_rate),
                           [v * f for v, f in zip(credit, factors)], credit)
    initiated = allocate_capped(round(sum(credit) * initiate_rate),
                                [v * f for v, f in zip(credit, factors)], credit)
    drawn = allocate_capped(round(sum(initiated) * draw_rate),
                            [v * f for v, f in zip(initiated, factors)], initiated)

    for index, row in enumerate(rows):
        hour = row["hour"]
        # 同一行 T0/T3/T7 是相同归因 cohort 的累计观察窗，不是三个独立分组。
        t0_fraction = 0.28 + 0.30 * (24 - hour) / 24
        for day, fraction in ((0, t0_fraction), (3, 0.84), (7, 1.0)):
            values = {
                "申完数": round(apply[index] * fraction),
                "授信数": round(credit[index] * fraction),
                "安全授信数": round(safe[index] * fraction),
                "发起数": round(initiated[index] * fraction),
                "动支数": round(drawn[index] * fraction),
            }
            for stage, value in values.items():
                row[f"T{day}{stage}"] = value
            row[f"T{day}动支金额"] = f"{values['动支数'] * mean_draw * factors[index]:.2f}"


def main():
    with SOURCE.open(encoding="utf-8", newline="") as file:
        daily_rows = list(csv.DictReader(file))

    result = []
    for daily in daily_rows:
        date, media = daily["date"], daily["media"]
        slots = [
            (hour, group)
            for hour in range(24)
            for group in active_groups(GROUPS[media], date, hour)
        ]
        weights = [weight(date, media, hour, group) for hour, group in slots]
        columns = {
            "花费": (int(round(float(daily["花费"]) * 100)), 100),
            "曝光": (int(daily["曝光"]), 1),
            "点击": (int(daily["点击"]), 1),
            "首登数": (int(daily["首登数"]), 1),
        }
        parts = {name: allocate(total, weights) for name, (total, _) in columns.items()}
        for index, (hour, group) in enumerate(slots):
            row = {"date": date, "hour": hour, "media": media, "ad_group": group}
            for name, (_, scale) in columns.items():
                value = parts[name][index]
                row[name] = f"{value / scale:.2f}" if scale == 100 else value
            result.append(row)

        add_conversions(result[-len(slots):], daily)

    # 按完整主键稳定排序，方便人工查看和重复生成后的差异检查。
    result.sort(key=lambda row: (row["date"], row["hour"], row["media"], row["ad_group"]))
    with TARGET.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(result)
    print(f"已生成 {TARGET}，共 {len(result)} 行")


if __name__ == "__main__":
    main()
