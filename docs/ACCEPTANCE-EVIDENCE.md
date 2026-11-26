# 未验收项的实跑证据（甲/乙/丙三组）

> 目标：把 `docs/PLAN-VS-IMPLEMENTATION.md` 里"只有代码、未验收"的那 11 项，能跑的都真跑一遍，
> 让申报材料里的"已实现"有运行级证据支撑；跑不动的，写清差什么条件。
>
> 基线：`main`，从 `e5c0ae0` 起跑，证据提交为**本文所在的那一次提交**（期间 `e694da1`
> 只改了 `docs/ACCEPTANCE-METRICS.md` 三行，不涉及代码）。后端 pid 20820
> （`run-backend.bat`，`MINDMAP_LLM_MODEL=deepseek-chat`），
> 前端 5173（vite dev），本地库 `backend/data/mindmaps.db`。
> 本机没有 `docker`、没有 `wsl`；有 LibreOffice（后面这一条改变了一个结论）。
>
> **边界**：本轮未改动 `backend/app/**` 与 `frontend/**` 任何一行。所有脚本与读数在
> `backend/scripts/acc2/`（`*.py` 是跑法，`*.json` 是读数，`shots/*.png` 是截图）。

---

## 零、最终数字

**11 项"代码就绪、未验收"→ 9 项拿到实跑证据，剩 2 项仍只能说"代码就绪"。**

| 项 | 原证据强度 | 本轮结果 | 现在该写什么 |
|---|---|---|---|
| P3 语音 | 代码 | **未验**（后端缺 `DASHSCOPE_API_KEY`，实测 422 且文案点名缺哪个变量） | 代码就绪、未验收 |
| P4 图像 | 代码 | **识别真跑通**（全新图 2.2 s → 150 字层级 Markdown，数字全对）；端到端生成被外部材料覆盖（B2） | 已验收（缺口：只出 Markdown + B2） |
| P5 PPT | 代码 | **真跑通**（`.pptx` 与 `.ppt` 都出文本，`.pptx` → 37 节点 4 层） | 已验收（容器默认不装 LibreOffice） |
| P6 布局/配色/偏好 | 实据+代码 | **真跑通**（三型图几何实测 + 偏好改图型生效；主题/字体/主色实测不渲染） | 已验收（缺口：主题不渲染） |
| P8 逻辑纠错 | 代码 | **真跑通**（5 类缺陷全检出；鱼骨/流程 422；relations 零感知） | 已验收（缺口：不碰关系） |
| P11 AI 代理生成 | 代码 | **真跑通**（`create` 一句话出整树/整鱼骨；`/tasks` 仍是桩） | 已验收（缺口：`/tasks` 桩） |
| P12 动态优化 | 代码 | **验出不通**（`suggest` 只产规则层 3 类，link 的"父位置/历史/材料"零产品入口） | 已验收：判定**不可达** |
| P14 碎片归类 | 代码 | **真跑通**（85 节点树；三个语义标签零命中；用户素材被外部材料覆盖，B2） | 已验收（缺口确认） |
| P15 前端栈 | 代码 | **真跑通**（`vue-tsc` + `vite build` 通过；1360 条用例全绿） | 已验收 |
| P18 向量库/微调 | 实据 | **前半真跑通**（4913×1024 float32、numpy 矩阵乘）；**微调零实现** | 已验收（缺口：微调 C） |
| P19 容器化 | 代码 | **未验**（本机无 docker） | 代码就绪、未验收 |

剩下的两项为什么剩：P3 缺一个阿里云百炼的 key（补它 10 分钟）；P19 缺一台装了 Docker 的机器
（补它约 1.5–2.5 小时，见第五节）。

---

## 一、怎么跑的（可复现）

