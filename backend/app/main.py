import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.router import router as agent_router
from app.auth.gate import GateMiddleware
from app.auth.identity import IdentityMiddleware
from app.auth.router import router as auth_router
from app.core.errors import register_error_handlers
from app.export.router import router as export_router
from app.importing.router import router as import_router
from app.ingestion.router import router as ingestion_router
from app.knowledge.router import router as knowledge_router
from app.memory.router import router as memory_router
from app.mindmaps.router import router as mindmap_router
from app.optimization.router import router as optimization_router
from app.prefs.router import router as prefs_router
from app.settings.router import router as settings_router

def _app_version() -> str:
    """线上版本号。

    由 `deploy/install.sh` 从被打包的 tag 写进 `MINDMAP_VERSION`（systemd 的
    EnvironmentFile），/api/health 原样带出，页面标题旁显示同一个值——这样
    "域名上跑的是不是刚部署的那份"不用登服务器就能判断。

    本机跑源码时没有这个变量，回落 `dev`：写死一个版本号只会让人以为本地也
    是发布版。
    """
    return os.getenv("MINDMAP_VERSION", "").strip() or "dev"


app = FastAPI(title="生成式 AI 思维导图", version=_app_version())
# 所有错误出口统一成 {code, message, detail, request_id}，见 app/core/errors.py
register_error_handlers(app)
# 每个请求解析一次"当前是谁"，放进 ContextVar（见 app/auth/identity.py）。
#
# **这一层不拒绝任何请求**：身份有三种（登录用户 / 游客 / 都没有），但没有一条
# 路径因为身份而 401。门禁按接口逐个打开，理由见 app/auth/__init__.py。
#
# 顺序在 CORS 之后、路由之前：它要包住所有路由，又不需要包住 CORS 的错误处理。
# 门禁（`app/auth/gate.py`）：业务接口要求"有身份"，匿名请求在这一层回 401。
#
# **注册顺序是这里唯一要紧的事，而且它与直觉相反**：Starlette 的 `add_middleware`
# 是 `insert(0)`，所以**后注册的包在外层**。要的嵌套是
# `CORS → 身份 → 门禁 → 路由`，因此必须**先注册门禁、再注册身份、最后 CORS**。
#
# 反过来（先身份后门禁）的表现是：门禁跑在身份解析之前，`current_identity()` 恒为
# 默认的匿名 → **整站 401**，包括带游客 cookie 与已登录的请求。那种故障看起来像
# 部署坏了或者 cookie 没生效，而真正的原因只是两行 add_middleware 的先后。
app.add_middleware(GateMiddleware)
# 每个请求解析一次"当前是谁"，放进 ContextVar（见 app/auth/identity.py）。
app.add_middleware(IdentityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    # 偏好面板要能删（`DELETE /api/prefs/{key}`）。只列出 GET/POST/PUT 时，浏览器
    # 的预检会把 DELETE 挡在门外，而错误在界面上表现为一句没有信息量的
    # "Failed to fetch"——那正是"删不掉"最难查的一种形状。
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    # Cookie 会话（`app/auth/`）要求这一位：不带它的话浏览器会拒收/拒发
    # `Set-Cookie`，表现为"登录返回 200 但下一次请求仍然是未登录"，而网络面板里
    # 看不出任何异常——它只是不生效。
    allow_credentials=True,
    allow_headers=["Content-Type"],
)

# 账号与会话：注册 / 登录 / 登出 / 问"我是谁"。这一批只做身份层，**不拦任何请求**
# （见 `app/auth/__init__.py`：门禁按接口逐个打开，避免合并当天所有人都进不去）。
app.include_router(auth_router, prefix="/api")

# 已实现的完整闭环
app.include_router(mindmap_router, prefix="/api")      # 保存 / 读取 / 版本
app.include_router(import_router, prefix="/api")        # 导入已有导图（只解析，不落库）
app.include_router(optimization_router, prefix="/api")  # 结构诊断与建议
app.include_router(export_router, prefix="/api")       # 导出 puml
app.include_router(ingestion_router, prefix="/api")    # 多模态输入
app.include_router(agent_router, prefix="/api")        # AI 代理：文本/多来源 → 导图
app.include_router(prefs_router, prefix="/api")         # 用户偏好：面板读 / 改 / 删
# 跨会话记忆：列 / 手工加 / 删 / 按主题检索。手工入口不是给自动化用的，它存在的
# 理由是让"检索与衰减对不对"能在空库上立刻被验证一次（见 `memory/router.py` 的
# docstring：自动总结在空库上会合法地产出 0 条，那段时间里什么都证明不了）。
app.include_router(memory_router, prefix="/api")

# 已规划、接口已固定、实现待补
app.include_router(knowledge_router, prefix="/api")    # 知识检索（返回空）
# 模型服务配置：面板读（密钥只有打码值）/ 存 / 测一次连接。改完**立即生效**——
# 三个模型出口每次都重新解析（见 `app/settings/store.py` 的回落顺序），没有进程内
# 缓存，所以不需要重启，也就不会打断正在跑的长图。
app.include_router(settings_router, prefix="/api")

# 生成只留 agent 这一条出口。旧的 `POST /api/generations`（同步、单段文本）与
# `POST /api/pipeline/generate`（多来源、返回阶段轨迹）已删除：前端默认走 agent，
# 它们独有的来源重建、引用核验与结构诊断都已搬进 `agent/tools.py` 的生成收尾，
# 阶段轨迹由 agent 的 `stage` 事件覆盖。留两条平行出口只会让同一件事有两处真相。


@app.get("/api/health", tags=["基础服务"])
def health():
    return {"status": "ok", "version": app.version}
