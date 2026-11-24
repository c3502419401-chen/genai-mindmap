"""genai-mindmap 容器化交付物的静态核验。

反复出现的那个问题：本机**没有 docker**，所以"镜像能不能建起来"这件事没法用
眼睛确认。这个脚本把能静态确认的部分逐条断言掉——COPY 的每个源路径真实存在、
.dockerignore 没把它排除掉、compose 的 service 名与 nginx 的 upstream 名是同一个
字符串、SSE 那几条配置一条不少、nginx 到这个阶段还没法解析的花括号是配平的。
它证明不了镜像能跑，但能保证"提交的东西内部自洽"。

只读，不改仓库里任何文件。逐条打印 PASS / FAIL，末尾汇总，有 FAIL 时退出码 1。

用法（需要 PyYAML，backend/.venv 里有）：
    python docker/verify-static.py .
    backend/.venv/Scripts/python.exe docker/verify-static.py .

这是**开发期工具**，不是运行时依赖：两个 Dockerfile 都不 COPY 它。
"""
import fnmatch
import json
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
results = []


def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  |  " + detail) if detail else ""))


def read(p):
    return (ROOT / p).read_text(encoding="utf-8")


compose_path = ROOT / "docker-compose.yml"
try:
    import yaml

    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    check("compose: YAML 可解析", True)
except Exception as exc:  # noqa: BLE001
    check("compose: YAML 可解析", False, repr(exc))
    compose = {}

# ---------------------------------------------------------------- compose 自洽
services = compose.get("services", {}) if isinstance(compose, dict) else {}
check(
    "compose: 服务集合恰为 {backend, frontend}",
    set(services) == {"backend", "frontend"},
    "实际: %s" % sorted(services),
)

be = services.get("backend", {}) or {}
fe = services.get("frontend", {}) or {}

for svc_name, svc in (("backend", be), ("frontend", fe)):
    build = svc.get("build", {}) or {}
    ctx = (ROOT / build.get("context", ".")).resolve()
    df = (ctx / build.get("dockerfile", "Dockerfile")).resolve()
    check("compose[%s]: build.context 真实存在" % svc_name, ctx.is_dir(), str(ctx))
    check("compose[%s]: build.dockerfile 真实存在" % svc_name, df.is_file(), str(df))

check(
    "compose: name 固定为 genai-mindmap（多份 compose 并存时不撞卷名）",
    compose.get("name") == "genai-mindmap",
    str(compose.get("name")),
)

top_vols = set(compose.get("volumes", {}) or {})
check(
    "compose: 顶层声明 mindmap-data 与 mindmap-models",
    top_vols == {"mindmap-data", "mindmap-models"},
    str(sorted(top_vols)),
)

be_mounts = {}
for m in be.get("volumes", []) or []:
    if isinstance(m, str):
        src, _, dst = m.partition(":")
        be_mounts[dst] = src
check("compose[backend]: mindmap-data 挂到 /data", be_mounts.get("/data") == "mindmap-data", str(be_mounts))
check("compose[backend]: mindmap-models 挂到 /models", be_mounts.get("/models") == "mindmap-models", str(be_mounts))

# env 与挂载点必须自洽
be_env = be.get("environment", {}) or {}
db_path = be_env.get("MINDMAP_DB_PATH", "")
check("compose[backend]: MINDMAP_DB_PATH 落在 /data 卷内", db_path.startswith("/data/"), db_path)
for key in ("HF_HOME", "DOCLING_CACHE_DIR"):
    val = be_env.get(key, "")
    check("compose[backend]: %s 落在 /models 卷内" % key, val.startswith("/models"), val)
check(
    "compose[backend]: HOME 指向可写卷（docling 缺省写 $HOME/.cache/docling）",
    be_env.get("HOME", "").startswith("/data"),
    str(be_env.get("HOME")),
)
check(
    "compose[backend]: 显式覆盖 MINDMAP_EXTERNAL_PROXY（缺省值指向容器自己的 127.0.0.1:7897）",
    "MINDMAP_EXTERNAL_PROXY" in be_env,
    str(be_env.get("MINDMAP_EXTERNAL_PROXY")),
)

