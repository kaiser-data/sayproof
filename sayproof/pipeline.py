"""Claim check pipeline: transcript -> claims (Nemotron) -> evidence (Tavily) -> verdicts (Nemotron).

The rule the whole product rests on: no source, no verdict. A verdict other than
"unverified" survives only if its evidence quote is found in the text Tavily returned.

Search runs in two tiers: first only reference sources (public bodies, science organisations, encyclopedias,
major news outlets); only if that leaves the claim unverified, the open web, and the source is labelled as such.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

VERDICTS = ("supported", "contradicted", "outdated", "unverified")
MAX_CLAIMS = 8
SOURCES_PER_CLAIM = 5
TIERS = ("reference", "open_web")


class ChatModel(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class SearchEngine(Protocol):
    async def search(self, query: str, tier: str) -> list[dict[str, Any]]: ...


@dataclass
class Source:
    id: int
    title: str
    url: str
    content: str
    published_date: str | None = None


@dataclass
class ClaimResult:
    claim: str
    quote: str
    position: int | None
    verdict: str
    explanation: str
    source: Source | None = None
    evidence_quote: str | None = None
    suggestion: str | None = None
    guard_note: str | None = None
    source_tier: str | None = None
    sources_considered: list[Source] = field(default_factory=list)


# ---------- text helpers ----------

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply (drops <think> blocks and code fences)."""
    text = _THINK.sub("", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start : end + 1])


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"[^\w%.,'\"-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in(quote: str, text: str) -> bool:
    q = normalize(quote).strip(" .,")
    return len(q) >= 12 and q in normalize(text)


def find_position(quote: str, transcript: str) -> int | None:
    idx = transcript.lower().find((quote or "").lower().strip())
    return idx if idx >= 0 else None


# ---------- prompts ----------

EXTRACT_SYSTEM = """You extract checkable factual claims from the transcript of a talk rehearsal.
A checkable claim states something about the world that a web search could confirm or refute:
numbers, dates, records, scientific or historical facts, who did what.
Skip opinions, predictions, jokes, greetings, and statements about the speaker's feelings.
Do include claims about the speaker's own company, product, team or results when they state a fact or a number
("our users tripled", "unser Umsatz hat sich verdoppelt"): the speaker needs to know these are unbacked.
Keep dates, units, qualifiers and attribution exactly as spoken ("in 2019", "about", "according to").
Write "claim" in the language of the transcript. "quote" must be copied from the transcript unchanged.
"search_query" may be in English if that finds better sources.
Reply with JSON only:
{"language": "<language of the transcript in English, e.g. German>",
 "claims": [{"claim": "<the claim as a standalone sentence>",
             "quote": "<the exact words from the transcript>",
             "search_query": "<a short web search query to check it>",
             "about_speaker": <true if the claim is about the speaker or their own company, product, team or
                               family; false for facts about the world, even when phrased "our planet">}]}
Return at most %d claims, in the order they appear. Return {"claims": []} if there are none.""" % MAX_CLAIMS

VERDICT_SYSTEM = """You check one claim from a talk against web search results. Today is {today}.
Decide one verdict:
- "supported": the sources confirm the claim as stated.
- "contradicted": the sources show the claim is false.
- "outdated": the claim was true at some point but is no longer true today. Prefer this over "contradicted"
  whenever the sources show the claim used to be true ("was", "formerly", "until 2006", "no longer").
  Example: "Pluto is our ninth planet" is outdated, not contradicted. Only use it for claims stated in the
  present tense; a claim that names its own date ("in 2019 ...") is judged for that date.
- "unverified": the sources do not settle it, disagree, or the claim is about private data.
Rules:
- Base the verdict only on the sources below, not on memory.
- Weigh sources by reliability: official bodies, scientific agencies, universities, encyclopedias and major news
  outlets outweigh blogs, content farms and marketing pages. If reliable sources agree and only weak pages
  disagree, follow the reliable sources. Use "unverified" for disagreement only when reliable sources disagree.
- Judge the claim in the sense a typical audience would understand it. Example: "the Berlin Wall fell" means the
  opening of the border in 1989, not the end of its demolition. Related but different events are not a
  disagreement between sources.
- A number rounded the way people speak matches a more precise source: "8,849 metres" matches 8,848.86 m.
  Only call a number contradicted if it is wrong beyond normal rounding.
- A claim about the speaker's own organisation, product, team or life ("we", "our", "my", "unser", "ich")
  is about that speaker, not about whoever a source describes. A source about another company, person or
  product does not support or contradict it: the verdict is "unverified", unless the source clearly names
  the same entity as the claim.
- For any verdict except "unverified", copy one sentence word for word from a source's content into
  "evidence_quote" and give that source's id. If you cannot, the verdict must be "unverified".
- The evidence sentence does not have to repeat the claim; it must clearly establish it. Example: "Pluto was
  reclassified as a dwarf planet in 2006" supports "In 2005 Pluto was still counted as the ninth planet".
- Write "explanation" and "suggestion" in {language}. The evidence quote stays in the source's language.
- "suggestion": for "contradicted" or "outdated", one sentence the speaker could say instead, supported by the
  source. Otherwise null.
Reply with JSON only:
{{"verdict": "...", "explanation": "<one short sentence>", "source_id": <int or null>,
  "evidence_quote": "<exact sentence or null>", "suggestion": "<sentence or null>"}}"""


ABOUT_SPEAKER_EN = "This is about you or your own organisation, so no public source can confirm it. Have your own data ready."
ABOUT_SPEAKER_DE = "Das betrifft dich oder deine eigene Organisation, keine öffentliche Quelle kann es belegen. Halte eigene Zahlen bereit."


