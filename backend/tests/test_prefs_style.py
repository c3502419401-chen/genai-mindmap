"""风格画像的**第一层**（`app/prefs/style.py`）：8 个结构参数怎么数、怎么算置信度。

这一层是纯函数 + 一次本地查询，所以用例全部离线：造若干 `DiagramContent`、
数形状、断言中位数 / 折叠 / 置信度 / 提示词那一段的措辞。

三条最容易被改坏的性质，每条都有一个专门的用例：

  1. **中位数而不是均值**：一次只出 2 支不该把画像拉到 2；
  2. **重复形状折叠**：同一形状出现 18 次不是 18 个独立观察（本机真实情况）；
  3. **样本 < 5 不启用**：2–3 个样本算出来的中位数等于其中某一个样本本身。
"""
from __future__ import annotations

import json

import pytest

from app.agent import shapes
from app.contracts.mindmap import Node
from app.prefs import style


def _leaf(node_id: str, label: str, *, detail: str | None = None,
          icon: str | None = None, color: str | None = None) -> Node:
    return Node(id=node_id, label=label, detail=detail, icon=icon, color=color)


def _tree(branches: int, depth: int, *, detail: str | None = "说明",
          label: str = "标题", icons: bool = False,
          colors: tuple[str, ...] | None = None) -> dict:
    """造一棵 `branches` 支、`depth` 层（含根）的树，用来喂 `observe`。"""
    def build(prefix: str, level: int) -> list[dict]:
        if level >= depth:
            return []
        return [{
            "id": f"{prefix}_{index}",
            "label": f"{label}{index}" if not label.endswith("器") else label,
            "detail": detail,
            "icon": "🔍" if icons else None,
            "color": (colors[index % len(colors)] if colors else None),
            "children": build(f"{prefix}_{index}", level + 1),
        } for index in range(branches)]

    return {"schema_version": 2, "title": "主题",
            "root": {"id": "r", "label": "主题", "children": build("b", 1)}}


def _content(payload: dict):
    from pydantic import TypeAdapter

    return TypeAdapter(shapes.DiagramContent).validate_python(payload)


def _obs(payload: dict, run_id: str = "r1") -> style.Observation:
    return style.observe(run_id, _content(payload))


def _distinct(branches: int, index: int, *, depth: int = 2, **kwargs) -> dict:
    """样本形状**各不相同**的一棵树。

    `summarize` 会把同形状的重复折叠成两票（`REPEAT_CAP`），所以"多来几个样本"
    的用例必须让每个样本的指纹不同，否则它们会先被折掉，断言看到的是空字典。
    改 detail 长度是最省事的一条：指纹里取的是 `detail_chars // 10`。
    """
    return _tree(branches=branches, depth=depth,
                 detail="说" * (10 + index * 10), **kwargs)


# ---------------------------------------------------------------- 数一次运行


def test_observe_counts_branches_and_depth():
    """一级分支数、总深度、每层宽度走的是 `shapes` 的口径（鱼骨 / 流程同构）。"""
    item = _obs(_tree(branches=3, depth=3))
    assert item.branches == 3
    assert item.depth == 3
    assert item.widths == (1, 3, 9)
    assert item.shape == "tree"


def test_observe_detail_and_label_lengths_are_per_node():
    item = _obs(_tree(branches=2, depth=2, detail="说明文字", label="标"))
    # 根 + 2 支 = 3 个节点，其中根的 label 是"主题"（2 字），两支各 2 字
    assert item.label_chars == pytest.approx((2 + 2 + 2) / 3)
    # 只有两支带 detail（根没有），每支 4 字
    assert item.detail_chars == pytest.approx(4.0)


def test_observe_without_detail_reports_zero_not_missing():
    """一张没有 detail 的图报 0.0，`summarize` 据此把它算进"说明很短"那一档。

    报 `None` 会让调用方多一个分支（"没有说明"与"说明长度 0"），而两者在画像里
    是同一件事：这个人这次没写说明。
    """
    item = _obs(_tree(branches=2, depth=2, detail=None))
    assert item.detail_chars == 0.0


def test_icon_ratio_counts_nodes_with_emoji():
    """比例的分母是**所有节点**（含根）：根没有图标，所以 3 个节点里 2 个带。"""
    payload = _tree(branches=2, depth=2, icons=True)
    assert _obs(payload).icon_ratio == pytest.approx(2 / 3)
    assert _obs(_tree(branches=2, depth=2, icons=False)).icon_ratio == 0.0


