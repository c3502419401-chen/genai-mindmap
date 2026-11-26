"""P6 的配色与图标子项：把带 `color` / `icon` 的真实产物装进页面，读数。

生成侧确实写了颜色与 emoji（`{color:"blue", icon:"👤"}` 这类六组），但"写了"不等于
"画出来了"——`_palette_fill()` 产出的值要经过 `[data-node-color]` 的样式表才变成像素。
这里读 computed style 与节点文本，两样一起判。

用法：
    python scripts/acc2/probe_ui_colors.py        # 用 p6_pref_fishbone.json 的六根骨
"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
URL = "http://127.0.0.1:5173"

SCRIPT = """
async (payload) => {
  const app = document.querySelector('#app').__vue_app__;
  const pinia = app.config.globalProperties.$pinia;
  const mod = await import('/src/stores/mindmap.ts');
  const store = mod.useMindMapStore(pinia);
  store.preview(payload, 'right');
  if (store.expandAllInView) store.expandAllInView();
  await new Promise(r => setTimeout(r, 800));
  return [...document.querySelectorAll('.vue-flow__node')].map(el => {
    const box = el.querySelector('.node, .mind-node, div');
    const style = box ? getComputedStyle(box) : null;
    return {
      id: el.getAttribute('data-id'),
      text: (el.textContent || '').trim().slice(0, 40),
      attrs: [...el.querySelectorAll('[data-node-color]')].map(n => n.getAttribute('data-node-color')),
      bg: style ? style.backgroundColor : null,
      borderColor: style ? style.borderColor : null,
      hasEmoji: /[\\u{1F300}-\\u{1FAFF}\\u{2600}-\\u{27BF}]/u.test(el.textContent || ''),
    };
  });
}
"""


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    data = json.loads((HERE / "p6_pref_fishbone.json").read_text(encoding="utf-8"))
    content = data["content"]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(1200)
        nodes = page.evaluate(SCRIPT, content)
        page.screenshot(path=str(SHOTS / "p6-colors.png"))
        browser.close()

    bones = {bone["category"]: bone["root"] for bone in content["bones"]}
    rows = []
    for node in nodes:
        label = node["text"].rstrip("−-").strip()
        bone = bones.get(label)
        rows.append({
            "text": node["text"][:20],
            "expected_color": (bone or {}).get("color"),
            "expected_icon": (bone or {}).get("icon"),
            "data_colors": node["attrs"],
            "bg": node["bg"],
            "border": node["borderColor"],
            "has_emoji": node["hasEmoji"],
        })
    report = {"total": len(nodes), "bones": rows}
    path = HERE / "p6_colors.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for row in rows:
        if row["expected_color"]:
            print(f"{row['text'][:12]:14s} color={row['expected_color']:7s} "
                  f"data={row['data_colors']} bg={row['bg']} icon={row['expected_icon']} "
                  f"emoji_in_dom={row['has_emoji']}")
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
