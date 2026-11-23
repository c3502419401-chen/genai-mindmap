# 观察 → 命题：两级压缩（只做"记"，不做"学"）

依据：Coco 论文（arXiv:2611.13457）的记忆骨架。落地基线 commit `37f1ab3`。
起点数据（实测，2026-12）：

```
memory_items       0 条
memory_sessions   10 条，全部 status=empty
edit_events      565 条：update 559 / style 4 / remove 2；add / move / edge 0 条
runs              93 条（create 61 / edit 18 / expand 3，其余失败与取消）
```

---

## 0. 借什么、不借什么

| 论文的部件 | 这里 | 为什么 |
| --- | --- | --- |
| 观测（observation） | `memory_observations` 表 | 借。原始形态是"桌面截图 + 键鼠"，这里换成 `runs` / `edit_events` 的结构化事件 |
| 两级压缩（观测 → 命题） | `memory_propositions` + 边表 | 借。这是全套里唯一"现在就能成立"的部分 |
| 修订（IDENTICAL / SIMILAR / UNRELATED + consolidation） | `propositions.revise` | 借。见 §3 |
| 双路检索（时间 + 语义，RRF） | `memory/recall.py` | 借。见 §4 |
| 隐式反馈学习（Coco-PE / Coco-LoRA） | **不做** | 见 §5 末。个位数的真实反馈都没有，学出来无法与"没学"区分 |
| 常驻 Observer 按时间片跑 | **不做** | 2C2G 跑不起常驻进程，而且这里没有截图流。换成"事件片"：run 结束时归纳一次 |
| 第三层偏好（叠加到 `prefs/style` / `prefs/profile`） | **不做** | 那两层已经有，再叠一层会让"这个风格是哪一层决定的"没有答案 |

---

## 1. 一次观测是什么

**定义**：一次观测 = 一个动作边界上的全部行，由两条判定给出。

| 类别 | 单位 | 依据 |
| --- | --- | --- |
| run 观测 | `runs` 的一行 | 有唯一 `run_id`、明确起止、一个 `goal`。用户的"一次要求"在库里只表达一次，不可再分 |
| edit 观测 | 同一 `map_id` 上 `at` 相同的一批 `edit_events` | `at` 是前端在**一次保存**时统一打上的动作时刻 |

**凭什么这样切（实测，不是感觉）**：那份库的 565 条编辑事件只有 **4 个不同的 `at`**，
而 `recorded_at` 有 16 个——入库时刻被网络与批处理打散（12:23:34.567 / .569 / .571）。
只有 `at` 保持了"这一次动作"的边界，所以切分键必须是它。

后果是量级上的：那 550 条同 `at` 的 `update` 按动作算是 **1 条观测（`stat=550`）**，
按行算是 550 条内容雷同的观测。后者会把一条命题的支撑数灌到 550，而"支撑数"是
置信度的来源，被灌水之后它就不再有意义——库里看起来却完全正常。

**为什么不是"同一 map、时间相近"**：那需要一个阈值，而阈值在这里没有独立依据（实测
两次保存的间隔从 0 毫秒到 107 秒都出现过）。用 `at` 相等来切不需要阈值——同一次保存
的时刻是**事实**，不是需要猜的参数。跨保存的合并已经由 `memory_sessions` 那一层负责
（`summarize.GAP_MINUTES`），在观测层再定义一次只会有两个不一致的答案。

**ChatDock 的提问**不需要第三类：它每次都落成一次 run（`runs.request.goal` 就是那句话），
run 观测已经覆盖它。

---

## 2. 命题存哪，出处怎么回指

`memory_items` 的列**一个都没改**：命题的正文与向量复用它的 `text` / `text_hash` /
`vector`（去重与余弦都是现成的），档位用 `kind='proposition'`。

| 论文 | 这里 |
| --- | --- |
| `proposition` | `memory_items.text`（`kind='proposition'`） |
| `reasoning` | `memory_propositions.reasoning` |
| `confidence` 1–10 | `memory_propositions.confidence` |
| `decay` 1–10 | `memory_propositions.decay` |
| `observation_ids` | `memory_proposition_observations`（两列，**二部图的边**） |

`decay` 与 `store.HALF_LIFE_DAYS` **不是一回事**：后者是按天连续下降的时间衰减（"这条
记忆有多旧"），前者是模型对这个判断**适用期**的分档（1 = 只适用于这段上下文，10 = 持
久偏好），不随时间变。两者都影响排序，但只有前者会动。`persistence(decay)` 是后者的
唯一映射：`0.5 + 0.5 * (decay-1)/9`，端点 0.5 / 1.0。

**回指是三段链，每一段都是主键**：

