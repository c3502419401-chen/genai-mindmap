"""跨图参照（`optimization/cross_map.py`）的判据。

这一层与前三层的差别是**它读库、读网络**。所以用例的重点不是"相似度算得对"，
而是四条边界：

  1. 关掉时一次调用都不发生（不读库、不算向量、不 import numpy）；
  2. 同名图不当参照（否则每次保存都在给上一版自己点赞）；
  3. 同一个历史节点最多当一次参照物（否则 5 条建议里 3 条说同一件事）；
  4. 任何失败都收敛成空列表（数据库/向量服务挂了不该让 `/suggest` 变 500）。

用例里的树很浅，规则层的 `SINGLE_CHILD` 会同时产出 `expand`。那些条数与这一层
无关，所以断言统一走 `cross_only()`——否则失败会指向错误的地方。
"""
from __future__ import annotations

import pytest

from app.contracts.diagnostic import SuggestionKind
from app.contracts.mindmap import MindMapContent, Node
from app.optimization import cross_map
from app.optimization.service import suggest


def node(node_id: str, label: str, *, detail: str | None = None,
         children: list[Node] | None = None) -> Node:
    return Node(id=node_id, label=label, detail=detail, children=children or [])


def content(*children: Node, title: str = "新图") -> MindMapContent:
    return MindMapContent(title=title, root=node("root", "根", children=list(children)))


def history_entry(map_id: str, title: str, item: Node, height: int):
    return (map_id, title, item, height)


def cross_only(items):
    return [item for item in items if item.kind is SuggestionKind.cross_map]


def run(target: MindMapContent, **kwargs):
    return cross_only(cross_map.suggestions(target, **kwargs))


class FakeVectors:
    """把 `vector_cache` 与 `embedding` 换成字典查表。

    向量只有两维，好让"相似 / 不相似"是判据而不是数值巧合：
    `[1, 0]` 与 `[1, 0]` 点积 1.0（同一个概念），`[1, 0]` 与 `[0, 1]` 是 0.0。
    """

    def __init__(self, mapping: dict[str, list[float]]):
        self.mapping = mapping
        self.puts: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.knowledge import embedding, vector_cache

        def get_vectors(hashes, model):
            return {key: self.mapping[key] for key in hashes if key in self.mapping}

        def put_vectors(items, model):
            self.puts.extend(key for key, _ in items)
            self.mapping.update({key: vector for key, vector in items})
            return len(items)

        def embed_texts(texts):
            # 用例没准备向量的文本（比如根节点「根」）给一个零向量：它与任何
            # 单位向量的余弦都是 0，不会命中阈值。断言式的失败会让"这一层返回
            # 空列表"这个正常路径变成异常，反而测不到东西。
            #
            # 宽度跟着已有向量走：numpy 不接受参差不齐的二维数组，而"维度必须
            # 一致"在生产上是 `chunk_vectors.dim` 列保证的，不该在这里重复实现。
            width = max((len(item) for item in self.mapping.values()), default=0)
            return [self.mapping.get(text) or [0.0] * width for text in texts]

        monkeypatch.setattr(vector_cache, "text_hash", lambda text: text.strip())
        monkeypatch.setattr(vector_cache, "get_vectors", get_vectors)
        monkeypatch.setattr(vector_cache, "put_vectors", put_vectors)
        monkeypatch.setattr(embedding, "embed_texts", embed_texts)


@pytest.fixture
def vectors(monkeypatch: pytest.MonkeyPatch) -> FakeVectors:
    fake = FakeVectors({})
    fake.install(monkeypatch)
    monkeypatch.setattr(cross_map, "_model", lambda: "text-embedding-v3")
    return fake