```bash
cd backend
python scripts/acc2/make_pptx.py              # 造一份 4 页 .pptx
python scripts/acc2/probe_ppt_legacy.py       # P5：.pptx / .ppt 两种格式对照
python scripts/acc2/run_once.py --label p5_ppt --goal "根据这份产品评审 PPT 整理成思维导图" \
       --source-file p5_pptx_source.json       # P5 端到端（真 SSE）
python scripts/acc2/probe_optimization.py     # P8 / P12：五种输入喂 check/suggest
python scripts/acc2/probe_p14.py              # P14：碎片文本入库
python scripts/acc2/run_once.py --label p14_fragment --goal "把这份会议纪要整理成思维导图" \
       --source-file p14_source.json
python scripts/acc2/probe_p14.py analyze p14_fragment
python scripts/acc2/probe_p18.py              # P18：库表 + 真检索 + 算法定位
python scripts/acc2/probe_ui_layout.py        # P6：真浏览器打开 5173
python scripts/acc2/probe_ui_shapes.py        # P6：三型图几何
python scripts/acc2/probe_ui_colors.py        # P6：配色与 emoji 图标的渲染
python scripts/acc2/probe_prefs.py            # P6：偏好写入与建议
python scripts/acc2/probe_media.py            # 乙组：真图片 / 真 WAV
python scripts/acc2/probe_vision_fresh.py     # P4：全新图片（排除缓存）
python scripts/acc2/probe_vision_upstream.py  # P4：对上游直发一次，确认是谁在识别
python scripts/acc2/probe_p4_generate.py      # P4：识别结果 → 材料
python scripts/acc2/run_once.py --label p4_image --goal "把这张草图整理成思维导图" \
       --source-file p4_source.json
```

前端侧：`npm run build`（含 `vue-tsc --noEmit`）、`npx vitest run`。

后端定向（本轮只跑与这 11 项直接相关的模块，全量 139 个文件跑不完 15 分钟）：

```bash
cd backend
python -m pytest tests/test_link_suggestions.py tests/test_optimization_rules.py \
  tests/test_source_refs.py tests/test_ingestion.py tests/test_vision.py tests/test_asr.py \
  --no-header
# 201 passed, 1 skipped, 3 subtests passed in 12.53s
```

浏览器用系统 Chrome（`playwright` 的 python 包版本要 chromium-1234，本机装的是 1248，
用 `channel="chrome"` 绕开，没额外下载浏览器）。

---

## 二、甲组：逐项读数

### P5 PPT 输入 —— 真能用（两种格式都通）

`.pptx`（自造 4 页：标题页 / 分点页 / 二级缩进页 / 表格页）→ `POST /api/ingestion/files`：

```
200 · 93.9 s（首次，Docling 冷启动）· 521 字 · status=parsed · truncated=false
```

抽出的文本保留了四级结构（标题 `#`、要点 `-`、嵌套缩进、表格转 Markdown 表）：

```
# 市场分析
- 目标人群：一线城市 25-40 岁有孩家庭
- 价格带：整套方案 8000-20000 元，用户对 15000 元以上敏感
# 功能设计
- 中枢网关
    - 本地语音唤醒，断网可用
    - 支持 Matter 与 Zigbee 双协议
| 难点 | 应对 |  ← 表格页被转成 Markdown 表
```

端到端（把这份 source 送进真实 SSE 生成）：

```
200 · 36.11 s · 51 帧（node_grow 39）· 37 节点 / 4 层 · source_usage 记 1 份 document
```

树里带回了 PPT 的具体数字与专名（8000-20000、3.2 倍、25%、12MB、Matter/Zigbee），
不是泛化骨架。**判定：真能用。**

`.ppt`（97–2003 二进制）实测**也能跑**：

```
200 · 17.8 s · 415 字（比 .pptx 少 106 字 —— 表格内容没有完整回来）
```

这与核对报告"容器默认不装 LibreOffice、`.ppt` 需系统级依赖"的结论**不矛盾**：本机装了
LibreOffice（`C:\Program Files\LibreOffice\program\soffice.exe` 存在），`legacy_office.soffice_cmd()`
按 `MINDMAP_SOFFICE_CMD` → PATH → 默认安装位置三级探测命中了它。缺口仍在**部署形态**上：
`WITH_OFFICE=0` 时同一个请求会得到"需要 LibreOffice"的 422。

小瑕疵（不影响判定）：同一份 `.pptx` 第二次上传 0.3 s 返回 —— 有解析缓存；缓存键是文件内容，
所以两次结果一致，不是错误来源。

### P6 自动布局 + 配色 + 偏好 —— 真能用（主题/字体/主色确实不渲染）

三型图在真浏览器里装载（走产品自己的 `store.preview()`），读 `vue-flow__node` 的真实
`transform`：

