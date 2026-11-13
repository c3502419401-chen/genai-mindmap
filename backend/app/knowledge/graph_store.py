"""图存储：实体与边落在 Neo4j，游走靠变长 Cypher 路径。

**为什么是 Neo4j**：上游定的，选型论据与当年 SQLite 版的实测数据留在
`docs/RAG-DECISIONS.md` 第 8 节（那一节是历史记录，不随实现改动）。这一层要的
是三样 SQLite 得自己攒的东西：变长路径（`neighbors` 里一条 `[*1..n]` 替掉整段
递归 CTE）、关系唯一性保证（同一条关系不会在一条路径里重复出现）、以及一个
天生带连接的存储（`add_edge` 的"两端 + 边"在一个事务里，不需要手写 upsert 顺序）。

**数据模型**：一个标签、一种关系类型，名字都写在常量里（`ENTITY_LABEL` /
`EDGE_TYPE`），因为它们会出现在每一条 Cypher 里，改一处就该改全部。

  - 约束 `entity_key`：`(e.name, e.type) IS UNIQUE`——同名同类型只有一个节点。
  - 节点 `(:Entity {name, type, created_at})`。
  - 边 `(:Entity)-[:REL {relation, source_id, chunk_start, chunk_end, created_at}]->
    (:Entity)`。`source_id` / `chunk_start` / `chunk_end` 是证据三件套，下一轮回填
    高亮全靠它们，落库后一个字节都不改。

**`type` 为什么折成空串而不是 null**：Cypher 里 `null = null` 求值为 `null`（不是
`true`），而 `MERGE` 靠属性相等来匹配既有节点——所以 `MERGE (e:Entity {name:'甲',
type:null})` **永远匹配不到上一行**，每写一次就多一个节点，幂等当场失效。唯一约束
也帮不上：Neo4j 把值为 null 的属性当作"属性不存在"，唯一约束对缺失属性不生效，
多个 `{name:'甲'}` 可以共存。SQLite 版撞的是同一个坑的另一种形态（`UNIQUE(name,
NULL)` 认为两个 NULL 互不相等），解法也一样：`None` → `''`（`_fold_type`）。`type`
不出现在任何返回结构里，所以不需要反向还原。

**写与读的降级不对称**：读接口（`neighbors` / `stats` / `resolve` / `entity_degrees`）
在库不可用或没配置时返回空值——"查不到"与"库坏了"在检索层是同一种结果，为一次查询
崩掉整条链路没有收益。写接口（`upsert_entity` / `add_edge` / `add_edges_bulk` /
`add_aliases` / `clear_aliases`）**照常抛错**——把"没写进去"说成"写好了"会让材料入库
静默缺边，而这种缺失没有任何信号能发现：图不会报错，它只是少了一条路。

**异常类型是 `OSError` 的子类**（`GraphStoreError`）。理由换过一次，两次都成立：
换引擎那一刻，调用方 `graph_ingest` 的降级分支写的是
`except (sqlite3.Error, OSError, ValueError)`，它按"块"计 `failed`；把这层换成
Neo4j 专有异常，`failed` 就会从"这块没写进去"变成"整份材料抛到上传接口"，语义
静默改变而测试看不出来。后来 `graph_ingest` 收敛成 `except (GraphStoreError,
ValueError)` 并加上了失败熔断，直接依赖这里的类型——`OSError` 仍然保留，因为它本来
就准确（连接被拒、超时都是 I/O 失败），而且仍在按 `except OSError` 兜底的调用方不会
因为换引擎而失守。`GraphStoreNotConfigured` 是它的子类，表示"密码没配"，与
unconfigured 语义一致。

**驱动是模块级惰性单例**：`Driver` 自己就是连接池，官方推荐进程内长期持有、退出时
`close()`。`close_driver()` 供测试（每个用例换一套环境变量）与进程退出调用。惰性
的意思是"第一次真正要连库时才建"，所以没配 Neo4j 的部署里 `import graph_store`
不会尝试连接。

**连接与查询都要有上限**：一次不可达的 7687 不能让整个请求挂住。连接超时
`CONNECT_TIMEOUT_SECONDS`、池等待 `ACQUISITION_TIMEOUT_SECONDS`、事务超时
`QUERY_TIMEOUT_SECONDS`（服务端执行超时，整批 `UNWIND` 也受它约束）、驱动重试
`RETRY_TIME_SECONDS`。全部是"秒级失败"，不是默认的 60 秒。

## 空类型实体的复用（`_pick_existing`）

`add_edge` 的 `src_type` / `dst_type` 是可选的，调用方（尤其是只关心"谁连着谁"
的下游）不会为了写一条边先去查类型。若直接把空类型当键去 `MERGE`，同一个"甲公司"
会在库里变成两个节点——一个 `type='组织'`（抽取时建的）、一个 `type=''`（写边时
建的），边全挂在后者上，前者成了孤立实体。实测 3000 个实体按 3 条边连完，`stats()`
报出 6000 个实体、3000 个孤立——图看着建成了，一半节点是死的。

所以类型为空时**先复用同名里最早的节点（任意类型）**，复用不到才建空类型的。这需
一次额外的 `MATCH`（`CYPHER_FIRST_KIND`）。SQLite 版这里同样是"一次 SELECT + 一次
INSERT"，不是往返次数退化；而且只有 `type` 为空时才多这一次。

## 游走是无向的（`neighbors`）

抽取模型没有稳定依据判断关系方向——"甲依赖乙"与"乙被甲依赖"在自然语言里都成立，
而方向判反之后**图不会断，只是路径反向**：从错误的那一端出发就走进死胡同，
`neighbors` 返回 `[]`，与"这两个实体确实无关"完全无法区分。哑失败在图上比在文本上
更贵：文本还能 grep，图上你连"该不该有边"都不知道。所以边按无向走，但**方向必须
留在路径里**——`GraphPath.directions` 逐跳记录这条边原本是 `nodes[i] → nodes[i+1]`
还是反过来，否则读路径的人看不出谁依赖谁。无向在 Cypher 里就是不给关系加箭头。

**环**：Neo4j 的变长模式枚举的是 *trail*（关系不重复），**节点可以重复**——所以
`甲-[e1]-乙-[e2]-甲` 是一条合法路径，而 SQLite 版枚举的是简单路径（节点不重复）。
为了保持返回结构一致（每条路径都是简单路径），语句里用
`all(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m = n))` 过滤。这条谓词对每条
候选路径求值，深度上限 5 时最多 6 个节点、36 次比较，代价可以忽略；换来的是
"路径里的节点一定不重复"这个下游可以直接依赖的性质。

**`hops` 与 `limit` 都收在硬上限内**（`MAX_HOPS` / `MAX_LIMIT`）：变长路径的枚举量
随深度组合增长，一个手滑传进来的 `hops=12` 会在稠密子图上跑出指数级的中间行。
另外 Cypher 的变长关系长度**不接受参数**（`[*1..$hops]` 是语法错误），只能内插，
所以 `_walk_query` 是全文唯一一处语句拼接——内插的值来自 `_clamp_hops` 的 int，
不是用户字符串，测试钉住这一点。

## 归一化规则

`normalize_name` 只做三件事：

  1. **全角转半角**：NFKC 归一化（`ＡＢＣ`→`ABC`、`１`→`1`、全角空格→半角）。
     顺带把 `㈱` 这类兼容字符折平——它们在中文材料里少见，折平只赚不赔。
  2. **去首尾空白、折叠内部连续空白**：`"  张三 "` 与 `"张三"` 必须是同一个实体，
     否则抽取层每次给的轻微差异都会造出一个新节点。
  3. **英文小写化**：`Transformer` 与 `transformer` 是同一个东西。

**中文不拆词**：实体名原样保留。项目里没有分词依赖（`service._bigram_terms` 的
二元组是给 BM25 用的相似度特征，不是词），在这里塞一个分词器会让"张三丰"与
"张三"变成两个实体的概率上升到不可控，而抽取层已经按"名词性"筛过一遍。存储层
再做一次切分，等于在两处各有一套粒度，谁都不服谁。

**同一 `name` 不同 `type` 是两个实体**（`UNIQUE(name, type)`）。这是刻意的：
材料里的"苹果"是公司、"苹果"是水果，合并它们会造出一条"苹果→iPhone"的假边。
代价是同一实体在不同块里被判成不同类型时会出现两行；抽取层用类型白名单
（`graph_extract.ENTITY_TYPES`）把这种分裂压到"有类型 vs 无类型"这一种。

## 实体消解：别名边（`ALIAS_OF`）

上面三条归一化只折全角、空白、大小写，不做别名归并——材料里的「国家市场监督管理
总局」与「市场监管总局」曾经在库里是**两个实体**，各带各的边。后果不是"少连了几
条边"，而是**孤岛与"确实无关"无法区分**：`neighbors` 在两份材料之间找不到路径时
返回 `[]`，与两份材料真的没关系时返回的 `[]` 逐字节相同。这一层现在补上存储与
游走的那一半，判断那一半在 `graph_resolve`。

**模型**：`(:Entity)-[:ALIAS_OF {source, similarity, created_at}]->(:Entity)`，方向是
**别名 → 主名**（`ALIAS_OF` 读作"我是它的另一个名字"）。别名不是 `REL`：它不进
`edges`、不进路径、不参与 `hops`，`stats()` 单独计数。混进 `REL` 的后果是别名会在
游走里变成一条"证据"——而它没有任何 `source_id` / `chunk_start`，恰好是证据三件套
最不能缺的那三个字段。

**为什么加边而不是把边搬到主名上（真合并节点）**：搬边不可逆。两条边并成一条之后，
"这条证据原本说的是哪个名字"就没了，而 `source_id` / `chunk_start` / `chunk_end`
的全部意义就是能指回原文。别名边保留两边的**全部**原始边，代价是每次游走多一次
别名表查询（见下）。

**为什么一次读全表而不是在 Cypher 里做变长别名模式**：`[…:ALIAS_OF*1..10]->` 能把
十跳上限写在语句里，但"链太长"与"没有别名"在结果上都是"没有行"，两者分不开——而
这两种情况一个该原样返回、一个该返回主名。别名边是"被合并过的名字对"，数量是实体
数的零头（人工/半自动挑出来的），一次读进内存之后链的走法（防环、上限、确定性）
是纯 Python，可以单独测。真实图上一千个实体配上几十条别名边，这条查询的代价与
`stats()` 同级。

**`neighbors` 怎么把别名纳进来**：起点换成"同一别名组的全部名字"（`$names` 是
列表，不是单个名字——边可能挂在任何一个别名上），返回前把每个节点名换成主名。
换完如果路径里出现重复节点，整条丢掉：合并之后那是一个环，而 `GraphPath` 的契约是
简单路径。**返回结构没变**（`nodes` / `relations` / `evidence` / `directions`），
只是名字统一成了主名。

**合并错了比没合并更贵**：把「数字化转型」与「数字化改革」并成一个，图上会走出一
条不存在的路径，而它看起来有证据、有位置、能通过校验。所以阈值偏高（0.92，理由
写在 `graph_resolve.DEFAULT_THRESHOLD`）、默认 `dry_run`、落库前能先看候选。

**仍然没做**：简称的构词规则（「市场监管总局」这种取首尾几个字的简写，字符层面
没有足够重叠，本轮的 embedding 阈值能不能捞到它取决于模型，见 `graph_resolve` 的
实测记录）、概念级的同义归并（需要领域词表或更强的模型）。所以定位没有变：
图上的"没路径"仍然只说明"不确定"，只是这个缺口比之前小了一圈。
"""
from __future__ import annotations

