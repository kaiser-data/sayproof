"""Tests for the pipeline logic. The fakes below stand in for Nemotron and Tavily in tests only."""

import asyncio
import datetime as dt
import json
import re

from sayproof.pipeline import (Source, apply_guard, check_transcript, find_position, parse_json_object, quote_in,
                               stream_transcript)

SRC = [Source(1, "IAU", "https://example.org/iau",
              "In 2006 the IAU reclassified Pluto as a dwarf planet. It is no longer counted among the planets.")]


def test_parse_json_strips_think_and_fences():
    reply = '<think>let me see {not json}</think>```json\n{"claims": [{"claim": "x"}]}\n```'
    assert parse_json_object(reply) == {"claims": [{"claim": "x"}]}


def test_quote_in_tolerates_quotes_and_spacing():
    assert quote_in("In 2006 the IAU  reclassified Pluto as a “dwarf planet”.", SRC[0].content.replace("dwarf planet", '"dwarf planet"'))
    assert not quote_in("Pluto is the ninth planet", SRC[0].content)
    assert not quote_in("Pluto", SRC[0].content)  # too short to count as evidence


def test_guard_keeps_backed_verdict():
    v, src, q, note = apply_guard({"verdict": "outdated", "source_id": 1,
                                   "evidence_quote": "In 2006 the IAU reclassified Pluto as a dwarf planet."}, SRC)
    assert (v, src.id, note) == ("outdated", 1, None)


def test_guard_downgrades_invented_quote():
    v, _, q, note = apply_guard({"verdict": "contradicted", "source_id": 1,
                                 "evidence_quote": "Pluto was demoted because it is too cold."}, SRC)
    assert v == "unverified" and q is None and "not found" in note


def test_guard_downgrades_missing_source_and_unknown_verdict():
    assert apply_guard({"verdict": "supported", "source_id": 9, "evidence_quote": "x" * 20}, SRC)[0] == "unverified"
    assert apply_guard({"verdict": "probably"}, SRC)[0] == "unverified"


class FakeModel:
    async def complete(self, system, user, think=True):
        if "stage line" in system:
            return json.dumps({"line": "The IAU says Pluto was our ninth planet until 2006."})
        if "extract" in system:
            return json.dumps({"claims": [
                {"claim": "Our users tripled this year.", "quote": "our users tripled", "search_query": "users tripled"},
                {"claim": "Pluto is the ninth planet.", "quote": "Pluto is our ninth planet", "search_query": "Pluto planet"},
            ]})
        if user.startswith("Claim: Pluto"):
            return json.dumps({"verdict": "outdated", "explanation": "Reclassified in 2006.", "source_id": 1,
                               "evidence_quote": "In 2006 the IAU reclassified Pluto as a dwarf planet.",
                               "suggestion": "Pluto was our ninth planet until 2006."})
        return json.dumps({"verdict": "supported", "source_id": 1, "evidence_quote": "made up sentence here"})


class FakeSearch:
    async def search(self, query, tier="open_web"):
        return [{"title": "IAU", "url": "https://example.org/iau", "content": SRC[0].content}]


def test_check_transcript_orders_by_position_and_guards():
    text = "Pluto is our ninth planet. And our users tripled."
    r = asyncio.run(check_transcript(FakeModel(), FakeSearch(), text, today=dt.date(2026, 9, 29)))
    assert [c["verdict"] for c in r["claims"]] == ["outdated", "unverified"]
    assert r["claims"][0]["suggestion"] == "The IAU says Pluto was our ninth planet until 2006."
    assert r["counts"] == {"supported": 0, "contradicted": 0, "outdated": 1, "misleading": 0, "unverified": 1}


def test_stream_sends_claims_first_then_each_verdict_then_counts():
    async def collect():
        return [e async for e in stream_transcript(FakeModel(), FakeSearch(), "Pluto is our ninth planet. And our users tripled.",
                                                   today=dt.date(2026, 9, 29))]
    events = asyncio.run(collect())
    assert [e["event"] for e in events] == ["claims", "result", "result", "done"]
    assert [c["quote"] for c in events[0]["claims"]] == ["Pluto is our ninth planet", "our users tripled"]
    assert {e["index"]: e["claim"]["verdict"] for e in events[1:3]} == {0: "outdated", 1: "unverified"}
    assert events[-1]["counts"]["outdated"] == 1


class ParaphrasingModel:
    """Paraphrases the quote on the first verdict call, copies it exactly on the retry."""

    def __init__(self):
        self.verdict_calls = 0

    async def complete(self, system, user, think=True):
        if "stage line" in system:
            assert "German" in system
            return json.dumps({"line": "Pluto war bis 2006 unser neunter Planet, sagt die IAU."})
        if "extract" in system:
            return json.dumps({"language": "German", "claims": [
                {"claim": "Pluto ist der neunte Planet.", "quote": "Pluto ist unser neunter Planet", "search_query": "Pluto"}]})
        self.verdict_calls += 1
        assert "German" in system
        quote = "Pluto got reclassified in 2006." if self.verdict_calls == 1 else \
            "In 2006 the IAU reclassified Pluto as a dwarf planet."
        return json.dumps({"verdict": "outdated", "source_id": 1, "evidence_quote": quote,
                           "suggestion": "Pluto war bis 2006 unser neunter Planet."})


