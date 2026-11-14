import asyncio
import inspect
import json
import os
import unittest
from unittest.mock import patch
import httpx
from app.contracts.mindmap import MAX_TREE_DEPTH, MAX_TREE_NODES, MindMapContent
from app.generation.service import (
    CONTEXT_TOKEN_LIMIT,
    DETAIL_PROMPT,
    MIN_BRANCHES,
    SOURCED_DETAIL_PROMPT,
    THINKING_MODELS,
    GenerationError,
    _demo_content,
    chat_async,
    diagnose_density,
    ensure_input_within_context,
    estimate_input_tokens,
    parse_model_json,
    thinking_block,
    thinking_options,
    validate_generated_content,
)


def _oversized(ratio: float = 1.6) -> str:
    """按 `CONTEXT_TOKEN_LIMIT` 现算一份超限样本。

    阈值是可调参数（60000 → 200000 就调整过一次），写死字数的样本会在每次
    调整后变成"没超限"的普通材料，把用例的语义悄悄改掉。汉字约 1.5 字/token，
    所以 1.6 倍字符数一定能换来超过上限的 token 数。
    """
    return "字" * int(CONTEXT_TOKEN_LIMIT * ratio)


def _tree(branches: int) -> dict:
    """造一棵有 `branches` 条一级分支的树。

    一级分支下限是 3，所以「合法模型结果」的最小形态是三条空分支 —— 老用例里
    只写一条分支的 fixture 现在会被挡下，凡是断言生成成功的用例都得用这个函数。
    """
    return {"title": "测试", "root": {"id": "1", "label": "测试", "children": [
        {"id": f"b{index}", "label": f"分支{index}", "children": []}
        for index in range(1, branches + 1)
    ]}}


def _response_body(tree: dict, finish_reason: str | None = None) -> bytes:
    """拼一个 Chat Completions 响应体；`finish_reason=None` 时整个字段不出现。"""
    choice: dict = {"message": {"content": json.dumps(tree)}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return json.dumps({"choices": [choice]}).encode()


class MockAsyncStreamResponse:
    """模拟 httpx 异步流式响应：既是异步上下文管理器，也按块吐字节。"""
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=self.request,
                                        response=httpx.Response(self.status_code, request=self.request))
    async def aiter_bytes(self):
        yield self.data


class MockAsyncClient:
    """模拟 httpx.AsyncClient。`stream()` 与真身一致，返回异步上下文管理器。"""
    def __init__(self, response):
        self.response = response
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FinishReasonTest(unittest.TestCase):
    """`finish_reason` 的三种取值。

    `provider._parse_completion()` 会拦 `length`，但工具内的生成与展开走的是
    `generation.service.chat_async()`，那条路径没有这一层。不看的话，被硬切的
    半截 JSON 会被 `_balance_brackets` 补成"合法"，半棵树照样返回成功。
    """

    TREE = {"title": "测试", "root": {"id": "1", "label": "测试",
            "children": [{"id": "b1", "label": "分支", "children": []}]}}

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "MINDMAP_LLM_MODE": "model",
            "MINDMAP_LLM_BASE_URL": "https://example.com/v1",
            "MINDMAP_LLM_MODEL": "test",
            "MINDMAP_LLM_API_KEY": "not-a-real-key",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def use_async_client(self, response):
        patcher = patch("app.generation.service.httpx.AsyncClient",
                        return_value=MockAsyncClient(response))
        client_class = patcher.start()
        self.addCleanup(patcher.stop)
        return client_class

    def test_chat_async_rejects_truncated_output(self):
        """finish_reason=length：抛错，绝不把补全括号后的半棵树当成功。"""
        self.use_async_client(MockAsyncStreamResponse(_response_body(self.TREE, "length")))
        with self.assertRaises(GenerationError) as raised:
            asyncio.run(chat_async("系统", "用户"))
        self.assertIn("截断", str(raised.exception))

    def test_chat_async_parses_without_finish_reason(self):
        """字段缺失照常解析：旧 mock 与部分兼容端点都不返回它。"""
        self.use_async_client(MockAsyncStreamResponse(_response_body(self.TREE)))
        result = asyncio.run(chat_async("系统", "用户"))
        self.assertEqual(result["title"], "测试")
        self.assertEqual(result["root"]["children"][0]["id"], "b1")

    def test_chat_async_parses_on_stop(self):
        self.use_async_client(MockAsyncStreamResponse(_response_body(self.TREE, "stop")))
        result = asyncio.run(chat_async("系统", "用户"))
        self.assertEqual(result["root"]["label"], "测试")


