"""查询计划生成器：开关、降级、解析容错、提示词里的那两条硬约束。

这里一律不联网。模型出口 `chat_async` 在用例里被替换；关开关的那几条用例
额外把 `httpx.AsyncClient` 换成"一构造就抛"的桩 —— 只有这样才能证明"零
HTTP"，而不是"看起来没发"。

项目没装 pytest-asyncio，异步入口统一用 `asyncio.run` 在同步用例里跑。
"""
import asyncio
import inspect
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx

from app.knowledge import query_plan
from app.knowledge.query_plan import (
    HYDE_MAX_CHARS,
    MAX_QUERIES,
    QueryPlan,
    plan_query,
)

MODEL_ENV = {
    "MINDMAP_LLM_MODE": "model",
    "MINDMAP_LLM_BASE_URL": "http://example.invalid/v1",
    "MINDMAP_LLM_API_KEY": "test-key",
    "MINDMAP_LLM_MODEL": "test-model",
}

FULL = {
    "corrected": "审批预算的权限在谁手里",
    "intents": ["谁有权批这笔预算", "这笔预算由谁审批", "审批权限在哪一级"],
    "hyde": "预算审批权按金额分级，额度以内由部门负责人批，超出部分上会。",
    "subqueries": ["部门负责人的审批额度上限", "超出额度后的上会流程"],
}


def run(coro):
    return asyncio.run(coro)


def bomb_client(*args, **kwargs):
    """HTTP 客户端的"一构造就抛"桩。"""
    raise AssertionError("这条路径不该构造 HTTP 客户端")


