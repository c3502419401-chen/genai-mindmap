# 节点来源引用

本次只扩展后端 v1 契约的 `Node.source_refs`，不启用完整 v2 契约。
旧请求和数据库记录可省略此字段，读取时默认为 `[]`；现有 SQLite JSON
存储会自动保存新增字段，无需表结构或数据迁移。

## 引用格式

```json
{
  "id": "n1",
  "label": "发布安排",
  "children": [],
  "source_refs": [
    {"source_id": "meeting", "quote": "下周发布", "start": 4, "end": 8}
  ]
}
```

这里 `Source.text` 为 `会议决定下周发布。`。字符区间使用原始 `Source.text`
上的 Python Unicode 字符索引，从零开始，左闭右开；不是 UTF-8 字节偏移，
也不是 JavaScript UTF-16 代码单元偏移。原文开头的空白、换行都计入索引。

每个节点最多 20 条引用。`source_id` 必填、非空白、最长 100 字；可选
`quote` 最长 300 字，空白引文归为 `null`；可选 `page` 为从 1 开始的整数。
`start` / `end` 要么都省略，要么同时提供非负整数，且 `end > start`。

## 三层校验

| 层次 | 执行位置 | 规则与行为 |
| --- | --- | --- |
| 1：字段 | `SourceRef` / `Node` | 类型、长度、页码、字符区间和数量限制；拒绝未知字段。请求不合法返回 422，模型结构不合法返回 502。 |
| 2：关联 | `MindMapContent` | 每条引用的 ID 必须存在于 `source_usage`；摘要 ID 不得重复。所有接收导图的接口共用此约束。 |
| 3：原文 | `verify_source_refs` | 引文必须逐字出现在指定来源中；带区间时检查边界和非空白内容。区间与引文对不上时由服务端**按引文重新定位**：唯一定位就补正偏移，多处出现就标待核，定位不到才剔除。 |

第三层不做模糊匹配或跨来源拼接，也不证明节点文字被引文语义支持。
既没有引文也没有字符区间、或只有区间而引文为空的引用标记为
`UNVERIFIED_SOURCE_REF` 并剔除——坐标本身说明不了引的是哪一段。
没有可核验引用的节点标记为 `UNSOURCED_NODE`，不自动删除节点或建议删除。

### 生成时的第三层判定表

内容与出处是两件事：节点内容可以凭模型自己的知识展开，**但引用必须标出处**。
判定只作用于引用，不影响节点是否保留。

| 情况 | 处置 | 诊断 |
| --- | --- | --- |
| 引文与区间逐字一致 | 保留 | — |
| 只给引文、没给偏移 | 保留并补上精确区间 | —（补齐元数据不算问题） |
| 偏移写错、引文在可见范围内唯一定位 | 保留并补正偏移 | `SOURCE_QUOTE_OFFSET_FIXED`（info） |
| 引文在可见范围内多处出现 | 保留，标"待核" | `SOURCE_QUOTE_AMBIGUOUS`（warn） |
| 引文在原文里定位不到 | **剔除** | `SOURCE_QUOTE_MISMATCH`（warn） |
| 引用落在本次可见的父块内、但不在命中的子块内 | 保留 | `SOURCE_REF_OUTSIDE_EVIDENCE`（warn） |
| 引用只有区间、没有引文 | **剔除** | `UNVERIFIED_SOURCE_REF`（warn） |
| 引用既无区间也无引文 | **剔除** | `UNVERIFIED_SOURCE_REF`（warn） |
| 来源不存在 / 未解析成功 / 正文为空 | **剔除** | `SOURCE_TEXT_UNAVAILABLE`（warn） |
| 页码超过总页数或指向解析失败的页 | **剔除** | `INVALID_SOURCE_PAGE`（warn） |

"可见范围"按形态区分：`rag` 形态下是本次检索命中的父块（450–700 字）加全文兜底；
`full` 形态下整份正文都在 prompt 里，因此**不做父块约束**，引用任何段落都是合法的。
`rag` 形态一次证据都没检索到时允许零引用，由 `GENERATION_WITHOUT_EVIDENCE`（warn）
说明这次没有依据——诊断从不参与成功判定。

子块约束保留下来是为了**可观测**：谁引了父块里与查询无关的部分，诊断里看得见；
要求模型精确落在命中的那 200 字里过于苛刻，实测会成批剔掉合法引用。

## 可选的第四层：NLI 语义打分

前三层都是机械判据，它们管不住一种情况：引文逐字对得上，但节点论断与原文
**语义相反**（原文「只在小数据集上验证」，节点写成「已在大规模数据上验证」）。

