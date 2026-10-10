"""Measure verdict accuracy and run-to-run stability of the production pipeline on eval/verdict_set.json.

Every claim goes through judge_claim (reference tier first, open web as fallback) REPEATS times.
By default Tavily results are cached per (query, tier), so repeats show model noise only and cost no
extra search credits; --fresh searches again on every repeat (model + search noise).
The talks in the set test claim extraction: which checkable claims are found, which opinions are skipped.

    uv run python scripts/eval_verdicts.py [--repeats 3] [--fresh] [--only German] [--no-talks]
    # needs NEBIUS_API_KEY and TAVILY_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from sayproof.clients import Nemotron, Tavily  # noqa: E402
from sayproof.pipeline import VERDICTS, extract_claims, judge_claim  # noqa: E402

load_dotenv()
EVAL = Path(__file__).resolve().parents[1] / "eval"
CONCURRENCY = 6
# must_find keys in the talks that are about the speaker; every other key is about the world.
SPEAKER_KEYS = {"tripled", "120", "churn"}


class Search:
    """The production Tavily client; with cache=True, one real search per (query, tier)."""

    def __init__(self, cache: bool):
        self.client, self.cache, self.lock, self.calls = Tavily(), {} if cache else None, asyncio.Lock(), 0

    async def search(self, query, tier="open_web"):
        if self.cache is None:
            self.calls += 1
            return await self.client.search(query, tier)
        async with self.lock:
            if (query, tier) not in self.cache:
                self.calls += 1
                self.cache[(query, tier)] = asyncio.ensure_future(self.client.search(query, tier))
        return await self.cache[(query, tier)]


async def run_claims(model, search, items, repeats, today):
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(item):
        # As in the app: extraction supplies the search query and the about_speaker flag.
        fallback = {"claim": item["claim"], "quote": item["claim"], "search_query": item["claim"],
                    "language": item["lang"]}
        async with gate:
            started = time.perf_counter()
            try:
                extracted = await extract_claims(model, item["claim"])
            except Exception:
                extracted = []
            c = extracted[0] if extracted else fallback
            r = await judge_claim(model, search, c, item["claim"], today)
            return r, c, round(time.perf_counter() - started, 1)

    jobs = [(item, one(item)) for item in items for _ in range(repeats)]
    done = await asyncio.gather(*(j[1] for j in jobs))
    rows = []
    for (item, _), (r, c, secs) in zip(jobs, done):
        rows.append({**{k: item[k] for k in ("claim", "expected", "category", "lang")},
                     "also_ok": item.get("also_ok", []), "verdict": r.verdict, "tier": r.source_tier,
                     "source": r.source.url if r.source else None, "evidence": r.evidence_quote,
                     "guard": r.guard_note, "explanation": r.explanation, "seconds": secs,
                     "search_query": c["search_query"], "about_speaker": bool(c.get("about_speaker"))})
    return rows


async def run_talks(model, talks, repeats):
    async def one(talk):
        try:
            return await extract_claims(model, talk["text"])
        except Exception as exc:  # a broken reply counts as finding nothing
            return [{"claim": f"<error: {exc}>", "quote": ""}]

    jobs = [talk for talk in talks for _ in range(repeats)]
    outs = await asyncio.gather(*(one(t) for t in jobs))
    rows = []
    for talk, claims in zip(jobs, outs):
        text = lambda cs: " | ".join(f"{c['claim']} {c['quote']}" for c in cs).lower()  # noqa: E731
        found, own = text(claims), text([c for c in claims if c.get("about_speaker")])
        rows.append({"talk": talk["text"][:60], "claims": [c["claim"] for c in claims],
                     "found": {k: k.lower() in found for k in talk["must_find"]},
                     "wrongly_kept": {k: k.lower() in found for k in talk["must_skip"]},
                     "speaker_flagged": {k: k.lower() in own for k in talk["must_find"]}})
    return rows


def pct(n, d):
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "-"


def report(rows, talk_rows, repeats, search_calls, elapsed):
    print(f"\n=== Verdicts: {len(rows) // repeats} claims x {repeats} runs, {search_calls} Tavily searches, "
          f"{elapsed:.0f}s ===\n")
    ok = lambda r: r["verdict"] == r["expected"]  # noqa: E731
    lenient = lambda r: ok(r) or r["verdict"] in r["also_ok"]  # noqa: E731
    print(f"strict accuracy   {pct(sum(map(ok, rows)), len(rows))}")
    print(f"lenient accuracy  {pct(sum(map(lenient, rows)), len(rows))}   (also_ok counts, e.g. contradicted for outdated)")
    wrong_confident = [r for r in rows if not lenient(r) and r["verdict"] != "unverified"]
    print(f"confidently wrong {pct(len(wrong_confident), len(rows))}   (a wrong verdict other than can't verify: the costly error)")

    by_claim = defaultdict(list)
    for r in rows:
        by_claim[r["claim"]].append(r)
    stable = [c for c, rs in by_claim.items() if len({r["verdict"] for r in rs}) == 1]
    majority_ok = sum(Counter(r["verdict"] for r in rs).most_common(1)[0][0] == rs[0]["expected"]
                      for rs in by_claim.values())
    print(f"stable claims     {pct(len(stable), len(by_claim))}   (same verdict in every run)")
    print(f"majority correct  {pct(majority_ok, len(by_claim))}")
    secs = sorted(r["seconds"] for r in rows)
    print(f"seconds per claim median {secs[len(secs) // 2]}, max {secs[-1]}")
    web = sum(r["tier"] == "open_web" for r in rows)
    print(f"open-web fallback {pct(web, len(rows))}")
    own = [r for r in rows if r["about_speaker"]]
    print(f"about the speaker {pct(len(own), len(rows))} (no search); wrongly flagged: "
          f"{sorted({r['claim'][:40] for r in own if r['expected'] != 'unverified'}) or 'none'}")

    print(f"\n{'category':<14}{'strict':>14}{'lenient':>14}{'stable':>10}")
    for cat in dict.fromkeys(r["category"] for r in rows):
        rs = [r for r in rows if r["category"] == cat]
        claims = {r["claim"] for r in rs}
        st = sum(c in stable for c in claims)
        print(f"{cat:<14}{pct(sum(map(ok, rs)), len(rs)):>14}{pct(sum(map(lenient, rs)), len(rs)):>14}"
              f"{f'{st}/{len(claims)}':>10}")

    print("\nconfusion (rows: expected, columns: got)")
    print(f"{'':<14}" + "".join(f"{v[:12]:>13}" for v in VERDICTS))
    for exp in VERDICTS:
        rs = [r for r in rows if r["expected"] == exp]
        if rs:
            print(f"{exp:<14}" + "".join(f"{sum(r['verdict'] == v for r in rs):>13}" for v in VERDICTS))

    print("\nmisses (claim: expected -> verdicts over runs)")
    for claim, rs in by_claim.items():
        if not all(ok(r) for r in rs):
            got = ", ".join(f"{v} x{n}" for v, n in Counter(r["verdict"] for r in rs).items())
            note = next((r["guard"] for r in rs if r["guard"]), "")
            print(f"  {claim[:58]:<60} {rs[0]['expected']:<12} -> {got}" + (f"  [{note}]" if note else ""))

    if talk_rows:
        print(f"\n=== Claim extraction: {len(talk_rows) // repeats} talks x {repeats} runs ===")
        found = [v for t in talk_rows for v in t["found"].values()]
        kept = [v for t in talk_rows for v in t["wrongly_kept"].values()]
        print(f"checkable claims found     {pct(sum(found), len(found))}")
        print(f"opinions/plans kept wrongly {pct(sum(kept), len(kept))}")
        own_ok = [t["speaker_flagged"][k] == (k in SPEAKER_KEYS) for t in talk_rows for k in t["found"] if t["found"][k]]
        print(f"about_speaker flag right   {pct(sum(own_ok), len(own_ok))}")
        for t in talk_rows:
            miss = [k for k, v in t["found"].items() if not v] + [f"+{k}" for k, v in t["wrongly_kept"].items() if v]
            miss += [f"flag?{k}" for k, v in t["speaker_flagged"].items() if t["found"][k] and v != (k in SPEAKER_KEYS)]
            if miss:
                print(f"  {t['talk']:<62} {', '.join(miss)}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--fresh", action="store_true", help="search again on every repeat")
    ap.add_argument("--only", help="run one category only")
    ap.add_argument("--no-talks", action="store_true")
    args = ap.parse_args()

    data = json.loads((EVAL / "verdict_set.json").read_text())
    items = [c for c in data["claims"] if not args.only or c["category"] == args.only]
    model, search, today = Nemotron(), Search(cache=not args.fresh), dt.date.today()
    started = time.perf_counter()
    rows, talk_rows = await asyncio.gather(
        run_claims(model, search, items, args.repeats, today),
        run_talks(model, [] if args.no_talks or args.only else data["talks"], args.repeats))
    elapsed = time.perf_counter() - started

    out = EVAL / f"verdict_eval{'_fresh' if args.fresh else ''}.json"
    out.write_text(json.dumps({"date": today.isoformat(), "model": model.model, "repeats": args.repeats,
                               "fresh_search": args.fresh, "claims": rows, "talks": talk_rows},
                              indent=1, ensure_ascii=False))
    report(rows, talk_rows, args.repeats, search.calls, elapsed)
    print(f"\nrows written to {out.relative_to(EVAL.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
