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
from collections.abc import AsyncIterator
from typing import Any, Protocol

VERDICTS = ("supported", "contradicted", "outdated", "misleading", "unverified")
# Verdicts that come with a line to say instead.
FIXABLE = ("contradicted", "outdated", "misleading")
# A 30-minute talk is about 30,000 characters. Extraction runs on chunks in parallel: one call over the whole
# talk misses claims late in it. Verdicts run at most JUDGE_PARALLEL at a time, to stay under API rate limits.
MAX_CLAIMS = 40
CLAIMS_PER_CHUNK = 15
CHUNK_CHARS = 5000
JUDGE_PARALLEL = 8
SOURCES_PER_CLAIM = 5
# Verdicts now separate the fact from the picture it gives ("misleading"), which needs judgement: the judge
# (JUDGE_MODEL, Nemotron 3 Ultra by default) reasons. On 2026-10-21 reasoning on Super made verdicts 2x slower
# without making them more accurate, but that set had no "misleading" claims.
VERDICT_THINKING = True
TIERS = ("reference", "open_web")


class ChatModel(Protocol):
    async def complete(self, system: str, user: str, think: bool = True) -> str: ...


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
    """Where the quote starts in the transcript, matching words only: speech transcripts punctuate freely."""
    words = re.findall(r"\w+", quote or "")
    if not words:
        return None
    match = re.search(r"\W+".join(map(re.escape, words)), transcript, re.IGNORECASE)
    return match.start() if match else None


# ---------- prompts ----------

EXTRACT_SYSTEM = """You extract checkable factual claims from the transcript of a talk rehearsal.
A checkable claim states something about the world that a web search could confirm or refute:
numbers, dates, records, scientific or historical facts, who did what.
Skip opinions, predictions, jokes, greetings, and statements about the speaker's feelings.
Do include claims about the speaker's own company, product, team or results when they state a fact or a number
("our users tripled", "unser Umsatz hat sich verdoppelt"): the speaker needs to know these are unbacked.
Keep dates, units, qualifiers and attribution exactly as spoken ("in 2019", "about", "according to").
Never correct or tidy a claim while restating it: keep the speaker's framing, even when it is wrong
("the edge of our universe" stays "the edge of our universe", not "the observable universe").
Write "claim" in the language of the transcript. "quote" must be copied from the transcript unchanged.
"search_query" may be in English if that finds better sources.
Reply with JSON only:
{"language": "<language of the transcript in English, e.g. German>",
 "claims": [{"claim": "<the claim as a standalone sentence>",
             "quote": "<the exact words from the transcript>",
             "search_query": "<a short web search query to check it>",
             "about_speaker": <true if the claim is about the speaker or their own company, product, team or
                               family; false for facts about the world, even when phrased "our planet">}]}
Return at most %d claims, in the order they appear. Return {"claims": []} if there are none.""" % CLAIMS_PER_CHUNK

