"""Compare Tavily source-filter settings on claims with a known answer.

Each config runs one Tavily search per claim (cached), then Nemotron judges each claim REPEATS times
on the same sources, so search effects and model noise can be told apart. "G_two_tier" is the app's
production path (reference sources first, open web as fallback), with searches cached per tier.

    uv run python scripts/eval_search.py [--niche] [--all]   # needs NEBIUS_API_KEY and TAVILY_API_KEY
    # default: A_current, D_restrict and G_two_tier; --all adds B, C, E, F
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from tavily import AsyncTavilyClient  # noqa: E402

from sayproof.clients import BLOCKED_DOMAINS, REFERENCE_DOMAINS, Nemotron, Tavily, require  # noqa: E402
from sayproof.pipeline import find_position, judge_claim, judge_in_tier  # noqa: E402

load_dotenv()
REPEATS = 2
OUT = Path(__file__).resolve().parents[1] / "eval"

CLAIMS = [
    ("Pluto is our ninth planet.", "outdated", "English"),
    ("You can see the Great Wall of China from space with the naked eye.", "contradicted", "English"),
    ("The Eiffel Tower grows by about 15 centimetres in summer.", "supported", "English"),
    ("The Berlin Wall fell in 1991.", "contradicted", "English"),
    ("Mount Everest is the highest mountain on Earth at 8,849 metres.", "supported", "English"),
    ("Humans only use 10 percent of their brains.", "contradicted", "English"),
    ("Goldfish have a memory span of only three seconds.", "contradicted", "English"),
    ("Die Berliner Mauer fiel im November 1989.", "supported", "German"),
    ("Lightning never strikes the same place twice.", "contradicted", "English"),
    ("Water boils at 100 degrees Celsius at sea level.", "supported", "English"),
]

# Less famous claims, where a short allowlist may lack coverage.
NICHE_CLAIMS = [
    ("The Berlin TV Tower is 368 metres tall.", "supported", "English"),
    ("Python was first released in 1991.", "supported", "English"),
    ("The first iPhone was released in 2007.", "supported", "English"),
    ("The Bitcoin whitepaper was published in 2008.", "supported", "English"),
    ("Rust 1.0 was released in 2015.", "supported", "English"),
    ("The Elbphilharmonie concert hall is in Berlin.", "contradicted", "English"),
    ("Kreuzberg ist ein Stadtteil von München.", "contradicted", "German"),
    ("Der Fernsehturm am Alexanderplatz wurde 1969 eröffnet.", "supported", "German"),
]
if "--niche" in sys.argv:
    CLAIMS = NICHE_CLAIMS

TRUSTED = REFERENCE_DOMAINS
# For scoring only: any public-sector or academic domain also counts as trusted.
TRUSTED_SUFFIXES = (".gov", ".edu", ".int", ".ac.uk", ".gov.uk", ".europa.eu")
SOCIAL = ["youtube.com", "tiktok.com", "instagram.com", "facebook.com", "x.com", "twitter.com", "pinterest.com"]

CONFIGS = {
    "A_current": dict(exclude_domains=SOCIAL),  # the app's setting before the two-tier search
    "B_blacklist": dict(exclude_domains=BLOCKED_DOMAINS),
    "C_prefer": dict(exclude_domains=SOCIAL, include_domains=TRUSTED, include_domains_mode="prefer"),
    "D_restrict": dict(include_domains=TRUSTED, include_domains_mode="restrict"),
    "F_prefer+bl": dict(exclude_domains=BLOCKED_DOMAINS, include_domains=TRUSTED, include_domains_mode="prefer"),
}
if "--all" not in sys.argv:
    CONFIGS = {k: CONFIGS[k] for k in ("A_current", "D_restrict")}
SCORE_MIN = 0.5  # config E = A's results with low-relevance results dropped


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_trusted(url: str) -> bool:
    d = domain(url)
    if d.endswith(TRUSTED_SUFFIXES):
        return True
    for t in TRUSTED:
        if t.startswith("*."):
            if d.endswith(t[1:]):
                return True
        elif d == t or d.endswith("." + t):
            return True
    return False


class Cached:
    def __init__(self, results):
        self.results = results

    async def search(self, query, tier="open_web"):
        return self.results


class CachedTiers:
    """The production Tavily client, with one real search per (query, tier)."""

    def __init__(self):
        self.client, self.cache, self.lock = Tavily(), {}, asyncio.Lock()

    async def search(self, query, tier="open_web"):
        key = (query, tier)
        async with self.lock:
            if key not in self.cache:
                self.cache[key] = asyncio.ensure_future(self.client.search(query, tier))
        return await self.cache[key]


async def main() -> None:
    tavily = AsyncTavilyClient(api_key=require("TAVILY_API_KEY"))
    model = Nemotron()
    today = dt.date.today()
    OUT.mkdir(exist_ok=True)

    searched: dict[str, list] = {}
    for name, extra in CONFIGS.items():
        started = time.perf_counter()
        resps = await asyncio.gather(*(
            tavily.search(query=claim, search_depth="advanced", max_results=5, **extra) for claim, _, _ in CLAIMS))
        searched[name] = [r.get("results", []) for r in resps]
        print(f"searched {name} in {time.perf_counter() - started:.1f}s", flush=True)
    if "--all" in sys.argv:
        searched["E_score"] = [[r for r in res if (r.get("score") or 0) >= SCORE_MIN] for res in searched["A_current"]]

    rows = []
    for name, per_claim in searched.items():
        jobs = []
        for (claim, expected, lang), results in zip(CLAIMS, per_claim):
            c = {"claim": claim, "quote": claim, "search_query": claim, "language": lang}
            for _ in range(REPEATS):
                jobs.append((claim, expected, results,
                             judge_in_tier(model, Cached(results), c, find_position(claim, claim), today, "open_web")))
        verdicts = await asyncio.gather(*(j[3] for j in jobs))
        for (claim, expected, results, _), v in zip(jobs, verdicts):
            rows.append({"config": name, "claim": claim, "expected": expected, "verdict": v.verdict,
                         "guard": v.guard_note, "source": v.source.url if v.source else None,
                         "n_results": len(results), "result_urls": [r.get("url") for r in results],
                         "scores": [r.get("score") for r in results]})
        print(f"judged {name}", flush=True)

    # Production path: two tiers, as the app runs it.
    tiers = CachedTiers()
    jobs = []
    for claim, expected, lang in CLAIMS:
        c = {"claim": claim, "quote": claim, "search_query": claim, "language": lang}
        jobs += [(claim, expected, judge_claim(model, tiers, c, claim, today)) for _ in range(REPEATS)]
    verdicts = await asyncio.gather(*(j[2] for j in jobs))
    for (claim, expected, _), v in zip(jobs, verdicts):
        urls = [s.url for s in v.sources_considered]
        rows.append({"config": "G_two_tier", "claim": claim, "expected": expected, "verdict": v.verdict,
                     "guard": v.guard_note, "source": v.source.url if v.source else None, "tier": v.source_tier,
                     "n_results": len(urls), "result_urls": urls, "scores": []})
    searched["G_two_tier"] = None
    fallbacks = sum(k[1] == "open_web" for k in tiers.cache)
    print(f"judged G_two_tier ({fallbacks} of {len(CLAIMS)} claims needed the open-web fallback)", flush=True)

    (OUT / ("search_eval_niche.json" if "--niche" in sys.argv else "search_eval.json")).write_text(json.dumps(rows, indent=1, ensure_ascii=False))

    print(f"\n{len(CLAIMS)} claims x {REPEATS} verdicts per config. Trusted = reference list in this script.\n")
    print(f"{'config':<12} {'correct':>9} {'unverif.':>9} {'guard':>6} {'trusted src':>12} {'cited trusted':>14} {'empty':>6}")
    for name in searched:
        rs = [r for r in rows if r["config"] == name]
        seen = {r["claim"]: r["result_urls"] for r in rs}
        urls = [u for us in seen.values() for u in us]
        cited = [r["source"] for r in rs if r["source"]]
        correct = sum(r["verdict"] == r["expected"] for r in rs)
        print(f"{name:<12} {correct:>4}/{len(rs):<4} {sum(r['verdict'] == 'unverified' for r in rs):>9} "
              f"{sum(bool(r['guard']) for r in rs):>6} "
              f"{sum(map(is_trusted, urls)):>5}/{len(urls):<6} "
              f"{sum(map(is_trusted, cited)):>6}/{len(cited):<7} "
              f"{sum(len(u) == 0 for u in seen.values()):>6}")

    print("\nMisses (claim: expected -> got):")
    for name in searched:
        misses = Counter((r["claim"][:40], r["expected"], r["verdict"])
                         for r in rows if r["config"] == name and r["verdict"] != r["expected"])
        for (c, e, g), n in misses.items():
            print(f"  {name:<12} {c:<42} {e} -> {g} (x{n})")
    g_web = [r for r in rows if r["config"] == "G_two_tier" and r.get("tier") == "open_web"]
    print(f"\nG_two_tier verdicts that came from the open-web tier: {len(g_web)}")
    for r in g_web:
        print(f"  {r['claim'][:45]:<47} {r['verdict']:<13} {r['source'] or '-'}")
    print("\nNon-trusted domains returned by A_current:")
    print("  " + ", ".join(sorted({domain(u) for us in searched['A_current'] for u in (r.get('url') for r in us)
                                   if u and not is_trusted(u)})))


if __name__ == "__main__":
    asyncio.run(main())
