"""Read-only access to the benchmark CSVs and metric definitions."""
from __future__ import annotations

import csv
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "03_模拟数据"
with (DATA / "ads_rta_media_daily.csv").open(encoding="utf-8-sig", newline="") as _stream:
    AS_OF = max(date.fromisoformat(row["date"]) for row in csv.DictReader(_stream))
NUMERIC = {"花费", "曝光", "点击", "首登数", "申完数", "授信数", "安全授信数", "发起数", "动支数", "首借金额", "授信额度合计", "安全首借金额", "转化数", "日预算", "基础出价", "参竞率", "pCTR", "pCVR", "回传比例", "CTR", "CPM", "均分预算参考值", "消耗", "RTA出价系数"}
TOTALS = ("花费", "曝光", "点击", "首登数", "申完数", "授信数", "安全授信数", "发起数", "动支数", "首借金额", "授信额度合计", "安全首借金额")


@lru_cache(maxsize=16)
def load(table: str) -> tuple[dict, ...]:
    if table not in {"media", "segment", "delivery", "creative", "quality", "strategy_experiment", "change_log", "funnel"}:
        raise ValueError("Unknown table")
    path = DATA / f"ads_rta_{table}_daily.csv"
    if table in {"strategy_experiment", "change_log"}:
        path = DATA / f"ads_rta_{table}.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return tuple({key: float(value) if key in NUMERIC and value else value for key, value in row.items()} for row in csv.DictReader(stream))


def rows(table: str, start: date, end: date, **filters: str) -> list[dict]:
    key = "变更日期" if table == "change_log" else "date"
    return [row for row in load(table) if start.isoformat() <= row[key] <= end.isoformat() and all(row.get(k) == v for k, v in filters.items())]


def aggregate(items: list[dict]) -> dict[str, float]:
    return {key: sum(float(row.get(key, 0) or 0) for row in items) for key in TOTALS}


def ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def metrics(x: dict) -> dict:
    get = lambda key: float(x.get(key, 0) or 0)
    spend, credit, amount, draws = get("花费"), get("授信数"), get("首借金额"), get("动支数")
    return {
        "花费": spend, "首借金额": amount, "授信数": credit, "动支数": draws,
        "CPS": ratio(spend, amount), "cpcl": ratio(spend, get("授信额度合计")),
        "授信成本": ratio(spend, credit), "授信单产": ratio(amount, credit),
        "安全过件率": ratio(get("安全授信数"), get("申完数")),
        "安全交易占比": ratio(get("安全首借金额"), amount),
        "首登成本": ratio(spend, get("首登数")), "申完成本": ratio(spend, get("申完数")),
        "动支率": ratio(draws, credit), "发起率": ratio(get("发起数"), credit),
        "发起动支率": ratio(draws, get("发起数")), "平均授信额度": ratio(get("授信额度合计"), credit),
        "人均借款": ratio(amount, draws),
        "额度使用率": ratio(amount * credit, draws * get("授信额度合计")),
        "CTR": ratio(get("点击"), get("曝光")), "CPM": ratio(spend * 1000, get("曝光")),
        "消耗进度": ratio(spend, get("日预算")),
    }


def windows(days: int, end: date = AS_OF) -> tuple[date, date, date, date]:
    if days < 1 or days > 30 or end > AS_OF:
        raise ValueError(f"时间窗口须为 1–30 天，且不能晚于模拟分析基准日 {AS_OF}")
    start = end - timedelta(days=days - 1)
    return start - timedelta(days=days), start - timedelta(days=1), start, end


def source(table: str, items: list[dict], start: date, end: date, filters: dict | None = None) -> dict:
    return {"table": f"ads_rta_{table}" + ("" if table in {"strategy_experiment", "change_log"} else "_daily"),
            "range": f"{start} ~ {end}", "filters": filters or {}, "rows": len(items)}


def maturity(end: date) -> bool:
    return end <= AS_OF - timedelta(days=7)
