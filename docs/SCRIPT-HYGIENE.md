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
2. **`backend/tests/test_script_hygiene.py`**：一条用例跑真仓库全部六档，本地
   `pytest` 就红，不必记得跑脚本。另外 18 条是"造一个违规样本，证明这档真的会红"
   ——一个从不失败的检查等于没有检查。
3. **`backend/scripts/check_script_hygiene.py --gate`**：六档，见下。
4. **`.gitignore`**：`backend/scripts/_*.json` + `.ruff_cache/`。七个原本挂在
   `git status` 里的游离产物（`_gen_repair.json`、`_layout_clarity_sample.json`、
   `_branch_digest_wide.json` 等，全是 `measure_*.py` 的 `DEFAULT_OUT`）现在被覆盖；
   `_selfcheck_once.json` 与 `_selfcheck_sliced.json` 留作例外，因为
   `docs/SUBAGENT-VS-SELFCHECK.md` 把它们当留档证据引用。

## 六档检查

| 档 | 判据 | 实测基线 |
|---|---|---|
| `load` | 探针脚本（`_*.py` / `probe_*.py`）逐个在**独立子进程**里 `runpy` 加载，`run_name` 不是 `__main__`；**跑在从 `git ls-files` 物化出来的干净树里** | 46 个全过 |
| `imports` | 全部脚本的 `import` 目标：模块 `find_spec` 存在，`from app.x import y` 的 `y` 存在 | 129 个全过 |
| `deps` | `scripts/` 的 import 在 **CI 上装得出来**吗：平台专属模块（`winreg` 等）不在 `try` 里就红；第三方模块不在 `requirements.txt` 直接声明里、也不在白名单里、且不在 `try` 里就红 | 1553 处全过 |
| `docs` | `docs/**/*.md` 里指向 `scripts/` 下 `.py` 的引用必须落得到文件 | 101 处全过 |
| `artifacts` | `scripts/**/_*.json`：已跟踪者必须被 docs 引用；未跟踪者必须被 `.gitignore` 覆盖 | 9 个全过 |
| `env` | `.env.example` 的变量在 `app/` 与 `scripts/` 里还找得到 | 23 个全过 |

## `load` 跑在哪棵树上：这条闸连红五次的教训

2026-11 的战绩：`7ac8400` / `f8bbc0a` / `b540fd8` / `00a623b` / `157f0b5` **五次全红**，
而本地每次都是绿的。成因一句话：`backend/scripts/_two_arch_*.py` 六个脚本在**模块顶层**
读 `backend/data/two_arch.json` 与 `backend/data/two-arch-blind/_key.json`，而
`.gitignore` 的 `data/` 让干净 checkout 上根本没有这个目录。

    $ cd backend && python scripts/check_script_hygiene.py --gate     # 本地：全部通过
    $ git worktree add ../gm-cd HEAD --detach && cd ../gm-cd/backend
    $ python scripts/check_script_hygiene.py --gate
    ✗ load      6 条
        scripts/_two_arch_check.py     ← FileNotFoundError: .../backend/data/two_arch.json
        scripts/_two_arch_coverage.py  ← FileNotFoundError: .../backend/data/two_arch.json
        scripts/_two_arch_key.py       ← FileNotFoundError: .../data/two-arch-blind/_key.json
        scripts/_two_arch_numbers.py   ← FileNotFoundError: .../backend/data/two_arch.json
        scripts/_two_arch_peek.py      ← FileNotFoundError: .../backend/data/two_arch.json
        scripts/_two_arch_rows.py      ← FileNotFoundError: .../data/two-arch-blind/_key.json

五次都不是代码难改，是**判据取错了树**：闸门在"有 `data/` 的开发树"上取结论，而它
要回答的问题是"CI 上能不能加载"。

所以修的是闸门，不只是那六个脚本：

- `load` 档**先物化一棵干净树**（`git ls-files` 的全部文件复制到临时目录），在那棵树里
  逐个 `runpy`。于是"本地绿"与"CI 绿"不再是两个需要对齐的判断，而是同一件事。
- 取不到 git 索引（tarball 副本、单元测试里的 `tmp_path` 假仓库）时：`--gate` **报红**，
  不退回工作树判绿。退回就是又一个"本地绿"。
- 物化树只放 git 里的文件会漏掉"刚写完、还没 `git add`"的探针，所以检查对象取工作树的
  全集、运行环境取干净树——新脚本照样在没有 `data/` 的树上被加载一次。
- 报错里的临时路径换成 `<干净 checkout>/backend`：同一条红不该每次长得不一样。

