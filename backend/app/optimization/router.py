from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.contracts.diagnostic import Diagnostic, SuggestResponse
from app.contracts.mindmap import DiagramContent, MindMapContent, SaveRequest
from app.contracts.source import Source
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
