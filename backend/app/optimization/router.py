from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.contracts.diagnostic import Diagnostic, SuggestResponse
from app.contracts.mindmap import DiagramContent, MindMapContent, SaveRequest
from app.contracts.source import Source
from app.optimization.repair import (
    AUTO_REPAIRABLE_CODES,
    RepairItem,
    RepairSkip,
    repair as repair_content,
    repair_summary,
)
from app.optimization.service import check, suggest

router = APIRouter(prefix="/optimization", tags=["逻辑优化"])

# 体检只支持树型。**这条闸在路由层，不在 `check()` 里**：`check()` 与
# `suggest()` 的判据是按树的形状写的（"只有一个子节点""分支深度对比"），
# 鱼骨的六根骨是并列类别、流程的阶段是独立的命名空间，那些判据搬过去会
# 变成噪声而不是错误 —— 所以不是"泛化一下"，是"这里不适用"。
#
# 失败形态值得记：在此之前传鱼骨/流程会 `AttributeError: ... has no attribute
# 'root'`（`service.py` 里 `_iter_nodes(content.root)`），也就是 **500**。前端
# `stores/structure.ts` 已经在发请求前拦住了，所以这个雷只在接口层可见 ——
# 换一个客户端（curl、将来的移动端）就会踩。422 比 500 诚实：`500` 说的是
# "服务器坏了"，而事实是"这个输入不在这个接口的服务范围里"。
_TREE_ONLY = "结构体检只支持树型导图（鱼骨与流程的判据不同，暂不支持）"


def _tree_content(content: DiagramContent) -> MindMapContent:
    if not isinstance(content, MindMapContent):
        raise HTTPException(status_code=422, detail=_TREE_ONLY)
    return content


class CheckResponse(BaseModel):
    diagnostics: list[Diagnostic]


class CheckRequest(SaveRequest):
    sources: list[Source] | None = Field(default=None, max_length=50)
    # 当前这张图在库里的 id。**只被跨图参照用**：历史库要从参照物里排除"这张图
    # 自己"，否则同一张图保存两次之后，每个节点都能在"历史"里找到一模一样的一条，
    # 建议会变成一堆"另一张图写了 140 字"（那张图就是它上一版）。
    #
    # 可缺省：新建未保存的图没有 id，前端也就没有东西可传；那时天然不会命中自己。
    map_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def unique_sources(self) -> "CheckRequest":
        if self.sources is not None and len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("核验来源 ID 不能重复")
        return self


@router.post("/check", response_model=CheckResponse)
def check_map(body: CheckRequest):
    """只读诊断；可传 sources，按原文核验节点引用。"""
    return CheckResponse(diagnostics=check(_tree_content(body.content), body.sources))


@router.post("/suggest", response_model=SuggestResponse)
def suggest_for_map(body: CheckRequest):
    """给出候选修改列表。用户确认后才能应用（当前无自动应用接口）。"""
    content = _tree_content(body.content)
    return SuggestResponse(suggestions=suggest(content, map_id=body.map_id),
                           diagnostics=check(content, body.sources))


class RepairRequest(CheckRequest):
    #: 这一趟只修哪几类。缺省三条全修；给了不认识的码是 422（不静默忽略——
    #: "我选了它、它没生效"是最难查的一类问题）。
    codes: list[str] | None = Field(default=None, max_length=8)


class RepairResponse(BaseModel):
    #: 修好的树。**客户端要把整棵树换掉**，不是打补丁——修复会删节点、改标题、
    #: 搬子树，增量补丁描述不了它。
    content: MindMapContent
    #: 每一步做了什么 + 它的反操作。撤销靠 `undo`，不靠前端的撤销栈
    #: （理由见 `optimization/repair.py` 的模块注释）。
    applied: list[RepairItem]
    #: 看到了但没修的（超出契约上限、或诊断在处理途中过期）。必须报出来：
    #: 静默跳过会让"点了一键修复却没变"变成一个查不出来的状态。
    skipped: list[RepairSkip]
    #: **修完之后**重跑的 `check()`。界面可以据此说"还剩几条建议"。
    diagnostics: list[Diagnostic]
    steps: int
    #: 一句人话（无修复时是空串）。
    summary: str


# `include_in_schema=False` 是刻意的，不是偷懒：
#
# `backend/openapi.json` 与 `frontend/src/api/schema.d.ts` 这一轮**由另一条线在改**
# （账号系统）。加一条进 schema 的路由就必须重新导出那两个文件，而那正是硬约束里
# 点名不许碰的东西。与此同时 `tests/test_openapi_snapshot.py` 钉着"快照与代码一致"，
# 所以"先加路由、稍后补快照"会让全量测试变红——那不是可以留给下一个人的技术债。
#
# 于是这一步走"接口存在、快照不动"：路由能调、`openapi.json` 一个字节不变。
# 代价是这个接口暂时不进自动生成的类型，前端要手写它的类型（`api/client.ts` 里
# `repairOptimization` 上面那段）。等账号系统落地、快照可以重新导出的那一轮，
# 删掉这一行、重导快照、把前端的手写类型换成生成的类型——三步都要做，别只做第一步。
@router.post("/repair", response_model=RepairResponse, include_in_schema=False)
def repair_map(body: RepairRequest):
    """三条"修法唯一"的规则自动修到不动点（只支持树型）。

    不自动应用：这个接口只把**修好的树 + 反操作**交给调用方，落地由调用方决定
    （界面是用户点一下，脚本是它自己决定）。与 `/check` 的口径一致——诊断是事实，
    修改是动作，动作永远要有人按下扳机。
    """
    content = _tree_content(body.content)
    codes = body.codes
    if codes is not None:
        unknown = [code for code in codes if code not in AUTO_REPAIRABLE_CODES]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"不能自动修的规则：{'、'.join(unknown)}"
                       f"（可自动修的是 {'、'.join(AUTO_REPAIRABLE_CODES)}）",
            )
    result = repair_content(content, **({"codes": codes} if codes is not None else {}))
    return RepairResponse(
        content=result.content,
        applied=result.applied,
        skipped=result.skipped,
        diagnostics=result.remaining,
        steps=result.steps,
        summary=repair_summary(result),
    )
