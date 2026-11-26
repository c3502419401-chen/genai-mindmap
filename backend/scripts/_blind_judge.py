"""用第二个评分者（deepseek-chat）独立给盲评卡打分，用于算评分者间一致率。

设计要点：
- 只喂 `data/blind/cards_*.md` 里的东西（代号 + 材料原文 + must 清单 + label 大纲 +
  detail 抽检），不给 S 编号、不给 must_rate / ref_coverage、不给任何"修复了什么"的背景。
- 档位表写死在 prompt 里（这是本次补的口径，原文档没有档位定义），两份评分用同一张表。
- 每张卡跑两遍：
    A 遍 = 只打四维（主口径）
    B 遍 = 四维 + 一个"是否切题"的是非题（测"盲评能不能量出跑题"）
  A/B 的四维分相同与否，就是评分者的自测复现率（test-retest）。

用法：
    python scripts/_blind_judge.py --kind new --repeat 1 --workers 4
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

BLIND = BACKEND / "data" / "blind"
BASE_URL = os.environ.get("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com")
API_KEY = os.environ.get("MINDMAP_LLM_API_KEY", "sk-7961cf33c9c94110b749e800adfb067d")
MODEL = os.environ.get("MINDMAP_LLM_MODEL", "deepseek-chat")

RUBRIC = """你是第三方验收评分员。下面给出一份【材料】和一张据它生成的思维导图（缩进大纲
表示层级，与材料原文无关的其他背景一律不予考虑 —— 你不知道这张图是怎么生成的、也不
知道它属于哪个版本）。

请按四个维度各打一个 1–5 的整数分。档位定义如下，必须严格按档位判：

【覆盖 cover】材料自己讲的核心内容，在树上出现到什么程度。
 5 = 材料的关键骨架（章节 / 议题 / 模块 / 结论 / 数据）几乎都成了一级或二级节点
 4 = 关键骨架在，缺 1–2 个次要要素，或缺的那个只在 detail 里被顺带提到
 3 = 材料骨架命中一半左右，相当一部分关键内容只出现在 detail 里、没成节点
 2 = 不到一半；或一级分支讲的是"这类文档一般怎么处理"，材料的具体内容被压进 detail
 1 = 与材料无关，或没有可评的树

【层次 hierarchy】树的分层是否成立、是否互斥穷尽。
 5 = 一级分支互不重叠且合起来覆盖材料骨架；深度 4–6；无单子继承链、无跨支同名
 4 = 基本成立，深度或各支扇出略不均
 3 = 骨架可辨，但有一支明显不合群 / 某支被压平 / 出现 1–2 处跨支同名
 2 = 一级分支互相重叠或严重不均，层级被压平
 1 = 无结构可评

【无捏造 nofabricate】节点里的具体断言（数字、人名、学时、百分比、结论）在材料里有没有依据。
 5 = 找不到无出处的具体断言
 4 = 有 1–2 处合理概括/外推，不算错
 3 = 有 3 处以上外推，或某一支整体是模型自己的通用知识
 2 = 一级分支整体是通用知识，材料只被当作举例素材
 1 = 大量材料之外的事实

【可读 readable】label 自身是否读得通。
 5 = label 短、完整、同级命名同构
 4 = 个别 label 偏长或命名不同构
 3 = 出现半句截断 / 超长 label / 重复 label 之一，影响阅读
 2 = 多处截断，或"父名 / 子名"被拼成一句长话
 1 = 不可读

只输出 JSON，不要解释在 JSON 之外：
{"cover":n,"hierarchy":n,"nofabricate":n,"readable":n,
 "why":{"cover":"一句话","hierarchy":"一句话","nofabricate":"一句话","readable":"一句话"}}"""

ASK_TOPIC = """
另外再回答一个判断（不影响上面四个分）：
 "这棵树讲的是『这类文档一般该怎么处理/怎么写』还是『这份材料具体说了什么』？"
 用 "specific" 表示讲这份材料，用 "generic" 表示讲通用知识。
 在 JSON 里加一个键 "topic": "specific" | "generic"。"""


def split_cards(text: str) -> dict[str, str]:
    blocks = re.split(r"^### ", text, flags=re.M)[1:]
    out = {}
    for block in blocks:
        code, _, body = block.partition("\n")
        out[code.strip()] = body.strip()
    return out


def ask(code: str, body: str, with_topic: bool) -> dict:
    import httpx

    prompt = RUBRIC + (ASK_TOPIC if with_topic else "") + f"\n\n----- 待评卡片 -----\n\n{body}"
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    last = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=180) as client:
                resp = client.post(f"{BASE_URL}/chat/completions",
                                   headers={"Authorization": f"Bearer {API_KEY}"},
                                   json=payload)
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(raw)
            dims = {k: int(data[k]) for k in ("cover", "hierarchy", "nofabricate", "readable")}
            if not all(1 <= v <= 5 for v in dims.values()):
                raise ValueError(f"分数越界: {dims}")
            return {"code": code, "ok": True, **dims,
                    "why": data.get("why") or {},
                    "topic": data.get("topic"), "attempts": attempt + 1}
        except Exception as exc:                       # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (attempt + 1))
    return {"code": code, "ok": False, "error": last}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="new", choices=["new", "old"])
    ap.add_argument("--repeat", type=int, default=1, help="同一个评分者跑第几遍")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--with-topic", action="store_true")
    args = ap.parse_args()

    cards = split_cards((BLIND / f"cards_{args.kind}.md").read_text(encoding="utf-8"))
    print(f"{args.kind}: {len(cards)} cards, repeat={args.repeat}, "
          f"with_topic={args.with_topic}", flush=True)

    def work(item):
        code, body = item
        row = ask(code, body, args.with_topic)
        print(f"  {code} {row.get('cover')}/{row.get('hierarchy')}/"
              f"{row.get('nofabricate')}/{row.get('readable')} "
              f"{row.get('topic') or ''} {'' if row['ok'] else row.get('error')}", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(work, sorted(cards.items())))

    suffix = f"r{args.repeat}" + ("-topic" if args.with_topic else "")
    out = BLIND / f"judge-{MODEL}-{args.kind}-{suffix}.json"
    out.write_text(json.dumps({"model": MODEL, "kind": args.kind,
                               "with_topic": args.with_topic, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for r in rows if r["ok"])
    print(f"-> {out}  ({ok}/{len(rows)} ok)")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
