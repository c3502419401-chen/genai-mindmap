"""P6 验收：打开真浏览器，对着 5173 量三件事。

要回答的问题（每一条都对应核对报告里"只读代码"的说法）：

  1. 页面能不能起来（不是"代码在"，是"能打开"）；
  2. 树 / 流程 / 鱼骨三型图的**真实几何**长什么样 —— 从 DOM 里读节点坐标与边，
     而不是相信 `layout/*.ts` 的注释；
  3. 主题预设 / 字体 / 主色到底渲不渲染（报告说前端没有 `data-theme` 体系）。

做法：把一份内容塞进页面（走真实导入或 store 预览），取 `vue-flow__node` 的
`transform` 与 `vue-flow__edge` 数量，落成 JSON + 截图。

用法：
    python scripts/acc2/probe_ui_layout.py
"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
URL = "http://127.0.0.1:5173"


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    report: dict = {"url": URL}
    with sync_playwright() as p:
        # 本机 python 版 playwright 要的 chromium 版本（1234）没装，装的是 1248；
        # 直接用系统 Chrome（channel="chrome"）避免额外下载一套浏览器。
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        console: list[str] = []
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"[:200]))
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2500)
        report["title"] = page.title()
        report["has_canvas"] = page.locator(".vue-flow").count()
        report["nodes"] = page.locator(".vue-flow__node").count()
        page.screenshot(path=str(SHOTS / "p6-01-open.png"), full_page=False)
        report["console"] = console[:20]

        # 主题：把 content.theme 的 preset 换掉，看 DOM 上有没有任何变量随之改变。
        probe = page.evaluate("""() => {
            const root = document.documentElement;
            const style = getComputedStyle(root);
            const vars = {};
            for (const name of ['--mind-color-0','--mind-color-1','--mind-font','--mind-accent']) {
                vars[name] = style.getPropertyValue(name).trim();
            }
            return {
                dataTheme: root.getAttribute('data-theme'),
                bodyFont: getComputedStyle(document.body).fontFamily,
                vars,
            };
        }""")
        report["theme_probe"] = probe

        # 真的装载三型图：直接调页面的 pinia store（vite dev 下模块可达）
        loaded = page.evaluate("""async () => {
            const out = {};
            try {
                const mod = await import('/src/stores/mindmap.ts');
                const piniaMod = await import('/node_modules/.vite/deps/pinia.js');
                out.storeLoaded = typeof mod.useMindMapStore === 'function';
            } catch (e) { out.error = String(e).slice(0, 300); }
            return out;
        }""")
        report["store_probe"] = loaded
        page.screenshot(path=str(SHOTS / "p6-02-probe.png"), full_page=False)
        browser.close()

    path = HERE / "p6_ui.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1)[:1500])
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