import logging
import os
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

from neo4j import READ_ACCESS as _READ, WRITE_ACCESS as _WRITE
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError

if TYPE_CHECKING:
    # 只在类型检查时导入：`graph_resolve` 运行时 import 本模块，这里反向运行时
    # import 就是环。运行时只用 duck typing 读 `.canonical` / `.aliases` /
    # `.similarity` 三个属性，所以省掉这次导入不影响任何行为。
    from app.knowledge.graph_resolve import MergeCandidate

logger = logging.getLogger(__name__)

# 数据模型的名字。写成常量是因为它们出现在每一条 Cypher 里：改标签却漏改某一条
# 语句的结果是"写入看不见、查询永远空"，而那种错查起来最费时间。
ENTITY_LABEL = "Entity"
EDGE_TYPE = "REL"
CONSTRAINT_NAME = "entity_key"

# 配置一律走环境变量，不写死。密码**没有默认值**：一个默认密码意味着"忘了配"
# 会连到一个碰巧存在的实例上，那比连不上更危险。
URI_ENV = "MINDMAP_NEO4J_URI"
USER_ENV = "MINDMAP_NEO4J_USER"
PASSWORD_ENV = "MINDMAP_NEO4J_PASSWORD"
DATABASE_ENV = "MINDMAP_NEO4J_DATABASE"

DEFAULT_URI = "bolt://127.0.0.1:7687"
DEFAULT_USER = "neo4j"
DEFAULT_DATABASE = "neo4j"

# 四个超时见模块 docstring 的「连接与查询都要有上限」。
CONNECT_TIMEOUT_SECONDS = 3.0
ACQUISITION_TIMEOUT_SECONDS = 5.0
QUERY_TIMEOUT_SECONDS = 10.0
RETRY_TIME_SECONDS = 3.0

# 路径里每一跳的方向。留成两个字符串而不是布尔，是因为它会进 `GraphPath` 的
# 返回结构、会被日志与将来的融合层读到：`"reverse"` 比 `False` 更难被误读。
FORWARD = "forward"   # 这一跳顺着边的原方向：nodes[i] 是 relation 的 src
REVERSE = "reverse"   # 这一跳逆着边的原方向：nodes[i] 是 relation 的 dst

# 游走深度的硬上限。变长路径的枚举量随深度组合增长：一个 10 度节点的三层完全图
# 能炸出上千条路径。无向游走把这个增长又放大一倍（平均度翻倍），所以这个上限比
# 有向时更必要。5 跳是"够用且不会失控"的界——真需要跨 5 跳以上才能连上的两件事，
# 靠这条边找回来的可信度也已经很低了。
MAX_HOPS = 5

# 单次游走的路径条数上限。检索阶段一次只拿几十条证据，多的用不上。
MAX_LIMIT = 200

# 别名关系的类型名。与 `EDGE_TYPE` 分开是设计：别名不是证据，见模块 docstring 的
# 「实体消解」。
ALIAS_TYPE = "ALIAS_OF"

# 别名链最多追几跳。这是防环的第二道闸：`_resolve_in` 自己有访问集合，但一条被
# 人手动改坏的长链（A→B→C→…→A 但中间少一环）也会在这里被截住，而不是让一次
# `resolve` 走出几千步。10 跳远大于任何真实的"简称套简称"层数。
MAX_ALIAS_HOPS = 10

# ---------------------------------------------------------------- Cypher 语句
#
# 全部抽成模块级常量，有两个理由：一是测试能静态断言"占位符与传参键一致"与
# "语句里没有拼进来的用户输入"；二是 `add_edges_bulk` 的回滚用例要靠替换某一条
# 常量来制造事务中途失败（见 tests/test_graph_neo4j.py）。
#
# 参数一律用 `$name` 风格，值只走 driver 的参数绑定——Cypher 注入之所以常见，
# 是因为把值格式化进了语句；这里没有任何一处这么做（唯一的拼接是 `hops`，见
# `_walk_query`）。

# 约束：同名同类型唯一。`IF NOT EXISTS` 让它可以每次连接都跑一遍而无副作用。
CYPHER_CONSTRAINT = (
    f"CREATE CONSTRAINT {CONSTRAINT_NAME} IF NOT EXISTS "
    f"FOR (e:{ENTITY_LABEL}) REQUIRE (e.name, e.type) IS UNIQUE"
)