| 图型 | 节点 | 边 | 几何读数 | 截图 |
|---|---|---|---|---|
| 树（direction=right） | 7 | 6（全 `smoothstep`） | root x=−90 → 一级 146 → 二级 382，向右生长 | `shots/p6-tree.png` |
| 鱼骨（六类） | 19 | 18（全 `fishbone`） | 主骨 x=−90（最右）；六骨 x −336…−996 向左排，三类 y=−236 / 三类 y=188 上下交替；每条原因比其骨更靠左 | `shots/p6-fishbone.png` |
| 流程（3 阶段 + 驳回） | 6 | 8（`smoothstep` + `mindEdge`） | 根 y=−24 → 提交 96 → 审查 216 → 付款 336，自上而下分层；驳回是虚线回退边 | `shots/p6-flow.png` |

鱼骨截图肉眼可判：主骨横贯、鱼头在右（黑框"为什么交付总是延期"）、六根骨斜插上下交替、
每根骨挂两条原因。**三种几何都是真的，不是"代码在"。**

配色与图标（生成侧给了 `{color, icon}`，渲染侧要都能画出来）：

```
六根骨：人=blue 👤 · 机=teal ⚙️ · 料=amber 📦 · 法=purple 📐 · 环=green 🌐 · 测=red 📏
DOM：31 个节点各带 data-node-color（六种值都出现），骨节点 textContent 含 emoji
截图：shots/p6-colors.png —— 六色卡片 + 彩色 emoji，同色系向下继承到原因
```

主题（另一件事）：生成内容里 `theme = {"preset":"default","font":"system","direction":"right"}`，
五种主题/三种字体/主色**契约有值、界面不渲染** —— 实测页面 `document.documentElement`：

```
data-theme = null
--mind-color-0 / --mind-color-1 / --mind-font / --mind-accent = ""（全空）
body font-family = system-ui, "Microsoft YaHei", sans-serif（固定一行）
```

报告 6.4 节"已查实成立"的结论，这一轮拿到了**运行期**证据（不只是读 `styles.css`）。
注意区分：节点级配色（`data-node-color`，真渲染）与主题预设（`theme.preset`，不渲染）是两套，
前者经实测可用，后者经实测无效。

偏好学习真能影响输出（这是本轮最实在的一条链）：

```
PUT /api/prefs/diagram_type.default {"value":"fishbone"}  → 200, changed=true, last_applied_at=null
GET /api/prefs/suggestions                                → 200 []（"近 3 次一致"规则还没满足）
run create "分析报销流程为什么慢"（不带 diagram_type）      → 200 · 40.79 s · 41 帧
  产出 diagram_type=fishbone, 6 根骨 × 4 条原因 = 31 节点 3 层
  problem = "为什么员工报销从提交到入账要拖这么久"
PUT /api/prefs/diagram_type.default {"value":"tree"}      → 改回，后续生成恢复树
```

**判定：真能用**，缺口与报告一致（主题/字体/主色不渲染；"偏好学习"是显式写入 + 规则，
不是模型学习）。可写键只有 4 个 + `note.*`（写错键会 400 并列出可写集合，这点做得对）。

### P8 逻辑纠错与冲突检测 —— 真能用（但只对树）

五种输入喂 `/api/optimization/check`（读数在 `p8_p12_optimization.json`）：

| 输入 | 状态 | 结果 |
|---|---|---|
| 真实生成的干净树（37 节点） | 200 | **0 条诊断**（好树不误报） |
| 人造 5 类缺陷树 | 200 | **11 条**：`DUPLICATE_LABEL` 1、`NEAR_DUPLICATE_LABEL` 1、`SINGLE_CHILD` 3、`SHALLOW_BRANCH` 5、`MISSING_DETAIL` 1 |
| 同上 + 4 条 `relations`（依赖/因果/冲突/引用） | 200 | **与不带 relations 时逐码相同** —— 对关系零感知 |
| 真鱼骨形状（`problem`+`bones`） | **422** | "结构体检只支持树型导图（鱼骨与流程的判据不同，暂不支持）" |
| 真流程形状（`stages`+`edges`） | **422** | 同上 |

引用核验真能工作，但有一个隐性前置：