```
命题 item_id → 边表的 observation_id → memory_observations.refs
             → {"run_ids": [...]} 或 {"event_ids": [...]}
             → runs.run_id / edit_events.id
```

三道闸保证它不是"我总结了一段话，出处是'用户习惯'"：

1. **模型只能选编号**。输入里的观测带 `[o1] [o2]` 前缀，编号↔真实 id 的映射留在本地；
   输出里出现候选集之外的编号，丢弃那一个编号（不丢整条命题）。
2. **新建命题至少两条观测**。一条观测支撑的是复述，不是归纳。这条只在新建时强制——
   挂到已有命题上的新观测可以只有一条。
3. **`observations.resolve_refs()` 当场核对**。给一批观测 id，回 `run_ids` /
   `event_ids` / `missing`，直接拿去两张原始表里数行。

实测：库上跑完，11 条命题里有一条的出处是 **563 行真实 `edit_events`**，其余是
1–20 行真实 `runs`。全部命中，`missing` 为空。

---

## 3. 修订：怎么判 SIMILAR，观测集怎么并

| 判定 | 判据 | 动作 | 代价 |
| --- | --- | --- | --- |
| IDENTICAL | `text_hash` 命中已有命题 | 只把新观测加进边表，正文与向量都不动 | 零模型调用 |
| SIMILAR | 与最近邻命题余弦 ≥ `SIMILAR_COSINE`（0.60） | consolidation（一次模型调用） | 一次调用 |
| UNRELATED | 其余 | 新建一条 | 零模型调用 |

**0.60 的来历与方向**：`store.MIN_COSINE` 的注释里有这份库的实测区间——同语境不同事
0.2–0.35，同一句话的两种说法 ≥0.45。命题更短更凝练，共享虚词比例更高，余弦会虚高。
两个方向的错判代价不对称：

- 判低（其实该合并，成了 UNRELATED）→ 多一条近义命题，占一个名额，**看得见、可以删**；
- 判高（其实不该合并，成了 SIMILAR）→ 两个不同判断被合成一条，旧的下线，**不可恢复**。

不可恢复贵得多，所以阈值往上取。0.60 是"明显是同一件事"的下界，不是"可能相关"的
下界——后者是 `MIN_COSINE` 管的事。

**consolidation 只有一个形状**：`{"propositions": [{..., "replaces": ["p1"]}]}`。
rewrite（一条换一条）、merge（一条换多条）、split（多条换一条）都是它的特例，所以只有
一条代码路径。

**观测集怎么并——不由模型决定**：

```
新命题的观测集 = 模型给的编号 ∪ 被 replaces 的那几条命题的全部旧边 ∪ 新命题自己的观测
```

代码强制（`_apply_consolidation`）。摘要改写不能丢证据：少一条边，新命题的置信度就建立
在更少的证据上，而它看起来与原来一样权威。

**已知的粗粒度**：split 时两条新命题都继承全部旧边。模型看不到旧观测的编号，无法做更细
的分配。取舍方向：支撑集偏大可以审阅、可以删边；丢证据不可恢复。

**修订是"新增 + supersede"，不是原地 UPDATE**：旧命题 `status='superseded'` +
`superseded_by` 指向新命题，新命题是新的两行。三条理由——`text_hash` 唯一索引不允许原地
改正文；用户要能看到系统改了什么（论文 Appendix A 的第一条 limitation）；把
`superseded_by` 置空就能回滚。

**降级方向**（模型挂 / 输出坏 / 落库失败）：把观测挂到**最近的候选**上，正文不动，
`degraded=True`。第一版实现是"退回新建"，被用例打回来了——SIMILAR 场景下新观测常常只有
一条（其余出处已在旧命题上），而新建要求两条，于是这条观测在 `mark_consumed` 之后再也
看不到。那伪装成了一次保守的降级。

---

## 4. 双路检索：是不是 RRF，参数怎么定

**是 RRF**，`1/(K + 名次)` 累加，与 `knowledge/service.py` 那套同一个常数。

| 常数 | 值 | 依据 |
| --- | --- | --- |
| `RRF_K` | 60 | 与 `knowledge.service.RRF_K` 同值（原论文经验值）。有一条用例断言两者相等 |
| `ROUTE_K` | 8 | 每路进融合池 8 条。池子等于最终条数时，第 7、8 名永远进不来，融合退化 |
| `HYBRID_TOP` | 6 | 与 `store.TOP_K` 同值：6 × 一行摘要 ≈ 400 token |

**为什么不能把两个分数加权**：时间路根本没有分（`at` 是硬序，只能给名次），语义路是
余弦 × 持久性权重。任何线性组合都要先编一个换算系数，那个系数没有依据，而且会随库变大
漂移（库越大，第 8 名的余弦越接近第 1 名）。RRF 只吃名次，对这些免疫。