# 建（或取回）一个精确的 (name, type) 节点。`$type` 一定是 `_resolve_kind` 算过的
# 落库形态，不会是 null（理由见模块 docstring）。
CYPHER_UPSERT_ENTITY = (
    f"MERGE (e:{ENTITY_LABEL} {{name: $name, type: $type}})\n"
    "ON CREATE SET e.created_at = $now\n"
    "RETURN id(e) AS id"
)

# 同名节点里最早的那个（任意类型）。给"类型不明"的调用方挑复用目标，与 SQLite 版
# `_pick_existing` 的"取 id 最小那行"一一对应。`created_at` 是同一批次里也唯一的
# 毫秒时间戳，再以内部 id 兜底，保证同一次入库的两次调用挑到同一个节点。
CYPHER_FIRST_KIND = (
    f"MATCH (e:{ENTITY_LABEL} {{name: $name}})\n"
    "RETURN e.type AS type\n"
    "ORDER BY e.created_at, id(e)\n"
    "LIMIT 1"
)

# 批量取候选（一次 UNWIND）。分组顺序在 Cypher 里定死，Python 侧不再排——
# 两处各排一次迟早分叉。
CYPHER_ENTITY_CANDIDATES = (
    f"UNWIND $names AS name\n"
    f"MATCH (e:{ENTITY_LABEL} {{name: name}})\n"
    "RETURN name, id(e) AS id, e.type AS type, e.created_at AS created_at\n"
    "ORDER BY name, created_at, id"
)

# 一条语句写完两端与边：`MERGE` 两端保证"边写进去了、实体没写进去"这种中间态
# 不存在。返回新建条数靠 `summary.counters.relationships_created`——比在边上打
# 时间戳标记可靠（同一毫秒内重复写不会误判），也不会在边上留下一次性属性。
CYPHER_ADD_EDGE = (
    f"MERGE (a:{ENTITY_LABEL} {{name: $src, type: $src_type}})\n"
    "ON CREATE SET a.created_at = $now\n"
    f"MERGE (b:{ENTITY_LABEL} {{name: $dst, type: $dst_type}})\n"
    "ON CREATE SET b.created_at = $now\n"
    f"MERGE (a)-[r:{EDGE_TYPE} {{relation: $relation, source_id: $source_id, "
    "chunk_start: $chunk_start}]->(b)\n"
    "ON CREATE SET r.chunk_end = $chunk_end, r.created_at = $now"
)

# 批量建实体。一次 UNWIND，不循环发语句——这是 Neo4j 的批量模式。
CYPHER_UPSERT_ENTITIES_BULK = (
    f"UNWIND $rows AS row\n"
    f"MERGE (e:{ENTITY_LABEL} {{name: row.name, type: row.type}})\n"
    "ON CREATE SET e.created_at = row.created_at"
)

# 批量建边。两端的类型是**解析后**的类型（`row.src_type` / `row.dst_type`），所以
# `MATCH` 一定命中上一条语句保证存在的节点；不需要再 MERGE 一次，也就不会因为
# 类型对不上而悄悄多建节点。
CYPHER_ADD_EDGES_BULK = (
    f"UNWIND $rows AS row\n"
    f"MATCH (a:{ENTITY_LABEL} {{name: row.src, type: row.src_type}})\n"
    f"MATCH (b:{ENTITY_LABEL} {{name: row.dst, type: row.dst_type}})\n"
    f"MERGE (a)-[r:{EDGE_TYPE} {{relation: row.relation, "
    "source_id: row.source_id, chunk_start: row.chunk_start}]->(b)\n"
    "ON CREATE SET r.chunk_end = row.chunk_end, r.created_at = row.created_at"
)

# 无向游走。五件事各自有理由：
#
#   - 起点是 **`$names` 列表**而不是单个名字：同一实体的边可能挂在任何一个别名上，
#     只按 `name` 出发会让"边在 `甲别名` 上、从 `甲` 问"永远返回 `[]`——那正是这
#     一层要消掉的那种哑失败。没有任何别名时列表里只有查的那个名字，行为与本轮
#     之前逐字节一致。
#   - 名字是归一化后的精确键（`=` / `IN`），不用 `CONTAINS`：模糊匹配会把"张三"
#     与"张三丰"混起来，那是两个东西。`IN` 仍然走 `name` 上的索引。
#   - 变长模式限定 `:REL`：不限定类型会把 `ALIAS_OF` 也当成一跳——别名不是证据，
#     它没有 `source_id` / `chunk_start`，走出来的路径在 `_parse_path` 里会被判成
#     形状异常（少三个属性）而整条丢掉，等于把真实的近路吃掉。
#   - 无向靠不给关系加箭头。Cypher 的 `-[:REL*1..n]-` 两个方向都会走。
#   - `{hops}` 由 `_walk_query` 内插（变长长度不接受参数，见模块 docstring）。
#   - `all(... single(...))` 把 trail 收成简单路径（Neo4j 默认允许节点重复）。
#   - `ORDER BY length(p)` 让短路径先出：调用方拿到的前几条就是最近的邻居，
#     `LIMIT` 截断时不会截掉整条近路而留下一堆远路。
CYPHER_WALK = (
    f"MATCH p = (start:{ENTITY_LABEL})-[:{EDGE_TYPE}*1..{{hops}}]-(other:{ENTITY_LABEL})\n"
    "WHERE start.name IN $names\n"
    "AND all(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m = n))\n"
    "RETURN p\n"
    "ORDER BY length(p)\n"
    "LIMIT $limit"
)

# ------------------------------------------------------------ 别名（实体消解）
#
# 全部原地 `MERGE` / 读，不搬边：理由见模块 docstring 的「实体消解」。

# 全量别名表：`{别名: 主名}` 的来源。一次读完再在 Python 里走链，而不是发一条
# 变长 Cypher——"链太长"与"没有别名"在 Cypher 里都是"没有行"，分不开。
# `ORDER BY alias, canonical` 定死同一别名有多行（同名不同型）时谁先到，`resolve`
# 的返回值才确定。
CYPHER_LOAD_ALIASES = (
    f"MATCH (a:{ENTITY_LABEL})-[r:{ALIAS_TYPE}]->(b:{ENTITY_LABEL})\n"
    "RETURN a.name AS alias, b.name AS canonical\n"
    "ORDER BY alias, canonical"
)

# 写别名。`MERGE` 的模式里**只有两端**，属性一律走 `ON CREATE SET`：把 `similarity`
# 放进 MERGE 模式会让"同一对、换个阈值重算"变成第二条边——别名是关系，不是度量。
# 两端用 `MATCH` 而不是 `MERGE`：候选来自图里已有的实体，凭空补一个没有类型、没有
# 任何边的节点只会让"孤立实体"这个指标变脏。
CYPHER_ADD_ALIASES = (
    f"UNWIND $rows AS row\n"
    f"MATCH (a:{ENTITY_LABEL} {{name: row.alias}})\n"
    f"MATCH (b:{ENTITY_LABEL} {{name: row.canonical}})\n"
    f"MERGE (a)-[r:{ALIAS_TYPE}]->(b)\n"
    "ON CREATE SET r.source = row.source, r.similarity = row.similarity, "
    "r.created_at = row.created_at"
)

# 清空别名表。条数走 `relationships_deleted`（服务端计数），不是先 COUNT 再 DEL——
# 两次往返之间别人可能又写了一条，那样返回的数就不等于真删掉的数。
CYPHER_CLEAR_ALIASES = f"MATCH ()-[r:{ALIAS_TYPE}]->() DELETE r"

CYPHER_COUNT_ALIASES = f"MATCH ()-[r:{ALIAS_TYPE}]->() RETURN count(r) AS n"
# 有别名指向它的实体数：主名的个数。"合并了几个实体"与"写了几条别名边"是两个
# 数——一组三个名字写两条边，合并的是两个实体。
CYPHER_COUNT_MERGED = (
    f"MATCH (e:{ENTITY_LABEL})<-[:{ALIAS_TYPE}]-() RETURN count(DISTINCT e) AS n"
)