VERDICT_SYSTEM = """You check one claim from a talk against web search results. Today is {today}.
Decide one verdict:
- "supported": the sources confirm the claim as stated.
- "contradicted": the sources show the claim is false.
- "outdated": the claim was true at some point but is no longer true today. Prefer this over "contradicted"
  whenever the sources show the claim used to be true ("was", "formerly", "until 2006", "no longer").
  Example: "Pluto is our ninth planet" is outdated, not contradicted. Only use it for claims stated in the
  present tense; a claim that names its own date ("in 2019 ...") is judged for that date. "Outdated" means the
  world changed (a reclassification, a new office holder, a new currency), not that people once believed it:
  a claim that was never true, such as a myth or a wrong date or number, is "contradicted".
- "misleading": the facts or numbers are right, but the picture the audience takes away is wrong. Example:
  "From the Big Bang to the edge of our universe it is 46 billion light-years" has the right number, but there
  is no known edge: 46 billion light-years is how far we can see, and looking that far means looking back to
  the Big Bang. Every point in the universe sees the same, like dots on a balloon.
  Use it only when a typical audience would come away believing something specific and false, and name that
  false belief in "explanation". A true claim that is merely incomplete, simplified or carefully hedged ("results
  are mixed", "most", "about") is "supported", not "misleading".
- "unverified": the sources do not settle it, disagree, or the claim is about private data.
Judge two things, the way a careful listener would: are the facts right, and is the understanding right?
Judge the understanding from the speaker's exact words, not from the tidied-up claim: the audience hears the words.
How precise a claim must be depends on what it is used for. Spoken rounding is fine: "four light-years" for
4.24, "eight minutes" for 8 min 20 s. Wrong categories and wrong orders of magnitude are not: the Sun called a
planet, "billiarden" for billions or trillions, one metre where the source gives about nine.
Estimates are not measurements. When the sources give an estimate or a range (the number of galaxies or stars,
species, ancient populations), judge the claim against the range: inside it is "supported"; far outside it is
"misleading", not "contradicted", because the true value is unknown. The line to say instead gives the estimate
as an estimate ("researchers estimate between ... and ...").
Rules:
- Base the verdict only on the sources below, not on memory.
- Weigh sources by reliability: official bodies, scientific agencies, universities, encyclopedias and major news
  outlets outweigh blogs, content farms and marketing pages. If reliable sources agree and only weak pages
  disagree, follow the reliable sources. Use "unverified" for disagreement only when reliable sources disagree.
- A source that repeats a popular statistic or "fun fact" in passing ("it has been estimated", "is often cited",
  "studies show", "a study found", "many respondents said") does not confirm it, even on a reputable site: such
  figures are often repeated without anyone checking them. "supported" for these needs a source that reports the
  original measurement or research. If the sources only repeat the claim, the verdict is "unverified". If
  reliable sources call the claim a myth, a misconception or pseudoscience, the verdict is "contradicted".
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
- "suggestion": for "contradicted", "outdated" or "misleading", a line the speaker can say on stage instead, supported by the
  source. Otherwise null. It is heard once, not read, so write it for the ear:
  - Never harder to follow than the claim it replaces. Keep the speaker's own sentence and change only the part
    that is wrong: "Goldfish have a three-second memory." becomes "Goldfish remember things for months."
  - At most 15 words, one breath. Two short sentences beat one long one.
  - The point first, in plain spoken words and active voice, in the speaker's register ("we", "you").
  - No hedging clauses ("though", "may vary", "depending on context"), no brackets.
  - Refer to the source the way a speaker would, in a few words, when it adds weight: "NASA says ...",
    "Memory researchers found ...". Never a URL or a page title.
  - Numbers and percentages are fine when the source backs them; say them the way people speak ("since 2006").
  - These are the speaker's own words to the audience, not advice to the speaker ("stop saying ...").
  - Don't repeat the false claim in full; name it in a few words, then say what is true.
  - Keep the speaker's point where the source allows it. A myth can be named as a myth.
  Examples: "Pluto was our ninth planet until 2006. Today it's a dwarf planet."
  "That learning pyramid you've seen on slides? It's a myth."
Reply with JSON only:
{{"verdict": "...", "explanation": "<one short sentence>", "source_id": <int or null>,
  "evidence_quote": "<exact sentence or null>", "suggestion": "<sentence or null>"}}"""