```
sources 的 id == 生成侧 id  → UNSOURCED_NODE 4（4 个目录级节点本来就没有引文），SOURCE_TEXT_UNAVAILABLE 0
sources 的 id 是 ingestion 返回的原 id → SOURCE_TEXT_UNAVAILABLE 33 + UNSOURCED_NODE 37
```

原因：生成链路落库时给 source 换了 id（`2fa76b62…` → `8007363c…`），SSE 的 `sources` 事件
用 `mapping` 把新 id 发回前端，前端 `stores/sources.ts:365 remap()` 照着改。**前端因此是对的，
纯 API 调用者会看到 33/37 条"无法核验"**。

**判定：能用但有已知缺口。** 缺口两条维持（"因果/关系建议"做不到；只对树生效），
且比报告更明确：报告说"传鱼骨会 500"，实测现在是 **422**（路由层 `_tree_content()` 拦的，
文案说明判据不适用）——报告的失败形态已过期，结论成立。

### P12 动态结构智能优化 —— 验出"不可达"

`/api/optimization/suggest` 对缺陷树返回 11 条建议：

```
kinds = {expand: 8, dedupe: 2, add_detail: 1}
```

全部来自规则层（诊断码 → 动作）。**没有一条 `kind="link"`**，也就是报告里作为 P12 证据的
那三件（`parent_choices` / `history_placements` / `material_hits`）在 API 输出里不存在。

查调用点：`app.optimization.link.suggest()` 的引用者只有两处 ——
`tests/test_link_suggestions.py`、`scripts/measure_link_placement.py`。**产品代码零调用**，
且开关 `MINDMAP_OPTIMIZATION_LINK` 缺省关、`MINDMAP_OPTIMIZATION_LINK` 在 `run-backend.bat` 里未设。

`link.py` 自己的 docstring 交代了"界面上它只有忽略、没有应用"，但没说更前一步：
**它连"出现在界面上"都没有发生**，因为没有调用者。度量脚本
`measure_link_placement.py`（它跑通过，所以算法本身不是空壳）与产品之间缺一根线。

**判定：不能用（产品路径不可达）。** 这不是本轮改坏的，是接线没做：算法 + 用例 + 度量脚本
齐备，缺一次"谁在什么时候调它"的决定。修法与代价见第四节 B1。

### P14 碎片化文本自动归类 —— 真能用，但没有语义归类

真粘一段 205 字的会议纪要（三类信息都写了显式标记："问题：" / "方案：" / "待验证："），
入库 → 生成：

```
200 · 62.41 s · 97 帧（node_grow 85）· 85 节点 / 4 层
title = "接口变慢排查与优化实录"
一级分支 = [压测与定位方法, 连接池与线程池调优, 线上故障排查实录, 慢查询与接口优化]
```

三个语义标签（问题描述 / 解决方案 / 待验证假设）**零命中**：出现的"线上故障排查实录"
"慢SQL与连接池假设"是内容复述，不是"按信息类型分栏"。与报告 P14 判定一致。

但这轮还多看到一个此前没有记录的现象 —— **用户素材被外部检索挤掉了**：

```
source_usage = 9 份：1 份 text（205 字）+ 8 份 web（合计 ≈ 2.1 万字）
我的材料里的具体细节在整棵树里出现次数：2.8→4（有）· 60%→0 · 八台→0 · 5%→0 · 老王→0
```

也就是用户粘的那段纪要，只有标题级的两个词进了树，5 处具体内容全部丢失；树的主体来自
联网检索回来的 8 篇文章（其中两篇是"被新领导约谈，我差点辞职"这类明显离题的公众号文）。
`MINDMAP_EXTERNAL_KNOWLEDGE=1` + `MINDMAP_EXTERNAL_DEEPSEEK_SEARCH=1` 是后端的线上配置，
用户无法在请求里关掉。**这条记在第四节 B2。**

**判定：能用但有已知缺口（缺的正是计划书举的那三个分支）。**

### P18 向量库 + 用户反馈微调 —— 前半真跑通，微调零实现

存储（本机库直接读）：

```
chunk_vectors: 4913 行 · model=text-embedding-v3 · vector 合计 20,123,648 字节
  校验：4913 × 1024 维 × 4 字节（float32）= 20,123,648 ✔ 逐字节对上
其它表：library_chunks 3513、mindmaps 24、edit_events 565、style_profile 8
```