check(
    "compose[backend]: 不向宿主机发布 8000（只有 expose）",
    "ports" not in be and be.get("expose") == ["8000"],
    "ports=%r expose=%r" % (be.get("ports"), be.get("expose")),
)
check("compose[frontend]: 发布 ${WEB_PORT:-80}:80", fe.get("ports") == ["${WEB_PORT:-80}:80"], str(fe.get("ports")))

dep = (fe.get("depends_on") or {}).get("backend", {}) if isinstance(fe.get("depends_on"), dict) else {}
check("compose[frontend]: depends_on backend 且 condition=service_healthy", dep.get("condition") == "service_healthy", str(fe.get("depends_on")))
check("compose[backend]: 有 healthcheck（frontend 的 service_healthy 依赖它）", "healthcheck" in be, "")

hc = json.dumps(be.get("healthcheck", {}), ensure_ascii=False)
check("compose[backend]: healthcheck 打 /api/health", "/api/health" in hc, "")

check("compose[backend]: env_file 指向根上的 .env", be.get("env_file") == [".env"], str(be.get("env_file")))

# docling 会拉起 python 子进程做格式转换，而容器的 PID 1 是 uvicorn——它不 reap。
# 少了 init 就是"跑久了进程表里全是 defunct"这种只能在现场查的事，代价却是一行。
check("compose[backend]: init: true（PID 1 不是 tini 时僵尸子进程没人收）",
      be.get("init") is True, "init=%r" % be.get("init"))

# 只看真的会被插值的行：注释里出现的 ${VAR} 是行文示例，不是配置项。
compose_text = "\n".join(
    l for l in compose_path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")
)
interpolated = sorted(set(re.findall(r"\$\{([A-Z0-9_]+)", compose_text)))
env_example = read("docker/.env.example")
missing = [v for v in interpolated if not re.search(r"^%s=" % re.escape(v), env_example, re.M)]
check(
    "compose<->.env.example: 每个 ${VAR} 在模板里都有赋值行",
    not missing,
    ("缺失: %s" % missing) if missing else "变量: %s" % interpolated,
)

df_backend = read("docker/backend.Dockerfile")
for arg in (be.get("build", {}) or {}).get("args", {}) or {}:
    check("backend.Dockerfile: 为 compose 的构建参数声明了 ARG %s" % arg, re.search(r"^ARG %s\b" % re.escape(arg), df_backend, re.M) is not None)

# ---------------------------------------------------------------- nginx <-> compose
nginx = read("docker/nginx.conf")
nginx_code = "\n".join(l for l in nginx.splitlines() if not l.strip().startswith("#"))

upstream_hosts = set(re.findall(r"https?://([A-Za-z0-9_.\-]+):\d+", nginx_code))
check("nginx: 反代主机名 == compose service 名 backend", upstream_hosts == {"backend"}, "解析到: %s" % sorted(upstream_hosts))
check("nginx: 后端端口 8000 与 uvicorn --port / EXPOSE 一致", "backend:8000" in nginx_code, "")

sse_required = [
    ("proxy_buffering off", r"proxy_buffering\s+off\s*;"),
    ("proxy_cache off", r"proxy_cache\s+off\s*;"),
    ("chunked_transfer_encoding on", r"chunked_transfer_encoding\s+on\s*;"),
    ("proxy_http_version 1.1", r"proxy_http_version\s+1\.1\s*;"),
    ('proxy_set_header Connection ""', r'proxy_set_header\s+Connection\s+""\s*;'),
    ("proxy_connect_timeout 10s", r"proxy_connect_timeout\s+10s\s*;"),
    ("proxy_send_timeout 900s", r"proxy_send_timeout\s+900s\s*;"),
    ("proxy_read_timeout 900s", r"proxy_read_timeout\s+900s\s*;"),
]
for label, pat in sse_required:
    check("nginx(/api/): 保住 %s" % label, re.search(pat, nginx_code) is not None)

