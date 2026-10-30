"""Check Sayproof's verdicts against TruthfulQA (Lin, Hilton & Evans 2022, Apache-2.0), a benchmark built from
common misconceptions.

Each sampled question gives two claims: its best true answer (expected "supported") and one false answer
(expected "contradicted", or "misleading"/"outdated", which also say "fix this"). Only answers that read as a
standalone statement are used. Every claim goes through judge_claim with the production judge, once.

    uv run python scripts/eval_truthfulqa.py [--questions 20] [--seed 7]
    # needs NEBIUS_API_KEY and TAVILY_API_KEY; writes eval/truthfulqa_eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from sayproof.clients import JUDGE_MODEL, Nemotron, Tavily  # noqa: E402
from sayproof.pipeline import judge_claim  # noqa: E402

load_dotenv()
EVAL = Path(__file__).resolve().parents[1] / "eval"
ROWS = "https://datasets-server.huggingface.co/rows?dataset=truthfulqa/truthful_qa&config=generation&split=validation"
CATEGORIES = {"Misconceptions", "Science", "Health", "History", "Nutrition", "Weather", "Biology"}
FIX = {"contradicted", "misleading", "outdated"}
CONCURRENCY = 6


def standalone(answer: str) -> bool:
    """A claim a speaker could say on stage, not "No" or "Nothing happens"."""
    words = answer.split()
    return len(words) >= 6 and words[0].lower().strip(",.") not in {"no", "yes", "nothing", "i", "it", "they", "there"}


def load_rows() -> list[dict]:
    rows, offset = [], 0
    with httpx.Client(timeout=60) as client:
        while True:
            page = client.get(f"{ROWS}&offset={offset}&length=100").json().get("rows", [])
            rows += [r["row"] for r in page]
            if len(page) < 100:
                return rows
            offset += 100


def sample(rows: list[dict], n: int, seed: int, every_category: bool = False) -> list[dict]:
    rng, out = random.Random(seed), []
    pool = [r for r in rows if (every_category or r["category"] in CATEGORIES) and standalone(r["best_answer"])]
    rng.shuffle(pool)
    for r in pool:
        wrong = [a for a in r["incorrect_answers"] if standalone(a)]
        if wrong:
            out.append({"question": r["question"], "category": r["category"],
                        "true": r["best_answer"], "false": rng.choice(wrong)})
        if len(out) == n:
            return out
    return out


async def main(n: int, seed: int, every_category: bool, out: str) -> None:
    items = sample(load_rows(), n, seed, every_category)
    claims = [(it, kind, it[kind]) for it in items for kind in ("true", "false")]
    model, judge, search = Nemotron(), Nemotron(JUDGE_MODEL), Tavily()
    gate, today, started = asyncio.Semaphore(CONCURRENCY), dt.date.today(), time.perf_counter()
    print(f"{len(items)} questions, {len(claims)} claims", flush=True)

    async def one(it, kind, text):
        c = {"claim": text, "quote": text, "search_query": text, "language": "English", "about_speaker": False}
        async with gate:
            r = await judge_claim(judge, search, c, text, today)
        right = r.verdict == "supported" if kind == "true" else r.verdict in FIX
        confidently_wrong = (r.verdict in FIX) if kind == "true" else r.verdict == "supported"
        return {"question": it["question"], "category": it["category"], "kind": kind, "claim": text,
                "verdict": r.verdict, "right": right, "confidently_wrong": confidently_wrong,
                "suggestion": r.suggestion, "source": r.source.url if r.source else None}

    results = await asyncio.gather(*(one(*c) for c in claims))
    by = Counter((r["kind"], r["verdict"]) for r in results)
    summary = {
        "dataset": "TruthfulQA generation/validation (Apache-2.0)", "date": today.isoformat(), "seed": seed,
        "judge": JUDGE_MODEL, "claims": len(results), "right": sum(r["right"] for r in results),
        "confidently_wrong": sum(r["confidently_wrong"] for r in results),
        "unverified": sum(r["verdict"] == "unverified" for r in results),
        "verdicts": {f"{k}:{v}": n for (k, v), n in sorted(by.items())},
        "seconds": round(time.perf_counter() - started, 1),
    }
    EVAL.joinpath(out).write_text(json.dumps({"summary": summary, "results": results},
                                                                indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))
    for r in results:
        if not r["right"]:
            print(f"{'WRONG' if r['confidently_wrong'] else 'miss '} {r['kind']:5} {r['verdict']:12} {r['claim'][:90]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--questions", type=int, default=20)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--all", action="store_true", help="every category and every usable question (use --questions 817)")
    p.add_argument("--concurrency", type=int, default=CONCURRENCY)
    p.add_argument("--out", default="truthfulqa_eval.json")
    a = p.parse_args()
    CONCURRENCY = a.concurrency
    asyncio.run(main(a.questions, a.seed, a.all, a.out))
