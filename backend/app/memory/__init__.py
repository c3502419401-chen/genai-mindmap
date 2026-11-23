"""跨会话记忆：观测 → 命题的两级压缩 + 双路检索。

五个模块，各管一件事，互相之间只有单向依赖：

  - `store`         表、向量存取、检索与衰减（记忆 schema 的唯一所有者，五张表都在这里建）
  - `observations`  观测：动作级事实（一次 run、一次保存），带 `refs` 回指原始行
  - `propositions`  命题：跨事件的判断（带 confidence / decay / observation_ids）
  - `recall`        双路检索：最近观测（时间）+ 最相关命题（向量），按名次 RRF 合并
  - `summarize`     会话结束判定、会话总结、以及"事件片"的触发（抽观测 + 归纳命题）
  - `router`        HTTP 面：列、加、删、按主题检索

`store` 在最下层，`recall` 在它们之上；`summarize` 依赖 `observations` 与
`propositions`，没有反向依赖——所以 `store.ensure_schema` 可以在不 import 上层模块的
情况下建出所有表（DDL 集中在 `store`，见那里的注释）。

**两级压缩的分工**（论文 arXiv:2611.13457 的骨架）：

  - 观测不带解释，只记"发生了什么、什么时候"；
  - 命题是跨事件的判断，必须带 `observation_ids`（那是二部图的边）。

第一级一旦带上解释，第二级就没有可压缩的东西了——这条边界是整套设计的地基，详细论证
在 `observations.py` 与 `propositions.py` 的模块 docstring 里，实测数据与已知缺口在
`docs/MEMORY-PROPOSITION.md`。

为什么不复用 `app/prefs`：那层是**用户显式写的参数与备注**，正确位置是请求层与一段固定
prompt；这层是**系统从历史里推断的条目**，规模会从个位数长到几百上千，正确位置是向量
检索。两者的可信度、写入源、失效方式都不同，混在一张表里会让"我明明写了它却没用"再发生
一次（`user_prefs` 的注释里写过这件事的代价）。

**只做"记"，不做"学"**：Coco 的第三段（隐式反馈学习）要几百条真实偏好对，而这份库连
`edit_events` 的 `add` / `move` 都是 0 条。在"可选阅"解决之前做"自动学"，方向是反的
（论文 Appendix A 的第一条 limitation 也是这件事）。
"""