class ContextBudgetTest(unittest.TestCase):
    """输入侧只剩一道边界：模型的上下文窗口。

    「材料不超过 20000 字」那种产品口径已经删掉——它是照一句话主题拍的，
    与模型真正的拒收条件无关。这里守住替代它的那道物理边界：估算口径、
    保守上限、以及"在发请求之前就拒"。
    """

    def test_estimate_counts_chinese_and_ascii_apart(self):
        """汉字约 1.5 字/token、ASCII 约 4 字符/token，两种密度差四倍。"""
        self.assertEqual(estimate_input_tokens(""), 0)
        self.assertEqual(estimate_input_tokens("字" * 15), 10)
        self.assertEqual(estimate_input_tokens("a" * 40), 10)
        # 混合内容按各自密度分别累加，不能整体按一个折算率算
        self.assertEqual(estimate_input_tokens("字" * 15 + "a" * 40), 20)

    def test_limit_is_a_positive_context_budget(self):
        """限额是可调参数，具体数字不是契约。

        原先断言 `== 60000`：那个数既没有实测依据，又会把每次调阈值都变成一次
        假失败。现在只守住"它是个像样的正数预算"，外加"默认边界确实由它决定"
        ——签名里的默认值必须就是这个常量，否则调常量等于没调。
        """
        self.assertIsInstance(CONTEXT_TOKEN_LIMIT, int)
        self.assertGreater(CONTEXT_TOKEN_LIMIT, 10_000)
        default = inspect.signature(
            ensure_input_within_context).parameters["limit"].default
        self.assertEqual(default, CONTEXT_TOKEN_LIMIT)

    def test_limit_covers_a_realistic_long_document(self):
        """限额必须装得下一份真实的长材料。

        只断言「大于一万」守不住量级——有人把它从 200000 悄悄改到 50000
        照样能过。这里用「十二万字」这把尺子：它约合 8 万 token，是一份
        五十页扫描件的量级，也是实测跑通过的长度（12106 字材料生成 526 节点）。
        阈值低于这个数，就等于把那批已经能用的场景重新关到门外。
        """
        realistic = "字" * 120_000
        self.assertGreater(CONTEXT_TOKEN_LIMIT, estimate_input_tokens(realistic))

    def test_roomy_material_passes_and_returns_the_estimate(self):
        self.assertEqual(ensure_input_within_context("字" * 1500), 1000)

    def test_overflow_raises_a_message_the_ui_can_show(self):
        text = _oversized()        # 约 1.07 倍上限的 token，必定超限
        tokens = estimate_input_tokens(text)
        self.assertGreater(tokens, CONTEXT_TOKEN_LIMIT)
        with self.assertRaises(GenerationError) as raised:
            ensure_input_within_context(text)
        message = str(raised.exception)
        self.assertIn(f"材料约 {len(text)} 字", message)
        self.assertIn(f"{tokens} token", message)
        self.assertIn(str(CONTEXT_TOKEN_LIMIT), message)
        self.assertIn("请精简后重试，或分成多次生成", message)

    def test_system_prompt_cost_counts_toward_the_limit(self):
        """系统提示词也在同一个窗口里，不能只算用户正文。"""
        text = "字" * 1500          # 1000 token
        self.assertEqual(ensure_input_within_context(text, extra_tokens=100), 1100)
        with self.assertRaises(GenerationError):
            ensure_input_within_context(text, limit=1050, extra_tokens=100)

    def test_chat_async_refuses_oversized_input_before_the_request_goes_out(self):
        """超限的请求不该先花一次网络往返、再拿回一个 400。

        agent 的工具（生成 / 展开 / 编辑）全走这条出口，它是唯一的一道输入边界。
        """
        env = {"MINDMAP_LLM_MODE": "model", "MINDMAP_LLM_BASE_URL": "https://example.com/v1",
               "MINDMAP_LLM_MODEL": "test", "MINDMAP_LLM_API_KEY": "not-a-real-key"}
        with patch.dict(os.environ, env), \
             patch("app.generation.service.httpx.AsyncClient") as client:
            with self.assertRaises(GenerationError):
                asyncio.run(chat_async("系统", _oversized()))
        client.assert_not_called()


