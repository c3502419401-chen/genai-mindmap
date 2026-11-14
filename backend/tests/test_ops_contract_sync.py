"""跨端 op 契约一致性：与前端 ops-contract.spec.ts 共读同一份 fixture。

这个文件存在的理由：执行器有两份实现（Python 与 TypeScript），各自独立演进
一定会漂——比如 move 的同父 index 上界，一边按摘除前算、一边按摘除后算，
同一个 batch 就会一边成功一边报 index_out_of_range，而模型只会看到"随机失败"。

所以断言不写在各自的实现测试里，而是写在这份共享用例上：任一端改了语义，
这一端的用例先红。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.ops import apply_ops
from app.contracts.mindmap import MindMapContent
from app.contracts.ops import OpBatch, OpError

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "mindmap_ops.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _base() -> MindMapContent:
    return MindMapContent.model_validate(_fixture()["base_tree"])


def _flatten(root) -> list[list[str]]:
    """前序遍历成 [id, label] 序列，与 fixture 的 expect_tree 同构。"""
    out: list[list[str]] = []

    def walk(node) -> None:
        out.append([node.id, node.label])
        for child in node.children:
            walk(child)

    walk(root)
    return out


def _flatten_field(root, field: str) -> list:
    """同一顺序取某个节点字段（`detail` / `note`），与 fixture 的 expect_* 同构。"""
    out: list = []

    def walk(node) -> None:
        out.append(getattr(node, field))
        for child in node.children:
            walk(child)

    walk(root)
    return out


CASES = _fixture()["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_backend_matches_shared_fixture(case: dict) -> None:
    base = _base()
    snapshot = base.model_dump()

    try:
        batch = OpBatch(batch_id="sync", base_revision=_fixture()["base_revision"], ops=case["ops"])
    except ValidationError:
        # 契约层就拒绝（当前只有 update 的空 changes 走这条）。前端在执行器里
        # 抛 OpError('empty_changes')，两端都拒绝即算一致。
        assert case.get("expect_error") in ("empty_changes", None), case["name"]
        return

    try:
        result = apply_ops(base, batch)
    except OpError as exc:
        assert "expect_error" in case, f"{case['name']}：预期成功，实际抛 {exc.code} {exc.message}"
        assert exc.code == case["expect_error"], case["name"]
        if case.get("expect_base_unchanged"):
            # 失败之后传入对象必须逐字节不变：整批原子不能只靠"调用方别用返回值"。
            assert base.model_dump() == snapshot, f"{case['name']}：失败后原树被改动"
        return

    assert "expect_error" not in case, f"{case['name']}：预期失败 {case['expect_error']}，实际成功"
    assert _flatten(result.root) == case["expect_tree"], case["name"]
    # 详情与备注：只在 fixture 声明的用例上比较。`detail` 归生成、`note` 归用户，
    # 两端都必须"改一个不动另一个"——这正是混用两个字段会立刻出事的地方。
    if "expect_details" in case:
        assert _flatten_field(result.root, "detail") == case["expect_details"], case["name"]
    if "expect_notes" in case:
        assert _flatten_field(result.root, "note") == case["expect_notes"], case["name"]


def test_fixture_covers_both_outcomes() -> None:
    """fixture 自身要有覆盖度：只有成功用例或只有失败用例都说明漏了边界。"""
    assert any("expect_tree" in case for case in CASES)
    assert any("expect_error" in case for case in CASES)
    assert any(case.get("expect_base_unchanged") for case in CASES)