# The verdict model writes a first "say instead" line along with everything else; this step rewrites it for the ear.
STAGE_LINE_SYSTEM = """You write the stage line: the words a speaker says in their talk instead of a false claim.
The audience hears it once, so it must be at least as easy to follow as the claim it replaces.
- One or two short sentences, at most 20 words. The point first, plain spoken words, the speaker's own voice.
- Refer to the source the way a speaker would, in a few words: "Britannica says ...", "NASA confirms ...",
  "Memory researchers found ...". Never a URL or a page title.
- Keep correct numbers and percentages from the draft or the evidence, with exactly the meaning the evidence gives
  them: a radius is not a width "across", an estimate is not a count. Add no fact that is not in the evidence.
- Don't repeat the false claim. Say what is true; name a myth as a myth in a few words.
- No hedging ("though", "depending on context"), no brackets, no dashes.
- Write in {language}.
Examples (false claim -> stage line):
  "Goldfish have a three-second memory." -> "Researchers found goldfish remember things for months."
  "Einstein failed maths at school." -> "Einstein's school records show top marks in maths. That story is a myth."
Reply with JSON only: {{"line": "<the stage line>"}}"""


ABOUT_SPEAKER_EN = "This is about you or your own organisation, so no public source can confirm it. Have your own data ready."
ABOUT_SPEAKER_DE = "Das betrifft dich oder deine eigene Organisation, keine öffentliche Quelle kann es belegen. Halte eigene Zahlen bereit."


# ---------- pipeline steps ----------


