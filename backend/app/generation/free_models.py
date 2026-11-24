"""免费的模型档：动态发现，不写死名字。

Command Code 的 coding plan 里，标 `:free` 的那些模型在活动期间不收费，但**这份
名单不是常驻的**——平台会新上架更多免费模型，也会把已有的下架。所以这里一个模型名
都不硬编码：每次刷新都去 `GET {base}/models` 现问，筛出当前带 `:free` 的那些。

三条实测约束决定了这个模块的形状（`docs/FREE-MODELS.md` 有完整数据）：

1. **列表要 20 秒以上**（本机两次实测 22.1s / 21.7s）。它绝不能出现在请求路径上：
   一次生成有 5–10 次模型调用，每次都等 22 秒等于把功能做成不可用。所以取列表是
   **后台线程 + TTL 缓存**，过期的旧列表继续用，刷新成功了再换。
2. **`:free` 后缀是唯一的免费标识**，`owned_by`、`name`、`context_length` 都不含这个
   信息。而且 id 必须带 `owner/` 前缀（`recursive/flint-v1.0:free`），文档里的短名
   `flint-v1.0:free` 直接 400 `unsupported_model`——所以只能原样用列表里的 id。
3. **上游会偶发 429**（"Upstream model provider is temporarily unavailable"），同一
   时刻同一 key 连发三次也可能全 200。所以选取要能给出**候选序列**，让调用方在一次
   运行内逐个试，而不是撞上一个就整次失败。

**降级要留下痕迹**（AGENTS.md 那条）：拉不到列表时保留旧列表并记 warning，而不是
静默返回空——"没有免费档可用"和"这次没拉到列表"是两件事，前者用户该看到提示，
后者该让我们在日志里发现。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

# 免费模型的唯一标识。**用 `endswith` 而不是 `in`**：模型名里带 `:free` 中段的那些
# 不存在，但用 `in` 判断会顺手把将来可能出现的 `x:free-trial` 之类当免费，而那类
# 名字恰恰可能是要计费的档位。
FREE_SUFFIX = ":free"

# 总开关。默认关，因为打开它就等于改变"这一次生成花谁的钱"：已有的部署把密钥放在
# 环境变量里、跑的是付费模型，升级之后如果默认改走免费档，行为会静默变成又慢又受
# 每日次数限制。**关掉时必须与上一个提交逐字节一致**（AGENTS.md）。
ENABLED_ENV = "MINDMAP_FREE_TIER"

# 列表的存活时间。取 1 小时是因为上下架不是分钟级事件，而每次刷新要占上游 20 秒；
# 比这更短只会增加上游压力，更长则让新上架的免费模型一整天用不上。
TTL_SECONDS = 60 * 60.0

# 拉列表的超时。它比实测的 22 秒留出余量，但不能无限等：刷新线程挂着不结束，
# 下一轮就永远不会开始，缓存会停在一个更旧的时间点上。
TIMEOUT_SECONDS = 45.0

# 只看这两个字段，别的字段即使上游加了也不影响我们。
_MODELS_PATH = "/models"

# 进程内缓存。按 base_url 分：不同的人可能配了不同的 Command Code 端点，
# 而"有哪些免费模型"是那个端点的事实，不是全局事实。
#   base → (取到列表的时刻, 免费模型 id 列表[已排序])
_CACHE: dict[str, tuple[float, list[str]]] = {}
# 正在刷新的 base。防重入：一小时内只会有一个线程去问上游，其余的立刻拿到旧列表。
_REFRESHING: set[str] = set()
_LOCK = threading.Lock()


def enabled() -> bool:
    """免费档是否被打开。**每次现读环境变量**，不在 import 时定死：部署方改完
    环境变量重启进程才生效是对的，但测试要能 monkeypatch 它。"""
    return os.getenv(ENABLED_ENV, "").strip() == "1"


def reset() -> None:
    """清空缓存。只给用例与"手工强制刷新"用，正常路径不该调它。"""
    with _LOCK:
        _CACHE.clear()
        _REFRESHING.clear()


def _sort_key(model: dict[str, Any]) -> tuple:
    """"最该先用哪个"的排序键：**上下文大的优先，同大按 id 升序**。

    上下文优先是因为免费档的常见用途是"让没有密钥的人也能看到一张完整的图"，
    上下文越大越不容易在中途撞上窗口上限（那会变成一次跑到一半才失败的生成）。
    id 升序是**确定性**：上游返回顺序在两次实测里一致，但换个上游版本、
    或者两个模型同时上架的顺序都不该影响"今天用哪个"。没有这一条，同一天里
    前一次用 flint、后一次用 glyph，用户会觉得功能时好时坏。
    """
    length = model.get("context_length")
    size = length if isinstance(length, int) and length > 0 else 0
    return (-size, str(model.get("id") or ""))


def pick_free(data: Iterable[Any]) -> list[str]:
    """从 `/models` 的 `data` 数组里挑出当前可用的免费模型 id，**已排好序**。

    纯函数，不碰网络也不读缓存——这是本模块唯一值得单测的部分。

    两个条件都要满足：

      - id 以 `:free` 结尾；
      - `supported_endpoints` 里有 `/chat/completions`。

    第二个条件不能省：列表里有 12 个模型只支持 `/messages`（Anthropic 格式），
    如果哪天某个免费模型只挂在那条路上，选它出来会让每一次调用都 400——
    而报错文案（"模型服务返回 HTTP 400"）完全看不出该换一个模型。

    **字段缺失时按"不支持"处理**：没有 `supported_endpoints` 的条目我们无法判断，
    宁可少一个候选，也不要在运行时才发现它调不通。
    """
    chosen: list[tuple[tuple, str]] = []
    for model in data:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id.endswith(FREE_SUFFIX):
            continue
        endpoints = model.get("supported_endpoints")
        if not isinstance(endpoints, list) or "/chat/completions" not in endpoints:
            continue
        chosen.append((_sort_key(model), model_id))
    chosen.sort()
    return [model_id for _key, model_id in chosen]


def _fetch(base_url: str) -> list[str]:
    """问一次上游，返回排好序的免费模型 id。

    抛异常由调用方决定怎么处理（`_refresh` 里统一吞掉并记日志）。

    **这里必须绕过 `_config()`**：它走 `settings.store.resolve()`，而 `resolve()`
    正是本模块的调用方。用它会变成 `resolve() → free_models() → _fetch() →
    _config() → resolve()` 的无限递归，而且因为缓存机制它不会立刻炸，而是在
    "第一次需要刷新"的那一刻才栈溢出——最难查的那种。

    所以取地址与密钥走 `store.free_tier_endpoint()`：它只回答"部署方那台
    Command Code 端点和它的 key 是什么"，不做任何免费档替换。

    **带鉴权**：现在 `/models` 不要 key 也返回同一份列表（实测无 token 与带 token
    的 105 个 id 完全一致），但带上没有代价，而且将来它要是变成需要权限的接口，
    不带就会静默退化成"永远没有免费档"——那是最难查的一种坏法。
    """
    from app.settings import store as settings_store

    base, key = settings_store.free_tier_endpoint()
    base = base or base_url
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = httpx.get(
        base.rstrip("/") + _MODELS_PATH,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("模型列表的形状不对：没有 data 数组")
    return pick_free(data)


def _refresh(base_url: str) -> None:
    """刷新一个 base 的免费模型列表。**永不抛异常**——它在后台线程里跑，
    抛出去只会变成一条没人接的线程异常栈。"""
    try:
        models = _fetch(base_url)
    except Exception as exc:  # noqa: BLE001 — 后台线程，任何异常都只能记下来
        with _LOCK:
            stale = _CACHE.get(base_url)
            # **失败也要放掉这个标记**。只放成功那一侧的话，一次失败会让这个 base
            # 一小时内再也不会重试（`_start_refresh` 看到它在集合里就直接返回），
            # 表现是"免费档时好时坏、重启之后又好了"——最像玄学的那种。
            _REFRESHING.discard(base_url)
        if stale is None:
            logger.warning(
                "拉不到模型列表，暂按没有免费档处理：%s: %s", type(exc).__name__, exc)
        else:
            age = int((time.monotonic() - stale[0]) / 60)
            logger.warning(
                "刷新模型列表失败，继续用 %d 分钟前的列表（%d 个免费模型）：%s: %s",
                age, len(stale[1]), type(exc).__name__, exc)
        return
    with _LOCK:
        _CACHE[base_url] = (time.monotonic(), models)
        _REFRESHING.discard(base_url)
    if models:
        logger.info("免费模型列表已刷新：%s", ", ".join(models))
    else:
        # 这一条要能看见：免费活动结束之后，界面上"用免费档生成"会一直失败，
        # 而日志里没有任何"当前 0 个免费模型"的记录会让排查从"查日志"变成"猜"。
        logger.warning("模型列表里当前没有任何带 %s 的可用模型", FREE_SUFFIX)


def _stale(base_url: str) -> bool:
    with _LOCK:
        entry = _CACHE.get(base_url)
    if entry is None:
        return True
    return (time.monotonic() - entry[0]) > TTL_SECONDS


def _start_refresh(base_url: str) -> None:
    """在后台线程里刷新，**本调用立即返回**。已经有了就直接跳过。"""
    with _LOCK:
        if base_url in _REFRESHING:
            return
        _REFRESHING.add(base_url)
    threading.Thread(
        target=_refresh, args=(base_url,),
        name="free-models-refresh", daemon=True,
    ).start()


def free_models(base_url: str) -> list[str]:
    """当前已知的免费模型，**按优先级排好序**。空列表 = 此刻没有免费档可用。

    语义是"现在能拿到的最好答案"：

      - 缓存新鲜 → 直接返回；
      - 缓存过期 → 起一个后台线程刷新，**这一秒仍然返回旧列表**；
      - 一次都没拉到过 → 返回空列表，同时起后台线程。

    第一次调用返回空是**有意**的：为了第一次请求能立刻出结果，宁可这一次报
    "免费档还没准备好"（用户点一下重试就好，后台已经拉好了），也不要让每个
    第一次访问的人都盯着"正在连接"等 22 秒。

    空列表与"拉取失败"在调用方看来是同一件事，这是可接受的：两者的正确反应都是
    "这次别用免费档"。
    """
    if not enabled():
        return []
    base = (base_url or "").rstrip("/")
    if not base:
        return []
    if _stale(base):
        _start_refresh(base)
    with _LOCK:
        entry = _CACHE.get(base)
    return list(entry[1]) if entry else []


def candidates(base_url: str, tried: Iterable[str] = ()) -> list[str]:
    """本次还能试的免费模型：全部候选里去掉已经试过的那些。

    调用方在一次运行内逐个试（上游 429 时换下一个），所以"试过哪个"必须由调用方
    记着——本模块不持有"这次运行"的状态：那是每请求的东西，放模块级会让并发请求
    互相污染（A 试过 flint，B 就永远拿不到 flint）。
    """
    done = {item for item in tried}
    return [model for model in free_models(base_url) if model not in done]


def current(base_url: str) -> str | None:
    """首选的那个免费模型，没有就是 None。"""
    found = free_models(base_url)
    return found[0] if found else None


__all__ = [
    "ENABLED_ENV",
    "FREE_SUFFIX",
    "TIMEOUT_SECONDS",
    "TTL_SECONDS",
    "candidates",
    "current",
    "enabled",
    "free_models",
    "pick_free",
    "reset",
]