**没采用的三条，以及为什么不：**

| 候选 | 为什么不 |
|---|---|
| 让人记得"先建干净 worktree 再跑闸门" | 已经证明"记得"在这个仓库不管用——五连红就是"记得"的产物 |
| 在 CI 之前再加一步自查 | 那是拿 CI 验 CI；本地仍然什么都发现不了 |
| 让每个脚本"声明依赖了哪个被忽略目录" | **声明会漏**，而漏掉的那一条正是下一次的红。物化不需要任何人声明 |

六个脚本本身的处置见下一节：**顶层不读文件，读取进 `main()`，缺数据 exit 2**。

## 修完那六条之后：干净树 ≠ 干净机器

第一次修（`523dde6`）把 `load` 档搬进物化出来的干净树，本地与干净 worktree 两侧
都绿，于是推上去 —— **CI 还是红的**，但红的是另外四类：

    scripts/_fix_ps1_bom.py                  ← FileNotFoundError: 'D:\\genai-mindmap\\check-neo4j.ps1'
    scripts/acc2/probe_ui_colors.py          ← ModuleNotFoundError: No module named 'playwright'
    scripts/acc2/probe_ui_layout.py          ← 同上
    scripts/acc2/probe_ui_shapes.py          ← 同上
    scripts/asr_local_poc.py:231 / :329      ← 模块不存在：sherpa_onnx / soundfile
    scripts/measure_fishbone_categories.py:57 ← 模块不存在：winreg（共 13 处，去重后显示一条）

四类的成因完全不同，但都属于同一句话：**干净树只对齐了"git 里有什么"，对不齐
"这台机器上装了什么、是什么操作系统"**。

| 类别 | 为什么本地绿 | CI 上为什么红 |
|---|---|---|
| 硬编码绝对路径 | 那台机器的仓库路径恰好是 `D:\genai-mindmap` | 路径不存在 |
| 本机装过的包（`playwright` / `soundfile` / `sherpa_onnx`） | `pip install` 过，site-packages 里有 | 不在 `requirements.txt`，装不到 |
| 平台专属模块（`winreg`） | 开发机是 Windows | runner 是 Ubuntu |
| 模块加载时写仓库文件（`_two_arch_judge_write.py`） | 写到本机的 `backend/data/`，看不出问题 | 每跑一次闸门就改一次数据 |

所以第 6 档 `deps` 的判据**不是"我这台机器上有没有"**——那个永远为真，永远查不出
东西。判据是**声明**：平台专属模块必须在 `try` 里；第三方模块必须在
`requirements.txt` 的直接声明里（或有理由的白名单里），否则必须在 `try` 里。

四条处置一起看，就是个统一的形态：**可选依赖包 `try`，缺的时候由 `main()` 说清
缺什么、装什么、退出码 2**；路径一律以 `__file__` 为锚；顶层不放副作用。

这一轮的教训比"改哪几个文件"重要：**"干净 checkout"是个二维概念，树对齐了还差
机器对齐**。下次说"本地全绿"之前要先问：这个绿是在哪棵树、哪台机器、哪个 Python 上
取的？

## `try` 不是终点：可选依赖要登记

把 import 包进 `try: ... except ImportError:` 之后，`deps` 档就绿了。但那是**半个**
答案：它只让加载不崩，不回答"这个脚本缺什么才能跑"。而这些脚本被 `docs/` 当复算
入口引用，引用者最需要的恰恰是"我该先装什么"。绿得越干净，缺的东西越看不见。

所以 `deps` 档另有三条判据，把"可选依赖"从隐含变成可查：

| 判据 | 反面长什么样 |
|---|---|
| `try:` 里的外部模块必须在本文件写 `# requires-optional: X` | `try` 成了藏依赖的口袋 |
| 那个名字必须在 `docs/OPTIONAL-DEPS.md` 里出现过 | 只有这一个文件知道的依赖不算登记 |
| `# requires-optional:` 里的名字必须真的在 `try` 里 import 过 | 过期声明——比没有更糟，它会让人以为那个依赖还在用 |

登记形态（docstring 之后、第一个语句之前）：

```python
# requires-optional: sherpa_onnx, soundfile
from __future__ import annotations
```

人读的那一半在 `docs/OPTIONAL-DEPS.md`：模块 / 哪些脚本要它 / 装法 / **装不上时
什么样** / 它解锁什么。最后一列是给下一个人判断"我能不能跳过它"用的，别写成
"需要安装"。

## 探针脚本的"没数据"必须能红

`_two_arch_*.py` 这六个的修法不是"没数据就跳过"，而是"**没数据就明确报错、退出码 2**"：

