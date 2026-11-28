"""P6 验收（续）：在真浏览器里装载树 / 鱼骨 / 流程三型图，把几何读出来。

为什么要读几何而不看截图：三次截图能看出"画出来了"，但看不出"鱼骨头在右、
六根骨对称、流程的阶段是上下分层"。所以直接取 `vue-flow__node` 的真实
`transform` 与 `vue-flow__edge` 的 type/路径，落成数字再判。

怎么把内容塞进页面：vite dev 下 `/src/**` 是可 import 的模块，pinia 允许
`useStore(pinia)` 显式绑定实例——所以可以直接调用页面的 store，走的是**产品
自己的** `preview()` 路径（而不是另写一份渲染）。

用法：
    python scripts/acc2/probe_ui_shapes.py
"""
# requires-optional: playwright
from __future__ import annotations

import json
import sys
from pathlib import Path

try:  # 可选依赖：UI 探针才需要，故意不进 requirements.txt（CI 不装浏览器）
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover — 没装时由 main() 报错退出
    sync_playwright = None

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
URL = "http://127.0.0.1:5173"

#: 三型图的内容由页面侧构造（避免把大 JSON 塞进 Python 字符串再转义）。
LOAD = """
async ([kind, payload]) => {
  const app = document.querySelector('#app').__vue_app__;
  const pinia = app.config.globalProperties.$pinia;
  const mod = await import('/src/stores/mindmap.ts');
  const store = mod.useMindMapStore(pinia);
  store.preview(payload, 'right');
  if (store.expandAllInView) store.expandAllInView();
  await new Promise(r => setTimeout(r, 700));
  const nodes = [...document.querySelectorAll('.vue-flow__node')].map(el => ({
    id: el.getAttribute('data-id') || el.id,
    transform: getComputedStyle(el).transform,
    left: el.style.left, top: el.style.top,
    label: (el.textContent || '').trim().slice(0, 30),
  }));
  const edges = [...document.querySelectorAll('.vue-flow__edge')].map(el => ({
    id: el.id,
    type: [...el.classList].find(c => c.startsWith('vue-flow__edge-')) || '',
  }));
  const positions = nodes.map(n => {
    const m = /matrix\\(([^)]+)\\)/.exec(n.transform || '');
    const parts = m ? m[1].split(',').map(Number) : [1,0,0,1,0,0];
    return { id: n.id, label: n.label, x: parts[4], y: parts[5] };
  });
  return { kind, nodeCount: nodes.length, edgeCount: edges.length,
           nodes: positions, edges: edges.slice(0, 12),
           storeNodes: store.nodes.length, storeEdges: store.edges.length };
}
"""


def tree_content() -> dict:
    return {
        "title": "碳中和路径",
        "diagram_type": "tree",
        "root": {
            "id": "root", "label": "碳中和路径",
            "children": [
                {"id": "a", "label": "能源结构调整", "children": [
                    {"id": "a1", "label": "风电与光伏", "children": []},
                    {"id": "a2", "label": "煤电灵活性改造", "children": []},
                ]},
                {"id": "b", "label": "工业过程减排", "children": [
                    {"id": "b1", "label": "水泥熟料替代", "children": []},
                ]},
                {"id": "c", "label": "碳汇与碳捕集", "children": []},
            ],
        },
    }


def fishbone_content() -> dict:
    cats = ["人", "机", "料", "法", "环", "测"]
    return {
        "title": "交付延期",
        "diagram_type": "fishbone",
        "problem": "为什么交付总是延期",
        "bones": [
            {"category": c, "root": {
                "id": c, "label": c,
                "children": [
                    {"id": f"{c}1", "label": f"{c}原因一", "children": []},
                    {"id": f"{c}2", "label": f"{c}原因二", "children": []},
                ]}}
            for c in cats
        ],
    }


def flow_content() -> dict:
    return {
        "title": "报销流程",
        "diagram_type": "flowchart",
        "stages": [
            {"id": "s1", "name": "提交", "root": {"id": "s1r", "label": "提交", "children": [
                {"id": "s1a", "label": "填单", "children": []}]}},
            {"id": "s2", "name": "审查", "root": {"id": "s2r", "label": "审查", "children": [
                {"id": "s2a", "label": "核对发票", "children": []}]}},
            {"id": "s3", "name": "付款", "root": {"id": "s3r", "label": "付款", "children": []}},
        ],
        "edges": [
            {"id": "e1", "source": "s1", "target": "s2"},
            {"id": "e2", "source": "s2", "target": "s3"},
            {"id": "e3", "source": "s3", "target": "s1", "label": "驳回"},
        ],
    }


def main() -> int:
    if sync_playwright is None:
        print("错误：这个探针要 playwright（pip install playwright && playwright install chromium）。",
              file=sys.stderr)
        return 2
    SHOTS.mkdir(exist_ok=True)
    report: dict = {}
    cases = {"tree": tree_content(), "fishbone": fishbone_content(), "flow": flow_content()}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)[:200]))
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(1500)
        for kind, payload in cases.items():
            try:
                result = page.evaluate(LOAD, [kind, payload])
            except Exception as exc:  # noqa: BLE001 - 页面侧异常要原样记录
                result = {"kind": kind, "error": str(exc)[:400]}
            report[kind] = result
            page.screenshot(path=str(SHOTS / f"p6-{kind}.png"))
            geometry = _geometry(result)
            result["geometry"] = geometry
            print(f"== {kind}: nodes={result.get('nodeCount')} edges={result.get('edgeCount')} "
                  f"{json.dumps(geometry, ensure_ascii=False)[:300]}")
        report["page_errors"] = errors
        browser.close()

    path = HERE / "p6_shapes.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {path}")
    return 0


def _geometry(result: dict) -> dict:
    """把坐标折成能判的读数：包围盒、主轴方向、是否左右对称。"""
    nodes = result.get("nodes") or []
    if not nodes:
        return {}
    xs = [n["x"] for n in nodes]
    ys = [n["y"] for n in nodes]
    out = {
        "bbox": {"x": [round(min(xs), 1), round(max(xs), 1)],
                 "y": [round(min(ys), 1), round(max(ys), 1)]},
        "distinct_x": len({round(x) for x in xs}),
        "distinct_y": len({round(y) for y in ys}),
        "root": next((n for n in nodes if n["id"] in ("root", "r")), None),
    }
    root = out["root"]
    if root:
        out["root_is_rightmost"] = root["x"] >= max(xs) - 1
        out["root_is_leftmost"] = root["x"] <= min(xs) + 1
        out["root_is_topmost"] = root["y"] <= min(ys) + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
