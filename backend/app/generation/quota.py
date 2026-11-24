"""公共免费额度的记账：同一台机器（按 IP）每天能免费用几次。

**它管的是什么**：部署方在 `MINDMAP_FREE_TIER_API_KEY` 里放一把"只给免费模型用"的
公共密钥，任何人没有自己的密钥时都能借它生成——但每天有次数上限。超出之后要么明
天再来，要么去「模型设置」里填自己的密钥。

**为什么按 IP 而不是按账号或 cookie**：这两条都是白送的重置手段。清 cookie 就换一个
游客身份，注册一串账号就换一批额度，两种都只需要点几下。按 IP 至少要求换网络出口；
代价是同一个 NAT 后面的人共享这份额度——校园网出口就是这种情况，这个代价是已知并
接受的（用户 m17141 选的"只按 IP"）。

**为什么额度存在 SQLite 而不是内存**：`--workers 1` 的前提下内存也对，但重启就没
了——而"重启一下额度就回来了"正是这类机制最容易被发现的那个洞。这张表和 `runs`
一样落在同一个库里。

**为什么降级方向是放开**：读不出表就当作还有额度。与 `auth/throttle.py` 同一个理由，
反过来会把所有人关在门外，而攻击者没法让表坏掉——他得先拿到写锁。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)

TABLE = "free_quota"

#: 公共密钥的环境变量名。**没有它就等于没开这项功能**：一个自建单机部署不需要
#: "每天 3 次"这种限制，那是给"对外提供免费试用"的部署用的。
PUBLIC_KEY_ENV = "MINDMAP_FREE_TIER_API_KEY"

#: 每人每天的免费次数。写成环境变量是因为这个数是产品口径，不是代码常量：
#: 演示期想放宽到 10 次、上线后想收紧到 1 次，都不该改代码。
LIMIT_ENV = "MINDMAP_FREE_QUOTA_LIMIT"
DEFAULT_LIMIT = 3

#: 上限的合理区间。高于这个数就不像"试用"了，低于 1 等于关掉。
_MAX_LIMIT = 100

_MAX_IP_KEY = 64

_CREATE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    bucket  TEXT NOT NULL,
    day     TEXT NOT NULL,
    used    INTEGER NOT NULL,
    last_at REAL NOT NULL,
    PRIMARY KEY (bucket, day)
)
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表。由 `repository.connect()` 在同一条连接上调用，幂等。"""
    conn.execute(_CREATE)


def _connect() -> sqlite3.Connection:
    """拿连接。函数内 import 避免环，与 `auth/store.py` 同一手法。"""
    from app.mindmaps import repository

    return repository.connect()


def public_key() -> str:
    """公共密钥。空串 = 这项功能没开。"""
    return (os.getenv(PUBLIC_KEY_ENV) or "").strip()


def enabled() -> bool:
    """有没有配公共密钥。**这是整项功能的开关**，不是另一个环境变量。

    理由：同一个环境变量既是"用哪把钥匙"也是"要不要限量"，两者不可能不一致。
    再加一个 `MINDMAP_FREE_QUOTA=1` 只会多出一种"配了钥匙但忘了开开关"的状态，
    而它表现出来就是"免费额度不存在"——用户看不出区别。
    """
    return bool(public_key())


def limit() -> int:
    """每天几次。环境变量写错（非数字、越界）就退回默认值并记 warning。"""
    raw = (os.getenv(LIMIT_ENV) or "").strip()
    if not raw:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认的 %d 次处理", LIMIT_ENV, raw, DEFAULT_LIMIT)
        return DEFAULT_LIMIT
    if value < 1 or value > _MAX_LIMIT:
        logger.warning(
            "%s=%d 超出 1–%d，按默认的 %d 次处理", LIMIT_ENV, value, _MAX_LIMIT, DEFAULT_LIMIT)
        return DEFAULT_LIMIT
    return value


def day_key(now: float | None = None) -> str:
    """配额归到哪一天。用**服务器本地时间**的日期。

    `time.localtime` 而不是 UTC：这台机器上的人看到的是本地零点重置。ECS 默认是
    UTC，部署时该把 `TZ=Asia/Shanghai` 写进 systemd 单元——否则"明天"会从北京时间
    早上八点开始，而界面上写的是"明天恢复"，两者对不上。
    """
    tm = time.localtime(time.time() if now is None else now)
    return f"{tm.tm_year:04d}-{tm.tm_mon:02d}-{tm.tm_mday:02d}"