`app/optimization/nli.py` 用一个小型 ONNX cross-encoder 对「节点标签 ↔ 引文」
再判一次语义蕴含，**只产出诊断，不改变任何引用**：

| 判定 | 诊断 |
| --- | --- |
| contradiction | `SOURCE_REF_CONTRADICTED`（warn），引用保留 |
| entailment / neutral | `SOURCE_REF_NLI`（info），附置信度 |

设计约束：概率模型不得推翻确定性判据。冲突时引用留着、同时给出警告，
由人判断。没有 `quote` 也没有字符区间的引用直接跳过——拿整篇原文去比一个
节点标签，NLI 只会给 neutral。

**默认关闭**（`MINDMAP_NLI_ENABLED` 未设即关）。关闭时连 `onnxruntime`
都不会 import，`annotate_nli` 立刻返回空列表。启用需要
`MINDMAP_NLI_MODEL_DIR` 指向含 `model.onnx` / `tokenizer.json` / `config.json`
的目录。加载前有内存闸门（默认 700 MB，Linux 可检测）。

实测参考：

| 模型 | 文件 | 内存增量 | 加载 | 单次推理 | 中文准确率 |
|---|---|---|---|---|---|
| `typeform/mobilebert-uncased-mnli` int8 | 25.7 MB | +83.5 MB | 9.9 s | 11–23 ms | 0/4 |
| `IDEA-CCNL/Erlangshen-Roberta-110M-NLI` int8 | 98.3 MB | +140.3 MB | 1.4 s | 15–44 ms | 6/6 |

**英文模型对中文完全失效**，不是「偏弱」：它对「完全不能识别图像」vs「本系统可以识别图像」给 `entailment 0.52`，对「数据库采用主从架构」这种无关内容给 `entailment 0.65`——四条中文用例全判成蕴含，说明它只在数词汇重叠，没有识别否定结构。中文场景必须用中文模型。

`id2label` 顺序各模型不同（mobilebert 是 `0=ENTAILMENT / 2=CONTRADICTION`，Erlangshen 是 `0=CONTRADICTION / 2=ENTAILMENT`），代码从 `config.json` 读，写死会整体错位。

## 各接口的行为

`SourceSpan.page` 是文档总页数，没有逐页文本映射。超出总页数或指向解析失败
页面的引用标记为 `INVALID_SOURCE_PAGE`；其余页码仍标记为
`SOURCE_PAGE_UNVERIFIED`，即使引文已在全文匹配。模型提示词要求只返回来源 ID
与逐字引文，不猜测页码或字符偏移。

## 生成、诊断与保存

- `POST /api/agent/runs/stream` 的请求带 `sources: [...]` 时，`mindmap_generate`
  把来源 ID 和完整原文以 JSON 边界交给模型；生成收尾按实际输入重建
  `source_usage`，不信任模型提供的摘要。未知来源与确实对不上原文的引用会被剔除
  （判定表见上节：父块越界与多处出现只告警、不剔除），原因通过 `diagnostics` 返回，
  并带上 `node_id`。节点本身保留，合法引用保留。
- 不带来源的生成（`intent=create` 且没有 `sources`）没有可追溯的来源记录；
  `validate_generated_content(sources=None)` 在契约校验前清空模型返回的摘要及所有
  节点引用。`node_grow` 预览帧也不携带未经核验的引用——它只有 `label` 与
  `expandable`。demo 不编造引用。
- `POST /api/optimization/check` 和 `/api/optimization/suggest` 可额外传
  `sources` 执行第三层；省略时只做原有结构诊断，传 `[]` 会明确报告缺失原文。
  两个接口都不改写导图或数据库，重复来源 ID 返回 422。
- `POST/PUT /api/mindmaps` 校验前两层并持久化引用，版本冲突仍返回 409。
  保存接口没有原文，不能执行第三层；重新核验时须把原文传给诊断接口。
  本功能不增加原文存储，引用匹配针对本次请求提供的原文，不代表外部事实核验。

诊断请求示例：

```json
{
  "content": {
    "title": "会议摘要",
    "root": {
      "id": "n1", "label": "发布安排", "children": [],
      "source_refs": [{"source_id": "meeting", "quote": "下周发布"}]
    },
    "source_usage": [{"source_id": "meeting", "kind": "text", "char_count": 9}]
  },
  "sources": [{
    "id": "meeting", "kind": "text", "status": "parsed", "text": "会议决定下周发布。"
  }]
}
```

针对引用的测试：在 `backend` 目录执行
`python -m pytest -p no:cacheprovider tests/test_source_refs.py tests/test_generation.py tests/test_stream.py`。