算法（`app/optimization/cross_map.py:349`）：

```python
sims = queries @ base.T          # numpy 一次矩阵乘
```

对外检索接口是**桩**：

```
POST /api/knowledge/search {"query":"缓存命中率对查询延迟的影响","top_k":5}
→ 200 · 25.3 ms · {"hits": [], "implemented": false}
```

后半（微调）零实现，`rg fine.?tune|微调` 在产品代码里只命中注释
（`external.py` 的"分数微调"、`mindmaps/edits.py` 的"DPO 微调要几百条偏好对"）。
`edit_events` 有 565 条，但采集口径/基线评估/可行性三件仍缺。

**判定：向量库本体真在且量级与 `OPTIMIZATION-CROSS-MAP.md` 一致（numpy 一侧毫秒级）；
对外 `/api/knowledge/search` 是桩；微调仍是 C。**

### P11 AI 代理自动生成 —— 真能用（`/tasks` 仍是桩）

- `create` 意图：两次端到端（P5 的 37 节点树、P6 偏好的 31 节点鱼骨）都是"一句话 → 完整图"，
  图型还能按偏好切换。**计划书那句"自动生成完整框架"成立。**
- `POST /api/agent/tasks {"goal":"设计一款智能家居产品"}` →
  `{"stages":["目标澄清","要点拆解","分支展开","风险与验证"],"implemented":false}` —— 桩确认，
  与报告一致。

### P15 前端技术栈 —— 真跑通（构建级证据）

```
npx vitest run  → 82 files / 1360 tests 全绿，20.4 s
npm run build   → vue-tsc --noEmit 通过 + vite build：105 modules，2.89 s
                  dist: index.js 592.36 kB（gzip 200.40）、index.css 48.76 kB（gzip 9.88）
```

技术栈偏差（React→Vue 3、Zustand→Pinia、axios→fetch、无 Zod）由 `package.json` 静态可判，
现在再加一条"它能构建、用例全绿"。**判定：真能用。**

---

## 三、乙组：凭证核查与真跑

凭证核查不能靠本机环境变量（后端是另一个进程），所以全部改成"让后端用行为回答"。

### P3 语音 —— 未验（缺 `DASHSCOPE_API_KEY`）

两份真实 WAV（真人录音 139 KB、TTS 16k 单声道 922 KB）都得到：

```
422 · 0.0 s · "语音转写未配置：缺少环境变量 DASHSCOPE_API_KEY；
       在阿里云百炼控制台创建后填入，详见 docs/DEPLOY.md 的「语音输入」"
```

后端进程环境（`.visioncheck/env-dump.txt`，`run-backend.bat` 自己 dump 的）里没有这个变量，
`MINDMAP_FREE_TIER_API_KEY` 也没有（所以游客配额确实没启用，本轮跑生成没有被计次）。

补它需要：一个阿里云百炼账号 → 开通 DashScope → 创建 API Key → 写进 `run-backend.bat` 的
`set DASHSCOPE_API_KEY=...` → 重启后端。**约 10 分钟，含一次真实录音端到端。**

### P4 图像 —— 意外地真跑通了（原判"需视觉凭证，本机没有"不成立）

先用库里已有的两张图（`i1_sketch_map.png` / `i4_outline.png`）测，200、1.9/2.3 s，但**可能是缓存**
（别的运行上传过），不能作数。于是造一张内容全新的手绘风草图（`fresh_sketch.png`，
900×740，三层 + 数字）：

```
200 · 2.2 s · 150 字
城市内涝治理方案
- 一、源头减排
  - 海绵型小区改造
    - 透水铺装面积占比 35%
- 二、管网提升
  - 主干管扩容至 DN1200
    - 雨污分流率从 62% 提到 90%
- 三、调蓄与预警
  - 地下调蓄池 2.4 万立方米
    - 内涝预警提前 45 分钟
```

层级、专名、数字（35%、DN1200、62%→90%、2.4 万、45 分钟）全部正确。2.2 秒比 `vision_pdf.py`
注释里"5–7 秒/页"快，所以对上游直发一次确认是谁在识别：

```
POST https://api.deepseek.com/chat/completions  model=deepseek-chat  + image_url(data URL)
→ 200 · served_model="deepseek-flash" · answer="城市内涝治理方案" · usage 433 prompt tokens
```