class ParseModelJsonTest(unittest.TestCase):
    """模型输出的 JSON 修复。

    实测两大类失败：收尾多写一个花括号（finish_reason 仍是 stop），
    以及撞 max_tokens 被硬切断（缺闭合符）。后者的合法性现在由 finish_reason
    先判掉，这里测的是"修复器本身还能用"。
    """

    TREE = ('{"title":"t","root":{"id":"1","label":"根","children":'
            '[{"id":"2","label":"分支","children":[]}]}}')

    def test_plain_json(self):
        self.assertEqual(parse_model_json(self.TREE)["title"], "t")

    def test_code_fence(self):
        self.assertEqual(parse_model_json(f"```json\n{self.TREE}\n```")["title"], "t")

    def test_extra_closing_brace_is_dropped(self):
        """末尾多一个 }：实测 100000 字符输入 3 次里 2 次这样。"""
        broken = self.TREE + "}"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(broken)
        self.assertEqual(parse_model_json(broken)["title"], "t")

    def test_extra_closing_bracket_is_dropped(self):
        broken = '{"a":[1,2]]}'
        self.assertEqual(parse_model_json(broken), {"a": [1, 2]})

    def test_missing_closing_braces_are_filled(self):
        """输出被 max_tokens 切断：结构没写完，补齐后仍可用。"""
        truncated = self.TREE[:-3]          # 去掉末尾的 "}}"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(truncated)
        parsed = parse_model_json(truncated)
        self.assertEqual(parsed["root"]["label"], "根")

    def test_braces_inside_strings_are_untouched(self):
        """节点文字里出现 { } 时不能被当成结构括号修掉。"""
        payload = ('{"title":"a}b","root":{"id":"1","label":"含 } 与 { 的文字",'
                   '"children":[]}}') + "}"
        parsed = parse_model_json(payload)
        self.assertEqual(parsed["title"], "a}b")
        self.assertEqual(parsed["root"]["label"], "含 } 与 { 的文字")

    def test_surrounding_prose_is_ignored(self):
        text = f"好的，结果如下：\n{self.TREE}\n以上。"
        self.assertEqual(parse_model_json(text)["title"], "t")

    def test_unrecoverable_content_still_raises(self):
        with self.assertRaises(ValueError):
            parse_model_json("完全不是 JSON")


