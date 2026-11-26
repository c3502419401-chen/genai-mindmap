# 探针脚本与它周围那圈"该查没人查"的规矩

这个文件管一件事：`backend/scripts/` 下那批探针 / 临时脚本，以及它们的产物、
`docs/` 里指向它们的引用、`.env.example` 里那些开关——**怎么从"记得就跑"变成
"不跑就红"**。

## 起因：同一类 lint 漏了三次

| 次 | 位置 | 规则 | 当时的状态 |
|---|---|---|---|
| 1 | `tests/test_few_branches.py` | F401 未使用的导入 | 红过一段时间 |
| 2 | `scripts/_blind_pack.py:83` | F541 无占位符 f-string | 红过一段时间 |
| 3 | `scripts/_probe_join_rule.py:24` | F401 `READABLE_LABEL_CHARS` | 红过一段时间 |

三条都是一行就能改完的东西。共同点不是难，是**没人跑**：`ruff` 不在 CI 里，本地也
只在想起来的时候跑。所以治的不是那三行，是"没人跑"。

第 3 条已经在改动里删掉：`_probe_join_rule.py` 的判据只用到 `LABEL_JOINER`，
`READABLE_LABEL_CHARS` 在那个脚本里从写下来就没被用过（同目录的
`_probe_slash_labels.py` 与 `_structure_audit.py` 是真的在用它的，所以那是**一处**
没用，不是这个常量没人用——这两件事在 lint 报错里长得一模一样，得看代码才知道）。

## 规矩：探针文件分两类，按"是否被引用"判

| 对象 | 归属 | 保护 |
|---|---|---|
| 脚本 `scripts/**/*.py` | **留在 repo** | 必须能加载 + import 目标必须还在 |
| 产物 `scripts/**/_*.json` | **默认不进 repo** | 已跟踪者必须被 `docs/` 引用 |

两条没采用的做法，以及为什么不：

- **用完就删**：`docs/` 里有二十多处把这些脚本当"复算入口"引用
  （`LABEL-JOIN-ADMISSION.md` 引用 `_probe_join_rule.py` / `_probe_replay_collapse.py`，
  `ACCEPTANCE-METRICS.md` 引用 `_structure_audit.py` / `_score_cards.py`，
  `LATENCY-BREAKDOWN.md` 引用 `_probe_latency.py`）。删掉脚本，那些数字就变成
  不可复算的断言。
- **给它们配一套宽松的 ruff 规则**：宽松规则意味着它们**不再被 lint 保护**。探针
  脚本的腐化速度不比产品代码慢——它们跟着 `app/**` 的重构漂，而且没有用例钉住。
  这等于"为了不红而把检查关掉"，是这类问题的常见死法。

## 自动生效的四个入口

不是"以后注意"，是四处都会红：

1. **`.github/workflows/ci.yml`**：`Lint（ruff）`（版本钉 `0.9.10`）+ `探针脚本卫生`
   两步。ruff 版本必须钉：浮动版本会让一次无关升级把历史代码判红，然后人们学会
   无视这条检查——那比没有检查更糟。
2. **`backend/tests/test_script_hygiene.py`**：一条用例跑真仓库全部五档，本地
   `pytest` 就红，不必记得跑脚本。另外 12 条是"造一个违规样本，证明这档真的会红"
   ——一个从不失败的检查等于没有检查。
3. **`backend/scripts/check_script_hygiene.py --gate`**：五档，见下。
4. **`.gitignore`**：`backend/scripts/_*.json` + `.ruff_cache/`。七个原本挂在
   `git status` 里的游离产物（`_gen_repair.json`、`_layout_clarity_sample.json`、
   `_branch_digest_wide.json` 等，全是 `measure_*.py` 的 `DEFAULT_OUT`）现在被覆盖；
   `_selfcheck_once.json` 与 `_selfcheck_sliced.json` 留作例外，因为
   `docs/SUBAGENT-VS-SELFCHECK.md` 把它们当留档证据引用。

## 五档检查

| 档 | 判据 | 实测基线 |
|---|---|---|
| `load` | 探针脚本（`_*.py` / `probe_*.py`）逐个在**独立子进程**里 `runpy` 加载，`run_name` 不是 `__main__` | 43 个全过 |
| `imports` | 全部脚本的 `import` 目标：模块 `find_spec` 存在，`from app.x import y` 的 `y` 存在 | 121 个全过 |
| `docs` | `docs/**/*.md` 里指向 `scripts/` 下 `.py` 的引用必须落得到文件 | 83 处全过 |
| `artifacts` | `scripts/**/_*.json`：已跟踪者必须被 docs 引用；未跟踪者必须被 `.gitignore` 覆盖 | 9 个全过 |
| `env` | `.env.example` 的变量在 `app/` 与 `scripts/` 里还找得到 | 23 个全过 |

三条判据上的取舍值得单独写下来，因为它们都属于"写错了就会变成噪声、然后就没人看"：

**`load` 必须开子进程。** 实测：同一进程里顺序 `runpy.run_path` 这 121 个脚本会
**挂死**（有脚本顶层就要连网络），而且一个 `os._exit` 会把整轮检查带走。子进程 +
20 秒超时是唯一能收敛的形态。`run_name` 故意不是 `__main__`：探针脚本的 `main()`
会真的花模型额度。

