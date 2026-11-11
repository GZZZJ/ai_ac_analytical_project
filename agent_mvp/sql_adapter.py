"""Constrained SQLite mirror of the public simulation CSVs.

Only internally generated, parameterized SELECT statements are accepted. The
connection exists for one query and cannot alter the source CSV files.
"""
from __future__ import annotations

import re
import sqlite3

from . import data

TABLES = {"media", "segment", "delivery", "creative", "quality", "strategy_experiment", "change_log", "funnel"}


def execute(table: str, start: str, end: str, media: str | None = None, *, limit: int = 5000) -> tuple[str, list[dict]]:
    if table not in TABLES:
        raise ValueError("表不在只读白名单中")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) or start > end:
        raise ValueError("日期范围无效")
    if limit < 1 or limit > 5000:
        raise ValueError("查询行数超出限制")
    records = data.load(table)
    if not records:
        return "", []
    columns = list(records[0])
    physical = "ads_rta_" + table + ("" if table in {"strategy_experiment", "change_log"} else "_daily")
    day_field = "变更日期" if table == "change_log" else "date"
    quoted = lambda value: '"' + value.replace('"', '""') + '"'
    sql = f"SELECT * FROM {quoted(physical)} WHERE {quoted(day_field)} BETWEEN ? AND ?"
    params: list = [start, end]
    if media and table != "change_log":
        if "media" not in columns:
            raise ValueError("该表不含渠道字段")
        sql += ' AND "media" = ?'
        params.append(media)
    sql += f" LIMIT {limit}"
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        definitions = ", ".join(f"{quoted(column)} {'REAL' if column in data.NUMERIC else 'TEXT'}" for column in columns)
        db.execute(f"CREATE TABLE {quoted(physical)} ({definitions})")
        placeholders = ",".join("?" for _ in columns)
        db.executemany(f"INSERT INTO {quoted(physical)} VALUES ({placeholders})", [tuple(row.get(c) for c in columns) for row in records])
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        db.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
        result = [dict(row) for row in db.execute(sql, params)]
        return sql, result
    finally:
        db.close()