m = re.search(r"proxy_read_timeout\s+(\d+)s", nginx_code)
read_t = int(m.group(1)) if m else 0
check("nginx: proxy_read_timeout > RunBudget.max_seconds(600)", read_t > 600, "%ss" % read_t)

m = re.search(r"client_max_body_size\s+(\d+)m", nginx_code)
check("nginx: client_max_body_size >= 21m（音频上限 20MB）", bool(m) and int(m.group(1)) >= 21, m.group(0) if m else "未设")

gz = re.search(r"gzip_types\s+([^;]+);", nginx_code)
gz_types = gz.group(1) if gz else ""
check("nginx: gzip_types 不含 event-stream 也不含通配 *", "event-stream" not in gz_types and "*" not in gz_types, gz_types.strip())

depth = 0
ok_semi = True
line_no = 0
bad = []
for raw in nginx_code.splitlines():
    line_no += 1
    line = raw.strip()
    if not line:
        continue
    depth += line.count("{") - line.count("}")
    if line.endswith(";") or line.endswith("{") or line.endswith("}"):
        continue
    bad.append((line_no, line))
check("nginx: 花括号平衡", depth == 0, "净深度 %d" % depth)
check("nginx: 每条指令以 ; 或 { 结尾", not bad, "可疑行: %s" % bad[:5])

check("nginx: 配了 resolver（proxy_pass 用变量时必需）", "resolver 127.0.0.11" in nginx_code, "")
check("nginx: set $backend 位于 server 块内", re.search(r"server\s*\{[^}]*set\s+\$backend", nginx_code, re.S) is not None, "")

# ---------------------------------------------------------------- 设置面板的鉴权（已弃用 Basic Auth）
# 这一面原先靠 nginx 的 Basic Auth 兜住，理由是"改 base_url + 点测试连接 =
# 把已存密钥送到任意地址"。账号体系上线后改用应用层承担：门禁要求有身份
# （`backend/app/auth/gate.py`），再加两道代码防线（`probe._refuse()` 拒内网与
# 元数据、`store.resolve()` 的密钥与地址绑定）。**别再往 nginx 上加全站口令**：
# 它会把游客挡在门外，而"游客每天 3 次免费"正是门禁允许匿名之外身份的前提。
check("nginx: 指令里没有任何 auth_basic（账号体系在应用层）",
      "auth_basic" not in nginx_code,
      "残留: %s" % [l.strip() for l in nginx_code.splitlines() if "auth_basic" in l])

plain_api = re.search(r"location\s+/api/\s*\{(.*?)\n    \}", nginx_code, re.S)
check("nginx(/api/): 反代到同一个后端且没有鉴权指令",
      plain_api is not None and "$backend$request_uri" in plain_api.group(1))

# 只看指令行：文件头注释里逐条**解释**了"去掉了 __DOMAIN__ 那一套"，
# 那是说明文字，不是残留。用剥掉注释后的 nginx_code 判断才准。
placeholders = [p for p in ("__DOMAIN__", "__WEB_ROOT__", "__CERT_DIR__", "__LISTEN6__") if p in nginx_code]
check("nginx: 指令里无 install.sh 占位符残留", not placeholders, str(placeholders))

# ---------------------------------------------------------------- Dockerfile 对齐仓库事实
fe_df = read("docker/frontend.Dockerfile")

check("backend.Dockerfile: 基于 python:3.12-slim（= CI python-version）", re.search(r"^FROM python:3\.12-slim", df_backend, re.M) is not None, "")
check("frontend.Dockerfile: 构建段 node:24（= CI node-version）", re.search(r"^FROM node:24", fe_df, re.M) is not None, "")
check("frontend.Dockerfile: 多阶段构建（builder -> nginx）", "AS build" in fe_df and re.search(r"^FROM nginx:", fe_df, re.M) is not None, "")
check("backend.Dockerfile: uvicorn --workers 1（SQLite 单写者，硬要求）", re.search(r'"--workers",\s*"1"', df_backend) is not None, "")
check("backend.Dockerfile: 非 root 运行（USER app）", re.search(r"^USER app\b", df_backend, re.M) is not None, "")
check("backend.Dockerfile: HEALTHCHECK 打 /api/health", "/api/health" in df_backend, "")
check("frontend.Dockerfile: HEALTHCHECK 存在", "HEALTHCHECK" in fe_df, "")