def chunks(transcript: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split at sentence ends into pieces of about `size` characters; a longer sentence stays whole."""
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", transcript.strip()):
        if current and len(current) + len(sentence) + 1 > size:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    return parts + [current] if current else parts


async def extract_claims(model: ChatModel, transcript: str) -> tuple[list[dict[str, str]], int]:
    """The claims to check, in transcript order, and how many more were found beyond MAX_CLAIMS."""
    replies = await asyncio.gather(*(model.complete(EXTRACT_SYSTEM, f"Transcript:\n\"\"\"\n{part}\n\"\"\"")
                                     for part in chunks(transcript)))
    parsed = [parse_json_object(reply) for reply in replies]
    language = next((str(p["language"]).strip() for p in parsed if p.get("language")), "the language of the claim")
    out, seen = [], set()
    for p in parsed:
        for c in p.get("claims", [])[:CLAIMS_PER_CHUNK]:
            claim = str(c.get("claim", "")).strip()
            key = " ".join(re.findall(r"\w+", claim.lower()))
            if claim and key not in seen:  # a speaker repeating a fact gets it checked once
                seen.add(key)
                out.append({
                    "claim": claim,
                    "quote": str(c.get("quote") or claim).strip(),
                    "search_query": str(c.get("search_query") or claim).strip(),
                    "language": language,
                    "about_speaker": c.get("about_speaker") is True,
                })
    return out[:MAX_CLAIMS], max(len(out) - MAX_CLAIMS, 0)


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


async def stage_line(model: ChatModel, c: dict[str, str], draft: str | None, src: Source | None,
                     evidence: str | None) -> str | None:
    """Rewrite the draft "say instead" line so it is easy to say and names its source. Keeps the draft on failure."""
    if not draft or src is None:
        return draft
    system = STAGE_LINE_SYSTEM.format(language=c.get("language", "the language of the claim"))
    prompt = (f"False claim: {c['quote']}\nDraft line: {draft}\nSource: {src.title} ({src.url})\n"
              f"Evidence from the source: {evidence or ''}")
    try:
        line = str(parse_json_object(await model.complete(system, prompt, think=False)).get("line") or "").strip()
    except Exception:
        return draft
    return line if line and (len(line.split()) <= 20 or len(line.split()) < len(draft.split())) else draft


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
    prompt = f"Claim: {c['claim']}\nThe speaker's exact words: {c['quote']}\n\nSources:\n{listing}"
    try:
        reply = await model.complete(system, prompt, think=VERDICT_THINKING)
        raw = parse_json_object(reply)
        verdict, src, quote, note = apply_guard(raw, sources)
        if verdict == "outdated" and not VERDICT_THINKING:
            # Without reasoning the model calls debunked myths "outdated" ("people used to believe it");
            # with it, it does not. Outdated verdicts are rare, so only they pay for a second, reasoned look.
            reply = await model.complete(system, prompt, think=True)
            raw = parse_json_object(reply)
            verdict, src, quote, note = apply_guard(raw, sources)
        if verdict == "misleading":
            # "Misleading" is the easiest verdict to over-apply to true claims (TruthfulQA, 2026-10-30: 2 of 20
            # true claims), so it must survive a second look that names the false belief.
            check = (f"{prompt}\n\nYour previous answer was:\n{reply}\n\nCheck it once more. Name the specific "
                     "false belief a typical listener would take away from the speaker's exact words. If there is "
                     "none, because the claim is true and only incomplete, simplified or hedged, answer \"supported\" "
                     "with an exact quote. Otherwise keep \"misleading\".")
            reply = await model.complete(system, check, think=True)
            raw = parse_json_object(reply)
            verdict, src, quote, note = apply_guard(raw, sources)
        if note:
            # One retry: models often paraphrase the quote. Ask for an exact copy, or an honest "unverified".
            retry = (f"{prompt}\n\nYour previous answer was:\n{reply}\n\nProblem: {note}. Copy one sentence "
                     "exactly as it appears in the source content, with the right source_id, or answer \"unverified\".")
            raw = parse_json_object(await model.complete(system, retry, think=VERDICT_THINKING))
            verdict, src, quote, note = apply_guard(raw, sources)
    except Exception as exc:
        return unverified("The verdict step failed.", f"verdict error: {exc}", sources)

    explanation = str(raw.get("explanation") or "").strip()
    if note:
        explanation = "Sayproof could not back a verdict with a quoted source."
    suggestion = raw.get("suggestion") if verdict in FIXABLE else None
    suggestion = await stage_line(model, c, suggestion, src, quote)
    return ClaimResult(c["claim"], c["quote"], position, verdict, explanation, src, quote,
                       suggestion, note, tier, sources)


async def stream_transcript(model: ChatModel, search: SearchEngine, transcript: str,
                            today: dt.date | None = None, judge: ChatModel | None = None) -> AsyncIterator[dict[str, Any]]:
    """Yield the claims as soon as they are found, then each verdict as it is ready, then the counts.

    Events: {"event": "claims", "claims": [...]}, {"event": "result", "index": i, "claim": {...}} in finishing
    order, {"event": "done", "counts": {...}}. Claims are in transcript order and "index" points into that list.
    """
    today = today or dt.date.today()
    claims, more = await extract_claims(model, transcript)
    at = {id(c): find_position(c["quote"], transcript) for c in claims}
    claims.sort(key=lambda c: (at[id(c)] is None, at[id(c)] or 0))
    yield {"event": "claims", "checked_on": today.isoformat(), "more": more,
           "claims": [{"claim": c["claim"], "quote": c["quote"], "position": at[id(c)]} for c in claims]}

    gate = asyncio.Semaphore(JUDGE_PARALLEL)

    async def judged(i: int, c: dict[str, str]) -> tuple[int, ClaimResult]:
        async with gate:
            return i, await judge_claim(judge or model, search, c, transcript, today)

    counts = dict.fromkeys(VERDICTS, 0)
    for next_done in asyncio.as_completed([judged(i, c) for i, c in enumerate(claims)]):
        i, result = await next_done
        counts[result.verdict] += 1
        yield {"event": "result", "index": i, "claim": asdict(result)}
    yield {"event": "done", "counts": counts, "checked_on": today.isoformat()}


async def check_transcript(model: ChatModel, search: SearchEngine, transcript: str,
                           today: dt.date | None = None, judge: ChatModel | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    more = 0
    async for event in stream_transcript(model, search, transcript, today, judge):
        if event["event"] == "claims":
            results, more = [{}] * len(event["claims"]), event["more"]
        elif event["event"] == "result":
            results[event["index"]] = event["claim"]
        else:
            return {"claims": results, "counts": event["counts"], "checked_on": event["checked_on"], "more": more}
    raise RuntimeError("stream ended without a done event")