结论：**这条链用的是现成的 DeepSeek key，视觉由上游 `deepseek-flash` 服务**。所以 P4 不缺凭证，
"需要视觉模型凭证"这个前提在本机不成立。

端到端也跑了一次（识别结果 → 材料 → create）：

```
200 · 52.53 s · 112 帧（node_grow 101）· 101 节点 / 4 层
title = "海绵城市与内涝防治要点整理"
一级分支 = [标准与指标, 排水与内涝防治, 污染控制与资源化, 空间格局与设计, 旧城改造与项目管控]
source_usage = 9 份：1 份 text（150 字）+ 1 份 paper + 7 份 web（合计 ≈ 2.8 万字）
```

**这里必须说清一个断层**：识别环节很准（层级、DN1200、2.4 万立方米、45 分钟、35%、62%→90%
全部正确），但生成环节把我这张图的内容压掉了 —— 整棵树里 `DN1200` 0 次、`2.4 万` 0 次、
`45 分钟` 0 次、`62%` 0 次、`管网提升` 0 次，一级分支全是检索回来的海绵城市标准文档的主题。
用户上传的是"我的草图"，拿到的图讲的是"国家标准的五个维度"。**与 B2 是同一个问题**，
在图像输入上表现得更刺眼（因为图里的内容 100% 是用户自己的）。

缺口与报告一致：产物是 Markdown（缩进层级还原得很准），**不识别节点间的语义关系**，
也没有来源标注与人工修正入口；SVG 仍不收（`router.py` 会用"模型拿到的是 XML 而不是画面"拒掉）。

**判定：识别环节真能用；端到端能用但被外部材料覆盖（B2）。**（这一条推翻了本轮开始时的假设，
写在这里是因为"凭证缺失"的结论会被直接抄进验收材料，错一次代价很高。）

---

## 四、发现的 bug 与风险（未改代码，逐条报）

### B1 `link.suggest` 零产品调用 —— "代码在、用例绿、但产品里根本走不到"

- **现象**：`/api/optimization/suggest` 只产规则层 3 类（expand/dedupe/add_detail），
  永远不产 `kind="link"`。P12 作为证据的那三件（父位置 / 历史放置 / 材料命中）在接口输出里不存在。
- **定位**：`app/optimization/service.py::suggest()` 只调 `_model_suggestions()` 与
  `cross_map.suggestions()`；`app/optimization/link.py::suggest()` 的引用者是
  `tests/test_link_suggestions.py`（8 条用例）与 `scripts/measure_link_placement.py`。
- **为什么危险**：这一项在核对报告里被判"B（代码就绪）"，用例与度量脚本都能跑，
  所以"绿"是真的；但产品路径上它从未被调用，**用例测的是函数，不是链路**。
  本轮定向复跑确认它确实绿：`tests/test_link_suggestions.py` + `test_optimization_rules.py` +
  `test_source_refs.py` + `test_ingestion.py` + `test_vision.py` + `test_asr.py`
  → **201 passed / 1 skipped，12.53 s**。绿与不可达同时成立。
- **影响**：5.2「动态结构智能优化」里"智能判断与已有节点的逻辑联系"这半句，用户看不到任何东西。
- **修法方向**（不擅自改）：在 `service.suggest()` 里按 `node_id` 为每个候选节点调一次
  `link.suggest()`，或挂到 expand 意图的落地回调上；同时在 `stores/structure.ts::APPLIERS`
  与 `mindmap/diagnostics.ts::APPLICABLE_KINDS` 里补 `link` 的处置（docstring 说这两处刻意没有，
  需要产品决定"点了应用之后改成什么"）。

### B2 外部检索把用户材料挤掉（P7/P14 交界）

- **现象**：205 字纪要 + 8 篇 web 材料（≈2.1 万字）→ 85 节点树的主体来自 web，用户素材细节
  5 项里丢 4 项（60%/八台/5%/老王 零命中）。图像输入更极端：150 字的草图识别结果 +
  8 份检索材料（≈2.8 万字）→ `DN1200`/`2.4 万`/`45 分钟`/`62%`/`管网提升` 全部 0 命中。
