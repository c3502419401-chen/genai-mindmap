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


class SuggestResponse(BaseModel):
    suggestions: list[Suggestion] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