def test_color_mode_distinguishes_by_branch_from_mixed():
    """一色一支 vs 混色：判据是"每一支内部只有一种颜色、支与支不同"。"""
    by_branch = _obs(_tree(branches=3, depth=2, colors=("red", "green", "blue")))
    assert by_branch.color_mode == "by_branch"
    # 所有支同一色 = mono（不是 by_branch：支与支没有区分）
    mono = _obs(_tree(branches=3, depth=2, colors=("red",)))
    assert mono.color_mode == "mono"
    none = _obs(_tree(branches=3, depth=2))
    assert none.color_mode == "none"


def test_color_mode_mixed_when_one_branch_has_two_colors():
    payload = _tree(branches=2, depth=3)
    payload["root"]["children"][0]["color"] = "red"
    payload["root"]["children"][0]["children"][0]["color"] = "blue"
    assert _obs(payload).color_mode == "mixed"


# ---------------------------------------------------------------- 汇总


def test_summarize_uses_median_not_mean():
    """一次只出 2 支的运行不该把画像拉到 2——这是"中位数不是均值"的那条。

    五个样本 2 / 8 / 8 / 8 / 8：均值 6.8，中位数 8。断言取的是 8。
    样本用 `_distinct` 造，避免同形状被 `REPEAT_CAP` 折掉（那是另一条用例的事）。
    """
    counts = (2, 8, 8, 8, 8)
    observations = [_obs(_distinct(count, index), f"r{index}")
                    for index, count in enumerate(counts)]
    params = style.summarize(observations)
    assert params["branches"].value == 8.0
    assert params["branches"].samples == 5


def test_summarize_returns_empty_below_min_samples():
    """样本 < `MIN_SAMPLES` 时不启用——返回空字典，而不是一堆 confidence=0。"""
    observations = [_obs(_tree(branches=3, depth=2), f"r{index}") for index in range(4)]
    assert style.summarize(observations) == {}


def test_repeated_shape_counts_at_most_two_votes():
    """同一形状出现 18 次不是 18 个独立观察（本机真实数据就是这样）。

    六个一模一样的 3 支小树 + 四个各不相同的树：折叠后 3 支只算 2 票，
    中位数因此落在后四个身上，而不是被那六个拉到 3。
    """
    repeated = [_obs(_tree(branches=3, depth=2, detail=None), f"same{index}")
                for index in range(6)]
    varied = [_obs(_tree(branches=count, depth=2), f"vary{index}")
              for index, count in enumerate((5, 6, 7, 8))]
    params = style.summarize([*repeated, *varied])
    assert params["branches"].samples == 6  # 2 + 4
    # [3, 3, 5, 6, 7, 8] 的中位数是 5.5；不折叠的话是 [3]*6 + 4 个 → 3
    assert params["branches"].value == pytest.approx(5.5)
    assert params["branches"].detail["folded_runs"] == 4
    assert params["branches"].detail["raw_samples"] == 10


def test_width_is_uneven_when_layer_sizes_vary():
    """`[1, 5, 8]` 的变异系数远超 `UNEVEN_CV`；`[1, 3, 9]` 也不均匀（几何增长）。"""
    five = style.summarize([
        _obs(_tree(branches=5, depth=2), "a"),
        _obs(_tree(branches=5, depth=3), "b"),
        _obs(_tree(branches=5, depth=2), "c"),
        _obs(_tree(branches=4, depth=2), "d"),
        _obs(_tree(branches=6, depth=2), "e"),
    ])
    assert five["width"].value == "uneven"


def test_confidence_rises_with_more_samples():
    """样本量因子：5 条与 12 条的置信度不同（12 条饱和）。"""
    def params(count: int) -> float:
        observations = [_obs(_distinct(4 + (index % 2), index, depth=3), f"r{index}")
                        for index in range(count)]
        return style.summarize(observations)["branches"].confidence

    assert params(5) < params(12)
    assert params(12) == pytest.approx(params(30))


def test_label_confidence_is_share_times_saturation():
    """分类量：置信度 = 众数占比 × 样本量饱和因子（6 条 → 0.5 的因子）。"""
    observations = [_obs(_distinct(3, index), f"r{index}") for index in range(6)]
    params = style.summarize(observations)
    # 6 条全是 tree，占比 1.0；样本量因子 6/12
    assert params["diagram_type"].confidence == pytest.approx(0.5)


def test_icons_confidence_uses_binary_share():
    """零膨胀分布：中位数很小但"稳定地没有"是高置信，不是 0。

    这条钉住的是 `_binary_confidence` 存在的理由：同一个参数交给
    `_confidence`（iqr/median）会算出 0，把"一个图标都没有"这个相当稳的结论
    当成噪声丢掉。
    """
    observations = [
        _obs(_distinct(3, index, icons=(index == 0)), f"r{index}")
        for index in range(12)
    ]
    params = style.summarize(observations)
    assert params["icons"].value == pytest.approx(0.0)
    assert params["icons"].confidence == pytest.approx(round(11 / 12, 4))


