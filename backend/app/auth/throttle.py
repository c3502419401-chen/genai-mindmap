"""登录失败限速：同 IP / 同账号 20 次失败 + 5 秒防抖。

**为什么需要它**：`authenticate()` 已经有一道 36 毫秒的 scrypt，那让每次尝试变贵，
但挡不住"一直试"——一个每秒 20 次的脚本，20 分钟就能把常见口令表扫一遍。真正
要限制的是**速率**，而速率必须在任何哈希计算**之前**就判掉，否则限速本身也成了
一种放大器。

**两个桶，取严**：`ip:<地址>` 与 `acct:<小写账号名>`。只按账号限，攻击者换账号
就绕过（撞库本来就是"一个口令试很多账号"）；只按 IP 限，一个 NAT 出口后面的人
会互相拖累，而且移动网络换 IP 太便宜。

**判据**（`WINDOW_SECONDS` 滑动窗口内）：

- 失败数 < `MAX_FAILURES` → 放行，正常 401。
- 失败数 ≥ `MAX_FAILURES` 且距上一次失败不足 `COOLDOWN_SECONDS` → **429 +
  `Retry-After`**，不查库、不算哈希。
- 距上一次失败已超过 `COOLDOWN_SECONDS` → 放行**一次**。

合起来就是"前 20 次随便试，之后每 5 秒一次"，也就是 20 次之后按 12 次/分钟封顶。
窗口滑出（`now - first_at > WINDOW_SECONDS`）后计数视同清零，安静 15 分钟就能
重新拿到 20 次——这让"半夜忘了密码连错十几次"的第二天不会还欠着。

**成功只清账号桶，不清 IP 桶**。原始口径是"成功即清零"，这里收窄了一格：
若成功也清 IP 桶，一个持有一个有效账号的攻击者就能用它给自己刷一次"重置"，
然后继续撞别人的账号——那正好是最需要防的形状。清账号桶保住了那个口径真正想
保住的东西：本人打错几次、登录成功后不再被继续针对。IP 桶只随窗口自然衰减。

**降级方向是放开**：读/写这张表抛错时只记 `logger.error` 并按"没有记录"处理。
理由是不对称——限速表坏掉时关掉保护，和限速表坏掉时把所有人关在门外，后者
更糟；而攻击者没法让这张表坏掉（他要不惜代价先拿到写锁，那已经越过了这里）。
"""
from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

#: 表名。与 `users` / `sessions` 一样由 `repository.connect()` 在启动时建好。
TABLE = "login_attempts"

#: 窗口内允许的失败次数。
MAX_FAILURES = 20

#: 滑动窗口。取 15 分钟：一个真人打错密码的频率低于每 45 秒一次，20 次足够宽；
#: 对脚本来说，15 分钟只给 20 次自由尝试，之后就要排队。
WINDOW_SECONDS = 15 * 60

#: 超限后每次尝试之间的最小间隔。
COOLDOWN_SECONDS = 5.0

#: 时间用 epoch 秒而不是仓库别处那个 ISO 文本：这张表只做减法，不参与跨表排序
#: 也不给人看。每次判断都要算一次时间差，解析字符串只多一层出错的地方。
_CREATE = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    bucket    TEXT PRIMARY KEY,
    failures  INTEGER NOT NULL,
    first_at  REAL NOT NULL,
    last_at   REAL NOT NULL
)"""

_IP_PREFIX = "ip:"
_ACCOUNT_PREFIX = "acct:"
#: 账号名的长度上限（`USERNAME_PATTERN` 是 3–32，但这里的键来自**未校验**的请求体，
#: 先截断再落库，免得一个 1MB 的 username 变成一行巨型 key）。
_MAX_ACCOUNT_KEY = 64
#: IP 文本的长度上限。`X-Real-IP` 是我们自己的 nginx 写的，但直接连上来的人
#: 什么都能填。
_MAX_IP_KEY = 64


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表。由 `repository.connect()` 在同一条连接上调用，幂等。"""
    conn.execute(_CREATE)


def _connect() -> sqlite3.Connection:
    """拿连接。函数内 import 避免环，与 `auth/store.py` 同一手法。"""
    from app.mindmaps import repository

    return repository.connect()