- **定位**：`source_usage` 里 9 份来源；web 材料每份被截到 4000 字（多份正好 4000），
  用户素材 150–205 字 ≈ 0.5%–1%。`MINDMAP_EXTERNAL_KNOWLEDGE=1` 与
  `MINDMAP_EXTERNAL_DEEPSEEK_SEARCH=1` 在后端 env 里写死，请求侧没有开关（`AgentRunRequest`
  里没有"本次不联网"字段）。
- **附带**：检索回来的 2 篇是明显离题的公众号文（"被新领导约谈，我差点辞职"），
  相关性过滤没挡住。
- **影响**：用户"粘贴自己的纪要"这个动作会产生一张不像自己材料的图。这是**质量**问题，
  不是可用性问题（图是完整的）。

### B3 `sources` 的 id 在生成侧被换掉，纯 API 客户端必踩

- **现象**：ingestion 返回 `2fa76b62…`，生成时落库变 `8007363c…`；把原 id 的 source 传给
  `/check` 会得到 33 条 `SOURCE_TEXT_UNAVAILABLE` + 37 条 `UNSOURCED_NODE`（本该 4 条）。
- **定位**：SSE `sources` 事件的 `mapping` 是唯一桥梁；前端 `stores/sources.ts:365 remap()`
  实现了，脚本/curl 不知道。
- **影响**：任何非前端调用者（移动端、验收脚本、第三方集成）都会看到"引用全都不成立"，
  很容易被误判成"引用核验功能坏了"。建议在 `CheckRequest` 上把这条前置写进 schema
  description，或让 `/check` 也接受 `mapping`。

### B4 `/api/knowledge/search` 是桩（`implemented:false`）

- **现象**：返回 `{"hits": [], "implemented": false}`，而 `chunk_vectors` 里真有 4913 条向量。
- **影响**：对外"知识库检索"这个能力不可用；内部的 `search_sources()`（生成链路用）是好的。
  用例覆盖的是 service 层，所以"绿"不代表路由可用。

### B5 报告里三处已过期（本轮实测纠正）

1. **"relations 界面不渲染"已过期**：`frontend/tests/relation-lines.spec.ts` 真挂载
   `@vue-flow/core` 并断言 `<g class="vue-flow__edge-mindRelation">` 出现；
   `mindmap/edits.ts::removeRelation()` 存在；git log 有 `8dcb524 feat(web): relations 画出来、详情里能看`。
   **但**：`/check` 对 relations 仍零感知（实测带 4 条关系的树与不带的诊断逐码相同），
   所以"因果关系建议做不到"的结论不变，变的只是**前置条件已满足**（不再需要"先让 relations 渲染"）。
2. **"传鱼骨/流程会 500"已过期**：现在是 422 + 说明判据不适用（`_tree_content()`）。
3. **前端用例规模**：报告说 `StructureCheck.vue` 有 9 条用例，实际全仓 82 个文件 / 1360 条。

---

## 五、丙组：容器化差什么

产物齐全（`docker-compose.yml` 两 service 两卷健康检查、`docker/backend.Dockerfile`、
`docker/frontend.Dockerfile`、`docker/nginx.conf`、`docker/verify-static.py`），本机没有
`docker` 也没有 `wsl`，所以**一次都没跑过**。

补它需要：一台装了 Docker + Compose 的机器（Linux 最省事），执行

```bash
docker compose build && docker compose up -d
python docker/verify-static.py          # 仓库自带的自检
```

**成本估算（X）**：

| 环节 | 体量 | 时间（网络顺利 / 不顺） |
|---|---|---|
| 拉基础镜像（python:3.12-slim 150MB + node:24-slim 250MB + nginx:1.27-alpine 23MB） | ≈420 MB | 5–15 min / 20–40 min |
| backend `pip install`（torch CPU + docling 全树；`docs/DOCKER.md` 第 7 节：镜像 1.3–1.7 GB） | 下载 ≈1.1–1.3 GB | 8–20 min（阿里云源）/ 30–60 min（官方源） |
| frontend `npm ci`（≈200 MB）+ `npm run build`（本机实测 2.89 s） | ≈200 MB | 2–5 min |
| 首次启动下 docling 版面/表格模型（`mindmap-models` 卷，`DOCKER.md:124`"几百 MB"） | ≈300–700 MB | 5–15 min |
| 逐条走 `DOCKER.md` 第 9 节清单（SSE 反代/健康检查/卷持久化/端口与 SQLite 三处冲突/`stop_grace_period`） | — | 30–60 min |
| **合计** | **下载 ≈2–2.5 GB，磁盘 5–7 GB** | **1.5–2.5 小时** |

