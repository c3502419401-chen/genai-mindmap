"""账号与会话的落库：两张表、一套口令哈希、一条会话解析。

**为什么口令自己做而不是引 `passlib` / `bcrypt` / `python-jose`**：这台机器是
2C2G 单机，依赖越少越好；而 `hashlib.scrypt` 是**标准库**，实测 n=2^14 一次
36 毫秒、16 MB 内存——一个登录动作付这个代价完全合适，它同时把"离线爆破一把
弱口令"的成本抬到了不可行的量级。引第三方库换来的只是更短的代码，却要多一个
`pip install` 与一套"这个包在 3.13 上还装得上吗"的部署风险。

**为什么不用 JWT**：JWT 的价值是"多个服务共享一份无状态凭证"。这里只有一台机器、
一个进程、一个 SQLite 文件；而它换来的代价是**撤销不了**——用户点了登出、或者
发现密钥泄漏要踢掉所有人，无状态 token 只能等它自己过期。会话落表之后撤销就是
一条 `UPDATE`。「可撤销」是这次安全整改的硬要求，所以先满足它。

**会话 token 只存哈希**：库里那一列是 `sha256(token)`。库文件泄漏（备份、误传、
`MINDMAP_DB_PATH` 指错）时，拿到的是哈希，不是一串能直接用的 cookie。与
`settings.api_key` 明文落库是**不同**的取舍：那把密钥必须能被后端还原出来去
发请求，token 不需要——比较的是哈希。

**两张表的分工**：`users` 是人，`sessions` 是"这台浏览器上的一次登录"。一个人
可以在手机和电脑上各登一次，踢掉其中一个不影响另一个；这也是"会话可撤销"能
按设备生效的前提。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from app.core.errors import AppError

logger = logging.getLogger(__name__)

USER_TABLE = "users"
SESSION_TABLE = "sessions"

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` / `edit_events` / `library_*` /
#: `model_settings` 完全一致。格式不同会在"谁更晚"上错一天，仓库里已经为这件事
#: 付过一次代价（`agent/runs.py:_NOW`）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

# ---------------------------------------------------------------- 口令

#: scrypt 参数。n 是 CPU/内存成本的主旋钮（内存约 `128 * n * r` 字节 = 16 MB），
#: r 是块大小，p 是并行度。
#:
#: 取 2^14：在开发机上实测 36 毫秒/次。这个数量级是刻意的——**登录**是低频动作，
#: 36 毫秒无感；而攻击者拿一库哈希去爆破，每个候选口令都要重付 36 毫秒 + 16 MB，
#: 单机跑不到每秒 30 次。再往上调（2^16 约 150 毫秒）会让 2C2G 的机器在多人同时
#: 登录时明显变慢，收益却只是把爆破速度再降四倍。
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16

#: 哈希串的形状：`scrypt$n$r$p$<salt_hex>$<hash_hex>`。
#:
#: 参数写进串里而不是只靠模块常量，是为了**将来调参不会让老密码全部失效**：
#: 校验时按串里那组参数重算，老的 2^14 与新写的 2^15 可以并存。只存裸哈希的话，
#: 改一次 `SCRYPT_N` 就等于把所有人的密码作废——而那时没人知道为什么登不上。
_HASH_PREFIX = "scrypt"
_HASH_PARTS = 6

#: 口令长度。下限 8 是"不许用 `123456`"的最低限度；上限 200 挡住"把一段文件
#: 粘进来"（scrypt 本身对长度不敏感，但没必要为它存一兆字节）。
#:
#: **不强制复杂度**（大小写、符号）：实测那类规则只会让人写出 `Passw0rd!`，
#: 长度才是有效维度。
PASSWORD_MIN_CHARS = 8
PASSWORD_MAX_CHARS = 200

# ---------------------------------------------------------------- 账号名

#: 账号名：3–32 位字母、数字、下划线。
#:
#: 选用户名而不是邮箱（这是产品决定）：仓库里没有 SMTP，邮箱**无法验证**——
#: 任何人都能填别人的邮箱注册，于是"这个邮箱已被注册"就成了一条信息泄漏，
#: 而"找回密码"也没法真的实现。用户名不假装自己能做这两件事。
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,32}$")

#: 唯一性按**不区分大小写**判（`COLLATE NOCASE`），显示保留用户打的大小写。
#: 只按区分大小写判的话，`Alice` 与 `alice` 会是两个账号——它们看起来一样，
#: 于是"我明明注册了却说没有"和"他怎么进了我的号"都会来找你。
_NOCASE = " COLLATE NOCASE"

# ---------------------------------------------------------------- 会话

#: 会话有效期。30 天：这是"演示/自用"这类使用频率下"不用天天登录"与"丢了一台
#: 设备不会永久留一把钥匙"之间的折中。过期由查询时比较 `expires_at` 判定，
#: 不依赖定时任务——单机没有可靠的调度器。
SESSION_TTL_SECONDS = 30 * 24 * 3600

#: `last_seen_at` 的刷新间隔。**不是每次请求都写**：SQLite 是单写者，一次生成
#: 期间会有几十上百个请求（SSE 首帧、保存、偏好、健康检查），每次都 `UPDATE`
#: 会把写锁变成热点。一小时一次足够回答"这个会话最近还在用吗"。
SESSION_TOUCH_SECONDS = 3600

#: cookie 名。前缀 `mm_` 是"mindmap"的缩写，避免同域下与别的应用撞名。
SESSION_COOKIE = "mm_session"
#: 游客身份。它现在只用来"记住这台浏览器是谁"，免额度那一批会在它上面计数。
GUEST_COOKIE = "mm_guest"

#: 游客 id 的形状：32 位小写十六进制（`uuid4().hex`）。
_GUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
#: 签名的字符数（sha256 hex 的前 16 位 = 64 bit）。16 足够：伪造一个游客身份需要
#: 2^64 次尝试，而它的收益是"读另一个游客的偏好设置"。截短是为了让 cookie 短一点，
#: 不是为了安全（`read_guest` 的比对才是安全的那一半）。
GUEST_SIG_CHARS = 16

#: 过期的会话行保留一段时间再清：刚过期就删会让"为什么突然要重新登录"没有任何
#: 痕迹可查（用户来问时，库里连那条会话都不在了）。留七天。
SESSION_GC_AFTER_SECONDS = 7 * 24 * 3600

_CREATE_USERS = f"""CREATE TABLE IF NOT EXISTS {USER_TABLE} (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL{_NOCASE} UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)"""

#: `token_hash` 是主键：同一次登录只该有一行。`user_id` 上建索引是为了
#: "踢掉这个人的所有会话"（改密码时）不用全表扫。
#:
#: `revoked_at` 与"直接删行"的区别：删了就没有"这个会话曾经存在、后来被撤销"
#: 这个事实。排查"我是不是在别处被登出了"时，删行只剩"查不到"，与"从来没登录过"
#: 一样。
_CREATE_SESSIONS = f"""CREATE TABLE IF NOT EXISTS {SESSION_TABLE} (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
)"""


class AuthError(AppError):
    """账号相关的业务异常。带着业务 code 出去，界面才有话可说。"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建两张表。由 `repository.connect()` 在同一个连接上调用，幂等。

    与 `model_settings` / `library_sources` / `memory_items` 建在同一时刻的理由
    一样：读它的路径（**每一个请求的中间件**）在热路径上，多一个"发现表不存在 →
    建表"的分支，只会在最不该出错的地方多一条可能出错的路径。
    """
    conn.execute(_CREATE_USERS)
    conn.execute(_CREATE_SESSIONS)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{SESSION_TABLE}_user "
                 f"ON {SESSION_TABLE}(user_id)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{SESSION_TABLE}_expires "
                 f"ON {SESSION_TABLE}(expires_at)")


def _connect() -> sqlite3.Connection:
    """拿连接。函数内 import 是为了避免环：`repository.connect()` 要调本模块的
    `ensure_schema`，模块顶层反向 import 回去就成环。与 `settings/store.py` 同一手法。
    """
    from app.mindmaps import repository

    return repository.connect()


# ---------------------------------------------------------------- 游客签名

#: 游客 cookie 的签名密钥。**进程级常量，不是环境变量**。
#:
#: 为什么不给一个 `MINDMAP_SECRET`：多一个环境变量就多一种"忘了设 / 设错了 / 两台
#: 机器的值不一样"的故障形态，而这两种故障都表现为"所有人突然变成游客"或"游客
#: 数据全部丢失"——排查时间全花在配置上。密钥从库文件的绝对路径派生：它天然是
#: 每台机器一份、随库一起搬、不需要任何运维动作。
#:
#: 代价说清楚：**同一个人在两台机器上会拿到同一个游客 id**（如果库文件恰好同名
#: 同路径）。那不是泄漏，是"这两台机器上的游客数据会合并"——本来就只有一个库。
_SECRET_PREFIX = b"mindmap-guest-v1:"


def _guest_secret() -> bytes:
    """从库路径派生签名密钥（见 `_SECRET_PREFIX` 的注释）。"""
    from app.mindmaps import repository

    return hashlib.sha256(
        _SECRET_PREFIX + str(repository.db_path()).encode("utf-8")).digest()


def sign_guest(guest_id: str) -> str:
    """给一个游客 id 签名，返回放进 cookie 的值 `"<id>.<sig>"`。

    **为什么需要签名**：游客 id 是身份。没有签名时，任何格式合法的 32 位十六进制
    串都会被当成一个身份（审查时实测：把 `mm_guest` 改成别人的值，就能读到别人的
    未保存草稿、待处理列表、甚至发出别人的那次运行）。这与"会话 token 只存哈希"
    是同一条纪律：拿到 cookie 的人应该只有他自己那一个身份。

    `hmac.compare_digest` 比对而不是 `==`：常量时间。游客 id 是公开的一半（它在
    cookie 里、也在 `/me` 的响应里），能被比对的只有签名；而"逐字节提前返回"会让
    签名变成可爆破的。
    """
    signature = hmac.new(_guest_secret(), guest_id.encode("utf-8"),
                         hashlib.sha256).hexdigest()[:GUEST_SIG_CHARS]
    return f"{guest_id}.{signature}"


def read_guest(raw: str) -> str | None:
    """从 cookie 的原始值读出**已验证**的游客 id；形状不对或签名不对都返回 None。

    返回 None 的后果由调用方决定：`identity.resolve_identity` 会当成"既没登录、
    也没发过游客 cookie"，然后由 `GET /auth/me` 补发一个新的。也就是说**伪造的
    cookie 等价于没有 cookie**，而不是 401——游客是合法状态，一份坏 cookie 不该
    让页面打不开。
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    guest_id, _, signature = text.partition(".")
    if not signature or not _GUEST_ID_PATTERN.match(guest_id):
        return None
    expected = hmac.new(_guest_secret(), guest_id.encode("utf-8"),
                        hashlib.sha256).hexdigest()[:GUEST_SIG_CHARS]
    return guest_id if hmac.compare_digest(signature, expected) else None