def bucket_ip(address: str) -> str:
    """IP 桶的键。空地址统一成 `ip:?`——那仍然是一个桶，不是"不限"。"""
    text = (address or "").strip()[:_MAX_IP_KEY]
    return f"{_IP_PREFIX}{text or '?'}"


def bucket_account(username: str) -> str:
    """账号桶的键。小写归一，与 `users.username` 的 `COLLATE NOCASE` 对齐。"""
    text = (username or "").strip().lower()[:_MAX_ACCOUNT_KEY]
    return f"{_ACCOUNT_PREFIX}{text or '?'}"


def _read(conn: sqlite3.Connection, bucket: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT failures, first_at, last_at FROM {TABLE} WHERE bucket = ?", (bucket,)
    ).fetchone()


def _blocked(conn: sqlite3.Connection, bucket: str, now: float) -> float:
    """这个桶还要等多少秒；0 表示放行。

    窗口滑出时直接放行、**不**顺手删行：删行要写锁，而"判断"发生在每一次登录
    尝试上（含被拒的那些）。清理留给下一次 `record_failure`——它本来就要写。
    """
    row = _read(conn, bucket)
    if row is None:
        return 0.0
    if now - float(row["first_at"]) > WINDOW_SECONDS:
        return 0.0
    if int(row["failures"]) < MAX_FAILURES:
        return 0.0
    waited = now - float(row["last_at"])
    if waited >= COOLDOWN_SECONDS:
        return 0.0
    return max(0.1, COOLDOWN_SECONDS - waited)


def retry_after(*, ip: str, username: str) -> int:
    """这次尝试要等几秒；0 表示可以继续。

    返回整数秒是因为 `Retry-After` 只认整数，而界面文案也要写"N 秒"。
    """
    now = time.time()
    keys = (bucket_ip(ip), bucket_account(username))
    try:
        conn = _connect()
        try:
            worst = max(_blocked(conn, key, now) for key in keys)
        finally:
            conn.close()
    except sqlite3.Error as error:
        logger.error("登录限速表读不出来，本次按未限速处理：%s", error)
        return 0
    return int(worst + 0.999) if worst > 0 else 0


def record_failure(*, ip: str, username: str) -> None:
    """记一次失败（两个桶各加一次）。失败本身不该让登录流程出错，所以吞异常。"""
    now = time.time()
    try:
        conn = _connect()
        try:
            for key in (bucket_ip(ip), bucket_account(username)):
                _bump(conn, key, now)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as error:
        logger.error("登录失败没记进限速表，本次限速会偏松：%s", error)


def _bump(conn: sqlite3.Connection, bucket: str, now: float) -> None:
    row = _read(conn, bucket)
    if row is None or now - float(row["first_at"]) > WINDOW_SECONDS:
        failures, first_at = 1, now
    else:
        failures, first_at = int(row["failures"]) + 1, float(row["first_at"])
    conn.execute(
        f"INSERT INTO {TABLE} (bucket, failures, first_at, last_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(bucket) DO UPDATE SET failures = excluded.failures, "
        "first_at = excluded.first_at, last_at = excluded.last_at",
        (bucket, failures, first_at, now),
    )


def clear_account(username: str) -> None:
    """登录成功后清掉这个账号的失败计数（IP 桶不动，理由见模块头）。"""
    try:
        conn = _connect()
        try:
            conn.execute(f"DELETE FROM {TABLE} WHERE bucket = ?",
                         (bucket_account(username),))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as error:
        logger.error("登录成功但没清掉失败计数：%s", error)


def clear_all() -> int:
    """清空所有桶。给用例与"部署方想立刻解开某人"用，不在任何请求路径上。"""
    try:
        conn = _connect()
        try:
            cursor = conn.execute(f"DELETE FROM {TABLE}")
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()
    except sqlite3.Error as error:
        logger.error("限速表清不掉：%s", error)
        return 0


__all__ = [
    "COOLDOWN_SECONDS",
    "MAX_FAILURES",
    "TABLE",
    "WINDOW_SECONDS",
    "bucket_account",
    "bucket_ip",
    "clear_account",
    "clear_all",
    "ensure_schema",
    "record_failure",
    "retry_after",
]