# 候选源：按边数降序取前 N 个实体名。`OPTIONAL MATCH` 让孤立实体也返回（度数为 0）
# ——它们参与不了路径，但可能是别人指向的那个名字。`count(r)` 在无向模式里对每条
# 入射边计一次，正是"这个实体挂着几条证据"。同名不同型的两行在这里按名字聚成
# 一行（Cypher 的隐式分组键），因为别名模型是名字级的。
CYPHER_ENTITY_DEGREES = (
    f"MATCH (e:{ENTITY_LABEL})\n"
    f"OPTIONAL MATCH (e)-[r:{EDGE_TYPE}]-()\n"
    "RETURN e.name AS name, count(r) AS degree\n"
    "ORDER BY degree DESC, name\n"
    "LIMIT $limit"
)

CYPHER_COUNT_ENTITIES = f"MATCH (e:{ENTITY_LABEL}) RETURN count(e) AS n"
CYPHER_COUNT_EDGES = f"MATCH ()-[r:{EDGE_TYPE}]->() RETURN count(r) AS n"
# 孤立实体：一条 `REL` 都没挂的节点。**只看 `REL`，不看任何关系类型**——别名边
# 不是证据，一条 `ALIAS_OF` 也不构成"这个实体参与过任何关系"。写成 `NOT (e)--()`
# 会让合并过的实体从"孤立"里消失（它挂了一条别名边），于是"这次抽取到底连上了
# 没有"这个指标开始撒谎，而它恰好是这一层唯一的总量信号。
CYPHER_COUNT_ISOLATED = (
    f"MATCH (e:{ENTITY_LABEL}) WHERE NOT (e)-[:{EDGE_TYPE}]-() RETURN count(e) AS n"
)


class GraphStoreError(OSError):
    """图存储不可用。**继承 `OSError` 是设计，不是巧合**。

    换引擎时选它的理由是：调用方（`graph_ingest`）的降级分支写着
    `except (sqlite3.Error, OSError, ValueError)`，按块计 `failed`。若抛 Neo4j
    专有异常，那条分支就接不住了——`failed` 从"这块没落库"变成"整份材料抛到上传
    接口"，而两种结果在测试里都可能看起来正常。

    现在 `graph_ingest` 已经收敛成 `except (GraphStoreError, ValueError)` 并按失败
    类型熔断，继承关系不再是"唯一让上游活下来的办法"。保留它的两个理由仍然成立：
    "连不上 7687"本来就是 I/O 失败，`OSError` 是最准确的归类；而按 `except
    OSError` 兜底的第三方调用方不会因为换引擎而失守。

    `str()` 里只有异常类名，**不带 Neo4j 的原始消息**：那条消息可能回显属性值
    （实体名、关系短语），而这一层的日志原则是"只有形状，没有内容"——材料可能是
    合同、病历、内部制度。
    """


class GraphStoreNotConfigured(GraphStoreError):
    """没配 `MINDMAP_NEO4J_PASSWORD`。写接口抛它，读接口当作"什么都没查到"。"""


@dataclass(frozen=True)
class GraphPath:
    """一条游走路径。四个列表**按下标对齐**：

    - `nodes[i]` 与 `nodes[i + 1]` 由 `relations[i]` 连接，证据是 `evidence[i]`；
    - `directions[i]` 说明这一跳在**原始边**里是顺着还是逆着（`FORWARD`/`REVERSE`）；
    - `len(relations) == len(evidence) == len(directions) == len(nodes) - 1`；
    - `nodes[0]` 是起点（归一化后的名字），`hops` 为 0 的路径不存在——只返回
      至少走过一条边的路径，否则"邻居"里会冒出一条"自己到自己的 0 跳证据"。

    **为什么必须留方向**：游走是无向的（见模块 docstring），但边本身有向。
    只给 `nodes` 与 `relations`，"甲 → 依赖 → 乙"与"甲 ← 依赖 ← 乙"读起来
    一模一样——而这两句话的意思相反。`directions[i] == FORWARD` 表示
    `nodes[i]` 是 `relations[i]` 的 src；`REVERSE` 表示它是 dst。任何要展示
    "谁依赖谁"的下游都得读这一位，只读 `relations` 会把它读反。

    `evidence` 是 `(source_id, chunk_start, chunk_end)`：下一轮把它并进
    `EvidenceHit` 时，这三样正好能拼回原文位置，不需要再查一次库。
    """

    nodes: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    evidence: list[tuple[str, int, int]] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 手工构造路径时 `directions` 很容易被漏写；关系短语本身没有方向，漏写
        # 就等于声明"全按正向读"。内部 `_parse_path` 总是给全，所以这里只兜
        # "空列表"这一种情况，长度不符留给调用方看见（静默补齐会掩盖存储层 bug）。
        if not self.directions and self.relations:
            object.__setattr__(self, "directions", [FORWARD] * len(self.relations))

    @property
    def hops(self) -> int:
        return len(self.relations)