class BranchFloorTest(unittest.TestCase):
    """一级分支下限（`MIN_BRANCHES = 3`）：只作用于模型整图生成这条路径。

    下限是**质量**要求而不是合法性要求：`MindMapContent.validate_tree()` 不加这道
    门槛，旧图和用户手工删到一条分支的图照常能存能开；demo 也显式跳过。

    门槛住在 `validate_generated_content()` 里，agent 的 `mindmap_generate` 收尾
    走的就是它，所以这里直接对那个函数断言——经由一条已经删掉的同步出口去测，
    测到的只会是那条出口有没有接上它，而不是门槛本身。
    """

    def test_single_branch_is_rejected(self):
        """一条分支就是一条链，不是层次结构。

        文案必须可操作：说清模型给了几条、要求几条，并给出「换一种说法」或
        「补充材料」这两条出路，而不是干巴巴一句「不合格」。
        """
        with self.assertRaises(GenerationError) as raised:
            validate_generated_content(_tree(1))
        message = str(raised.exception)
        self.assertIn("分支", message)
        self.assertIn("1 条", message)
        self.assertIn("换一种说法", message)
        self.assertIn("补充更多材料", message)

    def test_two_branches_are_rejected(self):
        with self.assertRaises(GenerationError) as raised:
            validate_generated_content(_tree(2))
        self.assertIn("2 条", str(raised.exception))

    def test_root_only_tree_is_rejected(self):
        """光杆根节点与「分支不够」是两种文案：前者连层次都没有。"""
        tree = {"title": "测试", "root": {"id": "1", "label": "测试", "children": []}}
        with self.assertRaises(GenerationError) as raised:
            validate_generated_content(tree)
        self.assertIn("没有任何分支", str(raised.exception))

    def test_three_branches_pass(self):
        content, diagnostics = validate_generated_content(_tree(3))
        self.assertEqual(len(content.root.children), 3)
        # 密度诊断不在这条出口里：它只是提示，由调用方（agent 的生成收尾）决定要不要带。
        self.assertEqual(diagnostics, [])

    def test_demo_single_child_placeholder_still_passes(self):
        """demo 是固定占位结构，本来就只有一个子节点，不该被模型质量门槛打回。

        demo 出口（agent 的 run）从不经过这道校验，仍是显式关掉它更接近真实调用。
        """
        content, _ = validate_generated_content(
            _demo_content("测试").model_dump(), enforce_min_branches=False)
        self.assertEqual(len(content.root.children), 1)

    def test_contract_still_accepts_a_single_branch_tree(self):
        """契约校验不设分支下限：旧图与手工编辑后的图必须照常能存能开。"""
        content = MindMapContent.model_validate(_tree(1))
        self.assertEqual(len(content.root.children), 1)

    def test_min_branch_constant(self):
        self.assertEqual(MIN_BRANCHES, 3)


class PromptQualityTest(unittest.TestCase):
    """prompt 里的数量约束与引用正反例：模型只照字面执行，不写就是没有。"""

    # quote 示例段本身是「原文：…」引出的一句话。锚点落在这里而不是整段第一个
    # ✅/❌：提示词里还有拆到底的形态对照（同样用 ❌/✅），按整段取首标记会被
    # 那些行抢走，取出来的"引用"其实是结构示例。
    QUOTE_ANCHOR = "原文：会议决定在十月十七日发布二点零版本。"

    def _quote_examples(self) -> str:
        return SOURCED_DETAIL_PROMPT.split(self.QUOTE_ANCHOR, 1)[1]

    def test_both_prompts_state_the_point_and_branch_floor(self):
        for prompt in (DETAIL_PROMPT, SOURCED_DETAIL_PROMPT):
            self.assertIn("至少 2 个要点", prompt)
            self.assertIn("不少于 3 条", prompt)

    def test_material_branch_allows_expanding_beyond_the_material(self):
        """带材料时**内容可以展开**，但出处必须标：这是这一轮改掉的旧要求。

        上一版写的是"忠实整理、材料里没有的不要添加、依据不足宁可少给"，
        实测把整棵树压成了材料的目录（同一份 12392 字材料，全文可见 100%、
        检索形态 28.8%）。新契约只保留引用纪律的那一半。
        """
        self.assertIn("材料没覆盖的方面按你的知识补全", DETAIL_PROMPT)
        self.assertIn("不要留「材料中没有相关内容」这种空节点", DETAIL_PROMPT)
        self.assertIn("凡引用、转述、概括材料的，必须标出处", DETAIL_PROMPT)
        self.assertIn("自主补充的内容不要冒充材料", DETAIL_PROMPT)
        # 旧要求必须真的没了，否则两句在提示词里打架，模型只会执行更保守的那句
        self.assertNotIn("依据不足时宁可", DETAIL_PROMPT)
        self.assertNotIn("不要添加材料里", DETAIL_PROMPT)

    def test_prompt_node_ceiling_follows_the_contract(self):
        """提示词里的节点/层级上限必须与契约同值。

        这两个数曾经各自写死：契约从 300 放到 1000 之后提示词还在叫模型
        「不超过 300」，模型自我设限，契约放宽等于没生效。
        """
        self.assertIn(f"节点不超过 {MAX_TREE_NODES}", DETAIL_PROMPT)
        self.assertIn(f"层级不超过 {MAX_TREE_DEPTH}", DETAIL_PROMPT)
        self.assertIn(f"节点不超过 {MAX_TREE_NODES}", SOURCED_DETAIL_PROMPT)

    def test_sourced_prompt_shows_verbatim_quote_example(self):
        """`quote` 退化成概括是最常见的一种坏引用，必须有正反例压住。"""
        original = "会议决定在十月十七日发布二点零版本。"
        self.assertIn("❌", SOURCED_DETAIL_PROMPT)
        self.assertIn("✅", SOURCED_DETAIL_PROMPT)
        self.assertIn(original, SOURCED_DETAIL_PROMPT)

        examples = self._quote_examples()
        good_line = examples.split("✅", 1)[1].split("\n", 1)[0]
        good_quote = good_line.split('quote="', 1)[1].split('"', 1)[0]
        # 正向示例必须是原文里的连续片段，逐字可查
        self.assertIn(good_quote, original)

        bad_line = examples.split("❌", 1)[1].split("\n", 1)[0]
        bad_quote = bad_line.split('quote="', 1)[1].split('"', 1)[0]
        # 反向示例是概括出来的话，原文里逐字找不到
        self.assertNotIn(bad_quote, original)


