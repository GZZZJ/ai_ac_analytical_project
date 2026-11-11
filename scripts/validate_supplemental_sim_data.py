#!/usr/bin/env python3
"""校验补充表的主键、时间可得性、业务约束及与原表的可对账字段。"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "03_模拟数据"
TARGET = ROOT / "03_模拟数据_补充"
END = date(2026, 9, 26)


def read(folder: Path, name: str) -> list[dict[str, str]]:
    with (folder / name).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def unique(rows: list[dict[str, str]], fields: tuple[str, ...], name: str) -> None:
    keys = [tuple(row[field] for field in fields) for row in rows]
    assert len(keys) == len(set(keys)), f"重复主键: {name} {fields}"


def check() -> None:
    media = read(SOURCE, "ads_rta_media_daily.csv")
    delivery = read(SOURCE, "ads_rta_delivery_daily.csv")
    quality = read(SOURCE, "ads_rta_quality_daily.csv")
    segment = read(SOURCE, "ads_rta_segment_daily.csv")
    source = {(r["date"], r["media"]): r for r in media}
    assert len(source) == len(media)
    prefix = "ads_rta_"
    snaps = read(TARGET, prefix + "asof_cohort_snapshots.csv")
    backend = read(TARGET, prefix + "media_backend_daily.csv")
    auction = read(TARGET, prefix + "auction_budget_daily.csv")
    callback = read(TARGET, prefix + "callback_daily.csv")
    fulfillment = read(TARGET, prefix + "fulfillment_daily.csv")
    model = read(TARGET, prefix + "model_monitor_daily.csv")
    vintage = read(TARGET, prefix + "vintage30_cohort.csv")
    audit = read(TARGET, prefix + "pipeline_audit_daily.csv")
    alert = read(TARGET, prefix + "alert_replay_daily.csv")
    scenario = read(TARGET, prefix + "20260927_delivery_scenario.csv")
    legacy_cases = read(TARGET, prefix + "legacy_alert_cases.csv")
    legacy_subgroups = read(TARGET, prefix + "legacy_subgroup_cases.csv")
    unique(snaps, ("cohort_date", "media", "maturity_day"), "snapshots")
    unique(backend, ("date", "media"), "backend")
    unique(auction, ("date", "media", "segment_type", "segment_name"), "auction")
    unique(callback, ("date", "media"), "callback")
    unique(fulfillment, ("date", "media"), "fulfillment")
    unique(model, ("date", "media", "model"), "model")
    unique(vintage, ("cohort_date", "media", "segment_type", "segment_name"), "vintage")
    unique(audit, ("date", "media"), "audit")
    unique(alert, ("date", "media", "metric"), "alert")
    unique(scenario, ("scenario_id", "date", "hour", "media"), "scenario")
    unique(legacy_cases, ("scenario_id",), "legacy_cases")
    unique(legacy_subgroups, ("scenario_id", "segment_type", "segment_name"), "legacy_subgroups")

    assert len(backend) == len(callback) == len(fulfillment) == len(audit) == len(source)
    assert len(model) == len(alert) == 2 * len(source)
    assert len(auction) == len(delivery) and len(vintage) == len(quality)
    grouped_snapshots = defaultdict(dict)
    for row in snaps:
        key = (row["cohort_date"], row["media"])
        assert key in source
        day = int(row["maturity_day"])
        assert day in (0, 3, 7)
        as_of = datetime.fromisoformat(row["as_of"])
        assert as_of.date() <= END and as_of.date() == date.fromisoformat(key[0]) + timedelta(days=day)
        expected_clock = "22:07:00" if day == 0 else "23:59:00"
        assert as_of.time().isoformat() == expected_clock
        values = [int(row[f"{field}_已观察"]) for field in
                  ("申完数", "授信数", "安全授信数", "发起数", "动支数")]
        applied, credited, safe, initiated, drawn = values
        assert applied >= credited >= initiated >= drawn >= 0 and credited >= safe >= 0
        assert Decimal(row["首借金额_已观察"]) >= 0
        grouped_snapshots[key][day] = row
        if day == 7:
            for field in ("申完数", "授信数", "安全授信数", "发起数", "动支数"):
                assert int(row[f"{field}_已观察"]) == int(source[key][field]), (key, field)
            assert Decimal(row["首借金额_已观察"]) == Decimal(source[key]["首借金额"])
    for key, days in grouped_snapshots.items():
        assert 0 in days and set(days) == {day for day in (0, 3, 7)
                                             if date.fromisoformat(key[0]) + timedelta(days=day) <= END}
        for field in ("申完数", "授信数", "安全授信数", "发起数", "动支数"):
            amounts = [int(days[day][f"{field}_已观察"]) for day in sorted(days)]
            assert amounts == sorted(amounts)

    for row in backend:
        key = (row["date"], row["media"])
        snap = grouped_snapshots[key][0]
        assert row["as_of"] == snap["as_of"]
        assert Decimal(row["媒体消耗"]) == Decimal(row["我方同时间花费"]) == Decimal(snap["花费_发生日累计"])
        assert int(row["我方首登cohort申完数_已观察"]) == int(snap["申完数_已观察"])
        assert int(row["媒体目标转化数_已观察"]) >= 0

    original_auction = {(r["date"], r["media"], r["segment_type"], r["segment_name"]): r for r in delivery}
    auction_groups = defaultdict(lambda: {"budget": Decimal(0), "spend": Decimal(0)})
    for row in auction:
        key = (row["date"], row["media"], row["segment_type"], row["segment_name"])
        assert key in original_auction
        original = original_auction[key]
        assert Decimal(row["花费"]) == Decimal(original["消耗"])
        assert Decimal(row["日预算_分组配置"]) >= Decimal(row["花费"])
        req, entered, won, exposed = (int(row[name]) for name in ("请求数", "参竞次数", "竞得次数", "曝光"))
        assert req >= entered >= won == exposed == int(original["曝光"])
        assert req - entered == int(row["未参竞请求数"])
        assert entered - won == int(row["参竞未竞得次数"])
        assert abs(entered / req - float(original["参竞率"])) < 0.00001
        group = auction_groups[key[:3]]
        group["budget"] += Decimal(row["日预算_分组配置"])
        group["spend"] += Decimal(row["花费"])
    for (day, media, _), totals in auction_groups.items():
        assert totals["budget"] == Decimal(source[(day, media)]["日预算"])
        assert totals["spend"] == Decimal(source[(day, media)]["花费"])

    for row in callback:
        key = (row["date"], row["media"])
        candidates, excluded, due, sent, timeout, rejected = (int(row[name]) for name in
            ("授信候选人数", "规则排除人数", "应回传消息数", "发送成功数", "发送超时数", "媒体拒收数"))
        assert candidates == int(source[key]["授信数"]) and candidates == excluded + due
        assert due == int(row["符合回传规则人数"]) == sent + timeout + rejected
        assert row["rule_version"] == "SIM_R1"
        assert abs(sent / due - float(row["模拟发送成功率"])) < 0.0001
        assert abs(sent / due - float(row["原表回传比例_参考"])) < 0.01

    for row in fulfillment:
        key = (row["date"], row["media"])
        original = source[key]
        credit, no_start, start, draw, risk, funding, other = (int(row[name]) for name in
            ("授信数", "未发起数_原因未知", "发起数", "动支数", "发起后风控拒绝数", "资金通道失败数", "用户撤销或超时数"))
        assert credit == int(original["授信数"]) == no_start + start
        assert start == int(original["发起数"]) == draw + risk + funding + other
        assert draw == int(original["动支数"])

    for row in model:
        key = (row["date"], row["media"])
        assert int(row["PSI样本量"]) == int(source[key]["首登数"])
        assert 0 <= float(row["PSI"]) < 1
        mature = date.fromisoformat(row["KS标签成熟日"]) <= END
        assert (row["KS"] != "") == mature
        if mature:
            assert 0 <= float(row["KS"]) <= 1

    source_segment = {(r["date"], r["media"], r["segment_type"], r["segment_name"]): r for r in segment}
    for row in vintage:
        key = (row["cohort_date"], row["media"], row["segment_type"], row["segment_name"])
        assert int(row["动支样本数"]) == int(source_segment[key]["动支数"])
        mature = date.fromisoformat(row["Vintage30成熟日"]) <= END
        assert (row["是否成熟"] == "是") == mature
        assert (row["Vintage30逾期人数_已观察"] != "") == mature
        assert (row["Vintage30逾期率_已观察"] != "") == mature and int(row["Vintage30逾期人数_已观察"] or 0) <= int(row["动支样本数"])

    delayed = []
    for row in audit:
        key = (row["date"], row["media"])
        src, wh, summary = (Decimal(row[name]) for name in
            ("源头花费", "入仓花费", "媒体日表花费"))
        assert wh == summary == Decimal(source[key]["花费"])
        assert src - wh == Decimal(row["待核对差额"])
        if src != wh:
            delayed.append(key)
            assert row["job_status"] == "RECONCILIATION_PENDING"
        else:
            assert row["job_status"] == "SUCCESS"
    assert set(delayed) == {("2026-09-22", "腾讯"), ("2026-09-22", "抖音")}

    for row in alert:
        key = (row["date"], row["media"])
        assert row["evaluated_at"] == grouped_snapshots[key][0]["as_of"]
        assert row["sent"] == "否"
        sample, minimum = int(row["sample_size"]), int(row["min_sample"])
        if sample < minimum:
            assert row["status"] == "DATA_INSUFFICIENT"
        else:
            value, threshold = float(row["metric_value"]), float(row["threshold"])
            abnormal = value > threshold if row["metric"] == "CPS" else value < threshold
            assert (row["status"] == "ALERT") == abnormal

    by_media = defaultdict(list)
    for row in scenario:
        assert row["scenario_id"] == "SIM_20260927_DOUYIN_UNDERDELIVERY"
        assert row["date"] == "2026-09-27" and 0 <= int(row["hour"]) <= 23
        assert row["as_of"] == f"2026-09-27 {int(row['hour']):02d}:59:00"
        assert int(row["请求数"]) >= int(row["参竞次数"]) >= int(row["竞得次数"]) == int(row["曝光"])
        by_media[row["media"]].append(row)
    assert set(by_media) == {"腾讯", "抖音"}
    for media_name, rows in by_media.items():
        rows.sort(key=lambda r: int(r["hour"]))
        assert [int(r["hour"]) for r in rows] == list(range(24))
        running = Decimal(0)
        for row in rows:
            running += Decimal(row["本小时花费"])
            assert running == Decimal(row["累计花费"])
            assert running <= Decimal(row["日预算"])
        baseline_spend = Decimal(source[("2026-09-26", media_name)]["花费"])
        assert (running < baseline_spend * Decimal("0.6")) if media_name == "抖音" else running > baseline_spend

    assert len(legacy_cases) == 9
    for row in legacy_cases:
        apply, credited, safe, initiated, drawn = (int(row[name]) for name in
            ("申完数", "授信数", "安全授信数", "发起数", "动支数"))
        assert apply >= credited >= initiated >= drawn >= 0 and credited >= safe >= 0
        assert Decimal(row["花费"]) > 0 and Decimal(row["授信额度合计"]) > 0
        if row["scenario_id"] == "SIM_MISSING_AMOUNT":
            assert row["首借金额"] == row["旧卡片CPS_关键字段"] == ""
        else:
            assert Decimal(row["首借金额"]) > 0
            assert abs(Decimal(row["旧卡片CPS_关键字段"]) -
                       Decimal(row["花费"]) / Decimal(row["首借金额"])) < Decimal("0.0001")
        assert (row["腾讯计费比_外部输入"] == "") == (row["media"] == "抖音")
    cases = {row["scenario_id"]: row for row in legacy_cases}
    assert int(cases["SIM_LOW_SAMPLE"]["动支数"]) < 8
    assert int(cases["SIM_LOW_SAMPLE"]["申完数"]) < 30
    for name in ("SIM_DUAL_TX", "SIM_DUAL_DY"):
        row = cases[name]
        assert Decimal(row["花费"]) / Decimal(row["首借金额"]) > Decimal("0.54")
        assert Decimal(row["安全授信数"]) / Decimal(row["申完数"]) < Decimal("0.17")

    grouped_legacy = defaultdict(list)
    for row in legacy_subgroups:
        grouped_legacy[(row["scenario_id"], row["segment_type"])].append(row)
        assert row["media"] == cases[row["scenario_id"]]["media"]
        assert row["segment_type"] in (("RTA策略", "版位") if row["media"] == "腾讯" else ("账号",))
    assert set(grouped_legacy) == {("SIM_DUAL_TX", "RTA策略"), ("SIM_DUAL_TX", "版位"), ("SIM_DUAL_DY", "账号")}
    for (case_id, dimension), parts in grouped_legacy.items():
        assert len(parts) == 4
        for field in ("花费", "申完数", "授信数", "安全授信数", "发起数", "动支数", "首借金额", "授信额度合计"):
            assert sum(Decimal(part[field]) for part in parts) == Decimal(cases[case_id][field]), (case_id, dimension, field)
        assert any(Decimal(part["花费"]) / Decimal(part["首借金额"]) <= Decimal("0.54") for part in parts)
        assert any(Decimal(part["花费"]) / Decimal(part["首借金额"]) > Decimal("0.54") for part in parts)

    print(f"PASS: 12 张补充表，{sum(map(len, (snaps, backend, auction, callback, fulfillment, model, vintage, audit, alert, scenario, legacy_cases, legacy_subgroups)))} 行。")
    print("PASS: 主键、T0/T3/T7 可得性、原表对账、回传/放款恒等式、Vintage 成熟度和告警重放均通过。")


if __name__ == "__main__":
    check()