# ---------------------------------------------------------------- 卷属主 / 非 root 可写
# 这一段是"第一次真跑最可能炸"的地方：named volume 首次创建时会把镜像里挂载点的
# 内容与**属主**一起复制进卷。镜像里 /data、/models 若没 chown 给 app，容器起来后
# 以 uid 10001 跑就写不进 SQLite，用户看到的是 "Permission denied"，会以为应用坏了。
# 本机没有 docker，跑不了容器，只能静态断言"这条因果链在文件里是接上的"：
#   1. 有 mkdir -p（目录在镜像里就存在，不是等运行时才建）
#   2. 有 chown -R app:app，且**在 USER app 之前**（放在之后等于没做，root 才跑得动）
#   3. root 侧断言属实主位（test -w 在这一步是恒真的假断言，比 stat 才有意义）
#   4. USER app 之后还有一条真探针，确认最终身份真的写得进
def df_index(pattern):
    """返回第一个**非注释**行匹配 pattern 的行号（0 基），没有则 -1。"""
    for i, line in enumerate(df_backend.splitlines()):
        s = line.strip()
        if s.startswith("#"):
            continue
        if re.search(pattern, s):
            return i
    return -1


i_mkdir = df_index(r"mkdir -p /data /models")
i_chown = df_index(r"chown -R app:app /data /models")
i_user = df_index(r"^USER app\b")
i_owner_assert = df_index(r"stat -c '%u:%g' /data")
i_probe = df_index(r"NOT WRITABLE as non-root user")

check("backend.Dockerfile: 有 mkdir -p /data /models（目录在镜像里就存在）", i_mkdir >= 0)
check("backend.Dockerfile: 有 chown -R app:app /data /models", i_chown >= 0)
check(
    "backend.Dockerfile: chown 在 USER app **之前**（放之后等于没做）",
    i_chown >= 0 and i_user >= 0 and i_chown < i_user,
    "chown@%d USER@%d" % (i_chown, i_user),
)
check(
    "backend.Dockerfile: chown 与 mkdir 同一条 RUN（属主不会中途被别的层覆盖）",
    i_mkdir >= 0 and i_chown >= 0 and i_chown - i_mkdir <= 3,
    "mkdir@%d chown@%d" % (i_mkdir, i_chown),
)
check(
    "backend.Dockerfile: root 侧断言用 stat 比数字属主（test -w 对 root 恒真，是假断言）",
    i_owner_assert >= 0 and i_owner_assert < i_user and "10001:10001" in df_backend,
    "stat@%d USER@%d" % (i_owner_assert, i_user),
)
check(
    "backend.Dockerfile: USER app 之后有可写性探针（以最终运行身份真建文件）",
    i_probe >= 0 and i_user >= 0 and i_probe > i_user,
    "probe@%d USER@%d" % (i_probe, i_user),
)
check(
    "backend.Dockerfile: 探针覆盖 /data 与 /models（SQLite 与模型缓存两个落点）",
    re.search(r"for d in /data /models", df_backend) is not None,
    "",
)
check(
    "backend.Dockerfile: 探针失败会 exit 1（构建失败，而不是运行时才 Permission denied）",
    i_probe >= 0 and "exit 1" in "\n".join(df_backend.splitlines()[max(0, i_probe - 6) : i_probe + 2]),
    "",
)

copy_srcs = []
for line in df_backend.splitlines() + fe_df.splitlines():
    s = line.strip()
    if not s.startswith("COPY "):
        continue
    if "--from=" in s:
        continue
    parts = [p for p in s[len("COPY "):].split() if not p.startswith("--")]
    copy_srcs.extend(parts[:-1])
copy_srcs = [c for c in copy_srcs if not c.startswith("/")]
for src in copy_srcs:
    check("COPY 源路径存在: %s" % src, (ROOT / src).exists(), "")

