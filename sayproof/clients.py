"""Real API clients: Nemotron on Nebius Token Factory, and Tavily search."""

from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI
from tavily import AsyncTavilyClient

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"


class MissingKey(RuntimeError):
    pass


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingKey(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


class Nemotron:
    def __init__(self) -> None:
        self.model = os.environ.get("NEMOTRON_MODEL", DEFAULT_MODEL)
        self.client = AsyncOpenAI(api_key=require("NEBIUS_API_KEY"),
                                  base_url=os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL))

    async def complete(self, system: str, user: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=4096,
        )
        return resp.choices[0].message.content or ""


# Tier 1: reference sources. Tavily rejects whole-TLD patterns ("*.gov", "*.edu"), so this names
# organisations; "*.org.tld" also matches their subdomains. Measured with scripts/eval_search.py.
REFERENCE_DOMAINS = [
    "*.nasa.gov", "*.nih.gov", "*.noaa.gov", "*.usgs.gov", "*.cdc.gov", "*.nist.gov", "*.si.edu",
    "*.europa.eu", "who.int", "esa.int", "britannica.com", "*.wikipedia.org", "nature.com", "science.org",
    "scientificamerican.com", "nationalgeographic.com", "smithsonianmag.com", "reuters.com", "apnews.com",
    "bbc.com", "bbc.co.uk", "theguardian.com", "nytimes.com", "dw.com", "tagesschau.de", "spiegel.de",
    "zeit.de", "bpb.de", "planet-wissen.de", "toureiffel.paris", "berlin.de", "stiftung-berliner-mauer.de",
]

# Tier 2 (open web): never use pages that are user-generated, video, or rarely quotable as evidence.
BLOCKED_DOMAINS = [
    "youtube.com", "tiktok.com", "instagram.com", "facebook.com", "x.com", "twitter.com", "pinterest.com",
    "scribd.com", "quora.com", "reddit.com", "medium.com", "linkedin.com", "slideshare.net",
    "brainly.com", "brainly.in", "chegg.com", "coursehero.com", "answers.com",
]


class Tavily:
    def __init__(self) -> None:
        self.client = AsyncTavilyClient(api_key=require("TAVILY_API_KEY"))

    async def search(self, query: str, tier: str = "open_web") -> list[dict[str, Any]]:
        if tier == "reference":
            filters = dict(include_domains=REFERENCE_DOMAINS, include_domains_mode="restrict")
        else:
            filters = dict(exclude_domains=BLOCKED_DOMAINS)
        resp = await self.client.search(query=query, search_depth="advanced", max_results=5, **filters)
        return resp.get("results", [])