# ---------- pipeline steps ----------


async def extract_claims(model: ChatModel, transcript: str) -> list[dict[str, str]]:
    reply = await model.complete(EXTRACT_SYSTEM, f"Transcript:\n\"\"\"\n{transcript}\n\"\"\"")
    parsed = parse_json_object(reply)
    claims = parsed.get("claims", [])
    language = str(parsed.get("language") or "the language of the claim").strip()
    out = []
    for c in claims[:MAX_CLAIMS]:
        claim = str(c.get("claim", "")).strip()
        if claim:
            out.append({
                "claim": claim,
                "quote": str(c.get("quote") or claim).strip(),
                "search_query": str(c.get("search_query") or claim).strip(),
                "language": language,
                "about_speaker": c.get("about_speaker") is True,
            })
    return out


def to_sources(results: list[dict[str, Any]]) -> list[Source]:
    sources = []
    for i, r in enumerate(results[:SOURCES_PER_CLAIM], start=1):
        content = r.get("raw_content") or r.get("content") or ""
        sources.append(Source(
            id=i,
            title=str(r.get("title") or r.get("url") or "source"),
            url=str(r.get("url") or ""),
            content=content[:4000],
            published_date=r.get("published_date"),
        ))
    return sources


def apply_guard(verdict: dict[str, Any], sources: list[Source]) -> tuple[str, Source | None, str | None, str | None]:
    """Return (verdict, source, evidence_quote, guard_note). Downgrades anything we cannot back with a quote."""
    v = str(verdict.get("verdict", "")).lower().strip()
    if v not in VERDICTS:
        return "unverified", None, None, f"model returned an unknown verdict: {v!r}"
    if v == "unverified":
        return v, None, None, None
    by_id = {s.id: s for s in sources}
    try:
        src = by_id.get(int(verdict.get("source_id")))
    except (TypeError, ValueError):
        src = None
    quote = verdict.get("evidence_quote") or ""
    if src is None:
        return "unverified", None, None, "no valid source id for the verdict"
    if not quote_in(quote, src.content):
        return "unverified", src, None, "evidence quote not found in the source text"
    return v, src, quote, None


async def judge_claim(model: ChatModel, search: SearchEngine, c: dict[str, str], transcript: str,
                      today: dt.date) -> ClaimResult:
    """Judge against reference sources first; fall back to the open web only if that leaves it unverified."""
    position = find_position(c["quote"], transcript)
    if c.get("about_speaker"):
        # No public source can back "our users tripled"; searching only finds someone else's numbers.
        note = ABOUT_SPEAKER_DE if c.get("language", "").lower() == "german" else ABOUT_SPEAKER_EN
        return ClaimResult(c["claim"], c["quote"], position, "unverified", note)
    result = None
    for tier in TIERS:
        result = await judge_in_tier(model, search, c, position, today, tier)
        if result.verdict != "unverified":
            break
    return result


async def judge_in_tier(model: ChatModel, search: SearchEngine, c: dict[str, str], position: int | None,
                        today: dt.date, tier: str) -> ClaimResult:
    def unverified(explanation: str, note: str | None = None, sources: list[Source] | None = None) -> ClaimResult:
        return ClaimResult(c["claim"], c["quote"], position, "unverified", explanation, guard_note=note,
                           source_tier=tier, sources_considered=sources or [])

    try:
        sources = to_sources(await search.search(c["search_query"], tier))
    except Exception as exc:  # search outage should not kill the whole receipt
        return unverified("Search failed for this claim.", f"search error: {exc}")
    if not sources:
        return unverified("No search results for this claim.")

    listing = "\n\n".join(
        f"[source {s.id}] {s.title}\nurl: {s.url}\npublished: {s.published_date or 'unknown'}\ncontent: {s.content}"
        for s in sources
    )
    system = VERDICT_SYSTEM.format(today=today.isoformat(), language=c.get("language", "the language of the claim"))
    prompt = f"Claim: {c['claim']}\n\nSources:\n{listing}"
    try:
        reply = await model.complete(system, prompt)
        raw = parse_json_object(reply)
        verdict, src, quote, note = apply_guard(raw, sources)
        if note:
            # One retry: models often paraphrase the quote. Ask for an exact copy, or an honest "unverified".
            retry = (f"{prompt}\n\nYour previous answer was:\n{reply}\n\nProblem: {note}. Copy one sentence "
                     "exactly as it appears in the source content, with the right source_id, or answer \"unverified\".")
            raw = parse_json_object(await model.complete(system, retry))
            verdict, src, quote, note = apply_guard(raw, sources)
    except Exception as exc:
        return unverified("The verdict step failed.", f"verdict error: {exc}", sources)

    explanation = str(raw.get("explanation") or "").strip()
    if note:
        explanation = "Sayproof could not back a verdict with a quoted source."
    suggestion = raw.get("suggestion") if verdict in ("contradicted", "outdated") else None
    return ClaimResult(c["claim"], c["quote"], position, verdict, explanation, src, quote,
                       suggestion, note, tier, sources)


async def check_transcript(model: ChatModel, search: SearchEngine, transcript: str,
                           today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    claims = await extract_claims(model, transcript)
    results = await asyncio.gather(*(judge_claim(model, search, c, transcript, today) for c in claims))
    results = sorted(results, key=lambda r: (r.position is None, r.position or 0))
    counts = {v: sum(r.verdict == v for r in results) for v in VERDICTS}
    return {"claims": [asdict(r) for r in results], "counts": counts, "checked_on": today.isoformat()}
