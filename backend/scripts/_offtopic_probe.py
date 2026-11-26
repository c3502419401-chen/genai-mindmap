"""跑题探针：同材料、同评分者，只换一级分支，看盲评能不能把"跑题"量出来。

背景（`docs/ACCEPTANCE-METRICS.md` §三）上一轮的留话是：
    "这两个自动口径量不出跑题：must 命中率比的是关键词在不在树上。S08 修完覆盖还是
     0.714 —— 分数没动，动的是一级分支讲的是『这份会评审了三个需求』还是『会议纪要
     怎么写』。"

要回答它，需要一个受控对照：**同一份材料、同一个评分者、唯一变量是一级分支**。
跑题的那 4 棵树（S08/S09/S10/S14）的树文件已经被 01:17 那次重跑覆盖掉了，但它们的
一级分支原文还留在 `data/acc_score_cards.md` 里（那是 0:21 生成的卡，正是 45% 的依据）。
所以可以拿"旧一级分支 + 新一级分支"配对，喂给同一个评分者。

两件事一起做：
1. 判定题：只给材料 + 一级分支，问 specific / generic。
2. 自动代理：一级分支 label 作为连续子串在材料原文里出现的比例（grounding rate）——
   跑题分支（"会议纪要的写作要求"）在材料里根本不存在，切题分支（"讨论要点"）在。

用法：
    python scripts/_offtopic_probe.py --repeat 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "data"
BLIND = DATA / "blind"
SAMPLES = Path(__file__).with_name("acc_samples.json")
BASE_URL = os.environ.get("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com")
API_KEY = os.environ.get("MINDMAP_LLM_API_KEY", "sk-7961cf33c9c94110b749e800adfb067d")
MODEL = os.environ.get("MINDMAP_LLM_MODEL", "deepseek-chat")

PAIRS = ["S08", "S09", "S10", "S14"]      # 上一轮判为"跑题"的四条

PROMPT = """你在做一次验收核查。下面是一份【材料】和这张思维导图的一级分支（顶层分类）。

请判断：这张图的一级分支讲的是
  A. "这份材料具体说了什么" —— 分支名来自材料里的真实内容/议题/模块
  B. "这类文档一般怎么处理、怎么写" —— 分支名来自该文体的通用知识框架
只输出 JSON：{"verdict":"A"|"B","reason":"一句话，指出哪几个分支名在材料原文里
找不到对应内容"}"""


def old_top_branches() -> dict[str, list[str]]:
    """从旧评分卡里抠出 45% 那一轮的一级分支原文。"""
    text = (DATA / "acc_score_cards.md").read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        sid = block[:3]
        m = re.search(r"一级分支（\d+）：(.+)", block)
        if not m:
            continue
        out[sid] = [p.strip() for p in re.split(r"[／/]", m.group(1)) if p.strip()]
    return out


def new_top_branches() -> dict[str, list[str]]:
    out = {}
    for sid in PAIRS:
        content = json.loads((DATA / "acc_trees" / f"{sid}.json").read_text(encoding="utf-8"))
        out[sid] = [(n.get("label") or "").strip()
                    for n in (content["root"].get("children") or [])]
    return out


def grounding(labels: list[str], text: str) -> float:
    """一级分支 label 整串是否作为连续子串出现在材料原文里。"""
    hit = sum(1 for label in labels if label and label in text)
    return round(hit / len(labels), 3) if labels else 0.0


def ask(tag: str, text: str, labels: list[str]) -> dict:
    import httpx

    body = (PROMPT + f"\n\n【材料】{text}\n\n【一级分支】\n"
            + "\n".join(f"- {x}" for x in labels))
    payload = {"model": MODEL, "messages": [{"role": "user", "content": body}],
               "temperature": 0.2, "max_tokens": 400,
               "response_format": {"type": "json_object"}}
    for attempt in range(3):
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(f"{BASE_URL}/chat/completions",
                                   headers={"Authorization": f"Bearer {API_KEY}"},
                                   json=payload)
                resp.raise_for_status()
                data = json.loads(resp.json()["choices"][0]["message"]["content"])
            return {"tag": tag, "ok": True, "verdict": data.get("verdict"),
                    "reason": data.get("reason")}
        except Exception as exc:                        # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (attempt + 1))
    return {"tag": tag, "ok": False, "error": last}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    samples = {s["id"]: s for s in json.loads(SAMPLES.read_text(encoding="utf-8"))["samples"]}
    old, new = old_top_branches(), new_top_branches()

    jobs, sides = [], []
    for sid in PAIRS:
        for kind, labels in (("old", old.get(sid, [])), ("new", new.get(sid, []))):
            if not labels:
                continue
            tag = f"{sid}-{kind}"
            jobs.append((tag, samples[sid]["text"], labels))
            sides.append({"tag": tag, "sample": sid, "kind": kind, "labels": labels,
                          "grounding": grounding(labels, samples[sid]["text"])})

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda j: ask(*j), jobs))

    for row in rows:
        side = next(s for s in sides if s["tag"] == row["tag"])
        print(f"  {row['tag']:9s} verdict={row.get('verdict')} "
              f"grounding={side['grounding']:.2f}  {row.get('reason','')[:60]}")

    payload = {"model": MODEL, "repeat": args.repeat, "rows": rows, "sides": sides}
    out = BLIND / f"offtopic-probe-r{args.repeat}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 自动代理指标：20 棵新树 / 13 棵旧树 的一级分支 grounding ----
    key = json.loads((BLIND / "key.json").read_text(encoding="utf-8"))
    table = {"new": {}, "old": {}}
    for kind, mapping, tree_dir in (("new", key["new"], "acc_trees"),
                                   ("old", key["old"], "acc_trees-before-fix")):
        for code, meta in mapping.items():
            path = DATA / tree_dir / f"{meta['sample']}.json"
            content = json.loads(path.read_text(encoding="utf-8"))
            labels = [(n.get("label") or "").strip()
                      for n in (content["root"].get("children") or [])]
            table[kind][meta["sample"]] = grounding(labels, samples[meta["sample"]]["text"])
    (BLIND / "grounding.json").write_text(json.dumps(table, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    for kind in ("new", "old"):
        vals = list(table[kind].values())
        print(f"{kind}: n={len(vals)} 一级分支 grounding 均值 "
              f"{sum(vals)/len(vals):.3f}  中位 {sorted(vals)[len(vals)//2]:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
