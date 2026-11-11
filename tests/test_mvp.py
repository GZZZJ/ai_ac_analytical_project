import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_mvp import analyzers, data, diagnostic, jobs, service, sql_adapter, store, team
from agent_mvp.api import app


class MVPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = store.DB
        store.DB = Path(self.temp.name) / "test.sqlite3"
        self.session = store.create_session()

    def tearDown(self):
        store.DB = self.old_db
        self.temp.cleanup()

    def send(self, query, answers=None):
        return service.send(self.session, query, answers, use_model=False)

    def test_01_ambiguous_question_clarifies_once(self):
        first = self.send("最近巨量的获客效果怎么这么差？")
        self.assertEqual(first["type"], "clarification")
        self.assertEqual({q["key"] for q in first["clarification"]["questions"]}, {"days"})
        completed = self.send("", {"metric": "获客效率 / CPS", "days": "近 7 天"})
        self.assertEqual(completed["type"], "result")
        self.assertTrue(completed["result"]["evidence"])

    def test_invalid_clarification_stops_after_two_rounds(self):
        first = self.send("最近巨量的获客效果怎么这么差？")
        self.assertEqual(first["type"], "clarification")
        again = self.send("", {"metric": "无法识别", "days": "近 99 天"})
        self.assertEqual(again["type"], "clarification")
        stopped = self.send("", {"metric": "无法识别", "days": "近 99 天"})
        self.assertEqual(stopped["type"], "blocked")
        self.assertIn("两轮澄清", stopped["answer"])

    def test_02_full_cost_question(self):
        x = self.send("腾讯近 7 天 CPS 为什么上涨？成本侧和单产侧各贡献多少？")
        self.assertEqual(x["type"], "result")
        values = x["result"]["contribution"]
        self.assertAlmostEqual(values["成本侧"] + values["单产侧"], 1)
        self.assertAlmostEqual(x["result"]["metrics"]["current"]["CPS"],
                               data.metrics(data.aggregate(data.rows("media", data.AS_OF - timedelta(days=6), data.AS_OF, media="腾讯")))["CPS"])

    def test_recent_week_exposes_cost_and_productivity_change(self):
        x = self.send("腾讯近 7 天 CPS 为什么上涨？成本侧和单产侧各贡献多少？")
        self.assertEqual(x["type"], "result")
        result = x["result"]
        previous, current = result["metrics"]["previous"], result["metrics"]["current"]
        self.assertGreater(current["CPS"] / previous["CPS"], 1.1)
        self.assertGreater(current["授信成本"], previous["授信成本"])
        self.assertLess(current["授信单产"], previous["授信单产"])
        self.assertGreater(result["contribution_absolute"]["成本侧"], 0)
        self.assertGreater(result["contribution_absolute"]["单产侧"], 0)
        self.assertEqual(len(data.rows("media", data.AS_OF, data.AS_OF, media="腾讯")), 1)

    def test_03_modify_scope(self):
        self.send("腾讯近 7 天 CPS 为什么上涨？")
        x = self.send("那抖音呢？")
        self.assertEqual(x["relation"], "MODIFY_SCOPE")
        self.assertIn("抖音", x["result"]["title"])
        self.assertEqual(x["result"]["handoffs"]["task_spec"]["task_version"], 2)

    def test_modify_scope_preserves_explicit_new_window(self):
        self.send("腾讯近 7 天 CPS 为什么上涨？")
        x = self.send("换成抖音看近 14 天 CPS？")
        self.assertEqual(x["relation"], "MODIFY_SCOPE")
        self.assertEqual(x["result"]["handoffs"]["task_spec"]["analysis_period"]["start"], "2026-09-13")

    def test_new_problem_domain_does_not_inherit_old_window(self):
        self.send("腾讯近 7 天 CPS 为什么上涨？")
        x = self.send("腾讯 RTA 策略 CPS 高但质量更好，还值得继续投吗？")
        self.assertEqual(x["relation"], "NEW_TASK")
        self.assertEqual(x["result"]["handoffs"]["task_spec"]["analysis_period"]["start"], "2026-08-28")

    def test_broad_dual_channel_question_does_not_reuse_strategy_review(self):
        self.send("上周 3W2Q 提价有没有效果？")
        first = self.send("抖腾为什么最近的投放效果都不太好")
        self.assertEqual(first["type"], "clarification")
        self.assertEqual(first["relation"], "NEW_TASK")
        self.assertEqual({q["key"] for q in first["clarification"]["questions"]}, {"days", "media"})
        self.assertEqual(store.task_by_id(first["task_id"])["state"]["slots"]["intent"], "GENERAL_DIAGNOSIS")
        completed = self.send("", {"metric": "获客效率 / CPS", "days": "近 7 个完整日", "media": "两者分别看", "__task_id": first["task_id"]})
        self.assertEqual(completed["type"], "result")
        self.assertEqual(completed["task_id"], first["task_id"])
        self.assertEqual(completed["result"]["intent"], "COST_ANOMALY")
        self.assertEqual(completed["result"]["handoffs"]["task_spec"]["channels"], ["腾讯", "抖音"])
        self.assertEqual(set(completed["result"]["channel_results"]), {"腾讯", "抖音"})
        self.assertEqual(completed["result"]["verification_status"], "PASS_WITH_WARNING")
        self.assertEqual(len(completed["result"]["analysis_steps"]), 2)
        self.assertIn("CPS＝授信成本÷授信单产", completed["answer"])
        self.assertIn("动支率", completed["answer"])
        self.assertIn("授信数", completed["answer"])
        self.assertTrue(all("子组" not in q for q in completed["result"]["next_questions"]))
        messages = store.history(self.session)
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"],
                         ["上周 3W2Q 提价有没有效果？", "抖腾为什么最近的投放效果都不太好"])
        self.assertEqual(sum(m["task_id"] == first["task_id"] and m["role"] == "assistant" for m in messages), 1)
        self.assertEqual(len(store.tasks_for_session(self.session)), 2)

    def test_model_guess_cannot_turn_broad_question_into_strategy_review(self):
        class WrongGuess:
            client = object()
            def structured(self, name, schema, instruction, payload):
                return {"intent": "STRATEGY_REVIEW", "media": "腾讯", "days": None,
                        "metric": "投放效果", "needs_clarification": False} if name == "intent_slots" else {"relation": "CONTINUE"}
        self.send("上周 3W2Q 提价有没有效果？")
        with patch.object(service, "LLMClient", return_value=WrongGuess()):
            response = service.send(self.session, "抖腾为什么最近的投放效果都不太好", use_model=True)
        self.assertEqual(response["type"], "clarification")
        self.assertEqual(response["relation"], "NEW_TASK")
        self.assertIn("media", {q["key"] for q in response["clarification"]["questions"]})

    def test_model_does_not_invent_channel_or_days_for_broad_question(self):
        class WrongGuess:
            client = object()
            def structured(self, name, schema, instruction, payload):
                return {"intent": "STRATEGY_REVIEW", "media": "腾讯", "days": 7,
                        "metric": "投放效果", "needs_clarification": False}
        with patch.object(service, "LLMClient", return_value=WrongGuess()):
            response = service.send(self.session, "最近投放效果为什么不好？", use_model=True)
        self.assertEqual(response["type"], "clarification")
        self.assertEqual({q["key"] for q in response["clarification"]["questions"]}, {"media", "days"})

    def test_open_question_keeps_comprehensive_goal_and_adapts_checks(self):
        first = self.send("抖腾为什么最近的投放效果都不太好")
        response = self.send("", {"media": "两者分别看", "days": "近 7 个完整日",
                                  "__task_id": first["task_id"]})
        self.assertEqual(response["type"], "result")
        result = response["result"]
        self.assertEqual(result["intent"], "GENERAL_DIAGNOSIS")
        self.assertEqual(result["handoffs"]["task_spec"]["target_metrics"],
                         ["规模", "CPS", "安全过件率", "首借金额"])
        self.assertIn("腾讯消耗+7.2%", response["answer"])
        self.assertIn("抖音消耗-14.6%", response["answer"])
        self.assertIn("腾讯·COST_ANOMALY", result["checks"])
        self.assertIn("抖音·SCALE_ANOMALY", result["checks"])
        self.assertIn("腾讯·QUALITY_CHECK", result["checks"])
        self.assertNotIn("腾讯·QUALITY_CHECK", result["investigation"][0]["next"])
        self.assertNotIn("腾讯·SCALE_ANOMALY", result["checks"])
        self.assertIn("T7", " ".join(result["reliability"]["limitations"]))
        self.assertGreater(len(result["investigation"]), 1)

    def test_open_question_blocks_tampered_overview(self):
        first = self.send("抖腾为什么最近的投放效果都不太好")
        original = diagnostic.sql_adapter.execute
        def tampered(table, start, end, media=None, **kwargs):
            sql, records = original(table, start, end, media, **kwargs)
            if table == "media" and media == "腾讯":
                records[0]["花费"] += 1000
            return sql, records
        with patch.object(diagnostic.sql_adapter, "execute", side_effect=tampered):
            response = self.send("", {"media": "两者分别看", "days": "近 7 个完整日",
                                      "__task_id": first["task_id"]})
        self.assertEqual(response["type"], "blocked")
        self.assertIn("复核失败", response["answer"])

    def test_open_question_planner_can_choose_next_check_but_not_skip_others(self):
        class Planner:
            client = object()
            def structured(self, name, schema, instruction, payload):
                options = payload["candidates"]
                preferred = "抖音·SCALE_ANOMALY"
                return {"action": preferred if preferred in options else options[0], "reason": "先核对掉量"}
        slots = {"intent": "GENERAL_DIAGNOSIS", "media": None, "days": 7,
                 "metric": "综合诊断", "channel_confirmed": True}
        result = diagnostic.analyze(self.session, "planner-test", "抖腾投放效果不好", slots,
                                    "NEW_TASK", 1, "active", None, None, Planner())["result"]
        self.assertEqual(result["investigation"][1]["checked"], "抖音·SCALE_ANOMALY")
        self.assertEqual(set(result["checks"]),
                         {"腾讯·COST_ANOMALY", "抖音·COST_ANOMALY", "抖音·SCALE_ANOMALY", "腾讯·QUALITY_CHECK"})

    def test_single_channel_open_question_uses_same_diagnostic_goal(self):
        first = self.send("最近抖音投放效果不好，为什么？")
        self.assertEqual({q["key"] for q in first["clarification"]["questions"]}, {"days"})
        result = self.send("", {"days": "近 7 个完整日", "__task_id": first["task_id"]})
        self.assertEqual(result["result"]["intent"], "GENERAL_DIAGNOSIS")
        self.assertEqual(result["result"]["handoffs"]["task_spec"]["channels"], ["抖音"])
        self.assertNotIn("两渠道", result["answer"])

    def test_open_question_keeps_verified_overview_when_one_drilldown_fails(self):
        first = self.send("抖腾为什么最近的投放效果都不太好")
        original = diagnostic.analyzers.run
        def fail_tencent_cost(intent, slots):
            if intent == "COST_ANOMALY" and slots["media"] == "腾讯":
                raise analyzers.Blocked("分组无法复核")
            return original(intent, slots)
        with patch.object(diagnostic.analyzers, "run", side_effect=fail_tencent_cost):
            response = self.send("", {"media": "两者分别看", "days": "近 7 个完整日",
                                      "__task_id": first["task_id"]})
        self.assertEqual(response["type"], "result")
        self.assertEqual(response["result"]["checks"]["腾讯·COST_ANOMALY"]["status"], "PARTIAL")
        self.assertEqual(response["result"]["checks"]["抖音·SCALE_ANOMALY"]["status"], "PASS_WITH_WARNING")
        self.assertIn("分组无法复核", " ".join(response["result"]["reliability"]["limitations"]))

    def test_running_progress_carries_confirmed_scope(self):
        snapshots = []
        self.send("腾讯近 7 天 CPS 为什么上涨？")
        service.send(self.session, "近 7 天抖音消耗为什么下滑？", use_model=False, on_progress=lambda steps: snapshots.append(steps))
        scopes = [step["scope"] for steps in snapshots for step in steps if "scope" in step]
        self.assertTrue(scopes)
        self.assertEqual(scopes[0]["channels"], ["抖音"])
        self.assertEqual(scopes[0]["analysis_period"]["end"], data.AS_OF.isoformat())

    def test_dual_channel_clarification_can_narrow_to_one_channel(self):
        first = self.send("腾讯和抖音最近 CPS 为什么上涨？")
        self.assertEqual(first["type"], "clarification")
        narrowed = self.send("", {"media": "抖音", "days": "近 7 个完整日", "__task_id": first["task_id"]})
        self.assertEqual(narrowed["type"], "result")
        self.assertEqual(narrowed["result"]["handoffs"]["task_spec"]["channels"], ["抖音"])

    def test_dual_scale_uses_each_channels_actual_delivery_grain(self):
        first = self.send("抖腾为什么最近的投放效果都不太好")
        completed = self.send("", {"metric": "投放量级 / 消耗", "days": "近 7 个完整日",
                                   "media": "两者分别看", "__task_id": first["task_id"]})
        self.assertEqual(completed["type"], "result")
        self.assertEqual(completed["task_id"], first["task_id"])
        self.assertIn("腾讯 日均消耗", completed["answer"])
        self.assertIn("抖音 日均消耗", completed["answer"])
        channel_results = completed["result"]["channel_results"]
        self.assertEqual(channel_results["腾讯"]["evidence"][1]["filters"]["segment_type"], "RTA策略")
        self.assertEqual(channel_results["抖音"]["evidence"][1]["filters"]["segment_type"], "账号")
        self.assertEqual([m["content"] for m in store.history(self.session) if m["role"] == "user"],
                         ["抖腾为什么最近的投放效果都不太好"])

    def test_scale_without_auxiliary_delivery_still_reports_channel_facts(self):
        original_load = data.load
        with patch.object(data, "load", side_effect=lambda table: () if table == "delivery" else original_load(table)):
            result = self.send("腾讯近 7 天消耗为什么变化？")
        self.assertEqual(result["type"], "result")
        self.assertIn("日均消耗", result["answer"])
        self.assertIn("竞价明细", " ".join(result["result"]["reliability"]["limitations"]))
        self.assertEqual(result["result"]["reliability"]["level"], "低")
        self.assertEqual(result["result"]["delivery"]["pCTR"], [None, None])

    def test_recover_blocked_dual_task_replaces_failed_answer(self):
        first = self.send("抖腾为什么最近的投放效果都不太好")
        answers = {"metric": "投放量级 / 消耗", "days": "近 7 个完整日",
                   "media": "两者分别看", "__task_id": first["task_id"]}
        with patch.object(service, "run", side_effect=analyzers.Blocked("模拟辅助数据故障")):
            failed = self.send("", answers)
        self.assertEqual(failed["type"], "blocked")
        slots = store.task_by_id(first["task_id"])["state"]["slots"]
        recovered = service._analyze_dual_channel(self.session, first["task_id"],
                                                   "抖腾为什么最近的投放效果都不太好", slots,
                                                   "NEW_TASK", 1, "disabled", None, None,
                                                   replace_existing_answer=True)
        self.assertEqual(recovered["type"], "result")
        self.assertEqual(store.task_by_id(first["task_id"])["state"]["recovered_from_error"], "腾讯分析失败：模拟辅助数据故障")
        answers_in_history = [m for m in store.history(self.session) if m["role"] == "assistant"]
        self.assertEqual(len(answers_in_history), 1)
        self.assertEqual(answers_in_history[0]["content"], recovered["answer"])

    def test_model_cannot_replace_explicit_dual_channel_cps_intent(self):
        class WrongGuess:
            client = object()
            def structured(self, name, schema, instruction, payload):
                return {"intent": "STRATEGY_REVIEW", "media": "腾讯", "days": 7,
                        "metric": "投放效果", "needs_clarification": False}
        with patch.object(service, "LLMClient", return_value=WrongGuess()):
            response = service.send(self.session, "腾讯和抖音最近 CPS 为什么上涨？", use_model=True)
        self.assertEqual(response["type"], "clarification")
        self.assertEqual({q["key"] for q in response["clarification"]["questions"]}, {"media", "days"})
        self.assertEqual(store.task_by_id(response["task_id"])["state"]["slots"]["intent"], "COST_ANOMALY")

    def test_04_side_query_preserves_task(self):
        first = self.send("腾讯近 7 天 CPS 为什么上涨？")
        side = self.send("CPS 是怎么算的？")
        self.assertEqual(side["relation"], "SIDE_QUERY")
        self.assertEqual(store.latest_task(self.session)["id"], first["task_id"])

    def test_05_new_strategy_task(self):
        self.send("腾讯近 7 天 CPS 为什么上涨？")
        x = self.send("再看看上周 3W2Q 提价有没有效果。")
        self.assertEqual(x["relation"], "NEW_TASK")
        self.assertEqual(x["result"]["intent"], "STRATEGY_REVIEW")
        self.assertFalse(x["result"]["causal_claim_allowed"])

    def test_06_unmatured_vintage_degrades_claim(self):
        x = self.send("腾讯 RTA 策略 CPS 高但质量更好，还值得继续投吗？")
        self.assertEqual(x["result"]["intent"], "TRAFFIC_VALUE")
        self.assertIn("长期风险收益", " ".join(x["result"]["reliability"]["limitations"]))
        self.assertNotEqual(x["result"]["reliability"]["level"], "高")

    def test_07_missing_execution_blocks(self):
        original = analyzers.load
        def no_change(table):
            if table == "change_log":
                return tuple()
            return original(table)
        with patch.object(analyzers, "load", side_effect=no_change):
            x = self.send("上周 3W2Q 提价有没有效果？")
        self.assertEqual(x["type"], "blocked")
        self.assertIsNone(x["result"])
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM errors").fetchone()[0], 1)

    def test_08_feedback_snapshot(self):
        x = self.send("腾讯近 7 天 CPS 为什么上涨？")
        store.add_feedback(x["task_id"], "negative", "结论太强 / 不可信", "需再查")
        saved = store.list_feedback()[0]
        self.assertIn("evidence_refs", saved["snapshot_json"])
        self.assertEqual(saved["reason"], "结论太强 / 不可信")

    def test_four_analyzers_have_real_sources(self):
        for intent, slots in (("COST_ANOMALY", {"media": "腾讯", "days": 7}), ("SCALE_ANOMALY", {"media": "抖音", "days": 7}), ("TRAFFIC_VALUE", {"media": "腾讯", "days": 30}), ("STRATEGY_REVIEW", {"media": "腾讯"})):
            result = analyzers.run(intent, slots)
            self.assertTrue(result["facts"])
            self.assertTrue(all(s.get("rows", 1) > 0 for s in result["evidence"]))

    def test_model_cannot_inject_invalid_media_slot(self):
        class InvalidSlotsModel:
            client = object()
            def structured(self, name, schema, instruction, payload):
                return {"intent": "STRATEGY_REVIEW", "media": "3W2Q", "days": 7, "metric": None, "needs_clarification": False}
            def compose(self, result):
                return None
        with patch.object(service, "LLMClient", return_value=InvalidSlotsModel()):
            x = service.send(self.session, "上周 3W2Q 提价有没有效果？", use_model=True)
        self.assertEqual(x["type"], "result")
        self.assertEqual(x["result"]["model_status"], "fallback")
        self.assertIn("腾讯", x["result"]["title"])

    def test_cost_segment_decomposition_reconciles(self):
        result = analyzers.cost("腾讯")
        delta = result["metrics"]["current"]["CPS"] - result["metrics"]["previous"]["CPS"]
        self.assertAlmostEqual(sum(result["contribution_absolute"].values()), delta)
        self.assertAlmostEqual(sum(s["CPS变化贡献"] for s in result["segments"]), delta)
        self.assertEqual(result["reliability"]["dimensions"]["数据完整性"], "通过")

    def test_handoffs_and_verification_blocks_bad_math(self):
        response = self.send("腾讯近 7 天 CPS 为什么上涨？")
        handoffs = response["result"]["handoffs"]
        plan = store.task_by_id(response["task_id"])["state"]["plan"]
        self.assertEqual([step["status"] for step in plan["steps"]], ["COMPLETED"] * 4)
        self.assertEqual(handoffs["verification"]["status"], "PASS_WITH_WARNING")
        self.assertEqual(handoffs["report_spec"]["verification_id"], handoffs["verification"]["object_id"])
        wrong = dict(response["result"])
        wrong["contribution_absolute"] = {"成本侧": 1.0, "单产侧": 0.0}
        verification = team.verify(handoffs["task_spec"], handoffs["data_result"], handoffs["findings"], wrong)
        self.assertEqual(verification["status"], "BLOCKED")

    def test_plan_does_not_skip_failed_dependency(self):
        spec = team.task_spec("test-task", "腾讯近 7 天 CPS", {"intent": "COST_ANOMALY", "media": "腾讯", "days": 7, "metric": "CPS"}, "NEW_TASK")
        plan = team.initial_plan(spec)
        with self.assertRaises(ValueError):
            team.advance(plan, "analyze", "COMPLETED", ["made-up"])
        team.advance(plan, "query", "BLOCKED")
        with self.assertRaises(ValueError):
            team.advance(plan, "analyze", "COMPLETED", ["made-up"])

    def test_verifier_rejects_tampered_result_in_each_domain(self):
        queries = ["腾讯近 7 天 CPS 为什么上涨？", "近 7 天抖音消耗为什么下滑？",
                   "腾讯 RTA 策略 CPS 高但质量更好，还值得继续投吗？", "上周 3W2Q 提价有没有效果？"]
        for query in queries:
            with self.subTest(query=query):
                response = service.send(self.session, query, use_model=False, force_new=True)
                result = response["result"]
                handoffs = result["handoffs"]
                wrong = dict(result)
                wrong["metrics"] = {key: dict(value) for key, value in result["metrics"].items()}
                first = next(iter(wrong["metrics"]))
                wrong["metrics"][first]["花费"] += 1000
                checked = team.verify(handoffs["task_spec"], handoffs["data_result"], handoffs["findings"], wrong)
                self.assertEqual(checked["status"], "BLOCKED")

    def test_multi_question_partial_and_targeted_clarification(self):
        response = self.send("腾讯近 7 天 CPS 为什么上涨？；最近巨量的获客效果怎么这么差？")
        self.assertEqual(response["type"], "batch")
        self.assertEqual([item["type"] for item in response["items"]], ["result", "clarification"])
        self.assertEqual(response["status"], "partial")
        self.assertEqual(len(store.requests_for_session(self.session)), 1)
        task_id = response["items"][1]["task_id"]
        completed = self.send("", {"__task_id": task_id, "metric": "投放量级 / 消耗", "days": "近 7 天"})
        self.assertEqual(completed["type"], "result")
        self.assertEqual(completed["task_id"], task_id)
        self.assertEqual(store.requests_for_session(self.session)[0]["status"], "completed")

    def test_cancel_one_running_subtask_keeps_sibling(self):
        ready = threading.Event()
        holder = {}
        original = service._run_graph
        def delayed(initial, on_progress=None, cancelled=None):
            if initial["slots"]["media"] == "腾讯":
                while not cancelled():
                    time.sleep(.005)
                raise InterruptedError("cancelled")
            return original(initial, on_progress, cancelled)
        def progress(steps):
            if len(steps) == 2 and steps[0].get("task_id"):
                holder["first_id"] = steps[0]["task_id"]
                ready.set()
        def run_batch():
            holder["response"] = service.send(self.session,
                "腾讯近 7 天 CPS 为什么上涨？；近 7 天抖音消耗为什么下滑？",
                use_model=False, on_progress=progress)
        with patch.object(service, "_run_graph", side_effect=delayed):
            thread = threading.Thread(target=run_batch)
            thread.start()
            self.assertTrue(ready.wait(2))
            self.assertEqual(service.cancel_subtask(holder["first_id"])["status"], "stopping")
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([item["type"] for item in holder["response"]["items"]], ["cancelled", "result"])
        self.assertEqual(store.requests_for_session(self.session)[0]["plan"]["steps"][1]["status"], "COMPLETED")

    def test_cancel_waiting_clarification(self):
        response = self.send("腾讯近 7 天 CPS 为什么上涨？；最近巨量的获客效果怎么这么差？")
        task_id = response["items"][1]["task_id"]
        self.assertEqual(service.cancel_subtask(task_id)["status"], "cancelled")
        self.assertEqual(store.task_by_id(task_id)["status"], "cancelled")
        self.assertEqual(store.requests_for_session(self.session)[0]["plan"]["steps"][0]["status"], "COMPLETED")

    def test_modify_one_subtask_versions_only_that_item(self):
        original = self.send("腾讯近 7 天 CPS 为什么上涨？；近 7 天抖音消耗为什么下滑？")
        old_first, old_second = [item["task_id"] for item in original["items"]]
        changed = service.send(self.session, "换成抖音看近 14 天 CPS？", use_model=False, target_task_id=old_first)
        self.assertEqual(changed["type"], "result")
        self.assertEqual(changed["result"]["handoffs"]["task_spec"]["task_version"], 2)
        self.assertEqual(store.task_by_id(changed["task_id"])["state"]["supersedes_task_id"], old_first)
        plan = store.requests_for_session(self.session)[0]["plan"]
        self.assertEqual(plan["plan_version"], 2)
        self.assertEqual(plan["steps"][0]["task_id"], changed["task_id"])
        self.assertEqual(plan["steps"][1]["task_id"], old_second)
        self.assertEqual(store.task_by_id(old_first)["status"], "completed")
        stale = dict(original["plan"])
        self.assertFalse(store.save_request(original["request_id"], self.session, "completed", stale))
        self.assertEqual(store.requests_for_session(self.session)[0]["plan"]["steps"][0]["task_id"], changed["task_id"])

    def test_yesterday_uses_available_complete_day(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 27, tzinfo=tz)
        with patch("agent_mvp.model.datetime", FixedDateTime):
            response = self.send("昨天巨量消耗为什么下降？")
        self.assertEqual(response["type"], "result")
        self.assertEqual(response["result"]["handoffs"]["task_spec"]["analysis_period"]["end"], data.AS_OF.isoformat())

    def test_question_led_investigations_verify_premises(self):
        scenarios = [
            ("腾讯 CPS 每天都没报警，但已经连续一周在上涨。", {"days": "近 7 个完整日"}, "trend", "并非每天连续上涨"),
            ("安全过件率突然下降，但 CPS 暂时还没明显变差。", {"days": "近 7 个完整日", "media": "腾讯"}, "quality", "CPS 已 +22.3%"),
            ("动支率突然腰斩，是用户不愿借、风控卡件，还是放款链路出了问题？", {"days": "近 7 个完整日", "media": "腾讯"}, "draw", "没有腰斩"),
            ("模型 PSI 异常后，安全过件率、CPS、授信单产是否已经受到影响？", {"days": "近 7 个完整日", "media": "腾讯"}, "model_impact", "模型影响不可判定"),
            ("回传比例突然下降，是链路故障，还是我们自己的回传规则把量卡掉了？", {"days": "近 7 个完整日", "media": "腾讯"}, "callback", "无法用它区分链路故障与规则收紧"),
        ]
        for query, answers, goal, expected in scenarios:
            with self.subTest(goal=goal):
                sid = store.create_session()
                first = service.send(sid, query, use_model=False)
                self.assertEqual(first["type"], "clarification")
                result = service.send(sid, "", dict(answers, __task_id=first["task_id"]), use_model=False)
                self.assertEqual(result["type"], "result")
                self.assertEqual(result["result"]["analysis_goal"], goal)
                self.assertIn(expected, result["answer"])
                self.assertTrue(result["result"]["analysis_steps"])

    def test_question_led_investigations_keep_distinct_decisions(self):
        scenarios = [
            ("媒体后台说效果没问题，但我们自己看 CPS 在涨，到底应该听谁的？", "腾讯", "reconciliation", "缺少媒体后台明细"),
            ("大盘正常，但腾讯部分出价方式 / 版位，或抖音部分账户组已经连续多日变差。", "两者分别看", "subgroup", "连续 7 日"),
            ("本周有没有低成本、高质量、还有规模空间的流量可以继续放大？优先尝试哪些？", "两者分别看", "opportunity", "规模空间尚未证实"),
            ("腾讯、抖音多个指标同时大幅下降，这是真业务异常还是数据出了问题？", "两者分别看", "integrity", "腾讯花费 +7.2%"),
        ]
        for query, media, goal, expected in scenarios:
            with self.subTest(goal=goal):
                sid = store.create_session()
                first = service.send(sid, query, use_model=False)
                self.assertEqual(first["type"], "clarification")
                result = service.send(sid, "", {"days": "近 7 个完整日", "media": media, "__task_id": first["task_id"]}, use_model=False)
                self.assertEqual(result["type"], "result")
                self.assertEqual(result["result"]["analysis_goal"], goal)
                self.assertIn(expected, result["answer"])

    def test_yesterday_missing_date_explains_gap(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 28, tzinfo=tz)
        with patch("agent_mvp.model.datetime", FixedDateTime):
            response = self.send("昨天巨量消耗怎么跑不出去？")
        self.assertEqual(response["type"], "blocked")
        self.assertIn("2026-09-27 尚无数据", response["answer"])

    def test_specific_rta_value_question_keeps_traffic_value_analysis(self):
        response = self.send("腾讯 RTA 系数出价最近 CPS 明显比大盘高，但安全过件率和首借单产都更好，这部分流量还值得继续投吗？")
        self.assertEqual(response["type"], "result")
        self.assertEqual(response["result"]["intent"], "TRAFFIC_VALUE")

    def test_investigation_followup_does_not_replay_old_goal(self):
        first = self.send("腾讯 CPS 每天都没报警，但已经连续一周在上涨。")
        self.send("", {"days": "近 7 个完整日", "__task_id": first["task_id"]})
        followup = self.send("那动支率有没有变化？")
        self.assertEqual(followup["type"], "clarification")
        self.assertIn("metric", {q["key"] for q in followup["clarification"]["questions"]})

    def test_investigation_records_deterministic_path_after_clarification(self):
        class NoModelCall:
            client = object()
            def structured(self, *args, **kwargs):
                raise AssertionError("明确调查目标不应请求模型来决定已有方法")
        with patch.object(service, "LLMClient", return_value=NoModelCall()):
            first = service.send(self.session, "腾讯 CPS 每天都没报警，但已经连续一周在上涨。", use_model=True)
            second = service.send(self.session, "", {"days": "近 7 个完整日", "__task_id": first["task_id"]}, use_model=True)
        self.assertEqual(second["result"]["model_status"], "deterministic")

    def test_csv_sql_adapter_is_parameterized_and_read_only(self):
        sql, rows = sql_adapter.execute("media", "2026-09-16", "2026-09-22", "腾讯")
        self.assertIn("BETWEEN ? AND ?", sql)
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(row["media"] == "腾讯" for row in rows))
        with self.assertRaises(ValueError):
            sql_adapter.execute("sqlite_master", "2026-09-16", "2026-09-22")
        with self.assertRaises(ValueError):
            sql_adapter.execute("media", "2026-09-22", "2026-09-16")

    def test_query_quality_blocks_cross_table_double_count(self):
        spec = team.task_spec("test-task", "腾讯近 7 天 CPS", {"intent": "COST_ANOMALY", "media": "腾讯", "days": 7, "metric": "CPS"}, "NEW_TASK")
        need = team.initial_need(spec)
        original = sql_adapter.execute
        def corrupt(table, *args, **kwargs):
            sql, rows = original(table, *args, **kwargs)
            if table == "segment":
                rows[0]["花费"] += 100000
            return sql, rows
        with patch.object(team.sql_adapter, "execute", side_effect=corrupt):
            data_result = team.semantic_query(spec, need)
        self.assertEqual(data_result["status"], "BLOCKED")
        self.assertTrue(data_result["quality_checks"]["reconciliation_errors"])

    def test_session_title_rename_and_delete(self):
        self.send("腾讯近 7 天 CPS 为什么上涨？")
        self.assertIn("腾讯", store.list_sessions()[0]["title"])
        store.rename_session(self.session, "运营周会")
        self.send("近 7 天抖音消耗为什么下滑？")
        self.assertEqual(store.list_sessions()[0]["title"], "运营周会")
        store.delete_session(self.session)
        self.assertFalse(store.session_exists(self.session))
        self.assertEqual(store.tasks_for_session(self.session), [])

    def test_http_rename_export_and_delete(self):
        result = self.send("腾讯近 7 天 CPS 为什么上涨？")
        client = TestClient(app)
        self.assertEqual(client.patch(f"/api/sessions/{self.session}", json={"title": "周会复盘"}).status_code, 200)
        self.assertEqual(client.get("/api/sessions").json()[0]["title"], "周会复盘")
        exported = client.get(f"/api/tasks/{result['task_id']}/export.md")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("CPS", exported.text)
        self.assertIn("## 分析过程", exported.text)
        self.assertIn("## 限制", exported.text)
        self.assertEqual(client.delete(f"/api/sessions/{self.session}").status_code, 200)

    def test_followup_uses_saved_drilldown(self):
        first = self.send("腾讯近 7 天 CPS 为什么上涨？")
        second = self.send("哪些 RTA策略拖累了 CPS？")
        self.assertEqual(second["result"]["followup_of"], first["task_id"])
        self.assertIn("贡献", second["result"]["facts"][0])
        self.assertEqual(second["result"]["handoffs"]["data_result"]["reused_from"], first["result"]["handoffs"]["data_result"]["object_id"])

    def test_old_result_displays_analysis_chain_and_account_group_wording(self):
        result = self.send("抖音近 7 天 CPS 为什么上涨？")
        task = store.task_by_id(result["task_id"])
        old = task["result"]
        old.pop("analysis_steps", None)
        old["next_questions"] = ["哪些子组拖累了 CPS？"]
        store.save_task(task["id"], self.session, task["intent"], task["status"], task["state"], old)
        loaded = TestClient(app).get(f"/api/sessions/{self.session}").json()["task"]["result"]
        self.assertEqual(loaded["next_questions"], ["哪些账户组拖累了 CPS？"])
        self.assertEqual(len(loaded["analysis_steps"]), 1)
        self.assertIn("CPS＝授信成本÷授信单产", loaded["analysis_steps"][0]["method"])
        followed = self.send("哪些账户组拖累了 CPS？")
        self.assertEqual(followed["result"]["followup_of"], result["task_id"])

    def test_progress_and_cooperative_stop(self):
        steps = []
        result = service.send(self.session, "近 7 天抖音消耗为什么下滑？", use_model=False, on_progress=lambda p: steps.append(p))
        self.assertEqual(len(steps), 5)
        self.assertEqual(result["progress"][-1]["status"], "completed")
        with self.assertRaises(InterruptedError):
            service._run_graph({"intent": "SCALE_ANOMALY", "slots": {"media": "抖音", "days": 7}, "progress": []}, cancelled=lambda: True)

    def test_background_job_stops_without_result(self):
        entered = threading.Event()
        def slow_send(session_id, query, answers, use_model, on_progress, cancelled):
            entered.set()
            for _ in range(100):
                if cancelled():
                    raise InterruptedError("stopped")
                time.sleep(.005)
            raise AssertionError("cancellation not observed")
        with patch.object(jobs.service, "send", side_effect=slow_send):
            job_id = jobs.start(self.session, "测试停止", use_model=False)
            self.assertTrue(entered.wait(1))
            self.assertEqual(jobs.stop(job_id)["status"], "stopping")
            for _ in range(100):
                record = store.job_by_id(job_id)
                if record["status"] == "stopped":
                    break
                time.sleep(.005)
        self.assertEqual(record["status"], "stopped")
        self.assertIsNone(record["response"])


if __name__ == "__main__":
    unittest.main()