def test_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关没开时**一次都不该发生**：连历史库都不读。"""
    monkeypatch.delenv(cross_map.ENV, raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("关掉时不该读历史库")

    monkeypatch.setattr(cross_map, "_history", boom)
    assert cross_map.suggestions(content(node("a", "甲"))) == []
    assert cross_map.enabled() is False


def test_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cross_map.ENV, "1")
    assert cross_map.enabled() is True


def test_leaf_gets_expand(monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors) -> None:
    """历史里这个节点有子节点、这里是个叶子 → 一条跨图建议，指向那张图那个节点。"""
    monkeypatch.setenv(cross_map.ENV, "1")
    vectors.mapping.update({"偏差-方差": [1.0, 0.0], "偏差-方差分解": [1.0, 0.0]})
    history = [history_entry("m1", "旧图", node("h1", "偏差-方差分解"), 2)]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    items = run(content(node("n1", "偏差-方差")))
    assert len(items) == 1
    assert items[0].node_id == "n1"
    assert items[0].evidence == "m1#偏差-方差分解"
    assert "旧图" in items[0].message
    assert "叶子" in items[0].message


def test_longer_body_gets_detail(
    monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors,
) -> None:
    """两边都是叶子，但参照图正文长得多 → 一条"写得更详细"。"""
    monkeypatch.setenv(cross_map.ENV, "1")
    vectors.mapping.update({"监督学习": [1.0, 0.0]})
    history = [history_entry("m1", "旧图", node("h1", "监督学习", detail="字" * 80), 1)]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    items = run(content(node("n1", "监督学习")))
    assert len(items) == 1
    assert "写得更详细" in items[0].message
    assert "80" in items[0].message


def test_below_threshold_is_silent(
    monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors,
) -> None:
    """相似度不到阈值就不是"同类节点"，差多少字都不报。"""
    monkeypatch.setenv(cross_map.ENV, "1")
    vectors.mapping.update({"甲": [1.0, 0.0], "乙": [0.0, 1.0]})
    history = [history_entry("m1", "旧图", node("h1", "乙", detail="字" * 200), 3)]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    assert run(content(node("n1", "甲"))) == []


def test_same_title_is_not_a_reference(
    monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors,
) -> None:
    """同名图不当参照：同一主题的上一版不是"别人的写法"。"""
    monkeypatch.setenv(cross_map.ENV, "1")
    vectors.mapping.update({"甲": [1.0, 0.0]})
    history = [history_entry("m1", "同一张图", node("h1", "甲", detail="字" * 200), 3)]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    assert run(content(node("n1", "甲"), title="同一张图")) == []
    # 换个标题就命中，证明前面的空结果确实是"同名"造成的
    assert len(run(content(node("n1", "甲"), title="另一张图"))) == 1


def test_self_is_excluded(monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors) -> None:
    """`map_id` 命中的那条记录不能当自己的参照物。"""
    monkeypatch.setenv(cross_map.ENV, "1")
    vectors.mapping.update({"甲": [1.0, 0.0]})
    history = [history_entry("self-id", "另一张图", node("h1", "甲", detail="字" * 200), 3)]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    target = content(node("n1", "甲"), title="另一张图")
    assert run(target, map_id="self-id") == []


def test_a_history_node_backs_only_one_suggestion(
    monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors,
) -> None:
    """同一个历史节点只当一次参照物：否则几条建议在说同一件事。"""
    monkeypatch.setenv(cross_map.ENV, "1")
    vectors.mapping.update({"甲": [1.0, 0.0], "甲一": [1.0, 0.0]})
    history = [history_entry("m1", "旧图", node("h1", "参照", detail="字" * 200), 1)]
    vectors.mapping["参照"] = [1.0, 0.0]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    target = content(node("n1", "甲"), node("n2", "甲一"))
    assert len(run(target)) == 1


def test_at_most_five(monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors) -> None:
    """一次体检最多 5 条：这类建议的信息密度低，刷满面板等于把规则层淹掉。"""
    monkeypatch.setenv(cross_map.ENV, "1")
    children, history = [], []
    for index in range(8):
        label = f"节点{index}"
        # 8 维正交基：每个节点只与自己那个历史节点相似（1.0），与别的是 0.0。
        # 用"都接近 1.0"的向量会让去重逻辑先把历史节点用光，测到的就不是上限。
        vector = [0.0] * 8
        vector[index] = 1.0
        vectors.mapping[label] = vector
        children.append(node(f"n{index}", label))
        history.append(history_entry(f"m{index}", f"旧图{index}",
                                     node(f"h{index}", label, detail="字" * 200), 1))
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    assert len(run(content(*children))) == cross_map.MAX_SUGGESTIONS


def test_embedding_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors,
) -> None:
    """向量服务挂了只是"这一类没有结论"，不是异常。"""
    from app.knowledge import embedding

    monkeypatch.setenv(cross_map.ENV, "1")
    history = [history_entry("m1", "旧图", node("h1", "甲", detail="字" * 200), 1)]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    def boom(_texts):
        raise embedding.EmbeddingError("服务不可用")

    monkeypatch.setattr(embedding, "embed_texts", boom)
    assert run(content(node("n1", "甲"))) == []


def test_history_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """历史图库读不出来（库文件被删、锁住）同样只等于"没有依据"。"""
    monkeypatch.setenv(cross_map.ENV, "1")

    def boom(_type):
        raise RuntimeError("库读不出来")

    monkeypatch.setattr(cross_map, "_history", boom)
    assert cross_map.suggestions(content(node("n1", "甲"))) == []


def test_suggest_does_not_include_cross_map_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`suggest()` 的默认路径（开关关）不能出现 `cross_map` 建议。"""
    monkeypatch.delenv(cross_map.ENV, raising=False)
    items = suggest(content(node("a", "甲"), node("b", "乙")), use_model=False)
    assert cross_only(items) == []


def test_suggest_takes_explicit_switch(
    monkeypatch: pytest.MonkeyPatch, vectors: FakeVectors,
) -> None:
    """显式 `use_cross_map=True` 时不用环境变量也生效（测试与脚本用）。"""
    monkeypatch.delenv(cross_map.ENV, raising=False)
    vectors.mapping.update({"甲": [1.0, 0.0]})
    history = [history_entry("m1", "旧图", node("h1", "甲", detail="字" * 200), 1)]
    monkeypatch.setattr(cross_map, "_history", lambda _type: history)

    items = suggest(content(node("n1", "甲")), use_model=False, use_cross_map=True)
    assert len(cross_only(items)) == 1


def test_height_counts_levels() -> None:
    """`_subtree_height` 的口径：叶子是 1 层，不是 0。"""
    assert cross_map._subtree_height(node("a", "叶")) == 1
    assert cross_map._subtree_height(node("a", "父", children=[node("b", "子")])) == 2


def test_body_len_reads_note_too() -> None:
    """用户自己写的 `note` 也算正文——只认 `detail` 会把"已经补过备注"报成空白。"""
    assert cross_map._body_len(node("a", "甲", detail="正文")) == 2
    assert cross_map._body_len(Node(id="a", label="甲", note="备注")) == 2
    assert cross_map._body_len(node("a", "甲")) == 0
