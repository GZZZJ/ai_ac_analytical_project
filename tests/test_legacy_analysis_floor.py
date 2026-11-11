"""The old project's implemented analysis paths are a regression floor."""
import csv
import tempfile
import unittest
from pathlib import Path

from agent_mvp import analyzers, data, investigation, service, store


CASES = Path(__file__).resolve().parent.parent / "03_模拟数据_补充" / "ads_rta_legacy_alert_cases.csv"
GROUPS = Path(__file__).resolve().parent.parent / "03_模拟数据_补充" / "ads_rta_legacy_subgroup_cases.csv"


def records(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def numeric(row):
    return {key: float(value) if key in data.TOTALS and value else value for key, value in row.items()}


class LegacyAnalysisFloor(unittest.TestCase):
    def test_product_routes_four_paths_and_exposes_evidence(self):
        old_db = store.DB
        with tempfile.TemporaryDirectory() as temporary:
            store.DB = Path(temporary) / "parity.sqlite3"
            try:
                examples = [
                    ("腾讯近 7 天 CPS 与安全过件率同时异常，为什么？", "joint", ("成本侧", "cpcl", "安全过件率")),
                    ("大盘 CPS 已异常，定位腾讯版位", "subgroup", ("版位", "CPS")),
                    ("腾讯近 7 天安全过件率越线，计费比怎样？", "quality", ("计费比", "17%")),
                    ("抖音近 7 天安全过件率越线，怎么排查？", "quality", ("不适用计费比", "17%")),
                ]
                for query, goal, expected in examples:
                    with self.subTest(query=query):
                        session = store.create_session()
                        response = service.send(session, query, use_model=False)
                        if response["type"] == "clarification":
                            response = service.send(session, "", {"days": "近 7 个完整日", "__task_id": response["task_id"]}, use_model=False)
                        self.assertEqual(response["type"], "result")
                        self.assertEqual(response["result"]["analysis_goal"], goal)
                        combined = response["answer"] + " ".join(response["result"]["facts"])
                        for token in expected:
                            self.assertIn(token, combined)
            finally:
                store.DB = old_db

    def test_dual_anomaly_is_not_reduced_to_one_metric(self):
        cases = {r["scenario_id"]: numeric(r) for r in records(CASES)}
        for name in ("SIM_DUAL_TX", "SIM_DUAL_DY"):
            row = cases[name]
            metrics = data.metrics(row)
            branch = investigation.quality_branch(row["media"], metrics, [row],
                external_billing=float(row["腾讯计费比_外部输入"]) if row["腾讯计费比_外部输入"] else None)
            self.assertGreater(metrics["CPS"], .54)
            self.assertTrue(branch["alert"])
            self.assertLess(metrics["安全过件率"], .17)
            self.assertEqual(branch["billing_state"], "偏高" if row["media"] == "腾讯" else "不适用")
        self.assertEqual(investigation.goal("腾讯 CPS 与安全过件率同时异常，为什么？"), "joint")

    def test_cost_components_include_cpcl_and_credit_productivity_identity(self):
        row = numeric(next(r for r in records(CASES) if r["scenario_id"] == "SIM_COST_COMPONENTS"))
        m = data.metrics(row)
        self.assertAlmostEqual(m["cpcl"], row["花费"] / row["授信额度合计"])
        self.assertAlmostEqual(m["CPS"], m["授信成本"] / m["授信单产"])
        self.assertAlmostEqual(m["授信单产"], m["平均授信额度"] * m["额度使用率"] * m["动支率"])
        result = analyzers.cost("腾讯", 7)
        self.assertEqual({"cpcl", "授信成本", "授信单产", "平均授信额度", "额度使用率"} - result["drilldown"].keys(), set())
        self.assertIn("版位", result["segment_dimensions"])
        self.assertTrue(result["segment_dimensions"]["版位"]["aligned"])
        self.assertFalse(result["segment_dimensions"]["版位"]["separable"])
        self.assertIn("同步核查质量：", " ".join(result["facts"]))

    def test_abnormal_market_can_still_localize_partial_groups(self):
        cases = {r["scenario_id"]: numeric(r) for r in records(CASES)}
        groups = [numeric(r) for r in records(GROUPS)]
        for scenario, dimension in (("SIM_DUAL_TX", "RTA策略"), ("SIM_DUAL_TX", "版位"), ("SIM_DUAL_DY", "账号")):
            main = data.metrics(cases[scenario])
            selected = [g for g in groups if g["scenario_id"] == scenario and g["segment_type"] == dimension]
            for key in ("花费", "申完数", "安全授信数", "首借金额"):
                self.assertAlmostEqual(sum(g[key] for g in selected), cases[scenario][key])
            self.assertGreater(main["CPS"], .54)
            bad = [g for g in selected if data.metrics(g)["CPS"] > .54 and data.metrics(g)["安全过件率"] < .17]
            self.assertGreater(len(bad), 0)
            self.assertLess(len(bad), len(selected))
            self.assertGreater(max(data.metrics(g)["CPS"] for g in selected) - min(data.metrics(g)["CPS"] for g in selected), .1)
        self.assertEqual(investigation.goal("大盘 CPS 已异常，定位腾讯版位"), "subgroup")

    def test_safety_alert_has_three_channel_specific_branches_and_sample_gate(self):
        cases = {r["scenario_id"]: numeric(r) for r in records(CASES)}
        expected = {"SIM_SAFETY_HIGH_BILL": "偏高", "SIM_SAFETY_NORMAL_BILL": "正常范围", "SIM_SAFETY_DY": "不适用"}
        for scenario, state in expected.items():
            row = cases[scenario]
            branch = investigation.quality_branch(row["media"], data.metrics(row), [row],
                external_billing=float(row["腾讯计费比_外部输入"]) if row["腾讯计费比_外部输入"] else None)
            self.assertTrue(branch["alert"])
            self.assertEqual(branch["billing_state"], state)
        low = cases["SIM_LOW_SAMPLE"]
        branch = investigation.quality_branch("腾讯", data.metrics(low), [low], external_billing=1.8)
        self.assertFalse(branch["qualified"])
        self.assertFalse(branch["alert"])
        self.assertEqual(investigation.goal("腾讯安全过件率单独越线，怎么排查？"), "quality")

    def test_normal_and_missing_key_field_are_not_reported_as_anomalies(self):
        cases = {r["scenario_id"]: numeric(r) for r in records(CASES)}
        normal = cases["SIM_NORMAL"]
        normal_metrics = data.metrics(normal)
        self.assertLess(normal_metrics["CPS"], .54)
        self.assertFalse(investigation.quality_branch("腾讯", normal_metrics, [normal], external_billing=1.2)["alert"])
        missing = cases["SIM_MISSING_AMOUNT"]
        self.assertIsNone(data.metrics(missing)["CPS"])


if __name__ == "__main__":
    unittest.main()
