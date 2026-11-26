"""P14 验收：真粘一段碎片化会议纪要，看生成出来的分支有没有"信息类型"语义。

判据（来自计划书原句）：产物里是否出现「问题描述 / 解决方案 / 待验证假设」这类
**按信息类型**的分支，而不是"通用骨架 + 层级树"。

为了让这个判定公平，喂进去的文本把三类信息写得非常显式（"问题：" "方案："
"待验证："开头），模型如果按语义归类，没有理由不这么分。之后逐节点扫标签与
正文，统计三类词的出现位置——出现在**一级/二级分支标签**上才算归类，出现在
叶子正文里只是复述。

用法：
    python scripts/acc2/probe_p14.py
"""
from __future__ import annotations

import http.cookiejar
import json
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"

#: 一段会议纪要。三类信息各自带显式标记词，夹杂一句闲聊（真实纪要都有）。
FRAGMENT = """周一例会随手记：
问题：订单查询接口 P99 已经 2.8 秒，客服投诉集中在下午三点以后。
问题：灰度机器只有两台，压测结论不可信。
方案：给热点商品加一级缓存，TTL 五分钟。
方案：灰度扩到八台，按 5% 流量分段放。
待验证：缓存命中率能不能到 60% 以上，如果只有 30% 那 QPS 仍然顶不住。
待验证：把查询拆成读模型之后，跨表 join 会不会反而变慢。
另外老王说他下周出差，评审往后挪。"""

#: 三类语义关键词（判定用）。计划书原文的三个标签 + 常见同义写法。
LABELS = {
    "问题描述": ("问题", "痛点", "风险", "现状", "故障", "投诉"),
    "解决方案": ("方案", "对策", "措施", "解法", "路径", "建议"),
    "待验证假设": ("待验证", "假设", "验证", "猜想", "待确认"),
}


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()
    return opener


def main() -> int:
    opener = session()
    # `kind` 的枚举只有 text / markdown（契约如此），"会议纪要"在数据里就是 text。
    payload = json.dumps({"text": FRAGMENT, "kind": "text"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/ingestion/text", data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with opener.open(request, timeout=60) as response:
        source = json.loads(response.read().decode("utf-8"))["source"]
    print(f"source: id={source['id'][:8]} kind={source.get('kind')} "
          f"chars={len(source.get('text') or '')}")
    (HERE / "p14_source.json").write_text(
        json.dumps({"source": source}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def analyze(label: str = "p14_fragment") -> None:
    """对已落盘的生成结果做三级统计（分支标签 / 叶子正文 / 整树无命中）。"""
    run = json.loads((HERE / f"{label}.json").read_text(encoding="utf-8"))
    content = run["content"]
    rows: list[dict] = []

    def walk(item: dict, depth: int) -> None:
        rows.append({"id": item["id"], "label": item["label"], "depth": depth,
                     "is_leaf": not (item.get("children") or []),
                     "detail": (item.get("detail") or "")[:80]})
        for child in item.get("children") or []:
            walk(child, depth + 1)

    walk(content["root"], 1)
    branches = [row for row in rows if not row["is_leaf"]]
    hits: dict[str, dict] = {}
    for name, words in LABELS.items():
        in_branch = [row["label"] for row in branches
                     if any(word in row["label"] for word in words)]
        in_any = [row["label"] for row in rows
                  if any(word in row["label"] for word in words)]
        hits[name] = {"branch_labels": in_branch[:8], "any_label_count": len(in_any)}

    out = {
        "label": label,
        "nodes": len(rows),
        "branches": len(branches),
        "max_depth": max(row["depth"] for row in rows),
        "first_level": [row["label"] for row in rows if row["depth"] == 2],
        "semantic_hits": hits,
        "top_branch_labels": [row["label"] for row in branches if row["depth"] <= 2],
    }
    path = HERE / f"{label}_analysis.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        analyze(*sys.argv[2:])
    else:
        raise SystemExit(main())
