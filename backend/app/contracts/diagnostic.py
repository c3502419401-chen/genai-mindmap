"""诊断与建议模型（F15 / F16）。

诊断（Diagnostic）是流水线内部产物，表示"这棵树有问题"；
建议（Suggestion）是面向用户的候选修改，必须由用户确认后才应用。
"""
from __future__ import annotations

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class DiagnosticLevel(str, Enum):
    info = "info"
    warn = "warn"
    error = "error"


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: DiagnosticLevel = DiagnosticLevel.info
    code: str                      # 如 UNSOURCED_NODE / DUPLICATE_BRANCH / DEPTH_JUMP
    node_id: str | None = None
    message: str
    evidence: str | None = None    # 原文片段或相似节点 id


class SuggestionKind(str, Enum):
    """建议的**动作**名词，不是诊断码。

    用户看到的是"点一下会发生什么"，所以值域按动作分，不按诊断分：同一个
    `NEAR_DUPLICATE_LABEL` 与 `DUPLICATE_LABEL` 都是 `dedupe`，因为对用户来说
    要做的是同一件事。`expand` / `add_detail` 是 F15 那一轮补的——`fill` 说的是
    "整张图缺了哪一块"（结构级），而"这一支没展开"与"这几个节点没有说明"是
    节点级的两件事，混进 `fill` 会让按钮文案说不清要点哪里。
    """

    dedupe = "dedupe"          # 合并重复分支
    merge = "merge"            # 层级归并
    link = "link"              # 补跨节点关系
    fill = "fill"              # 补缺失环节
    drop = "drop"              # 剔除无依据节点
    expand = "expand"          # 把这一支再展开一层
    add_detail = "add_detail"  # 给节点补说明
    # 依据来自**别的图**：同类图里这个节点被展开了 / 写得更详细。
    #
    # 与 `expand` / `add_detail` 分开而不是合并：动作指引可能一样，但用户在界面
    # 上要能分辨"这是从别的图学来的"——它依赖历史图库，图库空的时候这一类整类
    # 不出现，而 `expand`（同级里别的分支更深）与图库无关。混成一个值，用户点开
    # 一条建议时无法判断"这条需不需要联网"。
    cross_map = "cross_map"    # 参照历史图库里的同类节点


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: SuggestionKind
    node_id: str | None = None
    message: str
    evidence: str | None = None
    # 规则建议没有经过概率校准，None 表示尚未评估，而不是伪造可信度。
    confidence: float | None = Field(default=None, ge=0, le=1)


class LinkStatus(BaseModel):
    """`kind="link"` 这一层**这一趟到底跑没跑、跑出了什么**。

    为什么它必须出现在响应里：link 是唯一"有可能一条都不给"的建议类型——不花钱
    的规则层几乎总有三五条，而 link 要先挑目标、再逐目标调一次模型，任何一环不
    成立结果都是空列表。空列表与"这个功能没接上"在界面上长得一模一样，所以把
    原因单独带出来：`status` 说清是哪一环停的，`targets` 说清问的是哪几个节点。

    值域（`no_targets` / `disabled` / `no_choices` / `model_failed` /
    `no_candidates` / `already_best` / `ok`）与 `service.suggest_link` 的判定一一
    对应，新增值时必须同时更新那句话，否则用户会看到"原因：unknown"。
    """

    model_config = ConfigDict(extra="forbid")
    #: 这一趟的结论。`ok` = 至少产出一条；`already_best` = 问过，模型认为现在挂得对。
    status: str = "ok"
    #: 被问过"该挂哪儿"的节点 id，按实际调用顺序。
    targets: list[str] = Field(default_factory=list)
    #: 真的发出去了几次模型调用。0 说明这一层根本没花钱。
    calls: int = Field(default=0, ge=0)
    #: 模型说"现在这样最好"因此**没有**产出建议的条数。它是"图上没问题"的证据，
    #: 不是"算法没结果"。
    dropped: int = Field(default=0, ge=0)
    #: 一句给用户看的话（空串表示不必说）。界面在建议列表底下显示它。
    detail: str = ""


class SuggestResponse(BaseModel):
    suggestions: list[Suggestion] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    #: link 那一层的结果。`None` 表示这次请求没跑它（`link=false`），与"跑了但
    #: 一条都没给"（`status="already_best"` / `"no_targets"`）不是一回事。
    link: LinkStatus | None = None
