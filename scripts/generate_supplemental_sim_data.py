#!/usr/bin/env python3
"""在独立目录生成可复现的补充模拟数据，不改动原有九张表。"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "03_模拟数据"
TARGET = ROOT / "03_模拟数据_补充"
OBSERVED_END = date(2026, 9, 26)


def read(name: str) -> list[dict[str, str]]:
    with (SOURCE / name).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write(name: str, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(name)
    TARGET.mkdir(exist_ok=True)
    with (TARGET / name).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{name}: {len(rows)} rows")


def number(value: str) -> int:
    return int(value)


def money(value: str | float) -> int:
    return round(float(value) * 100)


def split(total: int, weights: list[float]) -> list[int]:
    raw = [total * weight / sum(weights) for weight in weights]
    result = [math.floor(value) for value in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - result[i], reverse=True)
    for index in order[: total - sum(result)]:
        result[index] += 1
    return result


def uniform(*parts: object) -> float:
    value = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(value[:12], 16) / 16**12


def time_string(day: date, clock: str) -> str:
    return f"{day.isoformat()} {clock}"


def make_snapshots(media_rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], dict[tuple[str, str], dict[str, object]]]:
    result = []
    same_day = {}
    for source in media_rows:
        day = date.fromisoformat(source["date"])
        for offset, fraction, clock in ((0, 0.38, "22:07:00"), (3, 0.82, "23:59:00"), (7, 1.0, "23:59:00")):
            observed = day + timedelta(days=offset)
            if observed > OBSERVED_END:
                continue
            counts = {name: round(number(source[name]) * fraction) for name in
                      ("申完数", "授信数", "安全授信数", "发起数", "动支数")}
            # 所有环节取相同累计比例，保持漏斗约束；安全授信属于授信子集。
            row = {
                "cohort_date": source["date"], "media": source["media"],
                "as_of": time_string(observed, clock), "maturity_day": offset,
                "花费_发生日累计": f"{money(source['花费']) * (0.92 if offset == 0 else 1) / 100:.2f}",
                "申完数_已观察": counts["申完数"], "授信数_已观察": counts["授信数"],
                "安全授信数_已观察": counts["安全授信数"],
                "发起数_已观察": counts["发起数"], "动支数_已观察": counts["动支数"],
                "首借金额_已观察": f"{money(source['首借金额']) * fraction / 100:.2f}",
            }
            result.append(row)
            if offset == 0:
                same_day[(source["date"], source["media"])] = row
    return result, same_day


def make_backend(same_day: dict[tuple[str, str], dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for (day, media), snap in sorted(same_day.items()):
        internal = int(snap["申完数_已观察"])
        # 媒体目标为「申完回传」，与我方 T7 首借金额不是同一分母或观察窗。
        reported = round(internal * (0.97 + 0.06 * uniform("backend", day, media)))
        rows.append({
            "date": day, "media": media, "as_of": snap["as_of"],
            "归因口径": "媒体点击归因", "归因窗": "点击后7天", "优化目标": "申完回传",
            "媒体消耗": snap["花费_发生日累计"], "媒体目标转化数_已观察": reported,
            "媒体目标转化成本": f"{float(snap['花费_发生日累计']) / reported:.2f}" if reported else "",
            "我方同时间花费": snap["花费_发生日累计"],
            "我方首登cohort申完数_已观察": internal,
            "口径备注": "媒体点击归因与我方首登cohort不逐人对账",
        })
    return rows


def make_auction(media_rows: list[dict[str, str]], delivery_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    source = {(r["date"], r["media"]): r for r in media_rows}
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in delivery_rows:
        groups[(row["date"], row["media"], row["segment_type"])].append(row)
    result = []
    for (day, media, dimension), rows in sorted(groups.items()):
        parent = source[(day, media)]
        spent = [money(r["消耗"]) for r in rows]
        headroom = money(parent["日预算"]) - sum(spent)
        if headroom < 0:
            raise ValueError(f"daily budget below spend: {day} {media}")
        budgets = [spend + add for spend, add in zip(spent, split(headroom, [1 + uniform(day, media, r['segment_name']) for r in rows]))]
        for row, budget in zip(rows, budgets):
            exposed = number(row["曝光"])
            participation = float(row["参竞率"])
            # 1 次竞得映射 1 次曝光；请求与参竞为新构造的过程量。
            requests = math.ceil(exposed / max(participation, 0.05) / (0.47 + 0.12 * uniform("win", day, row["segment_name"])))
            entered = max(exposed, round(requests * participation))
            result.append({
                "date": day, "media": media, "segment_type": dimension,
                "segment_name": row["segment_name"], "日预算_分组配置": f"{budget / 100:.2f}",
                "花费": row["消耗"], "请求数": requests, "参竞次数": entered,
                "竞得次数": exposed, "曝光": exposed,
                "未参竞请求数": requests - entered, "参竞未竞得次数": entered - exposed,
                "基础出价": row["基础出价"], "preA出价系数": row["preA出价系数"],
                "preG出价系数": row["preG出价系数"],
                "预算快照时间": time_string(date.fromisoformat(day), "23:59:00"),
            })
    return result


def make_callback(media_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    result = []
    for row in media_rows:
        day, media = row["date"], row["media"]
        candidates = number(row["授信数"])
        excluded = round(candidates * (0.20 + 0.10 * uniform("rule", day, media)))
        due = candidates - excluded
        successes = round(due * float(row["回传比例"]))
        failures = due - successes
        timeout = round(failures * 0.65)
        result.append({
            "date": day, "media": media, "rule_version": "SIM_R1",
            "授信候选人数": candidates, "规则排除人数": excluded, "符合回传规则人数": due,
            "应回传消息数": due, "发送成功数": successes,
            "发送超时数": timeout, "媒体拒收数": failures - timeout,
            "模拟发送成功率": f"{successes / due:.4f}" if due else "",
            "原表回传比例_参考": row["回传比例"],
            "口径备注": "原表比率独立构造，整数化后仅近似，不可替换原表",
        })
    return result


def make_fulfillment(media_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    result = []
    for row in media_rows:
        initiated = number(row["发起数"])
        drawn = number(row["动支数"])
        failures = initiated - drawn
        risk = round(failures * (0.30 + 0.20 * uniform("risk", row["date"], row["media"])))
        funding = round((failures - risk) * 0.6)
        result.append({
            "date": row["date"], "media": row["media"],
            "cohort_type": "首登日T7终值", "授信数": row["授信数"],
            "未发起数_原因未知": number(row["授信数"]) - initiated,
            "发起数": initiated, "动支数": drawn,
            "发起后风控拒绝数": risk, "资金通道失败数": funding,
            "用户撤销或超时数": failures - risk - funding,
            "口径备注": "未发起不等于用户不愿借；需用户行为事件进一步分辨",
        })
    return result


def make_model(media_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    result = []
    for row in media_rows:
        day, media = row["date"], row["media"]
        for model in ("preA质量模型", "preG转化模型"):
            psi = 0.045 + 0.045 * uniform("psi", day, media, model)
            if "2026-09-14" <= day <= "2026-09-19" and model.startswith("preA"):
                psi += 0.07
            ks = 0.23 + 0.07 * uniform("ks", day, media, model)
            result.append({
                "date": day, "media": media, "model": model,
                "model_version": "V10" if day >= "2026-09-14" else "V9",
                "PSI": f"{psi:.4f}", "PSI样本量": number(row["首登数"]),
                "KS": f"{ks:.4f}" if date.fromisoformat(day) + timedelta(days=7) <= OBSERVED_END else "",
                "KS标签成熟日": (date.fromisoformat(day) + timedelta(days=7)).isoformat(),
                "受影响授信数_上界": row["授信数"],
                "口径备注": "仅模拟模型监控；无逐用户分数，不能证明模型导致业务变化",
            })
    return result


def make_vintage(quality_rows: list[dict[str, str]], segment_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    segment = {(r["date"], r["media"], r["segment_type"], r["segment_name"]): r for r in segment_rows}
    result = []
    for row in quality_rows:
        day = date.fromisoformat(row["date"])
        key = (row["date"], row["media"], row["segment_type"], row["segment_name"])
        loans = number(segment[key]["动支数"])
        mature = day + timedelta(days=30) <= OBSERVED_END
        overdue = round(loans * float(row["Vintage30逾期率"])) if mature else ""
        result.append({
            "cohort_date": row["date"], "media": row["media"],
            "segment_type": row["segment_type"], "segment_name": row["segment_name"],
            "as_of": OBSERVED_END.isoformat(), "Vintage30成熟日": (day + timedelta(days=30)).isoformat(),
            "动支样本数": loans, "Vintage30逾期人数_已观察": overdue,
            "Vintage30逾期率_已观察": f"{overdue / loans:.4f}" if mature and loans else "",
            "是否成熟": "是" if mature else "否",
        })
    return result


def make_audit(media_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    result = []
    for row in media_rows:
        day, media = row["date"], row["media"]
        # 一条跨渠道共用管道异常，用于检验“有聚合值也可能是坏数据”。
        raw_spend = money(row["花费"]) + (1200000 if day == "2026-09-22" else 0)
        warehouse = money(row["花费"])
        result.append({
            "date": day, "media": media, "source_system": "广告媒体消耗账单",
            "源头花费": f"{raw_spend / 100:.2f}", "入仓花费": f"{warehouse / 100:.2f}",
            "媒体日表花费": f"{warehouse / 100:.2f}", "待核对差额": f"{(raw_spend - warehouse) / 100:.2f}",
            "job_status": "RECONCILIATION_PENDING" if raw_spend != warehouse else "SUCCESS",
            "job_finished_at": time_string(date.fromisoformat(day) + timedelta(days=1), "03:15:00"),
            "口径备注": "发生日花费模拟对账；差额为待核查线索，不自动认定入仓故障",
        })
    return result


def make_recent_scenario(media_rows: list[dict[str, str]], hourly_rows: list[dict[str, str]],
                         auction_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """单独构造 9 月 27 日小时场景；不属于现有历史事实表。"""
    baseline_day = "2026-09-26"
    scenario_day = "2026-09-27"
    baseline = {r["media"]: r for r in media_rows if r["date"] == baseline_day}
    result = []
    for media in ("腾讯", "抖音"):
        source = baseline[media]
        hourly_spend = [0] * 24
        for row in hourly_rows:
            if row["date"] == baseline_day and row["media"] == media:
                hourly_spend[int(row["hour"])] += money(row["花费"])
        dimension = "RTA策略" if media == "腾讯" else "账号"
        base_auction = [r for r in auction_rows if r["date"] == baseline_day
                        and r["media"] == media and r["segment_type"] == dimension]
        factor = 1.03 if media == "腾讯" else 0.56
        totals = {
            "花费": round(money(source["花费"]) * factor),
            "请求数": round(sum(int(r["请求数"]) for r in base_auction) * (1.03 if media == "腾讯" else 0.98)),
            "参竞次数": round(sum(int(r["参竞次数"]) for r in base_auction) * (1.03 if media == "腾讯" else 0.90)),
            "竞得次数": round(number(source["曝光"]) * (1.03 if media == "腾讯" else 0.62)),
        }
        assert totals["请求数"] >= totals["参竞次数"] >= totals["竞得次数"]
        allocated = {name: split(total, [value + 1 for value in hourly_spend])
                     for name, total in totals.items()}
        cumulative = baseline_cumulative = 0
        for hour in range(24):
            cumulative += allocated["花费"][hour]
            baseline_cumulative += hourly_spend[hour]
            result.append({
                "scenario_id": "SIM_20260927_DOUYIN_UNDERDELIVERY",
                "date": scenario_day, "hour": hour, "media": media,
                "as_of": f"{scenario_day} {hour:02d}:59:00",
                "日预算": source["日预算"], "本小时花费": f"{allocated['花费'][hour] / 100:.2f}",
                "累计花费": f"{cumulative / 100:.2f}",
                "前日同小时累计花费": f"{baseline_cumulative / 100:.2f}",
                "请求数": allocated["请求数"][hour],
                "参竞次数": allocated["参竞次数"][hour],
                "竞得次数": allocated["竞得次数"][hour],
                "曝光": allocated["竞得次数"][hour],
                "说明": "独立假设场景；竞得下降是观察结果，具体竞价原因未注入",
            })
    return result


def make_legacy_cases() -> list[dict[str, object]]:
    """旧规则模板的独立输入样本，不混入原历史时间序列。"""
    fixtures = [
        ("SIM_NORMAL", "腾讯", 300000, 300, 180, 60, 70, 65, 750000, 5400000, 1.20, 0.02),
        ("SIM_DUAL_TX", "腾讯", 450000, 300, 140, 35, 50, 46, 650000, 4000000, 1.80, -0.55),
        ("SIM_DUAL_DY", "抖音", 450000, 300, 140, 35, 50, 46, 650000, 4000000, "", -0.55),
        ("SIM_COST_COMPONENTS", "腾讯", 500000, 300, 150, 55, 28, 25, 250000, 4200000, 1.25, 0.01),
        ("SIM_SAFETY_HIGH_BILL", "腾讯", 300000, 300, 170, 30, 70, 65, 750000, 5400000, 1.80, 0.02),
        ("SIM_SAFETY_NORMAL_BILL", "腾讯", 300000, 300, 170, 30, 70, 65, 750000, 5400000, 1.20, 0.02),
        ("SIM_SAFETY_DY", "抖音", 300000, 300, 170, 30, 70, 65, 750000, 5400000, "", 0.02),
        ("SIM_LOW_SAMPLE", "腾讯", 25000, 25, 15, 2, 5, 4, 30000, 400000, 1.80, -0.55),
        ("SIM_MISSING_AMOUNT", "腾讯", 300000, 300, 180, 60, 70, 65, "", 5400000, 1.20, 0.02),
    ]
    result = []
    for case_id, media, spend, apply, credit, safe, initiated, drawn, amount, limit, bill, callback_delta in fixtures:
        result.append({
            "scenario_id": case_id, "media": media, "sample_type": "独立规则输入样本",
            "花费": spend, "申完数": apply, "授信数": credit, "安全授信数": safe,
            "发起数": initiated, "动支数": drawn, "首借金额": amount,
            "授信额度合计": limit, "腾讯计费比_外部输入": bill,
            "旧卡片CPS_关键字段": f"{spend / amount:.4f}" if amount else "",
            "回传比例变化幅度_外部输入": callback_delta,
            "备注": "不代表历史观测；空值与零值不同",
        })
    return result


def make_legacy_subgroups() -> list[dict[str, object]]:
    partition = [
        (180000, 90, 35, 5, 13, 12, 180000, 900000),
        (110000, 70, 34, 10, 12, 11, 180000, 1000000),
        (90000, 70, 35, 10, 13, 12, 170000, 1050000),
        (70000, 70, 36, 10, 12, 11, 120000, 1050000),
    ]
    result = []
    for case_id, media, dimension, names in (
        ("SIM_DUAL_TX", "腾讯", "RTA策略", [f"腾讯RTA策略_{letter}" for letter in "ABCD"]),
        ("SIM_DUAL_TX", "腾讯", "版位", ["微信朋友圈", "腾讯视频", "腾讯新闻", "QQ浏览器"]),
        ("SIM_DUAL_DY", "抖音", "账号", ["授信账户-常规", "授信账户-合作方A", "ROI账户-常规", "ROI账户-合作方A"]),
    ):
        for name, values in zip(names, partition):
            spend, apply, credit, safe, initiated, drawn, amount, limit = values
            result.append({
                "scenario_id": case_id, "media": media, "segment_type": dimension,
                "segment_name": name, "花费": spend, "申完数": apply,
                "授信数": credit, "安全授信数": safe, "发起数": initiated,
                "动支数": drawn, "首借金额": amount, "授信额度合计": limit,
            })
    return result


def make_alert(snapshots: dict[tuple[str, str], dict[str, object]], media_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    result = []
    for row in media_rows:
        day, media = row["date"], row["media"]
        snap = snapshots[(day, media)]
        apply = int(snap["申完数_已观察"])
        safe = int(snap["安全授信数_已观察"])
        drawn = int(snap["动支数_已观察"])
        amount = float(snap["首借金额_已观察"])
        spend = float(snap["花费_发生日累计"])
        cps = spend / amount if amount else None
        safety = safe / apply if apply else None
        for metric, value, abnormal, sample, minimum in (
            ("CPS", cps, cps is not None and cps > 1.3074, drawn, 8),
            ("安全过件率", safety, safety is not None and safety < 0.17, apply, 30),
        ):
            result.append({
                "date": day, "media": media, "evaluated_at": snap["as_of"],
                "rule_version": "SIM_T0_22H_V1", "metric": metric,
                "observation_window": "首登cohort T0截至22:07",
                "metric_value": f"{value:.4f}" if value is not None else "",
                "threshold": "1.3074" if metric == "CPS" else "0.17",
                "sample_size": sample, "min_sample": minimum,
                "status": "DATA_INSUFFICIENT" if sample < minimum else "ALERT" if abnormal else "NORMAL",
                "sent": "否", "send_reason": "仅为规则重放，非真实推送日志",
            })
    return result


def main() -> None:
    media = read("ads_rta_media_daily.csv")
    delivery = read("ads_rta_delivery_daily.csv")
    quality = read("ads_rta_quality_daily.csv")
    segment = read("ads_rta_segment_daily.csv")
    hourly = read("ads_rta_adgroup_hourly.csv")
    snapshots, same_day = make_snapshots(media)
    write("ads_rta_asof_cohort_snapshots.csv", snapshots)
    write("ads_rta_media_backend_daily.csv", make_backend(same_day))
    auction = make_auction(media, delivery)
    write("ads_rta_auction_budget_daily.csv", auction)
    write("ads_rta_callback_daily.csv", make_callback(media))
    write("ads_rta_fulfillment_daily.csv", make_fulfillment(media))
    write("ads_rta_model_monitor_daily.csv", make_model(media))
    write("ads_rta_vintage30_cohort.csv", make_vintage(quality, segment))
    write("ads_rta_pipeline_audit_daily.csv", make_audit(media))
    write("ads_rta_alert_replay_daily.csv", make_alert(same_day, media))
    write("ads_rta_20260927_delivery_scenario.csv", make_recent_scenario(media, hourly, auction))
    write("ads_rta_legacy_alert_cases.csv", make_legacy_cases())
    write("ads_rta_legacy_subgroup_cases.csv", make_legacy_subgroups())


if __name__ == "__main__":
    main()