**`imports` 补的是 ruff 的结构性盲区。** pyflakes 只做**文件名内**的检查：
`from app.optimization.repair import WHATEVER` 里 `WHATEVER` 不存在，它不报（它只在
你**没用**这个导入时报 F401）。探针脚本腐化的主要形态恰好是"`app/**` 改了名，脚本
还指着旧名字"，也就是 ruff 永远抓不到的那一类。符号存在性只对 `app.*` 做：第三方库
大量用模块级 `__getattr__` 做惰性导出，静态 `getattr` 会误报。

**`env` 是单向的。** 只查"文档写了、代码里没有"。反方向实测 59 条
（`MINDMAP_SECTION_SLICE`、`MINDMAP_NODE_POOL`、`MINDMAP_KEEP_BY_SCORE` 这类行为与
实验开关，大多不该交给部署的人去设），要查就得维护一份 59 条豁免名单——那正是
`find_unwired` 基线那套成本，而它拦的东西不会让任何东西坏。单向这一侧零噪声。

## 一类已经修掉的误报（写下来免得下次重犯）

- **BOM**：`scripts/audit_self_check.py` 与 `scripts/measure_external_rank.py` 带
  UTF-8 BOM。Python 执行它们没问题（PEP 263 跳过），但按 `utf-8` 解成 `str` 之后
  BOM 是一个 U+FEFF 字符，`ast.parse` 会当它非法。读源码要用 `utf-8-sig`。
- **`try: import 可选依赖`**：`measure_cross_map.py` 这样 import `pymilvus`（19 个
  传递依赖、75 MB，故意不进 requirements）。要求"模块必须存在"等于要求可选依赖变硬
  依赖，所以 `try` 块内的 import 一概跳过。
- **`docs/research/`**：存的是**别人仓库**的 README 快照，里面那些 `scripts/` 开头的
  路径指的是那个项目的文件，不是本仓库的。
- **`.json` 不查**：文档里的 `scripts/_xxx.json` 绝大多数是
  `--save scripts/_xxx.json` 这种**生成路径**，是"跑完之后会有"，不是"现在就该在"。
  只查 `.py`。

## 还有多少同类：一份带判据的清单

这件事不是"ruff 该进 CI"这一条。下面按**判据是否确定**分档——这一列比"重要不重要"
更能决定该不该进 CI，因为判据不确定的检查加进去只会变成豁免名单。

**A. 已经闭环（本次）**

| 事 | 判据 | 在哪 |
|---|---|---|
| ruff | 确定性 | CI |
| 临时产物落库 | `git check-ignore` + docs 引用，确定性 | `artifacts` 档 |
| docs 引用的脚本存在 | 路径存在，确定性 | `docs` 档 |
| 脚本能不能加载 / import 目标还在 | 子进程加载 + AST，确定性（有超时） | `load` / `imports` 档 |
| `.env.example` 的开关有没有人读 | 字符串字面量集合，确定性（单向） | `env` 档 |

**B. 判据确定、值得进 CI，但这次没做**

1. **`requirements.txt` 与实际 import 的第三方包对不上**。判据：`ast` 取
   `app/**` 的顶层第三方 import，包名归一化（`python-multipart` → `multipart`、
   `Pillow` → `PIL`）后与 requirements 求差集。它抓的是"部署时装不到"——一类
   只在别人的机器上发生、本地永远复现不了的失败。成本：一份别名映射表（十几行），
   噪声可控。
2. **前端「导出的 API 函数有没有 UI 调用」**。后端有 `find_unwired --gate`，前端没有。
   实测：`client.ts` 导出 `deleteLibrarySource`，调用点只有 `App.vue` 一处——这一次
   是接上的，但没有任何东西阻止下一个人加一个没人调的封装。成本中等（要做
   `ts` 侧的 `ast` 或正则 + 基线），收益与后端那份一样。
3. **`data/` 与库文件的增长**。`library.prune` 写好了但不自动调用（`WIRING-GAPS`
   §13.3 记为"不算缺口"，理由是最坏情况可控）。判据可以确定（条数 > 阈值），
   但正确处置是运维决策，不是 CI 该拦的。**降级为"值得有一条健康检查，不值得进
   CI"**。

**C. 不值得进 CI**

4. **`docs/` 里的数字能不能复算**。`PLAN-VS-IMPLEMENTATION.md` 自己定的规矩是
   "凡是引用文档数字，必须指到复现脚本"——但"数字对不对"要真跑模型、真花额度，
   不是确定性判据。能查的只有"那个脚本还在不在"，那已经是 `docs` 档。
5. **`.env.example` 反方向（代码里有、文档没写）**。59 条，见上。
6. **commit message 前缀**（`fix:` / `feat:`）。判据确定，收益低，且容易变成形式
   主义检查——那种检查被绕过的方式是写一个符合格式的空提交。
7. **单独的前端 `type-check` 步骤**。看起来像漏了，其实 `npm run build` 就是
   `vue-tsc --noEmit && vite build`，CI 的 `Build` 步骤已经覆盖。**这条写在这里是
   为了下次不要再判它一次**。

判据那一列的含义：A 里五条都是"跑一遍就知道对错"；B 里第 1、2 条同样确定，只是
成本还没付；C 里每一条都需要人读一遍才知道对不对——那种检查加进 CI 的结果，是
半年后有一份没人敢改的豁免名单。

## 跑法

    cd backend
    python scripts/check_script_hygiene.py              # 全跑，打印
    python scripts/check_script_hygiene.py --gate       # 闩门：有违规退出码 1
    python scripts/check_script_hygiene.py --only env   # 只跑一档

    python -m ruff check .                             # lint（CI 里钉 0.9.10）
