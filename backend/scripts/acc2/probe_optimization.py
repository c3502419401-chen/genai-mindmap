"""P8 / P12 验收探针（第二版）：五种真实输入，逐条记录接口返回。

第一版探针有三个输入造得不像真的（鱼骨/流程写成了树的形状，被联合类型解析成
`MindMapContent`，于是"后端拒鱼骨"这条根本没被验到）。这一版按契约形状造：

  1. `tree_clean`   —— 真实生成的好树（P5 的产物），看"没问题时是不是真的不报"；
  2. `tree_flawed`  —— 人为做出 重复标签 / 近重复 / 单子节点 / 浅分支 / 缺说明；
  3. `tree_relations` —— 带 4 种 `relations` 的树，看有没有"因果/关系"类诊断；
  4. `fishbone`     —— 真鱼骨形状（`problem` + `bones`），预期被路由层 422 拦住；
  5. `flow`         —— 真流程形状（`stages` + `edges`），同上。

用法：
    python scripts/acc2/probe_optimization.py
"""
from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()
    return opener


def post(opener, path: str, body: dict) -> tuple[int, dict | str]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}{path}", data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with opener.open(request, timeout=180) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def node(nid: str, label: str, children: list | None = None, detail: str | None = None) -> dict:
    item: dict = {"id": nid, "label": label, "children": children or []}
    if detail:
        item["detail"] = detail
    return item


def tree_flawed() -> dict:
    """五类缺陷各一处，全部按契约合法（能存得下，只是结构不干净）。"""
    return {
        "title": "缺陷样本",
        "root": node("root", "缺陷样本", [
            # 同级重复标签（逐字）
            node("a1", "成本控制"),
            node("a2", "成本控制"),
            # 近重复（只多插了两个字）
            node("b1", "供应链风险管理"),
            node("b2", "供应链风险的管理"),
            # 单子节点 + 该子树很深（对面是浅叶子）
            node("c1", "技术选型", [
                node("c1a", "模型层", [
                    node("c1a1", "推理成本", [
                        node("c1a1a", "缓存命中率", detail="命中率决定单次生成的实际单价。"),
                    ]),
                ]),
            ]),
            # 浅分支叶子（与上面的深度形成对比）
            node("d1", "组织保障"),
        ]),
    }


def tree_relations() -> dict:
    base = tree_flawed()
    base["relations"] = [
        {"id": "r1", "source": "a1", "target": "b1", "kind": "depends_on",
         "why": "成本控制依赖供应链风险管理的结论", "provenance": "ai"},
        {"id": "r2", "source": "c1a1", "target": "d1", "kind": "causes",
         "why": "推理成本高导致组织上要加预算审批", "provenance": "ai"},
        {"id": "r3", "source": "a2", "target": "d1", "kind": "conflicts",
         "why": "压成本与扩编制互斥", "provenance": "ai"},
        {"id": "r4", "source": "b2", "target": "c1", "kind": "references",
         "why": "风险清单引用技术选型的结论", "provenance": "user"},
    ]
    return base


def fishbone() -> dict:
    """**真鱼骨形状**：没有 `root`，有 `problem` + `bones`。"""
    return {
        "title": "交付延期分析",
        "diagram_type": "fishbone",
        "problem": "为什么交付总是延期",
        "bones": [
            {"category": "人", "root": node("人", "人", [node("人1", "排期过满")])},
            {"category": "机", "root": node("机", "机", [node("机1", "构建机排队")])},
        ],
    }


def flow() -> dict:
    """**真流程形状**：`stages` + 阶段间 `edges`，没有 `root`。"""
    return {
        "title": "报销流程",
        "diagram_type": "flowchart",
        "stages": [
            {"id": "s1", "name": "提交", "root": node("s1r", "提交", [node("s1a", "填单")])},
            {"id": "s2", "name": "审查", "root": node("s2r", "审查", [node("s2a", "核对发票")])},
        ],
        "edges": [{"id": "e1", "source": "s1", "target": "s2"}],
    }


def summarize_check(payload) -> dict:
    if not isinstance(payload, dict):
        return {"raw": str(payload)[:300]}
    diagnostics = payload.get("diagnostics") or []
    return {
        "count": len(diagnostics),
        "codes": dict(Counter(item.get("code") for item in diagnostics)),
        "levels": dict(Counter(item.get("level") for item in diagnostics)),
        "items": [
            {k: item.get(k) for k in ("code", "level", "node_id", "message")}
            for item in diagnostics
        ],
    }


def summarize_suggest(payload) -> dict:
    if not isinstance(payload, dict):
        return {"raw": str(payload)[:300]}
    items = payload.get("suggestions") or []
    return {
        "count": len(items),
        "kinds": dict(Counter(item.get("kind") for item in items)),
        "items": [
            {k: item.get(k) for k in ("kind", "node_id", "message")} for item in items
        ],
    }


def main() -> int:
    p5 = json.loads((HERE / "p5_ppt.json").read_text(encoding="utf-8"))
    source = json.loads((HERE / "p5_pptx_source.json").read_text(encoding="utf-8"))["source"]
    # 生成侧用的是它自己分配的 source id（见 SSE `sources` 事件的 mapping）。
    # 不改这一处，`check` 会对每条引用报 SOURCE_TEXT_UNAVAILABLE。
    source_with_gen_id = dict(source, id=p5["content"]["source_usage"][0]["source_id"])

    opener = session()
    cases = {
        "tree_clean": {"content": p5["content"]},
        "tree_flawed": {"content": tree_flawed()},
        "tree_relations": {"content": tree_relations()},
        "fishbone": {"content": fishbone()},
        "flow": {"content": flow()},
    }

    report: dict = {}
    for name, body in cases.items():
        status, payload = post(opener, "/api/optimization/check", body)
        entry = {"status": status, **summarize_check(payload)}
        if status != 200:
            entry["detail"] = (payload.get("detail") if isinstance(payload, dict) else payload)
        report[f"check:{name}"] = entry
        print(f"== check:{name} -> {status} codes={entry.get('codes')}")

    for name in ("tree_flawed", "tree_relations"):
        status, payload = post(opener, "/api/optimization/suggest", cases[name])
        report[f"suggest:{name}"] = {"status": status, **summarize_suggest(payload)}
        print(f"== suggest:{name} -> {status} kinds="
              f"{report[f'suggest:{name}'].get('kinds')}")

    # 引用核验：同一棵树，sources 的 id 对齐 / 不对齐各跑一次
    for tag, src in (("sources_aligned", source_with_gen_id),
                     ("sources_raw_id", source)):
        status, payload = post(opener, "/api/optimization/check",
                               {"content": p5["content"], "sources": [src]})
        entry = {"status": status, **summarize_check(payload)}
        report[f"refs:{tag}"] = entry
        print(f"== refs:{tag} -> {status} codes={entry.get('codes')}")

    path = HERE / "p8_p12_optimization.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