# ---------------------------------------------------------------- 进提示词


def _params(**overrides) -> dict[str, style.Param]:
    base = {
        "branches": style.Param("branches", "number", 5.0, 4.0, 7.0, 20, 0.8),
        "depth": style.Param("depth", "number", 4.0, 4.0, 4.0, 20, 0.9),
        "width": style.Param("width", "label", "uneven", None, None, 20, 0.9,
                             {"counts": {"uneven": 18, "even": 2}}),
        "detail_chars": style.Param("detail_chars", "number", 130.0, 110.0, 160.0, 20, 0.7),
        "label_chars": style.Param("label_chars", "number", 9.0, 8.0, 10.0, 20, 0.8),
        "icons": style.Param("icons", "number", 0.0, 0.0, 0.0, 20, 0.9),
        "colors": style.Param("colors", "label", "none", None, None, 20, 0.8,
                              {"counts": {"none": 18, "mixed": 2}}),
        "diagram_type": style.Param("diagram_type", "label", "tree", None, None, 20, 0.95,
                                    {"counts": {"tree": 19, "flowchart": 1}}),
    }
    base.update(overrides)
    return base


def test_prompt_block_renders_shape_then_rule():
    text = style.prompt_block(_params())
    lines = text.splitlines()
    assert lines[0] == style.STYLE_HEADER
    assert "一级分支 4–7 条" in text
    assert "总深度 4 层" in text
    assert "节点说明 110–160 字" in text
    assert "每层宽度不均" in text
    # **最后那句必须在**：没有它，模型会为凑数拆出没意义的节点。
    assert lines[-1] == style.SHAPE_RULE


def test_prompt_block_is_empty_without_params():
    """没有画像时返回空串——调用方按空串整段跳过，不留空标题。"""
    assert style.prompt_block({}) == ""


def test_prompt_block_skips_low_confidence_params():
    params = _params(depth=style.Param("depth", "number", 4.0, 4.0, 4.0, 20, 0.1))
    assert "总深度" not in style.prompt_block(params)


def test_prompt_block_skips_split_label_decisions():
    """一半一半的分类不写进提示词（`DECISIVE_SHARE`）：10 : 9 写成"基本不用颜色"
    是错的，它把一半的行为当成偏差。"""
    split = style.Param("colors", "label", "none", None, None, 21, 0.48,
                        {"counts": {"none": 10, "by_branch": 9, "mixed": 2}})
    assert "颜色" not in style.prompt_block(_params(colors=split))
    decisive = style.Param("colors", "label", "none", None, None, 21, 0.48,
                           {"counts": {"none": 18, "by_branch": 3}})
    assert "基本不用颜色" in style.prompt_block(_params(colors=decisive))


def test_prompt_block_reports_no_icons_when_ratio_is_low():
    text = style.prompt_block(_params())
    assert "不用图标" in text


# ---------------------------------------------------------------- 落库与读回


def test_refresh_clears_table_when_samples_drop(tmp_path):
    """样本不足时 `refresh` 清空表——留着旧结论会让"它明明写着这样"变成假话。"""
    params = style.refresh()
    assert params == {}
    assert style.read() == {}


def test_refresh_writes_all_eight_keys():
    for index, count in enumerate((4, 5, 6, 7, 8, 5)):
        _store_run(f"run{index}", _tree(branches=count, depth=index % 2 + 2))
    params = style.refresh()
    assert set(params) == set(style.PARAMS)
    assert set(style.read()) == set(style.PARAMS)


def test_refresh_drops_keys_no_longer_produced():
    """表里上一版实现留下的键会被删掉：`key` 不在 `PARAMS` 里的行不该活着。"""
    style.refresh()
    from app.mindmaps import repository
    from contextlib import closing

    with closing(repository.connect()) as conn, conn:
        conn.execute("INSERT INTO style_profile (key, value, samples, confidence, updated_at)"
                     " VALUES ('retired', '{}', 1, 0.1, '2026-01-01T00:00:00.000Z')")
    for index, count in enumerate((4, 5, 6, 7, 8)):
        _store_run(f"run{index}", _tree(branches=count, depth=3))
    style.refresh()
    assert "retired" not in style.read()


def test_provenance_reports_how_monotonous_the_data_is():
    """画像要自报家底：原始 / 可用 / 折叠后 / 最大单一形状占比。

    这几条不是好看的统计，它们是"画像学到的是习惯还是测试者的习惯"这个问题
    唯一能被机器回答的部分：`dominant_share` 高就说明画像主要在描述一件事。
    """
    repeated = [_store_run(f"same{index}", _tree(branches=3, depth=2, detail=None))
                for index in range(6)]
    for index, count in enumerate((5, 6, 7, 8)):
        _store_run(f"v{index}", _distinct(count, index))
    assert repeated is not None
    data = style.provenance()
    assert data["runs"] == 10 and data["usable"] == 10
    assert data["kept"] == 6            # 6 条同形状折成 2 票 + 4 条不同的
    assert data["folded"] == 4
    assert data["dominant_share"] == pytest.approx(0.6)
    assert data["repeat_cap"] == style.REPEAT_CAP


