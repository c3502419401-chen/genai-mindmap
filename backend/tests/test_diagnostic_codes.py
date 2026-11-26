"""诊断码的**产出点**只有一处：这条挡的是"同一件事两份实现各自演化"。

## 为什么值得一条用例

`SPARSE_BRANCH`（"这一支的要点数没达标"）曾经有两个产出点：

    agent/tools.py::_generate_deep      ← 活的那条：分片生成里按"要求几个、拿到几个"
                                          判，还说得清为什么不足（补调过仍不够 /
                                          额度不够），并排除从未展开的分支
    generation/service.py::diagnose_density  ← 零调用方；阈值写死 2，没有"为什么"，
                                          还把从未展开的分支算成稀疏

第二份已经删掉。它不会被"功能缺失"发现——两份都在、都能跑、用例都绿；被发现的
唯一途径是有人逐字读两边的口径。所以这里把"只有一个产出点"钉住：哪天有人再养一份，
这条红，而不是等用户在同一块面板上读到两句不一样的话。

顺带一条边界：**这里只管产出点，不管前端要不要给中文标题**。生成链上的诊断在
`AgentPanel` 只渲染后端给的 `message`（整句中文），码本身不上屏；结构面板
（`optimization.service.check()`）才把码当标题显示。这条分界的判断写在
`docs/WIRING-GAPS.md` 第七节。
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP = BACKEND_DIR / "app"


def _producers(code: str) -> list[str]:
    """产出这个诊断码的文件（按 `code="..."` 字面量扫描）。"""
    pattern = re.compile(rf'code="{re.escape(code)}"')
    found = []
    for path in sorted(APP.rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            found.append(path.relative_to(BACKEND_DIR).as_posix())
    return found


def test_sparse_branch_has_exactly_one_producer():
    assert _producers("SPARSE_BRANCH") == ["app/agent/tools.py"]


def test_density_is_not_a_diagnostic_source_any_more():
    """`generation/service.py` 不再产出任何 `SPARSE_BRANCH`。

    与上一条重叠是有意的：上一条说"只有一个"，这一条说"是哪一个"。两条一起，
    失败信息才直接指向"你是不是把 `diagnose_density` 那一套又搬回来了"。
    """
    assert not any(
        path.endswith("generation/service.py") for path in _producers("SPARSE_BRANCH")
    )