def bucket(ip: str) -> str:
    """配额的桶键。空地址统一成 `?`——那仍是一个桶，不是"不限"。"""
    return (ip or "").strip()[:_MAX_IP_KEY] or "?"


def seconds_until_tomorrow(now: float | None = None) -> int:
    """距离本地零点还有几秒。给 `Retry-After` 用。"""
    current = time.time() if now is None else now
    tm = time.localtime(current)
    midnight = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))
    return max(1, int(midnight + 24 * 3600 - current))


def _used(conn: sqlite3.Connection, key: str, day: str) -> int:
    row = conn.execute(
        f"SELECT used FROM {TABLE} WHERE bucket = ? AND day = ?", (key, day)
    ).fetchone()
    return int(row["used"]) if row is not None else 0


def remaining(ip: str) -> int:
    """这台机器今天还剩几次。读不出来时返回满额（降级方向是放开）。"""
    key, day = bucket(ip), day_key()
    try:
        conn = _connect()
        try:
            return max(0, limit() - _used(conn, key, day))
        finally:
            conn.close()
    except sqlite3.Error as error:
        logger.error("免费额度表读不出来，本次按不限量处理：%s", error)
        return limit()


def consume(ip: str) -> bool:
    """占一格。占到返回 True；用完了返回 False。

    **必须在开流之前调**，而不是生成成功之后：先发后记的话，两个并发请求会同时
    看到"还剩 1 次"然后都放行——额度是给"发得出去"设的闸，闸必须在门口。
    代价是失败的那次可能白扣一格，所以 `refund()` 是这套设计的一部分，不是补丁。

    `BEGIN IMMEDIATE`：读-判-写三步要原子。用默认的延迟事务时，两个进程/线程可以
    都读到旧值再各自 +1（与 `runs.begin()` 同一个理由）。
    """
    key, day = bucket(ip), day_key()
    ceiling = limit()
    try:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 顺手清掉旧日期的行：表只按天用，留着只是一直变大。放在这里是因为
            # 它是唯一一处"一定会写"的地方（与 `throttle` 把清理留给写入同一手法）。
            conn.execute(f"DELETE FROM {TABLE} WHERE day <> ?", (day,))
            used = _used(conn, key, day)
            if used >= ceiling:
                conn.rollback()
                return False
            conn.execute(
                f"INSERT INTO {TABLE} (bucket, day, used, last_at) VALUES (?, ?, 1, ?) "
                f"ON CONFLICT(bucket, day) DO UPDATE SET used = used + 1, last_at = excluded.last_at",
                (key, day, time.time()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error as error:
        # 见模块 docstring：读不出来就放开。这里**不**吞 `OperationalError` 之外
        # 的东西——那会是代码 bug，让它冒出来。
        logger.error("免费额度表写不进去，本次按不限量处理：%s", error)
        return True


def refund(ip: str) -> None:
    """把一格还回去。这次运行没交出任何结果时用——用户什么都没拿到，不该扣。"""
    key, day = bucket(ip), day_key()
    try:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE {TABLE} SET used = used - 1 WHERE bucket = ? AND day = ? AND used > 0",
                (key, day),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as error:
        # 还不回去只是多扣一次，比抛出去让一条已经结束的流变成 500 好。
        logger.warning("免费额度没能退回（%s），这次会多扣一格", error)


def clear_all() -> int:
    """清空整张表，返回删掉的行数。只给用例与运维手动重置用。"""
    try:
        conn = _connect()
        try:
            deleted = conn.execute(f"DELETE FROM {TABLE}").rowcount
            conn.commit()
            return int(deleted)
        finally:
            conn.close()
    except sqlite3.Error as error:
        logger.error("免费额度表清不掉：%s", error)
        return 0


__all__ = [
    "DEFAULT_LIMIT",
    "LIMIT_ENV",
    "PUBLIC_KEY_ENV",
    "bucket",
    "clear_all",
    "consume",
    "day_key",
    "enabled",
    "ensure_schema",
    "limit",
    "public_key",
    "refund",
    "remaining",
    "seconds_until_tomorrow",
]
