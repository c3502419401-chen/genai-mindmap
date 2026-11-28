"""鱼骨图的类别"可不可变"，在**真模型**下用五个主题量一遍。

    python scripts/measure_fishbone_categories.py --free off   # 现行（固定 5M1E）
    python scripts/measure_fishbone_categories.py --free on    # 按主题定（开关开）
    python scripts/measure_fishbone_categories.py --free both  # 两组连跑，成对打印

**为什么必须真跑**：这件事成不成立只有一个判据——**"硬塞"的条数**：某一条原因
明显属于另一个类别（实证的那张图：主题是人的管理，「料」下面写着「员工技能不足」）。
硬塞不是契约问题、不是接线问题，`tests/` 里的 mock 造不出来，只有真模型能回答。

五个主题是**刻意挑的**，两组对照：

  ① 人的管理   "为什么团队协作效率低"            ← 5M1E 最不合适的主题
  ② 制造业     "为什么设备故障停机时间降不下来"   ← 5M1E 本来就合适（对照组）
  ③ 服务业     "为什么客户复购率上不去"
  ④ 流程问题   "为什么报销审批要两周"
  ⑤ 智力型     "为什么我们的推荐算法准确率不达标"

判硬塞靠人读：脚本把**每根骨下面的原因逐条打出来**，不做自动判定——"这条原因
属于哪个类别"是语义判断，写一个正则去猜它只会给出一个看起来很客观的错数。脚本
只做两件机械的事：类别清单、每根骨的原因条数，其余留给人。

凭据与外部检索的处理与 `scripts/measure_shapes.py` 完全一致（同一个理由）。
"""
# requires-optional: winreg
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

CREDENTIALS = (
    "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL", "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
)

#: 五个主题。`key` 只用于报告里的分组。
CASES = (
    {"key": "①人的管理", "goal": "为什么团队协作效率低"},
    {"key": "②制造业", "goal": "为什么设备故障停机时间降不下来"},
    {"key": "③服务业", "goal": "为什么客户复购率上不去"},
    {"key": "④流程", "goal": "为什么报销审批要两周"},
    {"key": "⑤智力型", "goal": "为什么我们的推荐算法准确率不达标"},
)


def _user_env(name: str) -> str:
    try:
        import winreg
    except ImportError:  # 仅 Windows 有：Linux/CI 上没有 HKCU\Environment 这一层可读
        return ""

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


def _fill_credentials() -> None:
    for name in CREDENTIALS:
        if not os.environ.get(name):
            value = _user_env(name)
            if value:
                os.environ[name] = value


async def _one(case: dict) -> dict:
    """跑一次 `_generate`，把鱼骨图压成"类别 + 每根骨的原因"。"""
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
    from app.agent.tools import GenerateArgs, _generate
    from app.contracts.mindmap import FishboneContent

    request = AgentRunRequest(
        goal=case["goal"], intent="create", diagram_type="fishbone",
        budget=RunBudget(max_model_calls=12, max_output_tokens=32768),
    )
    ctx = RunContext(request=request)
    started = time.perf_counter()
    result = await _generate(
        GenerateArgs(topic=case["goal"], branches=6), ctx)
    seconds = round(time.perf_counter() - started, 1)
    if not result.ok:
        return {"ok": False, "code": result.data.get("code"),
                "detail": result.content, "seconds": seconds}
    content = ctx.candidate
    if not isinstance(content, FishboneContent):
        return {"ok": False, "code": "not_fishbone", "detail": "", "seconds": seconds}
    return {
        "ok": True,
        "problem": content.problem,
        "categories": [bone.category for bone in content.bones],
        "bones": [
            {"category": bone.category,
             "causes": [child.label for child in bone.root.children]}
            for bone in content.bones
        ],
        "diagnostics": [item["code"] for item in (result.data.get("diagnostics") or [])],
        "digest": hashlib.sha256(
            content.model_dump_json().encode("utf-8")).hexdigest()[:12],
        "seconds": seconds,
    }


def _print_one(payload: dict) -> None:
    if not payload["ok"]:
        print(f"    失败：{payload['code']} {payload['detail']}")
        return
    print(f"    problem：{payload['problem']}")
    print(f"    类别（{len(payload['categories'])}）：{' / '.join(payload['categories'])}")
    for bone in payload["bones"]:
        causes = bone["causes"]
        print(f"      「{bone['category']}」{len(causes)} 条：")
        for cause in causes:
            print(f"        - {cause}")
    if payload["diagnostics"]:
        print(f"    诊断：{payload['diagnostics']}")
    print(f"    {payload['seconds']}s  指纹 {payload['digest']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--free", choices=("off", "on", "both"), default="both")
    parser.add_argument("--only", default="", help="只跑某个主题的 key（前缀也认）")
    parser.add_argument("--json", default="", help="把逐次结果写到这个文件")
    args = parser.parse_args(argv)

    _fill_credentials()
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ.setdefault("MINDMAP_DB_PATH", "data/measure_fishbone_categories.db")

    arms = ("off", "on") if args.free == "both" else (args.free,)
    cases = [case for case in CASES
             if not args.only or case["key"].startswith(args.only)]
    report: list[dict] = []
    for arm in arms:
        if arm == "on":
            os.environ["MINDMAP_FISHBONE_FREE_CATEGORIES"] = "1"
        else:
            os.environ.pop("MINDMAP_FISHBONE_FREE_CATEGORIES", None)
        print(f"\n========== 类别可变：{arm.upper()} ==========")
        for case in cases:
            print(f"\n----- {case['key']}：{case['goal']}（{arm}）")
            payload = asyncio.run(_one(case))
            _print_one(payload)
            report.append({"arm": arm, "case": case["key"],
                           "goal": case["goal"], **payload})

    print("\n===== 汇总（硬塞条数由人读上面的原因判）")
    for arm in arms:
        rows = [item for item in report if item["arm"] == arm and item.get("ok")]
        print(f"  {arm}: {len(rows)}/{len(cases)} 次成功")
        for item in rows:
            print(f"    {item['case']}：{len(item['categories'])} 类 "
                  f"{' / '.join(item['categories'])}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n逐次结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