class DepthPromptTest(unittest.TestCase):
    """展开深度：判据是「还能不能再拆」，不是固定层数，也不是「越深越好」。

    改这段之前，「每个分支再给 2-4 个要点」把结构暗示成根 → 分支 → 要点，
    38036 字的论文实测只长到 4 层（103 个节点），`MAX_TREE_DEPTH = 8` 一次都
    没被碰到；反过来写「越深越好」又会让模型为了凑层数制造节点。这两面都要守。
    """

    def test_no_prompt_suggests_a_fixed_number_of_levels(self):
        for prompt in (DETAIL_PROMPT, SOURCED_DETAIL_PROMPT,
                       THINKING_MODELS["default"]["prompt"]):
            self.assertNotIn("2-4", prompt)

    def test_both_prompts_carry_the_split_until_rule(self):
        """两条出口共用同一套深度规则：有来源时只换判据，不换「要不要继续拆」。"""
        for prompt in (DETAIL_PROMPT, SOURCED_DETAIL_PROMPT):
            self.assertIn("还能不能再拆", prompt)
            self.assertIn("不要停在两级", prompt)

    def test_depth_ceiling_is_not_a_target(self):
        """上限继续取自契约常量，并且明确写成可读性上限——数字不是目标。"""
        for prompt in (DETAIL_PROMPT, SOURCED_DETAIL_PROMPT):
            self.assertIn(f"层级上限 {MAX_TREE_DEPTH}", prompt)
            self.assertIn("不是要拆到的目标", prompt)

    def test_stop_condition_is_stated(self):
        """没有停止条件，「继续拆」就会变成造层级。"""
        for prompt in (DETAIL_PROMPT, SOURCED_DETAIL_PROMPT):
            self.assertIn("最小可执行单元", prompt)
            self.assertIn("单一概念", prompt)

    def test_sourced_prompt_ties_depth_to_the_material_and_marks_the_rest_unsourced(self):
        """有来源时「继续拆」可以越过材料，但越过的那一层不挂引用。

        上一版的判据是"判据只看材料，材料没说到就停"，代价是整棵树变成材料的目录。
        新判据保留"材料有更细的说法就照它拆"，材料没说的部分改为可以凭知识补——
        同一句里必须把引用纪律钉上，否则"拆得深"会退化成"编得多"。
        """
        self.assertIn("原文里还有更具体的表述支撑", SOURCED_DETAIL_PROMPT)
        self.assertIn("也可以按你的知识继续拆——只是那一层不挂引用", SOURCED_DETAIL_PROMPT)
        self.assertIn("自己补的内容不要挂引用", SOURCED_DETAIL_PROMPT)
        # 旧措辞必须消失：两句同时在场时模型只会执行更保守的那一句
        self.assertNotIn("判据只看材料", SOURCED_DETAIL_PROMPT)
        self.assertNotIn("宁可少给", SOURCED_DETAIL_PROMPT)

    def test_default_thinking_model_does_not_cap_depth(self):
        """思考模型拼在生成提示词后面，它若给了层数区间就会被模型当成硬指标。"""
        prompt = THINKING_MODELS["default"]["prompt"]
        self.assertIn("不要停在两级", prompt)
        self.assertIn("拆出更具体的内容", prompt)


