# AGENTS.md

## 项目与部署

上传材料 → 检索 → 生成/生长导图 → 画布编辑与保存。部署目标是 **2C2G 单机 + SQLite 单文件**（`MINDMAP_DB_PATH`）。任何改动先问"这台机器扛得住吗"：不要引入常驻内存索引、多 worker 写假设、重依赖或启动期大计算。

## 技术栈

- 后端：FastAPI + Pydantic v2（`extra="forbid"` 是默认风格；CI 用 Python 3.12）
- 检索：SQLite + BM25/向量混合、RRF 融合，可选 rerank 与 Neo4j 图
- 前端：Vue 3 + Pinia + @vue-flow/core + Tailwind，Vite + vitest
- 契约：`backend/openapi.json` 是前后端唯一形状来源，`schema.d.ts` 由它生成

## Structure

| 路径 | 职责 |
| --- | --- |
| `backend/app/agent/` | tool-calling 循环：工具定义、op 执行器、provider、SSE |
| `backend/app/knowledge/` | 切块、检索、rerank、库、图 |
| `backend/app/generation/` | 提示词与模型出口、预算 |
| `backend/app/ingestion/` | 多格式解析（PDF / DOCX / PPTX / 图片 / 音频） |
| `backend/app/optimization/` | 诊断、建议、引用校验 |
| `backend/app/contracts/` | Pydantic 契约 |
| `frontend/src/` | 画布、Pinia store、API 客户端 |
| `docs/` | 决策与实测记录（`RAG-DECISIONS.md`、`RETRIEVAL-EVAL.md`） |

逐文件的改动步骤看 `.agents/skills/`，本文件不展开。

## Commands

```bash
# 后端（在 backend/ 下）
python -m pytest -o addopts="" -q       # -o addopts="" 必须带：pytest.ini 的 -q 会把统计数字吞掉
python -m ruff check app tests          # 仓库无 ruff 配置，走默认规则，只当冒烟
python scripts/export_openapi.py        # 改契约后重导 openapi.json 快照

# 前端（在 frontend/ 下）
npm test                                # vitest run
npm run type-check                      # vue-tsc --noEmit
npm run gen:api                         # 由 backend/openapi.json 重生成 schema.d.ts

# 启动
cd backend && python -m uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev
```

## 约定（每条都对应一次真实 bug，别删"为什么"）

