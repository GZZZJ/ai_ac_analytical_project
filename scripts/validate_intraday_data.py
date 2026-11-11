#!/usr/bin/env python3
"""校验小时广告组数据的主键、日级对账和 T0/T3/T7 累计约束。"""

import csv
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "03_模拟数据"
FIELDS = ["花费", "曝光", "点击", "首登数"]
STAGES = ["申完数", "授信数", "安全授信数", "发起数", "动支数", "动支金额"]


def read(name):
    with (DATA / name).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def main():
    daily = read("ads_rta_media_daily.csv")
    hourly = read("ads_rta_adgroup_hourly.csv")
    expected = {(row["date"], row["media"]): row for row in daily}
    assert len(expected) == len(daily), "媒体日表主键重复"

    keys = [(r["date"], int(r["hour"]), r["media"], r["ad_group"]) for r in hourly]
    assert len(set(keys)) == len(keys), "小时表主键重复"
    assert all(0 <= hour <= 23 for _, hour, _, _ in keys), "小时超出 0–23"
    assert all((date, media) in expected for date, _, media, _ in keys), "出现媒体日表以外的日期或媒体"
    assert all(group.startswith("TX_AG_" if media == "腾讯" else "DY_AG_")
               for _, _, media, group in keys), "广告组归属媒体错误"

    sums = defaultdict(lambda: {field: Decimal(0) for field in FIELDS})
    hour_counts = Counter()
    for row in hourly:
        key = (row["date"], row["media"])
        hour_counts[int(row["hour"])] += 1
        for field in FIELDS:
            value = Decimal(row[field])
            assert value >= 0, f"出现负值：{key} {field}"
            sums[key][field] += value
        assert Decimal(row["曝光"]) >= Decimal(row["点击"]) >= Decimal(row["首登数"]), \
            f"小时基础漏斗违例：{key} {row['hour']} {row['ad_group']}"
        old = int(row["30天以上未申完老户归因触达数"])
        population = int(row["归因触达人数"])
        assert old >= 0 and population == int(row["首登数"]) + old, \
            f"归因触达人数不自洽：{key} {row['hour']} {row['ad_group']}"
        for stage in STAGES:
            values = [Decimal(row[f"T{day}{stage}"]) for day in (0, 3, 7)]
            assert 0 <= values[0] <= values[1] <= values[2], \
                f"转化累计值不单调：{key} {row['hour']} {stage}"
        for day in (0, 3, 7):
            prefix = f"T{day}"
            apply = int(row[prefix + "申完数"])
            credit = int(row[prefix + "授信数"])
            safe = int(row[prefix + "安全授信数"])
            initiated = int(row[prefix + "发起数"])
            drawn = int(row[prefix + "动支数"])
            assert population >= apply >= credit >= initiated >= drawn, \
                f"转化漏斗违例：{key} {row['hour']} {prefix}"
            assert safe <= credit, f"安全授信数超过授信数：{key} {row['hour']} {prefix}"
            assert (Decimal(row[prefix + "动支金额"]) == 0) == (drawn == 0), \
                f"动支金额与动支人数不一致：{key} {row['hour']} {prefix}"

    assert set(sums) == set(expected), "媒体日表存在未覆盖的日期或媒体"
    for key, source in expected.items():
        for field in FIELDS:
            assert sums[key][field] == Decimal(source[field]), f"日级对账失败：{key} {field}"

    total = len(hourly)
    daytime_shares = [hour_counts[hour] / total for hour in range(6, 24)]
    assert all(0.04 <= share <= 0.05 for share in daytime_shares), "06–23 点记录数占比不在 4%–5%"
    assert all(hour_counts[hour] < hour_counts[6] for hour in range(1, 6)), "01–05 点记录不够稀疏"
    # 缺行就是缺行；不应为所有日期、小时、广告组补齐 0 值行。
    assert total < len(daily) * 24 * 4, "小时表被补齐为完整笛卡尔积"

    zero_logins = sum(Decimal(row["首登数"]) == 0 for row in hourly)
    assert zero_logins > 0, "缺少用于区分缺行与零值的已记录零值"
    old_touches = sum(int(row["30天以上未申完老户归因触达数"]) for row in hourly)
    assert old_touches > 0, "缺少 30 天以上未申完老户再次触达场景"
    print(f"PASS：{total} 行，{len(expected)} 个媒体日对账全部一致；小时主键唯一、基础漏斗成立。")
    print(f"PASS：06–23 点单小时记录占比 {min(daytime_shares):.2%}–{max(daytime_shares):.2%}；"
          f"01–05 点单小时 {hour_counts[1]} 行，白天单小时 {hour_counts[6]} 行。")
    print(f"PASS：存在 {zero_logins} 条首登数为 0 的记录；缺行不补 0。")
    print(f"PASS：{old_touches} 个老户归因触达；全部行的 T0/T3/T7 累计、漏斗和动支金额约束成立。")


if __name__ == "__main__":
    main()
