# 薄材料上骨架只给两条：现在怎么处置、复跑到了什么

2026-11-28。这条改动本身早就在（`docs/OFF-TOPIC-AND-FEW-BRANCHES.md` 记的是病灶，
`tests/test_few_branches.py` 记的是判据），缺的是**一份把"跑过了"写成数的话**。
这一篇补的是那句，顺便把"这次到底复跑到哪一层"划清。

---

## 一、病灶一句话

骨架那一次只看到"主题 + 文件?"，材料只有三四百字、又只有两块标题时，模型就照着
那两块写两条一级分支。而 `MIN_BRANCHES = 3` 是一条**结构下限**（两条不成图），于是
整次生成判废，用户拿到的是一张错误卡片。验收里那三条立项材料的 `too_few_branches`
→ 重试 → `too_many_failures` 就是这么来的。

## 二、补丁在哪（三处，缺一处都不成立）

| 处 | 做什么 | 位置 |
|---|---|---|
| 上游：目录要认得真实材料 | `一、标题` 紧贴正文也算章节；正文列表项不算 | `tools._outline_entries` |
| 中游：目录比下限窄时取消"优先照它写" | `OUTLINE_FEW_BLOCKS_NOTE` 拼在目录行之后 | `tools._render_outline` / `tools.py::OUTLINE_FEW_BLOCKS_NOTE` |
| 下游：少于下限的三种结局 | 纯主题判死；有材料先**带证据**重问一次；重问仍少则**交树 + `BRANCHES_BELOW_MIN`** | `tools._generate`（"一级分支太少"那一段）+ `validate_generated_content(enforce_min_branches=...)` |

第三处是"补丁"这个词最容易被误读的地方：它不是把门槛拆掉，而是**把失败拆成两档**。
纯主题仍然判死（`too_few_branches`），只有"有材料"这一档才降级交付，并且每次都以一条
warn 诊断记账。`enforce_min_branches=not branches_degraded` 是为了让降级路径走得到出口
——否则出口用同一条下限再判一次死，中间那段代码等于不存在。

## 三、复跑到了什么（`scripts/_probe_thin_material_branches.py`）

跑法：

```
cd backend
python scripts/_probe_thin_material_branches.py
```

它做两件不依赖模型的事。

### 1. 目录侧：20 条真实验收材料，骨架那一次看得见几块

```
S08–S14（会议纪要）  4–6 块，覆盖率 0.82–0.89
S15–S20（立项材料）  6–7 块，覆盖率 0.96
S01–S07（课程大纲）  0 块，覆盖率 0.00（版式不吃 `一、` 这套编号，走"按主题归纳"那条）
合计 20 条，目录少于 3 块（会补 `OUTLINE_FEW_BLOCKS_NOTE`）的有 0 条
```

**这就是"修好了"的直接证据**：当初判死的那批立项材料，现在骨架能看见 6–7 块、
覆盖率 0.96，`OUTLINE_FEW_BLOCKS_NOTE` 一次都不触发——它不再是靠"补那句提醒"救回来的，
而是上游的目录本身就能看见结构。那句话现在只服务真正的两块材料（比如那两条制度要点）。

课程大纲那 7 条 0 块不是缺陷，是版式：它们不用 `一、` 编号，`_outline_entries` 的
判据本来就不认；那 7 条的树反而是全批最稳的（4–5 支），因为"按主题归纳"这条路够用。

### 2. 骨架侧：三种结局用同一套代码路径跑一遍

```
① 纯主题 + 2 条                 ok=False  code=too_few_branches  骨架调用=1  一级分支=0  BRANCHES_BELOW_MIN=无
② 薄材料 + 先 2 条、重问 6 条    ok=True   code=-                骨架调用=2  一级分支=6  BRANCHES_BELOW_MIN=无
③ 薄材料 + 两次都 2 条           ok=True   code=-                骨架调用=2  一级分支=2  BRANCHES_BELOW_MIN=有
```

三行与用例一一对应（`test_two_branches_without_material_still_fails` /
`test_two_branches_with_material_is_retried_with_evidence` /
`test_two_branches_twice_is_delivered_with_a_diagnostic`）。① 的"骨架调用=1"也是判据：
纯主题不重问——没有材料证据可以换，同一段 user 再发一次是同一个答案。

脚本里有一处必须做的准备：**把本机的开关全部压回"什么都没开"**（`_clean_env`）。
第一次跑的时候 `MINDMAP_EXTERNAL_KNOWLEDGE` 是开的，外部检索真的发了外网请求并补进来
7 份材料，于是"①纯主题"那一格跑成了 `ok=True` —— 一个**把结论反过来的假阳性**。
用例那边由 `conftest.clean_env` 统一负责，脚本没有那层。

## 四、这一轮**没有**复跑到的一层

**真模型侧没有复跑**：本机 `.env` 里只有 `DASHSCOPE_API_KEY`（语音转写用），没有
`MINDMAP_LLM_API_KEY`，所以"模型面对这 20 条真材料会不会只给 2 条"这件事，今天没有新数据。

已有的真模型数据是**改之前**的（`docs/OFF-TOPIC-AND-FEW-BRANCHES.md` 里那三条判死的
立项材料）。今天能说的是：**上游的判据变了**（目录看得见 6–7 块），而"模型会不会照它写"
仍属未测。要补这一层，配一个 `MINDMAP_LLM_API_KEY` 后跑一次
`scripts/measure_acceptance.py`（它会对 `acc_samples.json` 那 20 条真生成），再对这一篇
加一节"真模型侧"。

**别把这一篇读成"缺陷已修、且已验证到底"**：已验证的是判据与代码路径（用例 + 上表三行），
未验证的是模型的反应。两者混起来读，下一轮又会有人以为这里已经结案。

## 五、顺带确认的一件旧事

`OUTLINE_MIN_COVERAGE = 0.6`、`MIN_BRANCHES = 3` 两个数没有动过。这一轮复跑它们仍然
在同样的位置生效：覆盖率不足的目录整段不进 prompt（S01–S07 那 7 条），条数不足的
按三档处置。门槛没有被这次改动悄悄降低。