def normalize_name(name: str | None) -> str:
    """实体名/关系短语的归一化键（规则见模块 docstring 第「归一化规则」节）。

    控制字符一并剔除：它们既会破坏日志与终端输出，也可能被 Cypher 当作属性值里的
    怪字符带进查询（参数绑定是安全的，但读的人会被它搞晕）。
    非字符串输入先转成字符串——抽取层的模型输出什么都可能给，与其在上游逐字段
    断言类型，不如让"`1` 归一化成 '1' 再因为太短被丢掉"这一条规则统一收尾。
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    text = "".join(char for char in text if char.isprintable())
    return " ".join(text.split()).lower()


def _fold_type(kind: str | None) -> str:
    """类型的落库形态：`None` → 空串。理由见模块 docstring 的「`type` 为什么折成空串」。"""
    return normalize_name(kind)


def _pick_existing(candidates: list[tuple[int, str]], kind: str) -> tuple[int, str] | None:
    """从同名节点的若干候选里挑一个复用；挑不出来就 `None`（该新建）。

    返回 `(内部 id, 实际类型)`——批量路径需要那个类型：边的 `MATCH` 是按
    `(name, type)` 定位节点的，用调用方传来的空类型会找不到复用出来的节点。

    规则只有两条：**类型明确就精确匹配；类型不明就取最早那个**（`candidates`
    按 `created_at`、id 升序）。第二条不是"随便挑一个"——理由见模块 docstring 的
    「空类型实体的复用」。

    单条写入（`_resolve_kind`）与批量写入（`add_edges_bulk`）共用这一个判断：两处
    各写一份，迟早出现"先建实体再写边"与"批量写"给出两套实体归属。
    """
    if not candidates:
        return None
    if not kind:
        return candidates[0]
    for entity_id, existing in candidates:
        if existing == kind:
            return (entity_id, existing)
    return None


def _now() -> str:
    """落库用的时间戳，格式与 SQLite 版的 `strftime('%Y-%m-%dT%H:%M:%fZ')` 同形。

    同形不是为了好看：`created_at` 参与排序（`CYPHER_FIRST_KIND` 用"最早"决定复用
    哪个节点），换一种格式就得换一处排序键。毫秒精度用整数拼出来，`strftime` 的
    `%f` 是微秒，直接截断会差三个数量级。
    """
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _clamp_hops(hops: int) -> int:
    """深度夹取。**返回值一定是 int**——它会被内插进 Cypher（见 `_walk_query`）。"""
    return min(max(int(hops), 0), MAX_HOPS)


def _clamp_limit(limit: int) -> int:
    """条数夹取。下限 1：`LIMIT 0` 会安静地返回空，比报错难查。"""
    return min(max(int(limit), 1), MAX_LIMIT)


def _walk_query(depth: int) -> str:
    """把夹过的深度内插进变长模式。

    Cypher 的变长关系长度**不接受参数**（`[*1..$hops]` 是语法错误），所以深度只能
    内插。这是本模块唯一一处语句拼接，拼接值是 `_clamp_hops` 返回的 int（0~5），
    不是调用方给的字符串——`tests/test_graph_rag.py` 有一条静态用例钉住这一点。
    `replace` 而不是 `format`：Cypher 里的 `{name: $name}` 花括号不该被当成占位符。
    """
    return CYPHER_WALK.replace("{hops}", str(_clamp_hops(depth)))


def _config() -> tuple[str, str, str, str]:
    """(uri, user, password, database)。密码没有默认值，见模块 docstring。"""
    uri = os.getenv(URI_ENV, "").strip() or DEFAULT_URI
    user = os.getenv(USER_ENV, "").strip() or DEFAULT_USER
    password = os.getenv(PASSWORD_ENV, "").strip()
    database = os.getenv(DATABASE_ENV, "").strip() or DEFAULT_DATABASE
    return uri, user, password, database


_DRIVER: Driver | None = None
_DRIVER_LOCK = threading.Lock()
_CONSTRAINT_READY = False


def _ensure_constraint(driver: Driver, database: str) -> None:
    """建唯一约束。**必须独立事务**：Neo4j 不允许 schema 修改与数据写在同一事务里
    （`ForbiddenDueToTransactionType`），所以它不能挂在任何一个写事务里顺手做。

    失败只记 warning、不抛：约束是"并发下的加固"，不是"能不能用"的前提——单线程
    的 `MERGE` 没有约束也是幂等的，而一次建不上就整层不可用是更差的交换。warning
    里也不带 Neo4j 的原始消息（可能有属性值），只带异常类名。
    """
    global _CONSTRAINT_READY
    if _CONSTRAINT_READY:
        return
    try:
        driver.execute_query(CYPHER_CONSTRAINT, database_=database)
    except (Neo4jError, DriverError, OSError) as exc:
        logger.warning("图谱约束创建失败（继续运行）：%s", type(exc).__name__)
    _CONSTRAINT_READY = True


def _driver() -> Driver:
    """惰性建池。没配密码就抛 `GraphStoreNotConfigured`，连不上由 driver 抛。

    约束的创建也在这把锁里：它只在第一次建池时真发语句（之后被 `_CONSTRAINT_READY`
    挡住），而放在锁外会让并发的首批请求各发一遍 `CREATE CONSTRAINT`。
    """
    global _DRIVER
    uri, user, password, database = _config()
    if not password:
        raise GraphStoreNotConfigured(
            f"未配置 {PASSWORD_ENV}：图存储视为未启用")
    with _DRIVER_LOCK:
        if _DRIVER is None:
            _DRIVER = GraphDatabase.driver(
                uri,
                auth=(user, password),
                connection_timeout=CONNECT_TIMEOUT_SECONDS,
                connection_acquisition_timeout=ACQUISITION_TIMEOUT_SECONDS,
                max_transaction_retry_time=RETRY_TIME_SECONDS,
                # `id()` 在 Neo4j 5.26 起被标成弃用（"will be removed without a
                # replacement"），而我们**故意**用它：`upsert_entity` 的返回类型是
                # int，`elementId()` 是字符串，换掉就是改接口。不关这一类通知的
                # 后果是每写一个实体刷一条 WARNING——一次入库上千个实体就是上千条
                # 日志，把真正要看的东西埋掉。只关 DEPRECATION 一类，别的通知照旧。
                notifications_disabled_classifications=["DEPRECATION"],
            )
        driver = _DRIVER
        _ensure_constraint(driver, database)
    return driver


def close_driver() -> None:
    """关掉单例并允许下次重建。测试用它切换环境变量，进程退出也调它。"""
    global _DRIVER, _CONSTRAINT_READY
    with _DRIVER_LOCK:
        driver, _DRIVER = _DRIVER, None
        _CONSTRAINT_READY = False
    if driver is None:
        return
    try:
        driver.close()
    except (Neo4jError, DriverError, OSError) as exc:
        # 关不掉的连接池不该让退出流程失败；进程结束时内核会收回来。
        logger.debug("图谱驱动关闭失败：%s", type(exc).__name__)


@contextmanager
def _session(read_only: bool = False) -> Iterator[Any]:
    """一个 session（= 一条连接 + 一个 database）。异常折成 `GraphStoreError`。

    `read_only` 只在集群里真正起作用（读请求可以被路由到 follower），单机 bolt
    直连下两种模式的代价一样；区别是语义：`neighbors` / `stats` 不该占着写路由。

    **两个异常基类都要捕获，少一个就漏**：driver 6.x 把异常树重排成
    `GqlError` → (`DriverError` | `Neo4jError`)，而 `ServiceUnavailable`（连不上、
    认证失败、连接池耗尽）挂在 `DriverError` 那一支下——它**不是** `Neo4jError` 的
    子类。只按 `Neo4jError` 捕获会漏掉"连不上 7687"，而那正是这里最需要接住的一类。
    两个名字一起写还有一个好处：driver 5.x 里 `DriverError` 是 `Neo4jError` 的子
    类，这套写法跨两个大版本都成立，`requirements.txt` 的范围才有意义。
    """
    driver = _driver()
    _, _, _, database = _config()
    mode = _READ if read_only else _WRITE
    try:
        with driver.session(database=database, default_access_mode=mode) as session:
            yield session
    except GraphStoreError:
        raise
    except (Neo4jError, DriverError) as exc:
        raise GraphStoreError(f"图存储不可用：{type(exc).__name__}") from exc
    except OSError as exc:
        raise GraphStoreError(f"图存储连接失败：{type(exc).__name__}") from exc


@contextmanager
def _transaction(read_only: bool = False) -> Iterator[Any]:
    """一个事务：整块要么全成功要么全失败。

    `with session.begin_transaction(...)` 正常退出时提交、异常时回滚，所以
    `add_edges_bulk` 里"先建实体、再建边"的两条语句天然是一个原子单位——第二条
    失败时第一条建的节点也退回去，不会留下一批"看起来只是没连上"的孤立实体。

    事务超时设在服务端（`timeout=`），所以整批 `UNWIND` 也受 `QUERY_TIMEOUT_SECONDS`
    约束：一次稠密图上的失控游走不会把请求挂到天荒地老。
    """
    with _session(read_only) as session:
        with session.begin_transaction(timeout=QUERY_TIMEOUT_SECONDS) as transaction:
            yield transaction


def _resolve_kind(transaction: Any, name: str, kind: str) -> str:
    """算出这个实体的**实际落库类型**（见模块 docstring 的「空类型实体的复用」）。

    类型明确就直接用：`(name, type)` 是唯一键，精确 MERGE 不可能撞到别的类型。
    类型不明时先看同名节点最早那个的类型——复用它的类型，`MERGE` 才会命中同一个
    节点，而不是造一个空类型分身。
    """
    if kind:
        return kind
    record = transaction.run(CYPHER_FIRST_KIND, {"name": name}).single()
    if record is None:
        return ""
    return str(record["type"] or "")


def _upsert(transaction: Any, name: str, kind: str) -> int:
    """在**已有事务**内取回实体 id，没有就建。规则与 `_resolve_kind` 同一套。"""
    resolved = _resolve_kind(transaction, name, kind)
    record = transaction.run(
        CYPHER_UPSERT_ENTITY,
        {"name": name, "type": resolved, "now": _now()},
    ).single()
    # 走到这里还拿不到 id 只能是并发下节点被删了，属于不可恢复的错误。
    if record is None:
        raise GraphStoreError("实体 MERGE 之后读不到 id")
    return int(record["id"])


def upsert_entity(name: str, type: str | None = None) -> int:
    """写入（或取回）一个实体，返回它的**内部节点 id**。同名同类型幂等。

    `type` 为空**不等于**"新建一个无类型实体"：库里已有同名实体时直接复用它
    （理由见模块 docstring 的「空类型实体的复用」）。反过来，`type` 明确给出时按
    `(name, type)` 精确匹配，所以 `("甲", "人物")` 与 `("甲", "组织")` 仍是两个
    节点——这是唯一约束的契约，也是"苹果公司 vs 苹果"能被区分开的原因。

    `name` 归一化后为空时抛 `ValueError`：空名字落库后所有空名字的实体都会被唯一
    约束撞成一行，那是比"这次没抽到实体"严重得多的静默错误。

    返回值是 Neo4j 的内部 id（`id(e)`），`int`，可直接比较相等——调用方拿它做
    "两次 upsert 是不是同一个节点"的判断，与 SQLite 版的自增主键用途相同。
    """
    entity_name = normalize_name(name)
    if not entity_name:
        raise ValueError("实体名为空")
    with _transaction() as transaction:
        return _upsert(transaction, entity_name, _fold_type(type))


def add_edge(
    src: str,
    dst: str,
    relation: str,
    *,
    source_id: str,
    chunk_start: int,
    chunk_end: int,
    src_type: str | None = None,
    dst_type: str | None = None,
) -> bool:
    """写一条边；**没有落库就返回 `False`**（已在库里，或这条边本身没意义）。

    两种 `False` 都不是错误：同一份材料重跑入库、相邻块因重叠再抽到同一条边，
    都是正常情况，重复写不该让整次入库算失败。真正的错误（库不可写、连不上）照常
    抛 `GraphStoreError`（`OSError` 子类），让调用方计入 `failed`。

    自环（`src == dst`）直接不写：它在游走里永远走不到（路径里节点唯一，回到起点
    就被过滤掉了），落库只会让"边数"虚高，而边数是判断图建得怎么样的唯一总量指标。

    `src` / `dst` 是**实体名**而不是 id：调用方（抽取层）手上只有名字，逼它先查
    id 只会把一次事务拆成两次。两端 `MERGE` 与边的 `MERGE` 在同一条语句、同一个
    事务里完成，不会出现"边写进去了、实体没写进去"的中间态。

    `chunk_start` / `chunk_end` 原样落库——它们是下一轮回填证据高亮的唯一依据，
    在这里做任何"修正"（比如把 end 拉到句末）都会让高亮标错字。
    """
    src_name, dst_name = normalize_name(src), normalize_name(dst)
    phrase = normalize_name(relation)
    if not (src_name and dst_name and phrase) or src_name == dst_name:
        # 不是错误，是"这条边没有内容"：模型给了空字段或把一个东西连向自己，
        # 丢掉比落一条走不通的边好。
        return False
    if int(chunk_end) < int(chunk_start):
        raise ValueError("证据区间倒置：chunk_end < chunk_start")
    now = _now()
    with _transaction() as transaction:
        params = {
            "src": src_name,
            "dst": dst_name,
            # 空类型要先解析成实际类型，否则会造出一个空类型分身。
            "src_type": _resolve_kind(transaction, src_name, _fold_type(src_type)),
            "dst_type": _resolve_kind(transaction, dst_name, _fold_type(dst_type)),
            "relation": phrase,
            "source_id": str(source_id),
            "chunk_start": int(chunk_start),
            "chunk_end": int(chunk_end),
            "now": now,
        }
        summary = transaction.run(CYPHER_ADD_EDGE, params).consume()
    # 新建条数是 driver 从服务端计数拿的：比"在边上打标记"可靠，重复写时不会
    # 因为标记残留而误报 True，也不会在边上留下一次性属性。
    return summary.counters.relationships_created > 0


@dataclass(frozen=True)
class EdgeInput:
    """一条待写入的边。参数与 `add_edge` 一一对应，只是打包成一个值。

    为什么要有这个类型，而不是让 `add_edges_bulk` 收 `list[tuple]`：六个位置参数
    排在一起，`chunk_start` 与 `chunk_end` 调换位置不会有任何报错，结果是高亮标错
    字。有字段名，至少错得看得见。
    """

    src: str
    dst: str
    relation: str
    source_id: str
    chunk_start: int
    chunk_end: int
    src_type: str | None = None
    dst_type: str | None = None


def _prepare_edge(item: EdgeInput) -> tuple[str, str, str, str, int, int, str, str] | None:
    """归一化 + 校验一条边；无意义（空名 / 自环 / 空关系）返回 `None`。

    返回的 8 元组顺序固定：`(src, dst, relation, source_id, start, end, src_type,
    dst_type)`。与 `add_edge` 走同一套判断。两处不一致的结局是"单条写得进去、
    批量写不进去"或者反过来——这是排查成本最高的一类不一致。
    """
    src_name, dst_name = normalize_name(item.src), normalize_name(item.dst)
    phrase = normalize_name(item.relation)
    if not (src_name and dst_name and phrase) or src_name == dst_name:
        return None
    start, end = int(item.chunk_start), int(item.chunk_end)
    if end < start:
        raise ValueError("证据区间倒置：chunk_end < chunk_start")
    return (src_name, dst_name, phrase, str(item.source_id), start, end,
            _fold_type(item.src_type), _fold_type(item.dst_type))


def _load_candidates(transaction: Any, names: list[str]) -> dict[str, list[tuple[int, str]]]:
    """一次查出这批名字已有的节点，按名字分组、组内按时间与 id 升序。

    一次 `UNWIND` 代替 SQLite 版的"分批 IN（每批 400 个变量）"：Neo4j 的列表参数
    没有变量个数上限，一次入库的实体数没有上限也不需要分批。组内顺序由 Cypher 的
    `ORDER BY name, created_at, id` 定死——`_pick_existing` 在类型不明时取第一个，
    这里必须保证"第一个"就是最早那个。
    """
    found: dict[str, list[tuple[int, str]]] = {}
    if not names:
        return found
    for record in transaction.run(CYPHER_ENTITY_CANDIDATES, {"names": names}):
        found.setdefault(str(record["name"]), []).append(
            (int(record["id"]), str(record["type"] or "")))
    return found


def add_edges_bulk(edges: list[EdgeInput]) -> int:
    """一个事务写完一批边，返回**真正落库**的条数。

    Neo4j 的批量模式是 `UNWIND`：一次提交把整批行喂给一条语句，而不是循环发
    语句。这里一共三条语句（取候选 → 建缺失实体 → 建边），全部在同一个事务里，
    所以整批要么全成功要么全失败。

    **失败不留半张图**：整批先在事务外校验（空名 / 自环 / 区间倒置），任何一条
    倒置直接抛 `ValueError` 且一条都不写；进了事务之后任何异常都由
    `begin_transaction` 的上下文回滚，实体与边一起退回去。批量写入的原子性以
    "这一批"为界，不以"整份材料"为界——调用方（`graph_ingest`）按块调用，所以
    失败粒度仍是块。

    返回值与 `add_edge` 同口径：重复的边（`MERGE` 命中的）不计入，但它们**不是
    错误**（重跑入库、相邻块重叠都会产生重复）。条数来自
    `summary.counters.relationships_created`，与 SQLite 版 `executemany` 的
    `rowcount` 语义一致。
    """
    if not edges:
        return 0
    prepared: list[tuple[str, str, str, str, int, int, str, str]] = []
    for item in edges:
        row = _prepare_edge(item)
        if row is not None:
            prepared.append(row)
    if not prepared:
        return 0

    # 先把这批边涉及的所有 (名字, 类型) 收齐，实体才能一次写完——不然"先建实体
    # 再建边"就又是一批往返。
    wanted: dict[str, set[str]] = {}
    for src_name, dst_name, _phrase, _sid, _s, _e, src_kind, dst_kind in prepared:
        wanted.setdefault(src_name, set()).add(src_kind)
        wanted.setdefault(dst_name, set()).add(dst_kind)

    now = _now()
    with _transaction() as transaction:
        existing = _load_candidates(transaction, list(wanted))
        # (名字, 调用方传的类型) → 实际落库类型。类型不明的走复用，其余原样。
        resolved: dict[tuple[str, str], str] = {}
        missing: list[dict] = []
        for name, kinds in wanted.items():
            for kind in kinds:
                picked = _pick_existing(existing.get(name, []), kind)
                if picked is None:
                    resolved[(name, kind)] = kind
                    missing.append({"name": name, "type": kind, "created_at": now})
                else:
                    resolved[(name, kind)] = picked[1]
        if missing:
            transaction.run(CYPHER_UPSERT_ENTITIES_BULK, {"rows": missing}).consume()
        rows = [
            {
                "src": src_name,
                "dst": dst_name,
                # 用解析后的类型定位节点：`MATCH` 一定命中上一条语句保证存在的
                # 那个节点，不会因为类型为空而漏掉复用出来的实体。
                "src_type": resolved[(src_name, src_kind)],
                "dst_type": resolved[(dst_name, dst_kind)],
                "relation": phrase,
                "source_id": sid,
                "chunk_start": start,
                "chunk_end": end,
                "created_at": now,
            }
            for src_name, dst_name, phrase, sid, start, end, src_kind, dst_kind in prepared
        ]
        summary = transaction.run(CYPHER_ADD_EDGES_BULK, {"rows": rows}).consume()
    return int(summary.counters.relationships_created)


def entity_degrees(*, limit: int = 500) -> list[tuple[str, int]]:
    """按边数降序返回前 `limit` 个 `(实体名, 边数)`。`graph_resolve` 的候选源。

    **为什么按边数排序**：边少的实体合并价值低——一个只出现过一次、挂着一条边的
    名字，即使真的和别的名字同义，合并它给图带来的通路也只有那一条。反过来，这
    也顺手把 N² 的规模按住：实体上千的图上，真正值得两两比的只有头部那些。

    同名不同类型的两个节点按名字聚成一行（`CYPHER_ENTITY_DEGREES` 的隐式分组）：
    别名模型是名字级的，按 `(name, type)` 拆开会让同一个名字拿到两个不同的度数，
    而"该选谁当主名"只该有一个答案。

    读不到库（没配密码 / 连不上 / 超时）时返回 `[]`，与其他读接口一致。
    """
    cap = max(int(limit), 1)
    try:
        with _transaction(read_only=True) as transaction:
            records = list(transaction.run(CYPHER_ENTITY_DEGREES, {"limit": cap}))
    except (GraphStoreError, OSError) as exc:
        logger.warning("图谱实体度数不可用：%s", type(exc).__name__)
        return []
    return [(str(record["name"]), int(record["degree"])) for record in records]


def _load_alias_map(transaction: Any) -> dict[str, str]:
    """读全量别名表：`{别名: 主名}`。同一别名有多行时取 `CYPHER_LOAD_ALIASES` 排好
    的第一个——不这样做，`resolve` 的返回值会随返回顺序漂移。"""
    mapping: dict[str, str] = {}
    for record in transaction.run(CYPHER_LOAD_ALIASES):
        alias = str(record["alias"])
        canonical = str(record["canonical"])
        if alias and canonical and alias != canonical:
            mapping.setdefault(alias, canonical)
    return mapping


def _resolve_in(alias_map: dict[str, str], name: str) -> str:
    """在已读入内存的别名表上走链。**纯函数**，因此防环与跳数上限能单独测。

    两种情况返回**原样**而不是"走得最远的那个名字"：

      - 链成环（`甲→乙→甲`，只可能来自人手改库）：环上没有主名，随便挑一个都是在
        编造一个不存在的等价关系；
      - 超过 `MAX_ALIAS_HOPS` 跳：同上。

    两种都记 warning 但**不记名字**（这一层的日志原则是"只有形状，没有内容"），
    返回原样让调用方看到"这个名字没有被合并"，而不是看到一个半路停下的名字。
    """
    current = name
    seen = {name}
    for _ in range(MAX_ALIAS_HOPS):
        following = alias_map.get(current)
        if not following or following == current:
            return current
        if following in seen:
            logger.warning("别名链成环（%d 个名字），按「无主名」处理", len(seen))
            return name
        seen.add(following)
        current = following
    following = alias_map.get(current)
    if not following or following == current:
        return current
    logger.warning("别名链超过 %d 跳，按「无主名」处理", MAX_ALIAS_HOPS)
    return name


def _alias_group(alias_map: dict[str, str], name: str) -> list[str]:
    """同一实体的全部名字：`name` 自己、它的主名、以及所有最终指向该主名的名字。

    游走要从**组里每一个名字**出发：边可能挂在任何一个别名上，只从主名出发会漏掉
    挂在别名上的那些边——那正是"从简称问、返回空"的老毛病换了个位置。
    """
    canonical = _resolve_in(alias_map, name)
    group = {name, canonical}
    for alias in alias_map:
        if _resolve_in(alias_map, alias) == canonical:
            group.add(alias)
    return sorted(group)


def resolve(name: str) -> str:
    """顺着别名链找到主名；没有别名、链成环、链超长时返回自己（归一化后的）。

    库不可用时也返回自己，与读接口的降级口径一致：`resolve` 的返回值永远是一个
    可用的实体名，调用方不需要为"图没配"准备第二个分支。空名字返回空串。
    """
    entity = normalize_name(name)
    if not entity:
        return ""
    try:
        with _transaction(read_only=True) as transaction:
            alias_map = _load_alias_map(transaction)
    except (GraphStoreError, OSError) as exc:
        logger.warning("别名解析不可用：%s", type(exc).__name__)
        return entity
    return _resolve_in(alias_map, entity)


def add_aliases(candidates: list[MergeCandidate], *, source: str = "embedding") -> int:
    """把候选落成别名边，返回**真正新建**的条数。

    幂等：同一对重复写只留一条（`MERGE` 只认两端）。三件写前拒绝的事，都不抛错：

      - 两端归一化后为空、或两边同名（自环）；
      - 两端里有名字在库里找不到节点（候选本该来自图里，找不到说明图变过了）；
      - 会成环——判断是"从主名出发能不能走到别名"，能就说明加了这条边之后别名会
        绕回自己。同一批里的前一行也算数（`甲→乙` 之后再写 `乙→甲` 被拒）。

    被拒的与本来就存在的都不计入返回值，调用方用 `总对数 - 返回值` 就能得到跳过
    了多少——这个数比"写成功了几个"更能说明库里的实际情况。

    `source` / `similarity` / `created_at` 只在**新建**时落：改阈值重算一遍不该把
    上一次的依据覆盖掉（那也是唯一一条能回答"这条边是谁加的"的线索）。`source` 优先
    取候选自带的 `reason`（`substring` / `embedding`），拿不到才回退到参数——"这条
    别名凭什么加的"必须能从边上读出来，而不是只能翻日志。
    """
    if not candidates:
        return 0
    pairs: list[tuple[str, str, float, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        canonical = normalize_name(candidate.canonical)
        if not canonical:
            continue
        # 候选自带判定依据就按它落；手工构造（没有 reason）时回退到调用方给的 source。
        origin = str(getattr(candidate, "reason", "") or source)
        for raw_alias in candidate.aliases:
            alias = normalize_name(raw_alias)
            if not alias or alias == canonical or (alias, canonical) in seen:
                continue
            seen.add((alias, canonical))
            pairs.append((alias, canonical, float(candidate.similarity), origin))
    if not pairs:
        return 0

    now = _now()
    with _transaction() as transaction:
        alias_map = _load_alias_map(transaction)
        known = _load_candidates(
            transaction, sorted({name for pair in pairs for name in pair[:2]}))
        rows: list[dict] = []
        for alias, canonical, similarity, origin in pairs:
            # 两端都得是库里已有的节点：`MATCH` 落空时 Cypher 会安静地少写一条，
            # 那种"少了一条边"在返回值以外没有任何信号。
            if alias not in known or canonical not in known:
                continue
            if _resolve_in(alias_map, canonical) == alias:
                # 加了它，别名就能顺着链绕回自己。拒绝后写的那条，先写的那条留下。
                logger.warning("别名成环，跳过一条（表内 %d 条）", len(alias_map))
                continue
            alias_map[alias] = canonical
            rows.append({"alias": alias, "canonical": canonical, "source": origin,
                         "similarity": similarity, "created_at": now})
        if not rows:
            return 0
        summary = transaction.run(CYPHER_ADD_ALIASES, {"rows": rows}).consume()
    return int(summary.counters.relationships_created)


def clear_aliases() -> int:
    """删掉全部别名边，返回删除条数。**只动 `ALIAS_OF`**，实体与 `REL` 一条不碰。

    供"合并错了要退回去"用：别名边是这一层唯一一处"表示两个名字是同一个东西"的
    断言，清掉它，图就回到合并之前的样子（原始边从来没有被搬动过）。
    """
    with _transaction() as transaction:
        summary = transaction.run(CYPHER_CLEAR_ALIASES).consume()
    return int(summary.counters.relationships_deleted)


def _canonicalize_path(path: GraphPath, alias_map: dict[str, str]) -> GraphPath | None:
    """把路径里的名字全换成主名；换完出现重复节点就丢掉这条路径。

    合并之后 `甲` 与 `甲'` 是同一个东西，于是 `甲'→乙→甲` 这样一条路径在语义上
    是一个环；而 `GraphPath` 的契约是简单路径。宁可少返回一条，也不返回一条
    `nodes` 里有重复的路径——下游会把重复读成"两个不同的东西连着两次"。

    起点也换：调用方拿到的名字一律是主名，做跨材料的名字匹配时不需要再
    `resolve` 一次。没有任何别名时这个函数根本不会被调用（`neighbors` 里有短路），
    所以本轮的改动对没合并过的图是零影响。
    """
    mapped = [_resolve_in(alias_map, node) for node in path.nodes]
    if len(set(mapped)) != len(mapped):
        return None
    if mapped == path.nodes:
        return path
    return GraphPath(nodes=mapped, relations=path.relations, evidence=path.evidence,
                     directions=path.directions)


def _parse_path(record: Any) -> GraphPath | None:
    """把一条 Cypher 路径拆成 `GraphPath`；形状不对就整条丢掉。

    宁可少一条路径，也不返回一条 `relations` / `directions` / `evidence` 错位的
    路径：错位的后果是证据指向错误的关系、方向标到错误的跳，而且这种错误在界面
    上一眼看不出来。

    方向靠**起止点比对**读出来：`path.nodes[i]` 是 `path.relationships[i]` 的起点
    就是 `FORWARD`，是终点就是 `REVERSE`。无向游走会把关系反过来走，所以这一步
    不能省；比对用 `element_id` 而不是 `name`——同名不同类型的实体是两个节点，
    按名字比会把方向判成"总是正向"。
    """
    try:
        path = record["p"]
        nodes = [str(node["name"]) for node in path.nodes]
        relations: list[str] = []
        evidence: list[tuple[str, int, int]] = []
        directions: list[str] = []
        for index, relation in enumerate(path.relationships):
            relations.append(str(relation["relation"]))
            evidence.append((str(relation["source_id"]),
                             int(relation["chunk_start"]),
                             int(relation["chunk_end"])))
            starts_here = (relation.start_node.element_id
                           == path.nodes[index].element_id)
            directions.append(FORWARD if starts_here else REVERSE)
    except (KeyError, TypeError, ValueError):
        logger.warning("图谱路径格式异常，跳过该路径")
        return None
    if not nodes or len(nodes) - 1 != len(relations) or len(relations) != len(evidence):
        return None
    if len(directions) != len(relations):
        return None
    if any(direction not in (FORWARD, REVERSE) for direction in directions):
        # 只可能来自 `_parse_path` 自己的逻辑写错，但方向读错比路径少一条危险得多。
        logger.warning("图谱路径方向取值异常，跳过该路径")
        return None
    return GraphPath(nodes=nodes, relations=relations, evidence=evidence,
                     directions=directions)


def _path_key(path: GraphPath) -> tuple:
    """路径去重键：四项全等才算同一条。

    只看 `nodes` 会把"甲依赖乙"与"乙依赖甲"合成一条，那是两个不同的断言；只看
    `relations` 会把同名不同类型的两个起点合成一条，那就丢掉一条独立的证据。
    """
    return (tuple(path.nodes), tuple(path.relations),
            tuple(path.directions), tuple(path.evidence))


def neighbors(name: str, *, hops: int = 2, limit: int = 50) -> list[GraphPath]:
    """从 `name` 出发**无向**游走 `hops` 跳，返回最多 `limit` 条路径（短路径优先）。

    为什么无向见模块 docstring；方向没有丢，`GraphPath.directions` 逐跳记着这条
    边原本是顺着还是逆着走的，所以"从 B 出发走到 A"仍然能读出"原本是 A→B"。

    `hops` 与 `limit` 都**收在硬上限内**（`MAX_HOPS` / `MAX_LIMIT`）：不是洁癖，
    是可用性——变长路径在稠密子图上会跑出指数级的中间行。截断只发生在超限时，
    正常取值不受影响。

    返回的每条路径都是**简单路径**（节点不重复）。Neo4j 的变长模式默认允许节点
    重复（只保证关系不重复），所以语句里额外用 `all(... single(...))` 过滤——详见
    模块 docstring 的「环」。

    同名不同类型的实体（"苹果"公司 / 水果）会各自当作起点，若两者挂着内容完全
    相同的边，会拿到两条一模一样的路径——四项（节点、关系、方向、证据）全等的
    重复在这一层合并掉；只差方向的两条**不合并**，它们是"甲依赖乙"与"乙依赖甲"
    两个不同的断言。

    **别名**：起点先 `resolve`，并且从**同一别名组的每个名字**出发（边可能挂在
    其中任何一个上），返回前把路径里的名字统一换成主名——详见模块 docstring 的
    「实体消解」。没合并过的图上别名表为空，这条路径与改动前逐字节一致。

    库不可用（没配密码 / 连不上 / 超时）时返回 `[]`——查询侧不需要为"图没建好"
    准备第二个分支。
    """
    entity = normalize_name(name)
    if not entity:
        return []
    depth = _clamp_hops(hops)
    if depth == 0:
        # 0 跳的语义是"没有走任何边"，那就没有路径。返回起点本身会让调用方
        # 拿到一条零证据的"命中"，比返回空更糟。
        return []
    cap = _clamp_limit(limit)
    query = _walk_query(depth)
    try:
        with _transaction(read_only=True) as transaction:
            # 一次事务里读别名表再游走：别名边可能在两次调用之间被加进来，分两次
            # 事务会出现"起点组是按旧表算的、路径是按新图走的"这种半数一致。
            alias_map = _load_alias_map(transaction)
            starts = _alias_group(alias_map, entity) if alias_map else [entity]
            records = list(transaction.run(query, {"names": starts, "limit": cap}))
    except (GraphStoreError, OSError) as exc:
        logger.warning("图谱游走不可用：%s", type(exc).__name__)
        return []
    paths: list[GraphPath] = []
    seen: set[tuple] = set()
    for record in records:
        path = _parse_path(record)
        if path is None:
            continue
        if alias_map:
            path = _canonicalize_path(path, alias_map)
            if path is None:
                continue
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def stats() -> dict:
    """实体数、边数、孤立实体数、别名边数、被合并的实体数。

    孤立实体是**有实体没边**的那些：它单独看没意义，但数量和总数一起看能直接
    回答"这次抽取到底连上了没有"。全是孤立实体，说明关系抽取一路空转，图建了
    等于没建——这个信号比"实体数"本身有用。

    `aliases` / `merged_entities` 是这一轮加的：前者是 `ALIAS_OF` 的条数，后者是
    "有别名指向它"的主名个数。两个数一起看才有意义——一组三个名字写两条边，合并
    的是两个实体；`aliases > 0` 而 `merged_entities == 0` 只可能是库被改坏了。

    读不到库（没配密码 / 连不上 / 超时）时返回全 0，与"空图"同形：调用方看到的
    都是"还没有图"。
    """
    zero = {"entities": 0, "edges": 0, "isolated_entities": 0,
            "aliases": 0, "merged_entities": 0}
    try:
        with _transaction(read_only=True) as transaction:
            entities = transaction.run(CYPHER_COUNT_ENTITIES).single()
            edges = transaction.run(CYPHER_COUNT_EDGES).single()
            isolated = transaction.run(CYPHER_COUNT_ISOLATED).single()
            aliases = transaction.run(CYPHER_COUNT_ALIASES).single()
            merged = transaction.run(CYPHER_COUNT_MERGED).single()
    except (GraphStoreError, OSError) as exc:
        logger.warning("图谱统计不可用：%s", type(exc).__name__)
        return zero
    return {
        "entities": int(entities["n"]) if entities else 0,
        "edges": int(edges["n"]) if edges else 0,
        "isolated_entities": int(isolated["n"]) if isolated else 0,
        "aliases": int(aliases["n"]) if aliases else 0,
        "merged_entities": int(merged["n"]) if merged else 0,
    }