class ThinkingModelTest(unittest.TestCase):
    """思维模型表：同一句主题换一套拆解步骤，模型产出的结构就不同。

    这里只测「拼装」这一段——真正有没有拆出不同的树取决于模型，没法在单测里断言；
    能守住的是三件事：选中的那套步骤确实进了提示词、未知值不会炸、五套东西不重复。
    """

    def test_block_carries_the_selected_prompt(self):
        block = thinking_block("first-principles")
        self.assertIn("思考方式", block)
        self.assertIn(THINKING_MODELS["first-principles"]["prompt"], block)
        # 换行包裹：拼到提示词末尾时不能和上一句黏成一行
        self.assertTrue(block.startswith("\n"))
        self.assertTrue(block.endswith("\n"))

    def test_unknown_value_falls_back_to_default(self):
        """未知值回退 default 而不是抛错：前端版本不一致时多一个陌生值不该让生成失败。"""
        self.assertEqual(thinking_block("不存在的值"), thinking_block("default"))
        self.assertEqual(thinking_block(""), thinking_block("default"))
        self.assertIn(THINKING_MODELS["default"]["prompt"], thinking_block("不存在的值"))

    def test_default_is_not_empty(self):
        """`default` 也是一种拆解方法，「通用」不等于「不加提示」。"""
        self.assertNotEqual(thinking_block("default").strip(), "")

    def test_options_expose_five_choices(self):
        options = thinking_options()
        self.assertEqual(len(options), 5)
        for option in options:
            self.assertEqual(set(option), {"value", "label"})
            self.assertTrue(option["value"])
            self.assertTrue(option["label"])
        # 默认项排第一：前端不选时选中的那项必须与后端默认值一致
        self.assertEqual(options[0]["value"], "default")
        self.assertEqual([option["value"] for option in options],
                         list(THINKING_MODELS))

    def test_every_prompt_is_distinct(self):
        """五份 prompt 必须互不相同：复制粘贴漏改时这条会红。"""
        prompts = [entry["prompt"] for entry in THINKING_MODELS.values()]
        self.assertEqual(len(prompts), len(set(prompts)))
        labels = [entry["label"] for entry in THINKING_MODELS.values()]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(labels, ["通用", "第一性原理", "费曼", "5W1H", "批判性"])


class DensityDiagnosticTest(unittest.TestCase):
    """`diagnose_density()` 的规则：只出 warn 提示，不构成失败。"""

    @staticmethod
    def _content(sparse_children: int, dense_children: int) -> MindMapContent:
        return MindMapContent.model_validate({
            "title": "测试",
            "root": {"id": "r", "label": "根", "children": [
                {"id": "sparse", "label": "稀疏分支", "children": [
                    {"id": f"s{index}", "label": f"要点{index}", "children": []}
                    for index in range(1, sparse_children + 1)]},
                {"id": "dense", "label": "充实分支", "children": [
                    {"id": f"d{index}", "label": f"要点{index}", "children": []}
                    for index in range(1, dense_children + 1)]},
            ]},
        })

    def test_sparse_branch_is_reported_with_its_count(self):
        diagnostics = diagnose_density(self._content(0, 2))
        self.assertEqual(len(diagnostics), 1)
        item = diagnostics[0]
        self.assertEqual(item.code, "SPARSE_BRANCH")
        self.assertEqual(item.level.value, "warn")
        self.assertEqual(item.node_id, "sparse")
        self.assertIn("稀疏分支", item.message)
        self.assertIn("0 个要点", item.message)

    def test_minimum_is_configurable(self):
        diagnostics = diagnose_density(self._content(0, 2), minimum=3)
        self.assertEqual([item.node_id for item in diagnostics], ["sparse", "dense"])

    def test_dense_tree_has_no_diagnostic(self):
        self.assertEqual(diagnose_density(self._content(2, 3)), [])


if __name__ == "__main__":
    unittest.main()
