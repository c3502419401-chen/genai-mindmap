# 可选依赖：哪个脚本缺什么就跑不起来

`backend/scripts/` 下的探针脚本有一半要额外的东西才能跑：有的要浏览器（`playwright`），
有的要真 ASR 运行时（`sherpa-onnx`），有的只在 Windows 上有（`winreg`）。这些东西
**故意不进 `backend/requirements.txt`**——产品部署不需要它们，它们只服务复算。

问题出在"**谁需要什么**"这件事以前只写在代码里。于是：

- 想把 `docs/TWO-ARCH-COMPARISON.md` 第 5 节的某一步复算一遍的人，只有跑起来之后
  才知道缺 `playwright`；
- `check_script_hygiene.py --gate` 也看不出这一层：把 import 包进
  `try: ... except ImportError:` 之后它就绿了，而**"这个脚本的功能依赖那个模块"
  这个事实随之消失**——绿得越干净，缺的东西越看不见。

所以这份文档与脚本头部的一行注释是配套的：**注释给机器查，文档给人读。**

## 机器怎么查（`check_script_hygiene.py --gate` 的 `deps` 档）

每个脚本在 docstring 之后、第一个语句之前，可以有一行：

```python
# requires-optional: sherpa_onnx, soundfile
```

三条判据，都在 `deps` 档里，一条不过就红：

1. **`try:` 里的模块必须登记。** 凡是"不在 `requirements.txt` 直接声明里"或"平台专属"
   的模块，`try` 包了也必须出现在本文件的 `# requires-optional:` 行里。
   否则 `try` 就成了藏依赖的口袋。
2. **登记的名字必须在这份文档里出现过。** 否则登记只是脚本内的局部知识。
3. **登记的名字必须真的在 `try` 里 import 过。** 否则是一条过期声明——比没有更糟，
   它会让下一个人以为那个依赖还在用。

## 登记表

| 模块 | 哪些脚本要它 | 怎么装 | 装不上时什么样 | 它解锁什么 |
|---|---|---|---|---|
| `playwright` | `scripts/acc2/probe_ui_colors.py`、`probe_ui_layout.py`、`probe_ui_shapes.py` | `pip install playwright && playwright install chromium` | `main()` 打印"这个探针要 playwright"并 exit 2；闸门侧不红（加载期不 import） | 真浏览器里量节点几何、配色、图标——"代码写了"与"像素画出来了"是两件事 |
| `sherpa_onnx` | `scripts/asr_local_poc.py` | `pip install sherpa-onnx` | `ImportError("需要 sherpa_onnx（pip install sherpa-onnx）")`，只在本地 ASR 那条路上抛 | 本地语音识别 PoC（SenseVoice + Silero VAD），见 `docs/RESEARCH-ASR-LOCAL.md` |
| `soundfile` | `scripts/asr_local_poc.py` | `pip install soundfile` | 同上，`ImportError` 带装法 | 读 wav（`wave` 只认 PCM，`soundfile` 认 float/多声道） |
| `pymilvus` | `scripts/measure_cross_map.py` | `pip install pymilvus`（19 个传递依赖、约 75 MB） | 脚本打印"未安装"，跳过向量那条路 | 与 Milvus 侧的跨图检索对照，见 `docs/OPTIMIZATION-CROSS-MAP.md` |
| `winreg` | `scripts/measure_fishbone_categories.py`、`measure_generation_strategy.py`、`measure_graph_outline.py`、`measure_graph_two_pass.py`、`measure_incontext.py`、`measure_latency_breakdown.py`、`measure_section_slice.py`、`measure_shapes.py`、`measure_single_call.py`、`measure_two_arch.py`、`measure_two_tier.py`、`probe_plan_inventory.py`、`probe_skeleton_only.py` | 标准库，**只在 Windows 上存在**（Linux/CI 上 import 就失败） | `_user_env()` 返回 `""`，即"没有从 `HKCU\Environment` 补环境变量"——让脚本只认进程环境 | 读用户级环境变量（这些测量脚本要模型 key，而 agent 的 shell 不继承用户级变量） |

`winreg` 那 13 个脚本共用同一段 `_user_env` / `_fill_env_from_registry`：它们是
`measure_*.py`，本机跑时靠注册表补 key，CI 上不跑它们、也跑不了（`data/` 也没有）。
所以它在 CI 上的正确姿态是"没有 Windows 注册表可读"而不是"脚本坏了"。

## 为什么 `try` 不是终点

`try: import X except ImportError: X = None` 只让**加载**不崩。它不回答"这个脚本
缺什么才能跑"。这个仓库的语境里后者更重要：这些脚本被 `docs/` 当复算入口引用，
引用者最需要的恰恰是"我该先装什么"。所以 `try` + `# requires-optional:` + 这份表，
三件一起才算登记完。

## 新增一个可选依赖时

1. 脚本里包 `try: ... except ImportError:`，并在 `main()` 或调用处给出**缺什么、装法、
   退出码 2**；
2. 脚本头部加 `# requires-optional: 模块名`；
3. 这份文档的登记表加一行，写清"装不上时什么样"——那一列是给下一个人判断"我能不能
   跳过它"用的，别写成"需要安装"。