class PlanTestBase(unittest.TestCase):
    """默认：开关打开、模型配置齐备。各用例按需改。"""

    def setUp(self):
        env = {**MODEL_ENV, "MINDMAP_QUERY_EXPANSION": "1"}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = patch("app.generation.service.httpx.AsyncClient", bomb_client)
        self.http.start()
        self.addCleanup(self.http.stop)

    def use_model(self, result):
        """替换模型出口。`result` 是返回值、异常或协程函数；返回调用记录。

        只记调用**次数**与入参，不参与解析 —— 这几条用例要断言的正是"解不
        出来时怎么办"，让 `chat_async` 的真身去解析反而把被测行为绕开了。
        """
        calls = []

        async def fake(system: str, user: str, **kwargs):
            calls.append({"system": system, "user": user, **kwargs})
            if isinstance(result, Exception):
                raise result
            if callable(result):
                return await result()
            return result

        patcher = patch.object(query_plan, "chat_async", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class SwitchTest(PlanTestBase):
    """开关：默认关，关着时零代价。"""

    def test_defaults_to_off_when_env_absent(self):
        """没设开关就是关 —— 这是"默认关闭"的字面含义。"""
        with patch.dict(os.environ, {"MINDMAP_QUERY_EXPANSION": ""}):
            self.assertFalse(query_plan._enabled())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINDMAP_QUERY_EXPANSION", None)
            self.assertFalse(query_plan._enabled())

    def test_off_sends_no_request_and_never_touches_the_model(self):
        """关着时不构造客户端、不调 `chat_async`、也不解析任何东西。

        `http` 那个桩一构造就抛，`use_model` 那个替身一调用就抛；两个都没炸，
        才说明这条路径上没有一次 HTTP、也没有一次 JSON 解析。
        """
        def explode(*args, **kwargs):
            raise AssertionError("开关关着时不该走到模型出口")

        with patch.dict(os.environ, {"MINDMAP_QUERY_EXPANSION": "0"}), \
             patch.object(query_plan, "chat_async", explode):
            plan = run(plan_query("它多少钱", materials=["材料标题\n材料开头"]))
        self.assertIsNone(plan)

    def test_off_is_byte_identical_to_not_having_the_feature(self):
        """"关"与"模型坏了"必须给出同一个返回值，调用方才能只有一个分支。"""
        with patch.dict(os.environ, {"MINDMAP_QUERY_EXPANSION": "0"}):
            off = run(plan_query("它多少钱"))
        out = self.use_model(RuntimeError("模型挂了"))
        with patch.dict(os.environ, {"MINDMAP_QUERY_EXPANSION": "1"}):
            broken = run(plan_query("它多少钱"))
        self.assertIsNone(off)
        self.assertIsNone(broken)
        self.assertEqual(len(out), 1)


class DegradeTest(PlanTestBase):
    """降级：没配置、超时、5xx、非法 JSON —— 都返回 None，且都只调用一次。"""

    def test_missing_llm_config_returns_none(self):
        """没有 MINDMAP_LLM_* 时走 `chat_async` 的预检，一句 HTTP 都不发。

        这里**不替换** `chat_async`：要测的就是它读配置失败后本模块怎么反应。
        """
        with patch.dict(os.environ, {"MINDMAP_LLM_BASE_URL": "",
                                     "MINDMAP_LLM_API_KEY": "",
                                     "MINDMAP_LLM_MODEL": ""}):
            self.assertIsNone(run(plan_query("它多少钱", materials=["标题"])))

    def test_timeout_returns_none_without_retry(self):
        """超时按降级处理，而且**不重试**：调用次数必须是 1。

        重试会把 5 秒的检索预算打成 13 秒，而这一次得到的仍然可能是一份计划。
        """
        async def slow():
            await asyncio.sleep(3)

        calls = self.use_model(slow)
        started = time.perf_counter()
        with patch.object(query_plan, "TIMEOUT_SECONDS", 0.05):
            plan = run(plan_query("它多少钱", materials=["标题"]))
        self.assertIsNone(plan)
        self.assertEqual(len(calls), 1)
        # 确实是 wait_for 掐断的，不是等满 3 秒
        self.assertLess(time.perf_counter() - started, 1.5)

    def test_http_error_returns_none_without_retry(self):
        from app.generation.service import GenerationError
        calls = self.use_model(GenerationError("模型服务返回 HTTP 503，请检查服务配置"))
        self.assertIsNone(run(plan_query("它多少钱", materials=["标题"])))
        self.assertEqual(len(calls), 1)

    def test_invalid_json_returns_none(self):
        """模型回了段解释文字，里面没有 JSON → 全坏 → None。"""
        calls = self.use_model("抱歉，我需要更多信息才能回答这个问题。")
        self.assertIsNone(run(plan_query("它多少钱", materials=["标题"])))
        self.assertEqual(len(calls), 1)

    def test_empty_query_costs_nothing(self):
        calls = self.use_model(FULL)
        self.assertIsNone(run(plan_query("   ", materials=["标题"])))
        self.assertEqual(calls, [])


class NoMaterialTest(PlanTestBase):
    """没有材料时只做纠错，一个扩展项都不许有。"""

    def test_only_correction_survives(self):
        """模型不听话、照样给了 intents/hyde/subqueries：代码闸把它们清掉。

        corrected 保留（模型确实给了），但 `source` 是 "fallback" —— 这份计划
        没有资格触发扩展检索。
        """
        self.use_model(FULL)
        plan = run(plan_query("审批预算的权限在谁手理"))    # 故意带错字
        self.assertIsNotNone(plan)
        self.assertEqual(plan.corrected, FULL["corrected"])
        self.assertEqual(plan.intents, [])
        self.assertEqual(plan.subqueries, [])
        self.assertIsNone(plan.hyde)
        self.assertEqual(plan.source, "fallback")

    def test_blank_materials_count_as_no_material(self):
        """调用方传了 ["", "  ", None] 这种空壳：等同于没有材料，不是"有材料"。

        否则模板渲染出来会多一段空白材料，模型会以为自己拿到了上下文。
        """
        self.use_model(FULL)
        plan = run(plan_query("它多少钱", materials=["", "  \n ", None]))
        self.assertEqual(plan.intents, [])
        self.assertEqual(plan.source, "fallback")

    def test_prompt_says_no_material_and_sits_out_expansion(self):
        """没有材料时发给模型的话里不再有材料段，只有"只做纠错"这一条。"""
        calls = self.use_model(FULL)
        run(plan_query("它多少钱"))
        user = calls[0]["user"]
        self.assertIn("本次没有提供材料", user)
        self.assertNotIn("[材料1]", user)

    def test_materials_are_passed_as_title_and_head(self):
        """有材料时进提示词的是标题 + 开头；换行压平，避免伪造指令分段。

        传全文是调用方最可能犯的错，这里守住"只有开头进得去"：末尾那个唯一
        标记绝不能出现在提示词里。
        """
        long_material = "预算管理办法\n\n第一章 总则 " + "正文" * 400 + "【末尾标记】"
        calls = self.use_model(FULL)
        run(plan_query("谁批预算", materials=[long_material]))
        user = calls[0]["user"]
        self.assertIn("[材料1] 预算管理办法 第一章 总则", user)
        self.assertNotIn("【末尾标记】", user)
        # 每份材料不超过 MATERIAL_HEAD_CHARS 字
        head = user.split("[材料1] ", 1)[1].split("\n", 1)[0]
        self.assertLessEqual(len(head), query_plan.MATERIAL_HEAD_CHARS)


class ParseTest(PlanTestBase):
    """解析：正常、围栏、类型错、部分坏。坏哪项丢哪项，其余保留。"""

    def test_full_plan(self):
        calls = self.use_model(FULL)
        plan = run(plan_query("谁能批预算", materials=["预算管理办法 第一章"]))
        self.assertEqual(plan.corrected, FULL["corrected"])
        self.assertEqual(plan.intents, FULL["intents"])
        self.assertEqual(plan.hyde, FULL["hyde"])
        self.assertEqual(plan.subqueries, FULL["subqueries"])
        self.assertEqual(plan.source, "llm")
        # 一次调用产出四样：四路往返在 2C2G + 5 秒预算下不可能
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["max_tokens"], query_plan.MAX_OUTPUT_TOKENS)

    def test_fenced_json_is_accepted(self):
        """围栏是实测里最常见的形态（18 次调用撞 1 次），必须吃掉。"""
        self.use_model("```json\n" + json.dumps(FULL, ensure_ascii=False) + "\n```")
        plan = run(plan_query("谁能批预算", materials=["标题"]))
        self.assertEqual(plan.intents, FULL["intents"])
        self.assertEqual(plan.source, "llm")

    def test_json_wrapped_in_prose_is_accepted(self):
        """模型在 JSON 前后各加一句解释，也照收 —— 解析器截最外层对象。"""
        self.use_model("以下是计划：\n" + json.dumps(FULL, ensure_ascii=False)
                       + "\n希望有帮助。")
        plan = run(plan_query("谁能批预算", materials=["标题"]))
        self.assertEqual(plan.corrected, FULL["corrected"])

    def test_wrong_type_drops_only_that_field(self):
        """`intents` 给了字符串：整项丢掉，corrected/hyde/subqueries 照留。

        不做"把字符串拆成一条"的猜测：猜错会悄悄多出一条无关检索。
        """
        self.use_model({
            "corrected": "它多少钱",
            "intents": "它多少钱",
            "hyde": "大概在这个量级。",
            "subqueries": ["查甲", "查乙"],
        })
        plan = run(plan_query("它多少钱", materials=["标题"]))
        self.assertEqual(plan.corrected, "它多少钱")
        self.assertEqual(plan.intents, [])
        self.assertEqual(plan.hyde, "大概在这个量级。")
        self.assertEqual(plan.subqueries, ["查甲", "查乙"])
        # 仍有扩展项 → 仍算 llm
        self.assertEqual(plan.source, "llm")

    def test_bad_elements_are_dropped_one_by_one(self):
        """数组里混了数字、空串、重复项：逐条丢，不带走整个数组。"""
        self.use_model({
            "corrected": None,
            "intents": ["它多少钱", 42, "   ", "它多少钱", "它的费用是多少"],
            "hyde": {"不该是对象": True},
            "subqueries": "不是数组",
        })
        plan = run(plan_query("它多少钱", materials=["标题"]))
        self.assertIsNone(plan.corrected)
        self.assertEqual(plan.intents, ["它多少钱", "它的费用是多少"])
        self.assertIsNone(plan.hyde)
        self.assertEqual(plan.subqueries, [])

    def test_all_fields_broken_returns_none(self):
        """四个字段全坏 → 整份计划作废（不能返回一个空壳让调用方当成功）。"""
        self.use_model({"corrected": None, "intents": "坏的", "hyde": 1,
                        "subqueries": {}})
        self.assertIsNone(run(plan_query("它多少钱", materials=["标题"])))

    def test_json_array_at_top_level_returns_none(self):
        """顶层是数组而不是对象 → 形状不对 → None。"""
        self.use_model("[1, 2, 3]")
        self.assertIsNone(run(plan_query("它多少钱", materials=["标题"])))