- **改一个能力的入口，要检查所有会走到它的路径。** 生成侧现在有**两条**实现、一条注册：`mindmap_generate`（`create` 唯一注册的生成路：骨架一次 → 每支检索一次 → 内容一次写完）与 `mindmap_generate_deep`（分片，代码与规格都在，但**不注册到 `create`**，服务的是"一次响应写不下 200+ 节点"的大图，见 `_generate_deep_spec`）。两条共用材料准备（`_intake`：短材料退化 + 外部检索）、骨架（`_skeleton_outline` / `_plan_ask`）与按支检索（`_retrieve_branches`）——**改这几处等于同时改两条路**，只改一条会得到一个"看起来都能跑、实际行为分叉"的入口。上任那个横切开关（`MINDMAP_SOURCE_MODE`）已删除（`docs/ONE-PATH.md`），别再按"两种形态"的旧心智改提示词——历史上四次"只改了两三条路径"，单点测试全绿而链路是断的。这次回到一次生成的理由与实测（含 detail 变短这个已知代价）见 `docs/ONE-CALL.md`。
- **父块坐标只认区间全等去重。** `_near_duplicate` / `_pick_ordered` 是为"同一份证据被切成两块"设计的，只能用于**块坐标**；父块之间的重叠来自切块器的句首吸附，套重叠率判据会把两个不同父块判成重复、静默吞掉一条（父块坐标用 `_dedupe_exact`）。
- **保底席位的口径必须跨过 rerank。** `_reserve_seats` 按交付口径（`top_k`）算，不是候选池口径（`depth`）。`_rerank_top(reserve=...)` 就是这件事的兑现点，漏了它扩池等于把交付口径偷换成池口径。
- **改一个开关后，关掉时必须与上一个提交逐字节一致。** 用旧模块对比（`git show <commit>:path` 载入旧模块跑同一批输入），不是"测试还绿"——测试可能只覆盖一半：`test_short_parents_are_not_deduped` 早年只测 `library=False`，库分支的 bug 因此活了很久。
- **用 `git show` 读历史的测试，CI 必须配 `fetch-depth`，且不能拿 `pytest.skip` 兜"读不到"。** `test_edges` / `test_agent_diagram_type` / `test_external_directions` 都靠 `git show <commit>:path` 载旧模块做逐字节比对，最远的基线距 HEAD 一百多个提交；`actions/checkout` 默认只拉 1 个提交，那些测试在 CI 上从被引入那天起一直红（或被 `skip` 静默跳过），而本地是完整克隆、永远绿。`fetch-depth: 0` 之后才转绿。`skip` 把"环境配错了"伪装成"这条测试不适用"，比红更糟——红会有人去查，跳过不会。
- **判据要取在 CI 会看到的那棵树上。** 本地有 `backend/data/`、`backend/.env`、`node_modules`、运行中的服务；干净 checkout 上这些都不存在。凡是"跑一遍看红不红"的判断，先问一句：它读的东西在 `git ls-files` 里吗——不在，本地绿不是结论。这条已经咬过人两次，形态不同、成因一样：`git show` 读历史却缺 `fetch-depth`（`test_edges` 从被引入那天起在 CI 上红，本地完整克隆永远绿），以及 `_two_arch_*.py` 六个脚本在模块顶层读 `backend/data/`（`check_script_hygiene.py --gate` **连红五次**，`7ac8400 / f8bbc0a / b540fd8 / 00a623b / 157f0b5`）。闸门侧的对策是**闸门自己物化干净树**（那道闸的 `load` 档：`git ls-files` 复制到临时目录再加载），不是写一条"记得先建干净 worktree"——"记得"在这个仓库已经证明不管用，`--gate` 取不到干净树时宁可红。
- **"干净 checkout"是二维的：树对齐了还要机器对齐。** 上一条修完之后 CI 又红了一次，那四类与 `git` 无关：硬编码 `D:\genai-mindmap\...` 绝对路径、本机 `pip install` 过但 `requirements.txt` 没声明的包（`playwright` / `soundfile` / `sherpa_onnx`）、Windows 专属模块（`winreg`）、以及模块加载时就写仓库文件的脚本。所以写探针脚本的四条硬规矩：**读文件放 `main()` 里不放顶层**；**路径以 `__file__` 为锚，不写本机绝对路径**；**可选依赖 / 平台专属模块包 `try: ... except ImportError:`**，缺的时候由 `main()` 说清缺什么、装什么、`raise SystemExit(2)`，不要打印"跳过"然后 exit 0（那与"算过了、没有异常"在终端上没有区别）；**顶层不放副作用**（`--gate` 会把 `_*.py` 加载一遍）。可选依赖还要在脚本头部写 `# requires-optional: X`，并登记进 `docs/OPTIONAL-DEPS.md`——`try` 只让加载不崩，它不回答"这个脚本缺什么才能跑"。这些都是机器判据：`check_script_hygiene.py --gate` 的 `load` / `deps` 两档。
- **只有改到 case 标注过的章节，才要同步 `backend/scripts/eval_cases_manual.py` 的 `MANUAL_SOURCE_HASHES`。** `docs/AGENT_PLAN.md`、`docs/RAG-DECISIONS.md`、`docs/PLAN.md`、`docs/ARCHITECTURE.md` 四份是评估语料，但指纹只覆盖 `sections` / `targets` 标注到的那几章（`evaluate_retrieval.annotated_hashes`）：改到标注章节 → 28 条手工标注失效，装载当场报错不静默跳过；改别的小节、往末尾追加变更记录 → 指纹不变，不该报错，也不该顺手把指纹改成新的。
- **引用校验只保证"逐字出自那个来源"，不保证"那个来源支持这个论断"。** 写提示词或诊断文案时不要暗示它有语义校验。
- **降级要留下"降过级"的痕迹。** 无 key 退纯 BM25、检索空就留空、向量批失败——每一项单看都对，合起来就是"坏了和本来就没有长得一样"。

## Commit Message

Conventional Commits。标题保留英文类型前缀，冒号后的描述用中文，例如 `fix: 修正父块去重的坐标系`。正文注明实际使用的 AI 工具与模型；多个就分别列，不确定版本如实写"未确认版本"，不要猜。

```text
fix: 修正父块去重的坐标系

父块重叠来自句首吸附，不能套重叠率判据。

AI 工具：Claude Code
AI 模型：claude-sonnet-4-5
```

## Agent constraints

- **不要 `grep` / `cat` 整个大文件找东西。** `backend/app/agent/tools.py`（6700+ 行）、`backend/app/knowledge/service.py`（2762 行）、`frontend/src/stores/mindmap.ts`（3000+ 行）。先按符号定位（`grep -n` 找 `def` / `export` 行），再按行号精读。
- **不要用 `python -c` / 内联脚本批量改文件。** 要改就逐处编辑，一次一处。
- **不要跑 `npm run build` 验证前端改动**（它 = `vue-tsc --noEmit && vite build`，慢，且 build 期报错会盖掉类型错误）。用 `npm run type-check` + `npm test`；build 交给 CI。
