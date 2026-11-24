"""数据归属的 owner 字符串：谁的数据算谁的。

**一张表一个 `owner` 列**，取值是 `settings.store.owner_of()` 那套口径
（`user:<id>` / `guest:<id>` / 空串＝无主）。这里只做两件薄事：把"当前请求是
谁"翻译成 owner，以及判断"当前这个人是不是部署方"。

**为什么单独一个模块**，而不是让 `prefs` / `memory` 各自去读 `ContextVar`：

  1. `app.auth.identity` 与 `app.settings.store` 互相 import（`settings.store`
     要 `current_identity()`，`identity` 要 `store.Identity`），别的模块直接
     `from app.settings.store import owner_of` 会让这条环更容易被踩到。这里的
     import 全放在函数内，`sys.modules` 缓存命中之后零开销。
  2. **后台线程读不到 `ContextVar`**。记忆总结跑在 `threading.Thread` 里，那里
     `current_identity()` 是缺省的匿名——所以"总结出谁的记忆"这件事必须由
     **发起请求的那一侧**显式传下去（见 `memory.summarize.maybe_summarize`）。
     把这条纪律写在模块 docstring 里，比写在某个调用点上更容易被后来者看到。
"""
from __future__ import annotations

#: 无主的 owner。与 `settings.store.INSTANCE_OWNER` 是同一个值，这里重复一次是
#: 为了让本模块不 import settings（见上面第 1 条）。加了断言钉住两者相等
#: （`test_isolation.py`），所以不是两个真相。
NO_OWNER = ""


def current_owner() -> str:
    """当前请求的 owner；既没登录也没游客 cookie 时是空串。

    只在**有请求上下文**的地方调它。线程里、后台任务里会拿到空串——那不是 bug，
    是这个值本身没有上下文可读，调用点必须自己把 owner 传进去。
    """
    from app.auth.identity import current_identity
    from app.settings.store import owner_of

    return owner_of(current_identity())


def is_deployer() -> bool:
    """当前这个人是不是部署方（这台机器上第一个注册的账号）。

    与 `settings.router._deployer` / `mindmaps.router._deployer` 同一个判据，
    只是这里是"给库层用的"版本：迁移与"无主数据归谁"要在不是路由的地方问这句话。
    匿名与游客都不是部署方——部署方一定是登录的人。
    """
    from app.auth import store

    identity_user = _current_user_id()
    if identity_user is None:
        return False
    first = store.first_user()
    return first is not None and first.id == identity_user


def _current_user_id() -> str | None:
    from app.auth.identity import current_user

    user = current_user()
    return None if user is None else user.id


__all__ = ["NO_OWNER", "current_owner", "is_deployer"]
