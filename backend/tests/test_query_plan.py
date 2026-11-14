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
from app.knowledge.service import _bigram_terms

MODEL_ENV = {
    "MINDMAP_LLM_MODE": "model",
    "MINDMAP_LLM_BASE_URL": "http://example.invalid/v1",
    "MINDMAP_LLM_API_KEY": "test-key",
    "MINDMAP_LLM_MODEL": "test-model",
}

FULL = {
    "corrected": "谁能审批预算",
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
    """开关：**默认开**，显式关掉时零代价。"""

    def test_defaults_to_on_when_env_absent(self):
        """没设开关就是开。

        默认值从 0 翻到 1 的依据写在 `query_plan._enabled` 的 docstring 里：
        接上向量路之后 manual 套 `recall@1` 71.4%→75.0%、`mrr@3` 79.8%→81.5%，
        而纯 BM25 下这三项是 0（同义问法共享同一批 bigram）。
        """
        with patch.dict(os.environ, {"MINDMAP_QUERY_EXPANSION": ""}):
            self.assertTrue(query_plan._enabled())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINDMAP_QUERY_EXPANSION", None)
            self.assertTrue(query_plan._enabled())

    def test_explicit_falsy_values_still_turn_it_off(self):
        """显式关掉仍然有效——默认开不等于关不掉。"""
        for raw in ("0", "false", "no", "off", " FALSE "):
            with patch.dict(os.environ, {"MINDMAP_QUERY_EXPANSION": raw}):
                self.assertFalse(query_plan._enabled(), raw)

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

        corrected 保留（模型确实给了，而且这次纠错本身就站得住），但 `source`
        是 "fallback" —— 这份计划没有资格触发扩展检索。
        """
        self.use_model(FULL)
        plan = run(plan_query("谁能审批予算"))    # 故意带错字
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
        plan = run(plan_query("谁能审批予算", materials=["", "  \n ", None]),
                   )
        self.assertIsNotNone(plan)
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


# 真实形态的材料（各约 200 字，与量阈值时用的是同一批）。长度都远超
# `MIN_MATERIAL_BIGRAMS`，材料闸在这些样本上是真的生效的。
BUDGET = (
    "XX集团预算管理办法（试行）第一章 总则 第一条 为规范全面预算管理，明确审批权限，"
    "依据公司章程制定本办法。第二条 预算分为年度预算与专项预算。单笔五十万元以下的支出"
    "由部门负责人审批，五十万元至二百万元由分管副总和财务负责人联签，"
    "二百万元以上报总经理办公会审议后提交董事会。第三条 预算外支出须单独申报，"
    "不得拆分规避审批权限。第四条 财务部负责预算执行的归口管理与月度分析，超支部分须书面说明。"
)
SECURITY = (
    "信息安全等级保护测评实施指南 定级：信息系统运营使用单位应按照业务信息安全与系统"
    "服务安全两个维度确定保护等级，二级以上系统须备案。测评：第三级系统每年至少测评一次，"
    "测评机构须具备资质。整改：高危风险项须在三个月内完成整改并复测，涉及未授权访问、"
    "弱口令、日志缺失的项列为高危。监督检查：公安机关对第三级以上系统开展定期监督检查。"
)
METRICS = (
    "数据指标口径说明 日活用户（DAU）指自然日内产生至少一次有效行为的去重用户数，"
    "同一用户跨端只计一次。月活用户（MAU）按自然月去重统计。订单量按支付成功口径统计，"
    "取消与退款订单不计入，退款率等于退款订单数除以支付成功订单数。"
    "新增用户指首次注册并完成实名认证的用户。"
)


class CorrectedGuardTest(PlanTestBase):
    """`corrected` 的三道闸：改长了、改跑了、没依据，任一成立就退回用户原话。

    这组用例逐道闸各给一个"必须挡住"和一个"必须放过"，因为三条闸各自都在挡
    别的闸挡不住的东西（见 `_guard_corrected` 的 docstring）：关掉任意一条，
    下面的用例就会红。
    """

    def test_off_topic_rewrite_is_dropped_but_other_fields_survive(self):
        """跨域编造（材料是预算审批，它改写成本领域的术语）→ 丢 corrected。"""
        self.use_model({"corrected": "资金归口管理部门的设置依据",
                        "intents": ["随便一问"], "hyde": "编的答案"})
        plan = run(plan_query("谁能批钱", materials=[BUDGET]))
        self.assertIsNone(plan.corrected)
        self.assertEqual(plan.intents, ["随便一问"])       # 其余字段原样
        self.assertEqual(plan.hyde, "编的答案")
        self.assertEqual(plan.source, "llm")               # 扩展项还在，仍是 llm

    def test_appending_a_second_question_is_dropped(self):
        """原句全留 + 追加一句 = 旧口径下的"完全重合"，恰恰最该挡。

        旧的单边重合口径给这种情况 1.00，`TRUSTED_OVERLAP=0.60` 直接放行；
        长度比 3.5 倍，这里必须挡下。
        """
        self.use_model({"corrected": "谁能批钱？预算外资金怎么审批"})
        plan = run(plan_query("谁能批钱", materials=[BUDGET]))
        self.assertIsNone(plan)

    def test_narrowing_to_a_sub_item_is_dropped(self):
        """把"谁能批钱"收窄成"差旅费用部分"——问题被悄悄改小，字面却几乎全留。"""
        self.use_model({"corrected": "谁能批这笔预算中的差旅费用部分"})
        plan = run(plan_query("谁能批钱", materials=[BUDGET]))
        self.assertIsNone(plan)

    def test_losing_the_question_focus_is_dropped(self):
        """换成同域名词短语：字面 0.55、材料依据 10 条，两道字面闸都拦不住。"""
        self.use_model({"corrected": "等级保护三级系统测评机构的资质要求"})
        plan = run(plan_query("等级保护三级系统多久测评一次", materials=[SECURITY]))
        self.assertIsNone(plan)

    def test_shrinking_to_a_topic_word_is_dropped(self):
        """问题退化成主题词（疑问焦点全没了），长度没变也挡。"""
        self.use_model({"corrected": "预算审批"})
        plan = run(plan_query("谁能批钱", materials=[BUDGET]))
        self.assertIsNone(plan)

    def test_focus_words_are_preserved_by_a_normal_correction(self):
        """「算不算」→「是否」是正常的术语化，不能判成丢焦点。"""
        self.use_model({"corrected": "未授权访问是否属于高危风险项"})
        plan = run(plan_query("未授权访问算不算高危", materials=[SECURITY]))
        self.assertEqual(plan.corrected, "未授权访问是否属于高危风险项")

    def test_typo_correction_survives_on_dice_alone(self):
        """错别字型纠错：字面高度重合，材料里没有那个词也照留（dice 这条路）。"""
        self.use_model({"corrected": "谁能批这笔钱"})
        plan = run(plan_query("谁能批钱", materials=[BUDGET]))
        self.assertEqual(plan.corrected, "谁能批这笔钱")

    def test_colloquial_correction_survives_on_material_evidence(self):
        """指代补全型：字面只剩 0.24，靠材料里的"预算/审批"兜住。"""
        self.use_model({"corrected": "该项目的预算由谁审批"})
        plan = run(plan_query("这个项目的钱谁来批", materials=[BUDGET]))
        self.assertEqual(plan.corrected, "该项目的预算由谁审批")

    def test_same_correction_is_dropped_without_material_evidence(self):
        """同样的指代补全，没有材料就没有依据——字面 0.24 一个人撑不住。"""
        self.use_model({"corrected": "该项目的预算由谁审批"})
        plan = run(plan_query("这个项目的钱谁来批"))
        self.assertIsNone(plan)

    def test_one_material_bigram_is_not_enough(self):
        """与材料只撞 1 条 bigram 不算依据：200 字材料里撞一条太容易。"""
        corrected = "报销单据的审批流转"
        # 先确认这条样本确实只撞 1 条，否则这条用例测不到它要测的东西
        self.assertEqual(
            len(set(_bigram_terms(corrected)) & set(_bigram_terms(BUDGET))), 1)
        self.use_model({"corrected": corrected})
        plan = run(plan_query("发票报销流程审批", materials=[BUDGET]))
        self.assertIsNone(plan)

    def test_known_gap_same_shape_different_head_is_kept(self):
        """已知边界（不是目标）：同句式换中心词，三道闸全过。

        「退款率怎么算的」→「订单量怎么算的」：长度 1.0、疑问焦点「怎么」保留、
        字面 dice 0.50（在 0.40 以上）、材料里两个词都有。任何 bigram 尺子都判
        不了它，因为换掉的正是"这条问题问的是哪个东西"。

        要关这个口子得靠语义（对 query 与 corrected 各算一次 embedding 余弦）或
        实体抽取，两者要么多一次网络调用、要么多一份词典，都不在本轮预算里。
        留着这条用例是为了让缺口可见：哪天补上了它会红，改的人会先读到这段。
        """
        self.use_model({"corrected": "订单量怎么算的"})
        plan = run(plan_query("退款率怎么算的", materials=[METRICS]))
        self.assertEqual(plan.corrected, "订单量怎么算的")

    def test_thin_material_cannot_rescue_a_bold_rewrite(self):
        """材料里一个 bigram 都没有时，它**不能当依据**，改写照样退回原话。

        这条与上一版相反：上一版把"材料太短"当成了放行的理由（材料短 → 不判）。
        两条闸里只要有一条拿不到证据就该退回原话——"度量不适用"时的默认动作是
        退回用户的原话，不是放行一段无法核实的改写。
        """
        self.use_model({"corrected": "报销单据的审批流转"})
        plan = run(plan_query("发票报销流程审批", materials=["标题"]))
        self.assertIsNone(plan)

    def test_one_char_query_with_identical_correction_is_kept(self):
        """度量退化到单字时，完全相同的"纠错"仍然保留（dice=1.0）。"""
        self.use_model({"corrected": "A"})
        plan = run(plan_query("A"))
        self.assertEqual(plan.corrected, "A")

    def test_guard_rebuilds_through_the_model_instead_of_copying(self):
        """被闸改写的计划要重新过一遍 validator，不能 `model_copy` 绕过。

        `model_copy(update=...)` **不跑 validator**。这里用 `model_construct` 造
        一份未校验的计划（hyde 400 字），闸删掉 corrected 之后重建它：如果实现
        退回 `model_copy`，hyde 会原样留下 400 字，这条就红。
        """
        raw = QueryPlan.model_construct(corrected="预算审批", intents=["甲"],
                                        hyde="答" * 400, subqueries=[], source="llm")
        guarded, verdict = query_plan._guard_plan(raw, "谁能批钱", [])
        self.assertEqual(verdict, "focus")
        self.assertIsNone(guarded.corrected)
        self.assertEqual(guarded.intents, ["甲"])
        self.assertEqual(len(guarded.hyde), HYDE_MAX_CHARS)

    def test_plan_is_none_when_the_guard_empties_it(self):
        """只纠错的计划被闸删空后必须返回 None，不能交出一份空壳。"""
        self.use_model({"corrected": "预算审批"})    # 没有材料，只有 corrected
        self.assertIsNone(run(plan_query("谁能批钱")))

    def test_guard_verdict_is_logged_without_the_query(self):
        """闸拦下时日志要能说出"被哪道闸拦的"，否则关了开关就没法查。"""
        self.use_model({"corrected": "谁能批钱？预算外资金怎么审批"})
        with self.assertLogs("app.knowledge.query_plan", level="INFO") as captured:
            run(plan_query("谁能批钱", materials=[BUDGET]))
        line = "\n".join(captured.output)
        self.assertIn("guarded:length", line)
        self.assertNotIn("谁能批钱", line)


class CorrectedGuardTableTest(PlanTestBase):
    """把量阈值用的那批样本钉在测试里：改任何一条阈值都会在这里红。

    keep 10 / drop 12 是人工标注的期望；当前三条闸在这 22 组上零误杀零漏杀。
    样本是我自己造的、规则也是看着这批样本定的，所以它**不是**泛化能力的证明，
    只是"别再悄悄把界线推回去"的回归锁 —— 上一版的 0.30/0.60 在同一批样本上
    是误杀 1、漏杀 8。
    """

    MATERIALS = {"预算": BUDGET, "等保": SECURITY, "指标": METRICS}

    CASES = [
        # (query, corrected, 材料, 期望是否保留)
        ("谁能批钱", "谁能批这笔钱", "预算", True),
        ("预算审批权限怎么划", "预算审批权限怎么划分", "预算", True),
        ("这个项目的钱谁来批", "该项目的预算由谁审批", "预算", True),
        ("未授权访问算不算高危", "未授权访问是否属于高危风险项", "等保", True),
        ("等级保护三级系统多久测评一次", "等级保护第三级系统多久测评一次", "等保", True),
        ("退款率怎么算的", "退款率如何计算", "指标", True),
        ("它多少钱", "超导量子比特的制备成本", "预算", False),
        ("谁能批钱", "谁能批这笔预算中的差旅费用部分", "预算", False),
        ("谁能批钱", "谁能批钱？预算外资金怎么审批", "预算", False),
        ("谁能批钱", "财务负责人和分管副总谁先签字", "预算", False),
        ("等级保护三级系统多久测评一次", "等级保护三级系统测评机构的资质要求", "等保", False),
        ("谁能批钱", "董事会审议超二百万元支出的表决程序", "预算", False),
        ("退款率怎么算的", "退款率与订单量的统计口径分别是什么", "指标", False),
        ("未授权访问算不算高危", "未授权访问的整改时限与复测要求", "等保", False),
        ("订单量怎么算", "订单量按支付成功口径统计，取消与退款订单不计入", "指标", False),
        ("谁能批钱", "预算审批", "预算", False),
    ]

    def test_measured_sample(self):
        current = {"payload": None}

        async def fake(system: str, user: str, **kwargs):
            return current["payload"]

        with patch.object(query_plan, "chat_async", fake):
            for query, corrected, material, expect_keep in self.CASES:
                with self.subTest(query=query, corrected=corrected):
                    current["payload"] = {"corrected": corrected}
                    plan = run(plan_query(query, materials=[self.MATERIALS[material]]))
                    kept = plan is not None and plan.corrected == corrected
                    self.assertEqual(
                        kept, expect_keep,
                        f"「{query}」→「{corrected}」应{'保留' if expect_keep else '丢弃'}")


if __name__ == "__main__":
    unittest.main()