def test_describe_reports_enabled_flag():
    style.refresh()
    assert style.describe()["enabled"] is False
    for index, count in enumerate((4, 5, 6, 7, 8)):
        _store_run(f"run{index}", _tree(branches=count, depth=3))
    style.refresh()
    described = style.describe()
    assert described["enabled"] is True
    assert described["min_samples"] == style.MIN_SAMPLES
    assert set(described["params"]) == set(style.PARAMS)


def _store_run(run_id: str, payload: dict, status: str = "done") -> None:
    """往 tmp 库里塞一条 `runs` 行（`candidate` 就是产物）。"""
    from contextlib import closing

    from app.mindmaps import repository

    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, status, request, candidate, created_at,"
            " updated_at) VALUES (?, ?, '{}', ?, '2026-11-19T00:00:00.000Z',"
            " '2026-11-19T00:00:00.000Z')",
            (run_id, status, json.dumps(payload, ensure_ascii=False)))


def test_observations_skip_failed_runs():
    """只有 `done` 且 `candidate` 非空的运行参与统计。"""
    for index, count in enumerate((4, 5, 6, 7, 8)):
        _store_run(f"run{index}", _tree(branches=count, depth=3))
    # 形状故意夸张（5 支 5 层 = 781 个节点），只要它不进统计就说明筛对了
    _store_run("failed", _tree(branches=5, depth=5), status="failed")
    assert len(list(style.iter_observations())) == 5


def test_observations_skip_unparsable_candidate():
    """一条解析不了就少一个样本，不该让整次统计失败。"""
    for index, count in enumerate((4, 5, 6, 7, 8)):
        _store_run(f"run{index}", _tree(branches=count, depth=3))
    from contextlib import closing

    from app.mindmaps import repository

    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, status, request, candidate, created_at,"
            " updated_at) VALUES ('broken', 'done', '{}', '{\"root\": 1}',"
            " '2026-11-19T00:00:00.000Z', '2026-11-19T00:00:00.000Z')")
    assert len(list(style.iter_observations())) == 5


def test_style_profile_schema_is_the_agreed_one():
    """表结构与约定一致：`owner, key, value, samples, confidence, updated_at`。

    `owner` 是 PR#27 审出来的那条（P0-2）：`style_profile` 是**观察**，观察只能进
    自己的画像。它排在第一位，因为复合主键是 `(owner, key)`——同一台机器上两个人
    各自的"平均分支数"必须能同时存在。
    """
    from contextlib import closing

    from app.mindmaps import repository

    with closing(repository.connect()) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(style_profile)")]
    assert columns == ["owner", "key", "value", "samples", "confidence", "updated_at"]


def test_style_does_not_touch_user_prefs():
    """画像落在**另一张表**：不塞 `user_prefs`（显式意图 vs 系统观察）。

    这条钉的是设计决定本身——一旦有人把画像写进 `user_prefs`，
    "我明明写了它却没生效"那个老问题就会再出现一次。
    """
    from contextlib import closing

    from app.mindmaps import repository

    for index, count in enumerate((4, 5, 6, 7, 8)):
        _store_run(f"run{index}", _tree(branches=count, depth=3))
    style.refresh()
    with closing(repository.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_prefs").fetchone()[0] == 0


def test_node_style_is_not_read_as_color():
    """配色读 `Node.color`（调色板键），不读 `NodeStyle.color`（导入侧的 hex）。

    两者分工在契约层已经划过（见 `NodeStyle` 的 docstring）；画像若读错了那一个，
    得到的会是"导入过的图才有配色"，而生成出来的图永远是 `none`。
    """
    payload = _tree(branches=2, depth=2)
    payload["root"]["children"][0]["style"] = {"color": "#ff0000"}
    assert _obs(payload).color_mode == "none"
    # 两支同色 = mono；`style.color` 的 hex 始终不参与判定
    for branch in payload["root"]["children"]:
        branch["color"] = "red"
    assert _obs(payload).color_mode == "mono"
    assert _content(payload).root.children[0].style.color == "#ff0000"


def test_leaf_helper_is_a_node():
    """`_leaf` 只是为了让本文件读起来短；它必须真的是契约里的 `Node`。"""
    assert isinstance(_leaf("x", "标题"), Node)