```python
def _read(name: str):
    path = DATA / name
    if not path.is_file():
        print("错误：" + _MISSING.format(name=name), file=sys.stderr)
        raise SystemExit(2)
    return json.loads(path.read_text(encoding="utf-8"))
```

两条判据：

1. **"能不能加载"与"能不能跑"是两件事。** 这道闸问的是前者，所以读取只能发生在
   `main()` 里——`runpy` 用 `run_name="__probe__"` 加载，`if __name__ == "__main__"`
   那个块不执行，顶层就只剩 import 与常量。
2. **"跳过"与"算过了"必须能分开。** 这六个都是 `docs/TWO-ARCH-COMPARISON.md` 第 5 节
   点名的复算入口，跳过之后脚本会打印空表并 exit 0——读的人分不清"没有重复"与"没跑"。
   形态上就是"把没测说成测了"。一次性探针（没人当复算入口引用的）才可以跳过，而这六个
   里没有一个属于这一类。

顺带修掉一处同族问题：`_two_arch_peek.py` 无参数时以前"什么都不打印、exit 0"，
在终端上与"看过了、没有异常"长得一模一样，现在打印用法并 exit 2。

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

## CI 的每一道闸 × 它依赖什么 × 干净 checkout 里有吗

上面那六档是**一道**闸。整个 CI 有十六步（后端 job 八步判定、前端 job 八步判定，
两个 job 并行）。五连红期间后端 job 红在第 3 步「探针脚本卫生」，它**后面的六步一次
都没执行过**——所以那六步的 CI 结论当时根本不存在，"本地绿"被当成了它们的证明，
而那不是证明（前端 job 不受影响，一直在跑）。下面是逐道闸问同一个问题：

> **它读的东西在 `git ls-files` 里吗？** 不在，本地绿就不是结论。

判定用一棵干净 worktree（`git worktree add ../gm-cd HEAD --detach`）与主工作树对跑。
"两侧"指这两棵树。

| CI 步 | 判据读什么 | 干净 checkout 里有吗 | 两侧实测 | 修法 |
|---|---|---|---|---|
| `ruff check .` | `backend/` 全部 `.py` | 有 | 都 passed | 无。反方向：本地多扫未跟踪文件，只会更严 |
| 探针脚本卫生 | `scripts/`、`docs/`、`.env.example`、git 索引 | 有 | 修前 0 条 vs **6 条**；修后都 0 条（六档） | **已修**：`load` 档物化干净树；新增 `deps` 档查"本机有、CI 没有" |
| 文档断言与代码一致 | `app/**`、`conftest.py`、`openapi.json`、`deploy/`、`tidy.ts`、`git ls-files '*.md'` | 有 | 都 64 条判定，通过 | 无（它只读 git 里的文本） |
| 文档分级 | `docs/`、`README.md`、`INDEX.md`、`git ls-files '*.md'`；`git log` 只打印归档日期 | 有（**需 `fetch-depth: 0`**） | 都 128 份，通过 | 已配 `fetch-depth: 0`。日期不参与判据，否则每次重跑都要改文档 |
| 诊断码两侧覆盖 | `app/**/*.py` 字面量 + `DIAGNOSTIC-TWO-SIDES.md`；可选跑 `data/acc_trees*` 72 棵真树 | 静态有；**真树没有** | 都通过，但**输出不同**：本地跑运行期 5 码 / 12 判定，CI 打印"运行期未测量" | 已诚实降级，但 **CI 少测一半**。要补就把运行期那半固化成 golden JSON 入库，CI 比对 |
| Test（pytest 全量） | 见下三行 | 大部分有 | **3470 passed / 37 skipped / 29 subtests，两侧逐条相同** | 无。`conftest.py` 清 23 个环境变量、库落 `tmp_path`、网络闸拦真请求 |
| 零引用函数棘轮 | `app/`、`tests/`、`scripts/` 文本 + 基线 JSON | 有 | 都 8 条在基线内 | 无 |
| OpenAPI 快照 | `app/**`、`openapi.json` | 有 | 两侧 `export_openapi.py` 后 `git diff` 都是空 | 无（生成的 schema 不随 `.env` 的取值变） |
| `npm ci` | `package-lock.json` + 网络 | lock 有 | — | 依赖 PyPI/npm 的安装步骤本身就可能红，与本项目改了什么无关 |
| 契约类型（`gen:api` + diff） | **`npx --yes openapi-typescript@7.13.0`：每次联网拉包 + 本机 npx 缓存** | **没有** | **本机曾经两侧都跑不起来**（npx 缓存里缺这个包）；CI 上是绿的 | 加 `--prefer-offline` + CI 缓存 `~/.npm/_npx`。**没**提进 `devDependencies`：7.13.0 的 peer 是 `typescript: ^5.x`，本仓库钉 `^6.0.3`，实测 `npm install -D` 直接 ERESOLVE |
| `npm test`（vitest） | `src/`、`tests/`、`node_modules` | 源码有；`node_modules` 由 `npm ci` 建 | 干净树 93 文件 / 1461 通过 / 2 skipped | 无 |
| 用例成本闸门 | `tests/**/*.spec.ts` 文本 + `test-cost-baseline.json` | 有 | 都 199/199 | 无 |
| 零引用棘轮（前端） | `src/**`、`tests/**` + 基线 | 有 | 都 1 条 | 无 |
| 断链棘轮 | `openapi.json`、`client.ts` + 基线 | 有 | 都 9 断 / 1 死 | 无 |
| 可达性棘轮 | `src/**`、`tests/**` + 基线 | 有 | 都 3 条不可达 | 无 |
| `npm run build` | `node_modules` | 由 `npm ci` 建 | 未在本地验（`AGENTS.md` 要求交给 CI） | 无 |