ignore_lines = [l.strip() for l in read(".dockerignore").splitlines()]
ignore_lines = [l for l in ignore_lines if l and not l.startswith("#")]


def ignored(path):
    p = path.replace("\\", "/")
    for pat in ignore_lines:
        pat2 = pat.replace("**/", "").lstrip("/")
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, pat2) or p == pat or p.startswith(pat.rstrip("/") + "/"):
            return pat
    return None


for src in copy_srcs:
    hit = ignored(src)
    check(".dockerignore 未排除 COPY 源: %s" % src, hit is None, ("被规则 %r 命中" % hit) if hit else "")

for must in (".git", "backend/.venv", "frontend/node_modules", "frontend/dist", ".env", "backend/data"):
    hit = ignored(must)
    check(".dockerignore 排除了 %s" % must, hit is not None, ("规则 %r" % hit) if hit else "未命中")

# 密码文件相关的规则留着（`.gitignore` / `.dockerignore` 里那两条不改）：
# Basic Auth 已经弃用、compose 也不再挂这个文件，但这条规则的成本是零，而一旦
# 哪天有人想起来要加一层反代口令，`htpasswd` 建出来的哈希正好会被它挡住。
for hidden in ("docker/.htpasswd", ".htpasswd"):
    hit = ignored(hidden)
    check(".dockerignore 排除了 %s" % hidden, hit is not None, ("规则 %r" % hit) if hit else "未命中")

gitignore_lines = [l.strip() for l in read(".gitignore").splitlines()]
gitignore_lines = [l for l in gitignore_lines if l and not l.startswith("#")]
check(
    ".gitignore 排除了密码文件（否则 htpasswd 建出来的哈希会被顺手提交）",
    any("htpasswd" in line for line in gitignore_lines),
    str([line for line in gitignore_lines if "htpasswd" in line]),
)

reqs = [l.split("=")[0].split(">")[0].split("<")[0].strip() for l in read("backend/requirements.txt").splitlines() if l.strip() and not l.strip().startswith("#")]
pkg = json.loads(read("frontend/package.json"))
check("requirements.txt: 包名可解析（无脏行）", bool(reqs) and all(re.match(r"^[A-Za-z0-9_.\-\[\]]+$", r) for r in reqs), str(reqs))
check("requirements.txt: 含 docling 与 uvicorn[standard]", "docling" in reqs and "uvicorn[standard]" in reqs, "")
check("package.json: 有 build 脚本", "build" in pkg.get("scripts", {}), pkg.get("scripts", {}).get("build", ""))
check("package.json: build == vue-tsc --noEmit && vite build", pkg.get("scripts", {}).get("build") == "vue-tsc --noEmit && vite build", "")
check("frontend/package-lock.json 存在（npm ci 的前提）", (ROOT / "frontend/package-lock.json").is_file(), "")
lock = json.loads(read("frontend/package-lock.json"))
check("package-lock: lockfileVersion 3（npm ci 可用）", lock.get("lockfileVersion") == 3, str(lock.get("lockfileVersion")))

ts_text = re.sub(r"//.*", "", read("frontend/tsconfig.json"))
check('tsconfig: include 含 "tests"（前端镜像必须 COPY frontend/tests）', '"tests"' in ts_text, "")

for k in ("MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY", "MINDMAP_LLM_MODEL"):
    check(".env.example: 含 %s" % k, re.search(r"^%s=" % k, env_example, re.M) is not None, "")
active_db = re.search(r"^MINDMAP_DB_PATH=(.+)$", env_example, re.M)
check(".env.example: MINDMAP_DB_PATH 未硬编码（交给 compose）", active_db is None, active_db.group(0) if active_db else "")

# dx: 每个 docker/ 下的文件都要被 Dockerfile 或 compose 引用（没有孤儿文件）
print()
fails = [r for r in results if not r[0]]
print("=" * 72)
print("共 %d 项核验，PASS %d，FAIL %d" % (len(results), len(results) - len(fails), len(fails)))
if fails:
    print("FAIL 项：")
    for _, name, detail in fails:
        print("  - %s  %s" % (name, detail))
sys.exit(1 if fails else 0)