class QueryPlanContractTest(unittest.TestCase):
    """`QueryPlan` 自身的字段约束：截断、去重、上限。"""

    def test_hyde_is_truncated_not_dropped(self):
        """HyDE 超长按截断处理：它只是给向量用的形状模板，切短仍有形状。

        丢掉则等于这次 token 白花 —— 这是本模块唯一一处"宁截不弃"。
        """
        plan = QueryPlan(hyde="答" * 400)
        self.assertEqual(len(plan.hyde), HYDE_MAX_CHARS)

    def test_hyde_whitespace_becomes_none(self):
        self.assertIsNone(QueryPlan(hyde="   \n ").hyde)

    def test_lists_are_deduped_and_capped(self):
        plan = QueryPlan(intents=["甲", " 甲 ", "乙", "", "丙", "丁", "戊", "己"])
        self.assertEqual(plan.intents, ["甲", "乙", "丙", "丁"])
        self.assertLessEqual(len(plan.intents), MAX_QUERIES)

    def test_defaults_are_an_empty_fallback_plan(self):
        plan = QueryPlan()
        self.assertEqual(plan.source, "fallback")
        self.assertEqual(plan.intents, [])
        self.assertIsNone(plan.hyde)

    def test_extra_fields_are_rejected(self):
        """计划是契约：多出来的字段说明模型误解了它，不该被静默接受。"""
        with self.assertRaises(Exception):
            QueryPlan.model_validate({"intents": ["甲"], "rewrite": "乙"})


