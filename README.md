# Sayproof

**Fact-check your talk before your audience does.**

Live demo: **https://sayproof.vercel.app**

Paste the transcript of a rehearsal: a class presentation, a pitch, a conference talk, a speech. Sayproof picks out the checkable claims, searches the web for each one, and returns a verdict with a quoted source:

| Verdict | Meaning |
|---|---|
| **Supported** | The sources confirm it. |
| **Contradicted** | The sources show it's false. Sayproof suggests a sourced line to say instead. |
| **Outdated** | It was true once, but not today (e.g. "Pluto is our ninth planet"). A claim that names its own year is judged for that year. |
| **Can't verify** | The sources don't settle it, or it's private data ("our users tripled"). Sayproof asks for your source instead of guessing. |

**No source, no verdict:** any verdict other than "can't verify" must quote a sentence that appears word for word in the text Tavily returned. If the model paraphrases, Sayproof asks it once to copy the sentence exactly. If the quote still isn't there, the verdict drops to "can't verify".

Talks in German and other languages work: explanations and suggestions come back in the language of the talk.

## How it works

```
transcript ─► Nemotron (Nebius Token Factory): extract checkable claims
           ─► Tavily search, one query per claim, in parallel
           ─► Nemotron: verdict + exact evidence quote + suggestion
           ─► quote guard (one retry, then downgrade) ─► receipt
```

Claims about the speaker's own company, team or results ("our users tripled") are flagged at extraction and never searched: no public source can back them, and a search only finds someone else's numbers. The receipt marks them "can't verify" and asks for the speaker's own data.

- **Model:** `nvidia/nemotron-3-super-120b-a12b` on Nebius Token Factory (OpenAI-compatible API), configurable with `NEMOTRON_MODEL`
- **Search:** Tavily, `search_depth="advanced"`, 5 results per claim, in two tiers:
  1. **Reference sources only** (`include_domains_mode="restrict"`): public bodies (NASA, NIH, EU…), science organisations, encyclopedias, major news outlets. List in `sayproof/clients.py`.
  2. **Open web as fallback**, only if tier 1 leaves the claim unverified, with user-generated, video and social sites blocked. The receipt labels these sources "outside the reference sources".
- **App:** FastAPI backend plus one static page (`sayproof/static/index.html`)

## Deploy

Hosted on Vercel (FastAPI preset, entrypoint `index.py`). Set `NEBIUS_API_KEY` and `TAVILY_API_KEY` in the project's environment variables, then run `vercel deploy --prod`. Regenerate `requirements.txt` after changing dependencies: `uv export --no-dev --no-hashes --no-emit-project -o requirements.txt`.

## Run it

```bash
cp .env.example .env        # add NEBIUS_API_KEY and TAVILY_API_KEY
uv sync
uv run uvicorn sayproof.app:app --port 8765
# open http://localhost:8765
```

Tests (pipeline logic and quote guard; Nemotron and Tavily replaced by test fakes):

```bash
uv run pytest
```

## Status

- Text input works end to end against the live Nemotron and Tavily APIs (about 9–15 s for 3–4 claims).
- The public demo limits each visitor to 6 checks per 10 minutes, 300 checks a day, 3000 characters and 8 claims per check. The limits are in memory per server instance, so they slow abuse down but are not a hard cap. Set `LIMIT_PER_IP`, `LIMIT_WINDOW_S` and `LIMIT_PER_DAY` to change them.
- Source filtering was chosen by measurement (`scripts/eval_search.py`, results in `eval/`). On 18 claims with a known answer (10 well-known myths, 8 less-known facts in English and German), each judged twice:

  | Setting | Correct verdicts | Cited source from reference list |
  |---|---|---|
  | Open web, social sites blocked (previous) | 36/36 | 16/36 |
  | Reference sources only | 33/36 | 33/33 |
  | **Two tiers (current)** | **36/36** | **35/36** |

  The set is small, and a single setting varied by up to 2 verdicts between runs, so the verdict column cannot separate the settings. The source column can. Tavily does not accept whole-TLD patterns such as `*.gov` or `*.edu` in `include_domains`, so the reference list names organisations.
- **Verdict quality** (`scripts/eval_verdicts.py`, set in `eval/verdict_set.json`): 50 claims with a known answer (true facts, myths, wrong numbers, outdated present-tense claims, dated claims, claims about the speaker, 9 in German), each through the production path 3 times. Run on 2026-10-10:

  | | Result |
  |---|---|
  | Verdicts that match the label, or a defensible alternative (contradicted for outdated) | 150/150 |
  | Exact match with the label | 146/150 |
  | Confidently wrong (a wrong verdict other than "can't verify") | 0/150 |
  | Same verdict in all 3 runs | 49/50 claims |
  | Checkable claims found in 4 sample talks, opinions and plans skipped | 30/30 found, 0/27 opinions kept |

  Model noise only: Tavily results are cached across the 3 runs (`--fresh` searches every run). Two earlier runs of the same set had 2 and 3 confidently wrong verdicts out of 150. Those runs led to the speaker-claim rule above, after "Unser Umsatz hat sich verdoppelt" was "supported" with another company's revenue, and to a rule that rounded numbers (8,849 m for 8,848.86 m) match. One of them also called "bulls are enraged by red" supported, citing a study on cattle in red light. 50 claims is still a small set.
- **Speech input** runs locally with NVIDIA Parakeet (`uv sync --extra speech`, Apple Silicon). Parakeet is not in the Nebius Token Factory model list, so the hosted demo is text only.
- Verdicts can vary between runs, because search results and model output change. For a claim that is judged borderline, re-run it or check the source yourself.

## Pitch

- Deck: https://sayproof-pitch.netlify.app (source in `deck/`)

## License

MIT, see `LICENSE`.