# ---------------------------------------------------------------- 口令哈希

def hash_password(password: str) -> str:
    """把明文口令变成可直接落库的哈希串。

    每次都新生成盐：同一句口令在两个账号上哈希不同，于是"这两个人用了同一个
    密码"在库里看不出来（彩虹表也就不成立）。
    """
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"{_HASH_PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。返回 False 而不是抛：调用方（登录）要对外只给一句"账号或密码不对"。

    任何解析失败都返回 False 并记一条 warning，**不抛**：库里一行形状不对的
    `password_hash` 应该表现为"这个账号登不上"，而不是这个接口 500——后者会把
    "一个人坏了"变成"所有人都登不上"。

    参数从串里读（理由见 `_HASH_PREFIX` 的注释），所以老格式的哈希在校验时
    仍然按它自己那组参数重算。
    """
    parts = (stored or "").split("$")
    if len(parts) != _HASH_PARTS or parts[0] != _HASH_PREFIX:
        logger.warning("库里的口令哈希形状不对，无法校验（不看内容，只记形状）")
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        logger.warning("库里的口令哈希参数解析失败")
        return False
    # 上下限兜一道：参数来自库，而 `hashlib.scrypt` 对 n 的内存需求是 `128*n*r`
    # 字节。一行被改过的记录（n=2^30）会让这个进程尝试申请几百 GB —— 那是拒绝
    # 服务，不是校验失败。
    if not (0 < n <= 2 ** 20 and 0 < r <= 32 and 0 < p <= 16):
        logger.warning("库里的口令哈希参数超出合理范围，拒绝按它计算")
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                            dklen=len(expected) or SCRYPT_DKLEN)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------- 数据形状

@dataclass(frozen=True)
class User:
    """一个人。**不含 `password_hash`**：这个对象会一路流到路由层与请求上下文，

    带着哈希流转只会多一次被 `repr()` 进日志的机会。校验口令需要它时单独查。
    """

    id: str
    username: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Identity:
    """这个请求是谁。

    `user` 与 `guest_id` 不会同时有值；都没有就是"既没登录、也没发过游客 cookie"。
    三种状态在后面的批次里分别对应不同的额度口径，所以这里不把"游客"当成"未登录
    的匿名"合并掉——它们要分开计数。
    """

    user: User | None = None
    guest_id: str | None = None
    #: 这次请求带来的会话 token 的哈希。登出要用它删对应的那一行；不再回查库。
    session_hash: str | None = None

    @property
    def kind(self) -> str:
        if self.user is not None:
            return "user"
        if self.guest_id:
            return "guest"
        return "anonymous"


def _user(row: sqlite3.Row) -> User:
    return User(id=row["id"], username=row["username"],
                created_at=row["created_at"], updated_at=row["updated_at"])


# ---------------------------------------------------------------- 账号

def _check_username(username: str) -> str:
    text = (username or "").strip()
    if not USERNAME_PATTERN.match(text):
        raise AuthError(
            "auth_username_invalid",
            "账号名要 3 到 32 位，只能用字母、数字和下划线。",
            status=400,
            detail={"pattern": USERNAME_PATTERN.pattern},
        )
    return text


def _check_password(password: str) -> str:
    # 不 strip：口令里的空格是口令的一部分，剥掉会让"我明明粘对了"变成登不上。
    if len(password or "") < PASSWORD_MIN_CHARS:
        raise AuthError("auth_password_too_short",
                        f"密码至少 {PASSWORD_MIN_CHARS} 位。", status=400)
    if len(password) > PASSWORD_MAX_CHARS:
        raise AuthError("auth_password_too_long",
                        f"密码最多 {PASSWORD_MAX_CHARS} 位。", status=400)
    return password


def create_user(username: str, password: str) -> User:
    """注册。账号名重复抛 `auth_username_taken`（409）。

    **错误信息不区分"重复"与别的失败**这件事只对登录成立；注册必须说清是重名，
    否则用户会一直试同一个名字。这正是"用户名"而不是"邮箱"的好处：重名是公开
    信息，泄漏它是零成本的；邮箱重名会泄漏"这个邮箱在这站注册过"。
    """
    from uuid import uuid4

    name = _check_username(username)
    _check_password(password)
    user_id = str(uuid4())
    hashed = hash_password(password)
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                f"INSERT INTO {USER_TABLE} (id, username, password_hash, created_at, updated_at) "
                f"VALUES (?, ?, ?, {_NOW}, {_NOW})", (user_id, name, hashed))
    except sqlite3.IntegrityError as exc:
        # `UNIQUE ... COLLATE NOCASE` 是唯一会在这条 INSERT 上撞的约束（id 是新
        # uuid，不会撞）。但仍然只认这一种：把任何 IntegrityError 都说成"账号名
        # 被占用"，会让真正的缺陷以一句用户看得懂的话被吞掉。
        if "unique" not in str(exc).lower():
            raise
        raise AuthError("auth_username_taken", "这个账号名已经有人用了，换一个。",
                        status=409) from exc
    return get_user(user_id)  # type: ignore[return-value]


def get_user(user_id: str) -> User | None:
    with closing(_connect()) as conn:
        row = conn.execute(f"SELECT * FROM {USER_TABLE} WHERE id = ?", (user_id,)).fetchone()
    return _user(row) if row else None


def find_user(username: str) -> User | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT * FROM {USER_TABLE} WHERE username = ?{_NOCASE}",
            ((username or "").strip(),)).fetchone()
    return _user(row) if row else None


def authenticate(username: str, password: str) -> User | None:
    """账号名 + 口令 → 人。任何一项不对都返回 None。

    **失败不区分"账号不存在"与"密码错"**：区分开等于提供一个账号枚举接口——
    攻击者可以先确认哪些账号名存在，再针对性爆破。代价是用户打错账号名时看到
    的是"账号或密码不对"，要多想一步；这个代价换的是不泄漏"谁注册过"。

    账号不存在时**仍然算一次哈希**（拿一个固定的假哈希去校验）：不这么做的话，
    "账号不存在"会比"密码错"快 36 毫秒，计时差就是一个精确的账号枚举信道。
    """
    name = (username or "").strip()
    if not name or not password:
        return None
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT * FROM {USER_TABLE} WHERE username = ?{_NOCASE}", (name,)).fetchone()
    if row is None:
        # 与真校验同代价的占位（值本身无意义）。
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return _user(row)


def change_password(user_id: str, password: str) -> bool:
    """改口令，并**撤销这个人的全部会话**。

    撤销不是顺手做的：改密码最常见的动机是"我怀疑有人进过我的号"，那时还留着
    别的设备上的会话，等于没改。返回是否命中一行（False = 没有这个用户）。
    """
    _check_password(password)
    hashed = hash_password(password)
    with closing(_connect()) as conn, conn:
        result = conn.execute(
            f"UPDATE {USER_TABLE} SET password_hash = ?, updated_at = {_NOW} WHERE id = ?",
            (hashed, user_id))
    if not result.rowcount:
        return False
    revoke_all_sessions(user_id)
    return True


#: 给"账号不存在"那条路用的占位哈希。它必须是一条**格式合法**的 scrypt 串，
#: 否则 `verify_password` 会在解析阶段就返回 False，那条路的耗时就又变短了。
_DUMMY_HASH = "scrypt$16384$8$1$" + ("00" * SALT_BYTES) + "$" + ("00" * SCRYPT_DKLEN)


# ---------------------------------------------------------------- 会话

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def open_session(user_id: str) -> str:
    """新建一次登录，返回**明文** token（只在这一刻存在，之后库里只有哈希）。

    同时顺手清一次过期的老会话：单机没有定时任务，把 GC 挂在"有人登录"这个
    低频动作上是最省事且不会漏的做法——每天纪要地有人登录一次就够清干净。
    """
    token = secrets.token_urlsafe(32)
    with closing(_connect()) as conn, conn:
        _gc_sessions(conn)
        conn.execute(
            f"INSERT INTO {SESSION_TABLE} "
            f"(token_hash, user_id, created_at, last_seen_at, expires_at, revoked_at) "
            f"VALUES (?, ?, {_NOW}, {_NOW}, "
            f"strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), NULL)",
            (_hash_token(token), user_id, f"+{SESSION_TTL_SECONDS} seconds"))
    return token


def _gc_sessions(conn: sqlite3.Connection) -> None:
    """删掉过期够久的会话行。过期没多久的留着，好让"为什么又要登录"有据可查。"""
    conn.execute(
        f"DELETE FROM {SESSION_TABLE} WHERE expires_at < "
        f"strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)",
        (f"-{SESSION_GC_AFTER_SECONDS} seconds",))


def session_user(token: str) -> User | None:
    """token → 人。不存在、已撤销、已过期都返回 None。

    过期判定放在 SQL 里比"读出来再用 Python 比时间"更稳：库里那一列是
    `strftime` 出来的定宽 UTC 文本，字典序就是时间序，比较不会踩时区。
    """
    if not token:
        return None
    token_hash = _hash_token(token)
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                f"SELECT u.*, s.last_seen_at AS s_last_seen "
                f"FROM {SESSION_TABLE} s JOIN {USER_TABLE} u ON u.id = s.user_id "
                f"WHERE s.token_hash = ? AND s.revoked_at IS NULL "
                f"AND s.expires_at > {_NOW}", (token_hash,)).fetchone()
    except sqlite3.Error:
        # 会话层读不出来时**当作未登录**，不让它变成 500：身份层现在不拦任何请求，
        # 一个坏库应该表现为"这次认不出你是谁"，而不是整站不可用。
        logger.warning("读取会话失败，本次按未登录处理", exc_info=True)
        return None
    if row is None:
        return None
    _touch(token_hash, row["s_last_seen"])
    return _user(row)


def _touch(token_hash: str, last_seen: str | None) -> None:
    """按 `SESSION_TOUCH_SECONDS` 节流地刷新 `last_seen_at`（理由见常量注释）。

    节流条件写在 SQL 的 `WHERE` 里而不是"先读出来再比"：并发请求可能同时判断
    "该刷新了"，让数据库来裁决，写锁只会被争到一次。失败也不抛——刷新时间
    失败不该让一个本来正常的请求失败。
    """
    with closing(_connect()) as conn:
        try:
            conn.execute(
                f"UPDATE {SESSION_TABLE} SET last_seen_at = {_NOW} "
                f"WHERE token_hash = ? AND last_seen_at < "
                f"strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)",
                (token_hash, f"-{SESSION_TOUCH_SECONDS} seconds"))
            conn.commit()
        except sqlite3.Error:
            logger.warning("刷新会话时间失败（不影响这次请求）", exc_info=True)


def session_hash(token: str) -> str | None:
    return _hash_token(token) if token else None


def revoke_session(token: str) -> bool:
    """登出：把这一次会话标记为撤销。返回是否真的改了行（重复登出返回 False）。"""
    if not token:
        return False
    with closing(_connect()) as conn, conn:
        result = conn.execute(
            f"UPDATE {SESSION_TABLE} SET revoked_at = {_NOW} "
            f"WHERE token_hash = ? AND revoked_at IS NULL", (_hash_token(token),))
    return bool(result.rowcount)


def revoke_all_sessions(user_id: str) -> int:
    with closing(_connect()) as conn, conn:
        result = conn.execute(
            f"UPDATE {SESSION_TABLE} SET revoked_at = {_NOW} "
            f"WHERE user_id = ? AND revoked_at IS NULL", (user_id,))
    return int(result.rowcount)


def count_sessions(user_id: str) -> int:
    """这个人当前有几个活着的会话。给"账号页显示已登录设备"用，也是测试的判据。"""
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS total FROM {SESSION_TABLE} "
            f"WHERE user_id = ? AND revoked_at IS NULL AND expires_at > {_NOW}",
            (user_id,)).fetchone()
    return int(row["total"])


def count_users() -> int:
    """一共几个账号。只给测试与"第一个用户"这类判断用，不是业务指标。"""
    with closing(_connect()) as conn:
        row = conn.execute(f"SELECT COUNT(*) AS total FROM {USER_TABLE}").fetchone()
    return int(row["total"])


def first_user() -> User | None:
    """最早注册的那个人。**部署方**就是它，这不是一个额外的概念。

    为什么用"第一个注册的账号"而不是 `MINDMAP_ADMIN` 之类的环境变量：这台机器上
    第一个注册的人只可能是部署它的人（别人还没拿到地址）。一个环境变量会多一个
    "忘了设 / 设错了"的故障形态，而"谁是第一个"是库里已经有的、不会撒谎的事实。

    排序带上 `id` 兜底：`created_at` 的精度是毫秒，同一毫秒注册两个人时让结果
    稳定（否则"谁是部署方"会在两次调用之间摇摆，而那决定谁能改公共配置）。
    """
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT id, username, created_at, updated_at FROM {USER_TABLE} "
            f"ORDER BY created_at ASC, id ASC LIMIT 1").fetchone()
    return None if row is None else _user(row)


__all__ = [
    "AuthError",
    "GUEST_COOKIE",
    "Identity",
    "PASSWORD_MAX_CHARS",
    "PASSWORD_MIN_CHARS",
    "SESSION_COOKIE",
    "SESSION_TTL_SECONDS",
    "USERNAME_PATTERN",
    "User",
    "authenticate",
    "change_password",
    "count_sessions",
    "count_users",
    "create_user",
    "ensure_schema",
    "find_user",
    "first_user",
    "get_user",
    "hash_password",
    "open_session",
    "read_guest",
    "revoke_all_sessions",
    "revoke_session",
    "session_hash",
    "session_user",
    "sign_guest",
    "verify_password",
]