**不做保底席位**：`knowledge.service._fuse` 给文本路保底半数席位，根据是"用户这次带来的
材料比库里的更该被引用"。观测与命题之间没有这种优先级——最近的观测不一定比一条稳定判断
更有用。同分时命题优先（跨事件的归纳比一次动作信息密度高）。

两路的原始命中都留在 `Bundle` 里：`semantic_available=False` 有三种原因（没配向量服务 /
查询向量算不出 / 库里没有带向量的命题），它们在外部表现上一样，要能分开。

---

## 5. 自评：现在能产出什么

**不是 0 条。** 在真实库上跑了一次（`scripts/build_propositions.py`）：

```
观测   99 条（93 run + 6 edit，来自 93 次 run 与 565 条编辑事件）
命题   11 条有效 + 2 条被 supersede
边    100 条
模型   3 次调用（40 + 40 + 19 条观测分批）
```

11 条举例（原文）：

- 「他会在同一主题上反复重开新图，而不是在旧图上继续改。」支撑 9 条观测 → 9 行 run
- 「他遇到失败不会换思路，而是原样重发同一条指令直到成功。」支撑 18 条观测 → 18 行 run
- 「他会亲自做大量细粒度的图上编辑（update、style、remove 等）」支撑 4 条 → **563 行 edit_events**

**最少需要多少真实事件**（实测，不是论证）：造三个规模的合成库跑同一套提示词——

| 观测数 | 命题数 | 内容 |
| --- | --- | --- |
| 2 | **0** | 两条几乎相同的记录看不出判断（模型返回空数组，合法结论） |
| 4 | 2 | "只给主题加图型、不附结构要求"；"同一句指令反复生成，第一次多半只是试看" |
| 8 | 2 | 同上，措辞更完整，支撑数更高 |

所以：代码下界是 2 条观测（`MIN_INPUT_OBSERVATIONS` / `MIN_SUPPORT_FOR_NEW`），**实测下界
约 4 条同类观测**（≈ 4 次 run）。这份库有 93 次 run，早就过了下界——它卡住的从来不是
阈值，是**观测的密度与配比**。

**一条必须说清的话**：那 563 行 `edit_events` 的构成可疑。550 条 `update` 共享同一个
`at`（12:24:14.174）、覆盖 550 个不同节点、全是 `label` 改动、全部落在 234 毫秒里——
那是批量操作（脚本或一次性替换），不是一个人手工改图。所以"他会亲自做大量细粒度编辑"
这条命题的证据**性质**可疑：它可能是一个自动化动作，而不是习惯。这正是"数据不够时不该
学"的另一面——命题能建起来，不等于证据可信。

---

## 6. 已知缺口（写出来，不让下一个人重新发现）

1. **"要求 → 反应"配不上对**。`runs` 表没有 `map_id`，`edit_events` 有 563/565 行的
   `run_id` 是 NULL。于是"他对这次生成的反应"无法挂到那次生成上，两类观测只能各自独立。
   要补这个，得让保存接口把 `run_id` 带上（前端知道当前那棵树是哪次生成的），那是另一条线。
2. **split 的支撑集偏大**（见 §3）。
3. **`RRF_K` 是两处常量**（`recall.RRF_K` 与 `knowledge.service.RRF_K`），靠一条用例钉住
   相等。不共用 import 是因为 `knowledge.service` 会拉起 BM25 / rerank / 图检索一整条链。
4. **孤儿边**：没有外键（与 `edit_events` 同一条理由），删掉的 `memory_items` 会留下边。
   读的地方一律 JOIN 进来所以读不到，但 `COUNT(*)` 会偏大。`propositions.prune_orphans()`
   能清。
5. **不做隐式反馈学习**。论文的第三段（Coco-PE / Coco-LoRA）要几百条偏好对，这份库连
   `add` / `move` 事件都是 0 条。而且论文 Appendix A 的第一条 limitation 就是"用户想看到、
   改正、忘掉系统学到的东西"——在"可选阅"解决之前做"自动学"，方向是反的。

---

## 7. 怎么跑、怎么验

```bash
cd backend
python scripts/memory_corpus_report.py      # 只读：这份库上这条链有什么素材
python scripts/build_propositions.py --dry-run   # 只抽观测，不调模型
python scripts/build_propositions.py        # 抽观测 + 归纳（要模型配置）

python -m pytest -o addopts="" -q tests/test_memory_observation.py \
    tests/test_memory_proposition.py tests/test_memory_hybrid.py
```

自动入口在 `summarize.schedule()` 起的后台线程里：一次 run 开始时判定"上一段工作结束
了"，先跑会话总结，再抽观测 + 归纳。开关 `MINDMAP_MEMORY_PROPOSITIONS`（默认开，只认
显式的 `0/false/no/off`）。
