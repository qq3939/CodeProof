"""评测命令行：

    uv run python -m app.eval run --suite small
    uv run python -m app.eval run --suite petclinic --retrieval hybrid --baseline
    uv run python -m app.eval check --suite small   # 只检查数据集格式（CI 用）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.logging import setup_logging


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="跑评测集并生成报告")
    r.add_argument("--suite", required=True)
    r.add_argument("--retrieval", choices=["agentic", "vector", "hybrid"], default=None, help="不填则三种都跑")
    r.add_argument("--baseline", action="store_true", help="同时跑基线（通用 Agent + 提示词）")
    c = sub.add_parser("check", help="检查数据集格式")
    c.add_argument("--suite", required=True)
    args = p.parse_args()
    setup_logging("WARNING")
    if args.cmd == "check":
        from app.eval.runner import load_suite

        s = load_suite(args.suite)
        problems = []
        qids = {q["id"] for q in s.questions}
        for q in s.questions:
            if not 2 <= len(q["key_points"]) <= 5:
                problems.append(f"{q['id']} 要点数不在 2-5 之间")
        for a in s.answers:
            if a["question_id"] not in qids:
                problems.append(f"{a['id']} 指向不存在的题")
            lab = s.labels.get(a["id"])
            q = next((q for q in s.questions if q["id"] == a["question_id"]), None)
            if lab is None:
                problems.append(f"{a['id']} 没有人工标注")
            elif q and len(lab["points"]) != len(q["key_points"]):
                problems.append(f"{a['id']} 标注的要点数和题目不一致")
        print(json.dumps({"questions": len(s.questions), "answers": len(s.answers), "problems": problems},
                         ensure_ascii=False, indent=2))
        sys.exit(1 if problems else 0)

    from app.eval.runner import run

    out = asyncio.run(run(args.suite, args.retrieval, args.baseline))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