四类"不在 git 里"的东西，以及各自的方向：

- **`backend/data/`、`backend/uploads/`**（`.gitignore` 的 `data/`、`uploads/`）：只有
  `load` 档与 `diagnostic_sides` 的运行期半段读它们。前者已修成物化干净树；后者已
  诚实降级成"未测量"，代价是 CI 少测一半。
- **`frontend/node_modules`**（`node_modules/`）：所有前端闸门都靠它，但 `npm ci` 会建，
  且 `package-lock.json` 在 git 里。变异来源是**联网**，不是"文件缺失"。
- **本机环境变量与 `backend/.env`**：`conftest.py` 清了 23 个变量；`app/` 里没有任何
  `env_file` / `dotenv` 加载（`backend/.env` 只给 `run-backend.bat` 那种启动脚本看），
  所以 `.env` 的存在不改变任何用例。这一条**当初就不是**靠"记得清"成立的，是靠用例。
- **本机的包与操作系统**（这一条是修完树之后才暴露的，见上一节）：本机装过但
  `requirements.txt` 没声明的包、只在某个平台存在的标准库模块、以及硬编码的绝对
  路径。三条都由 `deps` 档 + 「路径以 `__file__` 为锚」这条规矩接管。

还有两处与"干净 checkout"不同源、但同样会造出"本地 ≠ CI"的东西，写下来免得下次
重新判：

- **换行符**：`core.autocrlf=true`，工作树的 `.py` 有 CRLF，git 里的 blob 是 LF。
  `Path.read_text()` 走 universal newlines，`ts.createSourceFile` 与 ACL 也无关于换行，
  所以现有十六道闸两侧一致（前端四道已在 CRLF 的干净 worktree 上对过读数）。
  **判：CI 里不需要显式设 `autocrlf=false`**——runner 是 Linux、默认就是 false，而
  `.gitattributes` 的 `* text=auto eol=lf` 已经让 blob 权威为 LF；在 CI 里再设一遍
  只是把同一件事说两次。**要守的是写法**：新写的闸门读文本一律 `read_text()` /
  `splitlines()`，不要 `split("\n")` 或按字节算偏移——那种写法在 Windows 上写出来
  只会在这台机器上对。
- **Python 版本**：本地 3.13、CI 钉 3.12。`scripts/_two_arch_check.py` 里有 PEP 701
  的嵌套引号 f-string（3.12+ 才合法）。**判：不改本地版本，改判据**——
  `imports` 档现在用 `ast.parse(source, feature_version=(3, 12))` 再解析一遍，
  所以"用了比 CI 新的语法"在本地就红，不必等 CI。运行期行为差异（stdlib 某函数
  签名变了之类）没有机器判据，只能靠 CI；这一半记在这里，别假装它已经被覆盖。

按风险排的下一次红：**`gen:api`（唯一要联网的闸）> 诊断码运行期那一半（CI 永远
不测）> 新写的、直接读 `backend/data/` 或本机环境的脚本（`deps` 档接了后一半）>
`npm run build`（未本地验）**。

## 跑法

    cd backend
    python scripts/check_script_hygiene.py              # 全跑，打印
    python scripts/check_script_hygiene.py --gate       # 闩门：有违规退出码 1
    python scripts/check_script_hygiene.py --only env   # 只跑一档

    python -m ruff check .                             # lint（CI 里钉 0.9.10）