class PromptContractTest(unittest.TestCase):
    """提示词里那两句必须写死：必须看材料、禁止意图膨胀。

    这两条是 `docs/RAG-DECISIONS.md` 2.3 的失败模式翻出来的，不是风格偏好：
    哪天有人"精简提示词"把它们删掉，这里要红。
    """

    def test_materials_are_mandatory_in_the_prompt(self):
        self.assertIn("必须看材料", query_plan.SYSTEM_PROMPT)
        self.assertIn("材料里没有出现过的", query_plan.SYSTEM_PROMPT)
        self.assertIn("以材料为准", query_plan.SYSTEM_PROMPT)

    def test_intent_inflation_is_forbidden_in_the_prompt(self):
        self.assertIn("禁止意图膨胀", query_plan.SYSTEM_PROMPT)
        self.assertIn("不得增加原问题没有的方面", query_plan.SYSTEM_PROMPT)
        # 反例（"它多少钱"被扩成收费模式/优惠政策）必须在
        self.assertIn("收费模式", query_plan.SYSTEM_PROMPT)

    def test_hyde_is_declared_a_vector_only_template(self):
        self.assertIn("只用来算向量相似度", query_plan.SYSTEM_PROMPT)
        self.assertIn("永远不会被当作答案或证据", query_plan.SYSTEM_PROMPT)
        self.assertIn(str(HYDE_MAX_CHARS), query_plan.SYSTEM_PROMPT)

    def test_subqueries_are_gated_on_complexity(self):
        self.assertIn("简单事实问题", query_plan.SYSTEM_PROMPT)
        self.assertIn("不要为了显得周全每次都拆", query_plan.SYSTEM_PROMPT)

    def test_unknown_fields_are_told_to_be_empty(self):
        self.assertIn("不确定就给 null 或 []", query_plan.SYSTEM_PROMPT)

    def test_signature_matches_the_contract(self):
        params = inspect.signature(plan_query).parameters
        self.assertEqual(list(params), ["query", "materials"])
        self.assertIs(params["materials"].default, None)
        self.assertEqual(params["materials"].kind,
                         inspect.Parameter.KEYWORD_ONLY)


class LogPrivacyTest(PlanTestBase):
    """日志只记形状，不记 query 原文与材料内容。"""

    def test_log_has_no_query_or_material_text(self):
        query = "谁能批这笔预算"
        material = "预算管理办法 第一章总则 内部文件"
        self.use_model(FULL)
        with self.assertLogs("app.knowledge.query_plan", level="INFO") as captured:
            run(plan_query(query, materials=[material]))
        line = "\n".join(captured.output)
        self.assertIn("查询计划", line)
        self.assertIn("耗时=", line)
        self.assertIn("token", line)
        self.assertNotIn(query, line)
        self.assertNotIn("预算管理办法", line)
        self.assertNotIn("第一章", line)

    def test_degrade_also_logs_without_content(self):
        self.use_model(RuntimeError("boom"))
        with self.assertLogs("app.knowledge.query_plan", level="INFO") as captured:
            run(plan_query("它多少钱", materials=["标题"]))
        line = "\n".join(captured.output)
        self.assertIn("outcome=error", line)
        self.assertNotIn("它多少钱", line)


