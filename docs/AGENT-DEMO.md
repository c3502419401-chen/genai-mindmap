# 开启 agent 与现场演示指南

> 面向"要在别人面前演示一遍"的场景：怎么启动、会看到什么、坏了怎么查。
> 所有开关与默认值都对照代码核实过，不是凭印象。

## 先说一件容易踩空的事

**agent 没有 demo 模式。**

`MINDMAP_LLM_MODE` 这个变量只在两个地方被读：`backend/app/generation/service.py`（普通生成）和 `backend/app/ingestion/vision_pdf.py`（PDF 视觉识别）。agent 侧完全没有分支——`agent/provider.py::default_provider()` 直接返回 `DeepSeekProvider()`，每一次工具调用都是真实的模型请求。

也就是说：

- 设 `MINDMAP_LLM_MODE=demo` 时，普通生成返回固定占位内容，**agent 仍然调真实模型**。
- 没配 key 时，agent 会失败（`请在后端环境变量中配置模型地址、密钥和模型名称`），而普通生成照常出 demo 内容。

所以"演示 agent"= 配真实 key 演示。不想要这个依赖的话，见最后一节「没有网络或不想花钱时怎么办」。

## 两个开关

agent 能不能用，由两个独立的环境变量决定，**两个都要开**：

| 开关 | 在哪一侧 | 默认 | 开的值 |
|---|---|---|---|
| `MINDMAP_AGENT_ENABLED` | 后端 | **开**（`"1"`，只有设 `0` 才关） | 不用设；关了要设回 `1` |
| `VITE_MINDMAP_AGENT` | 前端 | **关** | 必须设 `1` |

前端默认是关的，所以"面板根本不出来"最常见的原因就是漏了 `VITE_MINDMAP_AGENT=1`。

两个变量都在**进程启动时**读取：

- 后端改了要重启 `uvicorn`；
- 前端是 Vite 在启动时注入 `import.meta.env`，改了要**重启 `npm run dev`**，光刷新页面不够。

后端被关掉时，`POST /api/agent/runs/stream` 返回 `503`，`detail.code` 是 `agent_disabled`。

## 最短路径

两个终端，分别在 `backend/` 和 `frontend/` 下。

**终端一（后端）**

```powershell
cd D:\genai-mindmap\backend
$env:MINDMAP_LLM_MODE='model'
$env:MINDMAP_LLM_BASE_URL='https://api.deepseek.com/v1'
$env:MINDMAP_LLM_API_KEY='你的key'
$env:MINDMAP_LLM_MODEL='deepseek-chat'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

看到 `Application startup complete` 与 `Uvicorn running on http://127.0.0.1:8000` 即可。

**终端二（前端）**

```powershell
cd D:\genai-mindmap\frontend
$env:VITE_MINDMAP_AGENT='1'
npm run dev
```

看到 `Local: http://localhost:5173/` 后打开它。

**确认两边都通**（可选，但演示前建议做一次）：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/docs -UseBasicParsing | Select-Object StatusCode   # 期望 200
Invoke-WebRequest http://localhost:5173/ -UseBasicParsing | Select-Object StatusCode       # 期望 200
```

前端通过 Vite 代理把 `/api` 转发到 `127.0.0.1:8000`，所以后端必须是 8000 端口，否则前端能开、生成会失败。

## 演示时会看到什么

打开页面后，右侧会多出一个 agent 面板（顶部状态标签与工具调用列表）。三个值得当场指出来的点：

**一、节点直接在画布上长出来。** 点「合并生成导图」之后 2~3 秒，中心主题和一级分支就出现，之后子节点陆续往下挂。不是等十几秒黑屏、然后整棵树同时出现。

- 预览中的节点是**虚线 + 半透明**，候选到达后变实线，仍然半透明——它不是你的图，是模型的产出。
- 顺序是：预览（虚线）→ 候选（实线，仍未应用）→ 你点应用/保存才写进画布。
- **预览和候选都不能选中、不能编辑**。原因很实在：它们的 id 后端不认识，选中之后会被带进 expand 请求的 `selected_ids`。点「应用」之前它们只是给你看。

**二、只有明确"到底了"的节点才带标记。** 预览里带小圆点的节点，是模型自己声明「我已经是最小可执行单元」的。没有圆点表示它没说，或者它说还能继续拆。

**三、深度和规模是内容决定的，不是写死的。** 同一份论文实测能到 5 层、100 多个节点；纯主题展开会更深更宽。层级上限 8 是可读性上限，不是目标。

## 现场容易出问题的四个地方

**面板不出现。** 前端漏了 `VITE_MINDMAP_AGENT=1`，或者设了但没重启 `npm run dev`。

**点了生成，面板立刻报错。** 先看面板上那一行红字下面的 `code：message`——现在失败原因会直接显示出来。常见的是：

- `generation_failed` + 「请在后端环境变量中配置模型地址、密钥和模型名称」→ 后端三个变量没配全（地址、key、模型名缺一不可）。
- `budget_exhausted` + 「模型调用次数已用完」→ 树已经建好了，只是没额度再问下一轮。**这不算失败**：会以已建好的树收尾，节点照常可用。
- HTTP `503` + `agent_disabled` → 后端 `MINDMAP_AGENT_ENABLED=0`。

**生成很久没有新节点。** 真机上单次生成 25~65 秒都出现过，中间大段没有新节点是模型在憋输出，不是卡死。判断依据是面板顶部的工具行还在「运行中」，以及后端日志里没有错误记录。

**画布上不生，只有右侧面板在长。** 说明你打开了一张旧图再点生成。`content` 非空时预览和候选都不会顶掉它——这是有意的，不然模型产出会覆盖你已经保存的图。**要演示画布上的生长，从空白画布开始。**

## 演示前的检查清单

1. 后端三个 `MINDMAP_LLM_*` 变量齐全，且 `MINDMAP_LLM_MODE=model`。
2. 前端 `VITE_MINDMAP_AGENT=1`，且改完之后**重启过** `npm run dev`。
3. 后端在 8000，前端在 5173，`/docs` 与首页都返回 200。
4. 画布是空的（没有打开过旧图）。
5. 准备好的材料能在界面上正常解析完，字数不超上限（合并后 300000 字以内）。
6. 先自己跑一遍完整流程，记下这次的耗时——现场用同一份材料，心里有数。

## 关闭与回滚

- 临时关掉 agent、退回原有生成路径：后端设 `MINDMAP_AGENT_ENABLED=0` 并重启。前端不用动（面板还在，但请求会被 503 挡掉）。
- 彻底不显示面板：前端去掉 `VITE_MINDMAP_AGENT` 并重启 `npm run dev`。
- 两者都不需要回滚代码。

## 没有网络或不想花钱时怎么办

agent 这条路必须有模型。可行的替代：

1. **提前录屏**。最稳，也不受现场网络影响。
2. **演示已保存的图**。导图可以存到 SQLite 再从列表打开（不需要生成），讲结构、讲编辑、讲布局都成立，只是没有"流式生长"这一段。
3. **接受真实调用**。DeepSeek 按量计费，演示一次的量级很小；但要提前跑一遍确认额度够。

如果哪天真需要"agent 也能 demo"，那是一个功能改动（给 `default_provider()` 加一个可回放的假 provider），不是改一个环境变量就能做到的——别指望 `MINDMAP_LLM_MODE=demo` 会有效果。
