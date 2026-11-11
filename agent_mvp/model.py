"""Optional DeepSeek language adapter. All numeric facts come from analyzers."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_local_config() -> None:
    """Read only the known model settings from a private project .env file."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    allowed = {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_REASONING_ROUTING"}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class LLMClient:
    def __init__(self):
        _load_local_config()
        self.key = os.getenv("DEEPSEEK_API_KEY", "")
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.client = None
        self.last_error: dict | None = None
        if self.key:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.key, base_url=self.base_url, timeout=30)

    def structured(self, name: str, schema: dict, instruction: str, payload: dict) -> dict | None:
        if not self.client:
            return None
        self.last_error = None
        for _ in range(2):
            try:
                result = self.client.responses.create(model=self.model,
                    instructions=instruction + " 仅返回符合 JSON Schema 的 JSON。",
                    input=json.dumps(payload, ensure_ascii=False),
                    text={"format": {"type": "json_schema", "name": name, "schema": schema}},
                    reasoning={"effort": os.getenv("DEEPSEEK_REASONING_ROUTING", "low")})
                value = json.loads(result.output_text)
                if isinstance(value, dict):
                    self.last_error = None
                    return value
            except Exception as exc:
                self.last_error = {"kind": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
                continue
        return None

SLOT_SCHEMA = {"type": "object", "properties": {
    "intent": {"type": "string", "enum": ["GENERAL_DIAGNOSIS", "COST_ANOMALY", "SCALE_ANOMALY", "TRAFFIC_VALUE", "STRATEGY_REVIEW", "UNKNOWN"]},
    "media": {"type": ["string", "null"], "enum": ["腾讯", "抖音", None]}, "days": {"type": ["integer", "null"]},
    "metric": {"type": ["string", "null"]}, "needs_clarification": {"type": "boolean"}},
    "required": ["intent", "media", "days", "metric", "needs_clarification"], "additionalProperties": False}
RELATION_SCHEMA = {"type": "object", "properties": {"relation": {"type": "string", "enum": ["CONTINUE", "MODIFY_SCOPE", "SIDE_QUERY", "NEW_TASK"]}}, "required": ["relation"], "additionalProperties": False}


def rule_slots(query: str, previous: dict | None = None) -> dict:
    dual_media = asks_both_channels(query)
    media = None if dual_media else "抖音" if any(w in query for w in ("抖音", "巨量")) else "腾讯" if "腾讯" in query else None
    match = re.search(r"近\s*(\d+)\s*天", query)
    days = int(match.group(1)) if match else 30 if "近30" in query else None
    if "上周" in query and days is None:
        days = 7
    explicit_end = None
    if "昨天" in query:
        days = 1
        explicit_end = str(datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1))
    if "3W2Q" in query and any(w in query for w in ("提价", "系数", "效果", "复盘")):
        intent = "STRATEGY_REVIEW"
    elif any(w in query for w in ("该不该关", "值得", "流量价值", "更好")):
        intent = "TRAFFIC_VALUE"
    elif any(w in query for w in ("消耗", "掉量", "跑不出去", "规模", "拿量")):
        intent = "SCALE_ANOMALY"
    elif any(w in query for w in ("CPS", "获客效率", "成本", "单产")):
        intent = "COST_ANOMALY"
    else:
        intent = "UNKNOWN"
    metric = "整体表现" if "整体" in query else "CPS" if intent == "COST_ANOMALY" else "消耗" if intent == "SCALE_ANOMALY" else "质量/价值" if intent == "TRAFFIC_VALUE" else None
    if previous and intent == "UNKNOWN" and previous.get("intent") != "INVESTIGATION" and not broad_performance_query(query):
        intent = previous["intent"]
        metric = previous.get("metric")
    if previous and not broad_performance_query(query):
        media = media or previous.get("media")
        days = days or previous.get("days")
    result = {"intent": intent, "media": media, "days": days, "metric": metric,
            "needs_clarification": intent == "UNKNOWN" or (days is None and intent in {"COST_ANOMALY", "SCALE_ANOMALY"})}
    if explicit_end:
        result["end"] = explicit_end
    elif previous and previous.get("end"):
        result["end"] = previous["end"]
    return result


def asks_both_channels(query: str) -> bool:
    return bool(re.search(r"抖腾|抖音.{0,24}腾讯|腾讯.{0,24}抖音|双渠道|两个渠道", query))


def broad_performance_query(query: str) -> bool:
    return bool(re.search(r"投放效果|获客效果|整体表现|效果.{0,6}(?:不好|差)", query)) and not any(
        word in query for word in ("CPS", "获客效率", "消耗", "成本", "单产", "安全过件率", "3W2Q", "提价", "策略调整")
    )


def relation(query: str, previous: dict | None) -> str:
    if not previous:
        return "NEW_TASK"
    if asks_both_channels(query) or broad_performance_query(query):
        return "NEW_TASK"
    if re.search(r"(?:CPS|安全过件率|授信单产).{0,5}(?:怎么算|是什么|定义)", query):
        return "SIDE_QUERY"
    if any(w in query for w in ("那抖音", "那腾讯", "换成抖音", "换成腾讯", "另一个渠道")):
        return "MODIFY_SCOPE"
    if any(w in query for w in ("再看看", "新问题", "上周.*提价", "3W2Q.*效果")):
        return "NEW_TASK"
    fresh = rule_slots(query)
    if fresh["intent"] != "UNKNOWN" and fresh["intent"] != previous.get("intent"):
        return "NEW_TASK"
    if fresh["media"] and fresh["media"] != previous.get("media"):
        return "NEW_TASK"
    return "CONTINUE"