class FakeResponse:
    """假响应：只实现 `_chat_once` 用到的那三个方法。"""

    def __init__(self, body: bytes):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self.body


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, **kwargs):
        self.requests += 1
        return self.response


class RealClientTest(PlanTestBase):
    """走真的 `chat_async`：证明本模块与仓库现有客户端的契约确实对得上。

    其余用例替换了 `chat_async`，那样测的是本模块的解析与降级；这里不替换它，
    只换掉 httpx，覆盖的是"配置 → 请求 → `parse_model_json` → `QueryPlan`"
    这条完整链路 —— 万一 `chat_async` 哪天改成返回别的东西，上面那些用例
    仍然全绿，这条会红。
    """

    def test_end_to_end_through_the_real_client(self):
        content = "```json\n" + json.dumps(FULL, ensure_ascii=False) + "\n```"
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        client = FakeClient(FakeResponse(body))
        with patch("app.generation.service.httpx.AsyncClient", return_value=client):
            plan = run(plan_query("谁能批预算", materials=["预算管理办法 第一章"]))
        self.assertEqual(plan.intents, FULL["intents"])
        self.assertEqual(plan.source, "llm")
        # 一次调用一个请求：既没并发扇出，也没偷偷重试
        self.assertEqual(client.requests, 1)

    def test_http_500_degrades_through_the_real_client(self):
        response = FakeResponse(b"")
        response.raise_for_status = _raise_500
        client = FakeClient(response)
        with patch("app.generation.service.httpx.AsyncClient", return_value=client):
            plan = run(plan_query("谁能批预算", materials=["标题"]))
        self.assertIsNone(plan)
        self.assertEqual(client.requests, 1)


def _raise_500():
    request = httpx.Request("POST", "http://example.invalid/v1/chat/completions")
    raise httpx.HTTPStatusError("HTTP 500", request=request,
                                response=httpx.Response(500, request=request))


if __name__ == "__main__":
    unittest.main()

_LONG_MATERIAL = (
    "预算管理办法 第一章 总则。金额在五万元以内的支出由财务经理核准，"
    "超过五十万元的需总经理办公会审议。批钱这件事按金额分级，审批留痕。"
)


class CorrectedGuardTest(PlanTestBase):
    """`corrected` 的双向闸：换了题、或压根没看材料，都退回用户原话。

    它是四个字段里唯一会替换用户原话的一个，而失败是哑的——下沉到检索时
    相似度高、命中非空，没有任何信号会报警。
    """

    def test_off_topic_correction_is_dropped(self):
        """把口语问题改写成书面语、丢掉原问题的字面——这是换题，不是纠错。"""
        self.use_model({"corrected": "资金归口管理部门的设置依据", "intents": ["随便一问"]})
        plan = run(plan_query("谁能批钱", materials=[_LONG_MATERIAL]))
        self.assertIsNone(plan.corrected)
        self.assertEqual(plan.intents, ["随便一问"])   # 只丢 corrected，其余保留

    def test_correction_without_material_evidence_is_dropped(self):
        """与材料毫无交集 = 没看材料，正是那次覆盖率 0.0 的签名。"""
        self.use_model({"corrected": "量子退相干时间的一般取值"})
        plan = run(plan_query("退相干时间是多少", materials=[_LONG_MATERIAL]))
        self.assertIsNone(plan.corrected)

    def test_normal_correction_survives(self):
        self.use_model({"corrected": "谁能批这笔钱"})
        plan = run(plan_query("谁能批钱", materials=[_LONG_MATERIAL]))
        self.assertEqual(plan.corrected, "谁能批这笔钱")

    def test_short_material_does_not_trip_the_material_gate(self):
        """材料短到一个标题时，「与材料无交集」不构成没看材料 的证据。"""
        self.use_model({"corrected": "谁能批钱"})
        plan = run(plan_query("谁能批钱", materials=["标题"]))
        self.assertEqual(plan.corrected, "谁能批钱")

    def test_correction_survives_when_bigrams_are_absent(self):
        """短到没有 bigram，度量不适用——宁可放过，也不要误删。"""
        self.use_model({"corrected": "A"})
        plan = run(plan_query("A"))
        self.assertEqual(plan.corrected, "A")