#!/usr/bin/env python3
"""
Web tools: web_search (local SearXNG) and web_fetch (readable-text extraction).
Uses requests; the SearXNG endpoint comes from config.
"""

import re
from html.parser import HTMLParser
from typing import Any, Dict

import requests

# Set by config.apply_to_tools() at startup.
SEARXNG_URL = "http://localhost:8899"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LOCAL-Intelligence-agent/0.1"
_SEARCH_TIMEOUT = 20
_FETCH_TIMEOUT = 25


def set_searxng_url(url: str) -> None:
    global SEARXNG_URL
    SEARXNG_URL = (url or SEARXNG_URL).rstrip("/")


def web_search(inp: Dict[str, Any]) -> str:
    query = str(inp.get("query", "")).strip()
    if not query:
        return "Error: 'query' is required"
    max_results = min(int(inp.get("max_results", 6) or 6), 10)

    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json"},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=_SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.ConnectionError:
        return (
            f"Error: web search unavailable — could not reach SearXNG at {SEARXNG_URL}. "
            "Is the SearXNG container running? (Start Docker Desktop, or run the install script's "
            "search step.) File and shell tools still work without it."
        )
    except Exception as e:
        return f"Error: web search failed ({e})."

    results = data.get("results", [])[:max_results]
    if not results:
        return f"No results found for: {query}"

    lines = [f"Web results for: {query}"]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = re.sub(r"\s+", " ", (r.get("content") or "").strip())[:250]
        lines.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")

    answers = data.get("answers") or []
    if answers:
        first = answers[0]
        text = first.get("answer") if isinstance(first, dict) else str(first)
        if text:
            lines.append(f"\nInstant answer: {text[:400]}")

    lines.append("\nUse web_fetch on the most promising URL(s) to read full content.")
    return "\n".join(lines)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def web_fetch(inp: Dict[str, Any]) -> str:
    url = str(inp.get("url", "")).strip()
    if not url:
        return "Error: 'url' is required"
    if not url.lower().startswith(("http://", "https://")):
        return f"Error: only http(s) URLs are supported: {url}"
    max_chars = min(int(inp.get("max_chars", 8000) or 8000), 20000)

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept": "*/*"},
            timeout=_FETCH_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.raw.read(3_000_000, decode_content=True)
        final_url = resp.url
    except Exception as e:
        return f"Error fetching {url}: {e}"

    body = raw.decode("utf-8", errors="replace")

    if "html" in content_type or body[:200].lstrip().lower().startswith(("<!doctype", "<html")):
        extractor = _TextExtractor()
        try:
            extractor.feed(body)
            text = "".join(extractor.parts)
        except Exception:
            text = body
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    else:
        text = body.strip()

    header = f"Content of {final_url}" + (f" (redirected from {url})" if final_url != url else "")
    if len(text) < 200:
        return (
            f"{header}: little or no readable text extracted "
            f"({len(text)} chars — this page likely renders via JavaScript). "
            "Try web_fetch on a DIFFERENT search result, preferably a news article or docs page."
            + (f"\n\n{text}" if text else "")
        )
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated at {max_chars} chars)"
    return f"{header}:\n\n{text}"