本机可佐证体量的实测：`site-packages` 里 `torch` 513.8 MB、`pymupdf` 109.0 MB、
`transformers` 108.4 MB、`numpy` 31.3 MB —— 与 `DOCKER.md` 第 7 节的 1.3–1.7 GB 同一量级。

**P19 的另一半（生成 < 3 秒）与容器化无关**，判 D 的理由（3 次模型调用、耗时 ≈ 节点数 × 0.35 秒）
本轮的真跑数据继续支持它：37 节点的 PPT 树 36.1 s、31 节点的鱼骨 40.8 s、85 节点的纪要树 62.4 s。

---

## 六、自评：还剩几项"代码就绪"，以及最危险的那一类

### 6.1 数字

**11 → 9 项已验，剩 2 项（P3 缺 DashScope key、P19 缺 Docker 主机）。**

验掉的 9 项里，**5 项是"真能用"**（P5、P6、P8、P11、P15），
**3 项是"能用但有已知缺口"**（P4、P14、P18 前半 —— 其中 P4/P14 的缺口同源：外部材料覆盖用户素材，B2），
**1 项是"验出不可达"**（P12）。

这不是把 11 项都变成绿灯 —— 有三项的实测结论**比报告写的更糟或更松**：
P12 从"B（代码就绪）"变成"产品路径不可达"；P4 从"缺凭证"变成"真能用"；
P8 的失败形态从 500 变成 422（更诚实）。

### 6.2 "代码在、用例绿、但真跑不通"—— 有，三条

这是本轮最该被记住的部分。用例绿但链路不通，意味着**用例测的是函数，不是路径**：

1. **`link.suggest` 零调用（B1）**：8 条用例 + 一个能跑通的度量脚本，产品里没有调用者。
   用例测"给定输入产出什么建议"，永远不测"这个建议会不会出现在用户面前"。
2. **`/api/knowledge/search` 是桩（B4）**：`chunk_vectors` 4913 条向量、`search_sources()` 可用、
   用例覆盖 service；路由返回 `implemented:false`。用例测 service，不测路由。
3. **`sources` id 漂移（B3）**：用例里 `content` 与 `sources` 用同一份 id；真链路里生成侧换了 id，
   前端靠 SSE `mapping` 缝上，纯 API 客户端 33/37 条诊断全红。用例测的是"id 一致时对不对"，
   不测"id 会不会被换"。

共同点：**这三处都不是逻辑写错，是"接线"没做或没被告知**。所以它们不会让任何用例变红，
只会让用户看不到功能。这也是核对报告"证据强度"这一列真正该防的东西 —— 它比"没写代码"更难发现，
因为文档、代码、用例三处都看起来是对的。

反过来说，本轮**没有**发现"用例绿、真跑时逻辑出错"的那一类（算错、越界、状态错乱）。
三次真实生成（36.1 s / 40.8 s / 62.4 s）都完整收尾，SSE 帧序正常，落库内容与快照一致，
没有一次半途而废或空树。这一点比预期好。

### 6.3 边界与残留风险

- 本轮只新增 `backend/scripts/acc2/**` 与本文，未改产品代码、未重导 `openapi.json` / `schema.d.ts`。
- 三次生成都用了真实的 DeepSeek key（费用为几分钱量级）；游客配额未启用（`free_quota` 0 行、
  `MINDMAP_FREE_TIER_API_KEY` 未设），所以没有被计次。
- **库被写脏了一点**：这几次运行新增了 `runs` / `library_chunks` / `chunk_vectors` 行，
  并临时写过一条 `user_prefs`（`diagram_type.default`，已改回 `tree`）。
- `p6_pref_fishbone` 那次第一次统计错成"1 节点"（`run_once.py` 原来只认 `root`），
  已改成三型分算并重算为 31 节点 —— 这条记下来是因为**验证脚本自己的口径也会错**，
  它错了会给出"生成失败"的假结论。