def test_retry_recovers_exact_quote_and_passes_language():
    model = ParaphrasingModel()
    r = asyncio.run(check_transcript(model, FakeSearch(), "Pluto ist unser neunter Planet.", today=dt.date(2026, 9, 29)))
    assert model.verdict_calls == 2
    assert r["claims"][0]["verdict"] == "outdated" and r["claims"][0]["guard_note"] is None
    assert r["claims"][0]["suggestion"] == "Pluto war bis 2006 unser neunter Planet, sagt die IAU."


class TieredSearch:
    """Reference tier finds nothing for 'startup', open web does."""

    def __init__(self):
        self.calls = []

    async def search(self, query, tier="open_web"):
        self.calls.append((query, tier))
        if tier == "reference" and "startup" in query:
            return []
        return [{"title": "IAU", "url": "https://example.org/iau", "content": SRC[0].content}]


class TierModel:
    async def complete(self, system, user, think=True):
        if "extract" in system:
            return json.dumps({"language": "English", "claims": [
                {"claim": "Pluto is the ninth planet.", "quote": "Pluto is our ninth planet", "search_query": "Pluto"},
                {"claim": "The startup tripled users.", "quote": "we tripled", "search_query": "startup users"}]})
        return json.dumps({"verdict": "outdated", "source_id": 1,
                           "evidence_quote": "In 2006 the IAU reclassified Pluto as a dwarf planet."})


def test_reference_tier_first_open_web_only_as_fallback():
    search = TieredSearch()
    r = asyncio.run(check_transcript(TierModel(), search, "Pluto is our ninth planet. And we tripled.",
                                     today=dt.date(2026, 9, 29)))
    tiers = {c["quote"]: c["source_tier"] for c in r["claims"]}
    assert tiers == {"Pluto is our ninth planet": "reference", "we tripled": "open_web"}
    assert ("Pluto", "open_web") not in search.calls  # no fallback search when the reference tier settled it


class SpeakerClaimModel:
    async def complete(self, system, user, think=True):
        assert "extract" in system, "a claim about the speaker must not reach the verdict step"
        return json.dumps({"language": "German", "claims": [
            {"claim": "Unser Umsatz hat sich verdoppelt.", "quote": "Unser Umsatz hat sich verdoppelt",
             "search_query": "Umsatz verdoppelt", "about_speaker": True}]})


class NoSearch:
    async def search(self, query, tier="open_web"):
        raise AssertionError("a claim about the speaker must not be searched")


def test_claim_about_speaker_is_unverified_without_search():
    r = asyncio.run(check_transcript(SpeakerClaimModel(), NoSearch(), "Unser Umsatz hat sich verdoppelt.",
                                     today=dt.date(2026, 10, 10)))
    [c] = r["claims"]
    assert c["verdict"] == "unverified" and c["source"] is None and c["position"] == 0
    assert c["explanation"].startswith("Das betrifft dich")


def test_find_position_ignores_punctuation_and_case():
    transcript = "Good morning. Octopuses have three hearts, and since our launch, our users tripled."
    assert find_position("Octopuses have three hearts.", transcript) == transcript.index("Octopuses")
    assert find_position("since our launch our users tripled", transcript) == transcript.index("since")
    assert find_position("Bats are blind", transcript) is None
    assert find_position("", transcript) is None


class FailingStageLineModel(FakeModel):
    async def complete(self, system, user, think=True):
        if "stage line" in system:
            raise RuntimeError("model down")
        return await super().complete(system, user, think)


def test_stage_line_failure_keeps_the_draft():
    r = asyncio.run(check_transcript(FailingStageLineModel(), FakeSearch(), "Pluto is our ninth planet.",
                                     today=dt.date(2026, 9, 29)))
    assert r["claims"][0]["suggestion"] == "Pluto was our ninth planet until 2006."


def test_long_talk_is_split_at_sentence_ends():
    from sayproof.pipeline import chunks
    talk = " ".join(f"Sentence number {i} is here." for i in range(400))
    parts = chunks(talk, size=1000)
    assert len(parts) > 5 and all(len(p) <= 1000 for p in parts)
    assert " ".join(parts) == talk and all(p.endswith(".") for p in parts)


class ChunkModel:
    """Finds one claim per chunk, and the same claim again in every chunk."""

    async def complete(self, system, user, think=True):
        if "extract" in system:
            first = re.search(r"Sentence number (\d+)", user).group(1)
            return json.dumps({"language": "English", "claims": [
                {"claim": f"Chunk starting at sentence {first}.", "quote": "Sentence number", "search_query": "x"},
                {"claim": "Light takes 8 minutes from the Sun.", "quote": "Sentence number", "search_query": "x"}]})
        return json.dumps({"verdict": "unverified"})


def test_extraction_covers_every_chunk_and_checks_repeats_once(monkeypatch):
    from sayproof import pipeline
    monkeypatch.setattr(pipeline, "MAX_CLAIMS", 4)
    talk = " ".join(f"Sentence number {i} is here." for i in range(2000))
    claims, more = asyncio.run(pipeline.extract_claims(ChunkModel(), talk))
    n_chunks = len(pipeline.chunks(talk))
    assert sum(c["claim"].startswith("Light") for c in claims) == 1
    assert len(claims) == 4 and more == n_chunks + 1 - 4
