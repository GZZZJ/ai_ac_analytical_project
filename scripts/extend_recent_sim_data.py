#!/usr/bin/env python3
"""Append reproducible recent synthetic rows to the existing simulation CSVs."""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "03_模拟数据"
BASE_END = date(2026, 9, 22)
END = date(2026, 9, 26)
TABLES = ("media_daily", "segment_daily", "funnel_daily", "quality_daily",
          "delivery_daily", "creative_daily", "strategy_experiment", "change_log")


def read(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def append_if_prefix(path: Path, expected_fields: list[str], expected_rows: list[dict]):
    """Protect existing rows; support safe reruns and resume after partial append."""
    fields, existing = read(path)
    if fields != expected_fields or existing != expected_rows[:len(existing)]:
        raise ValueError(f"既有数据与生成结果不一致，未修改 {path.name}")
    if len(existing) == len(expected_rows):
        return 0
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writerows(expected_rows[len(existing):])
    return len(expected_rows) - len(existing)


def validate_recent(all_rows: dict[str, list[dict]]):
    expected_dates = {(BASE_END + timedelta(days=i)).isoformat() for i in range(1, 5)}
    media_rows = [r for r in all_rows["media_daily"] if r["date"] in expected_dates]
    media = {(r["date"], r["media"]): r for r in media_rows}
    assert len(media) == 8
    for name in TABLES[:-1]:
        rows = [r for r in all_rows[name] if r["date"] in expected_dates]
        assert rows and {r["date"] for r in rows} == expected_dates, name
    for name, group_field, amount_field in (
        ("segment_daily", "segment_type", "花费"),
        ("delivery_daily", "segment_type", "消耗"),
    ):
        sums = defaultdict(float)
        for row in all_rows[name]:
            if row["date"] in expected_dates:
                sums[row["date"], row["media"], row[group_field]] += float(row[amount_field])
        for (day, channel, _), value in sums.items():
            assert abs(value - float(media[day, channel]["花费"])) < 1.0, (name, day, channel)
    for row in media_rows:
        assert float(row["首借金额"]) > 0 and float(row["花费"]) > 0

    def cps(channel, start, end):
        rows = [r for r in all_rows["media_daily"] if r["media"] == channel and start <= r["date"] <= end]
        return sum(float(r["花费"]) for r in rows) / sum(float(r["首借金额"]) for r in rows)

    assert cps("腾讯", "2026-09-20", "2026-09-26") > cps("腾讯", "2026-09-13", "2026-09-19") * 1.1
    assert float(media["2026-09-25", "抖音"]["花费"]) > float(media["2026-09-23", "抖音"]["花费"]) * 1.1


def main():
    with tempfile.TemporaryDirectory(prefix="sim-recent-") as temporary:
        staged = Path(temporary)
        env = dict(os.environ, SIM_DATA_OUT_DIR=str(staged), SIM_DATA_END=END.isoformat())
        subprocess.run([sys.executable, str(ROOT / "scripts/generate_sim_data.py")],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        generated = {}
        for table in TABLES:
            filename = f"ads_rta_{table}.csv"
            fields, rows = read(staged / filename)
            old_fields, old_rows = read(DATA / filename)
            cutoff = "变更日期" if table == "change_log" else "date"
            original = [row for row in rows if row[cutoff] <= BASE_END.isoformat()]
            if old_fields != fields or old_rows[:len(original)] != original:
                raise ValueError(f"原始 56 天与生成结果不一致，未修改 {filename}")
            generated[table] = (fields, rows)
        validate_recent({name: rows for name, (_, rows) in generated.items()})

        sys.path.insert(0, str(ROOT / "scripts"))
        import generate_intraday_data as hourly
        hourly.SOURCE = staged / "ads_rta_media_daily.csv"
        hourly.TARGET = staged / "ads_rta_adgroup_hourly.csv"
        hourly.main()
        hour_fields, hour_rows = read(hourly.TARGET)
        old_hour_fields, old_hour_rows = read(DATA / hourly.TARGET.name)
        original_hours = [r for r in hour_rows if r["date"] <= BASE_END.isoformat()]
        if old_hour_fields != hour_fields or old_hour_rows[:len(original_hours)] != original_hours:
            raise ValueError("原始小时数据与生成结果不一致，未修改数据")

        for table, (fields, rows) in generated.items():
            count = append_if_prefix(DATA / f"ads_rta_{table}.csv", fields, rows)
            print(f"ads_rta_{table}.csv：追加 {count} 行")
        count = append_if_prefix(DATA / hourly.TARGET.name, hour_fields, hour_rows)
        print(f"{hourly.TARGET.name}：追加 {count} 行")
    print(f"模拟数据现覆盖至 {END}；原有记录保持不变。")


if __name__ == "__main__":
    main()
