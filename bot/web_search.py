import asyncio
import logging
import re
import urllib.parse
from typing import List, Dict

import httpx

try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

logger = logging.getLogger(__name__)


def sanitize_utf8(text: str) -> str:
    """Sanitize text to remove invalid Unicode surrogate characters (U+D800-U+DFFF)."""
    if not text:
        return ""
    return re.sub(r'[\uD800-\uDFFF]', '', str(text))


def clean_text_no_links(text: str) -> str:
    """Remove any URLs, web links, and invalid Unicode surrogates from text."""
    if not text:
        return ""
    text = sanitize_utf8(text)
    cleaned = re.sub(r'https?://\S+|www\.\S+', '', text)
    return cleaned.strip()


def extract_search_query(text: str) -> str:
    """Extract a clean, effective search query from a conversational user message."""
    if not text:
        return ""
    q = re.sub(r'@[a-zA-Z0-9_]+', '', text).strip()
    patterns = [
        r'^(?:can\s+you\s+)?(?:please\s+)?(?:search\s+(?:for\s+)?|find\s+(?:out\s+)?(?:about\s+)?|look\s+up\s+)',
        r'^(?:can\s+you\s+)?(?:please\s+)?(?:tell\s+me\s+(?:about\s+)?|give\s+me\s+(?:info\s+on\s+)?|what\s+is\s+|who\s+is\s+|where\s+is\s+)',
        r'^(?:hey|hi|hello)\s+(?:bot\s+)?,?\s*',
    ]
    for p in patterns:
        q = re.sub(p, '', q, flags=re.IGNORECASE).strip()
    q = q.rstrip('?!.,')
    return q.strip() if len(q.strip()) >= 3 else text.strip()


def is_search_worthy(text: str) -> bool:
    """Determine if a user message warrants a live web search."""
    if not text:
        return False
    t = text.lower().strip()
    # Skip pure conversational greetings
    greetings = {"hi", "hello", "hey", "good morning", "good evening", "good night", "how are you", "who are you", "what are you doing", "what's up", "sup", "nya", "hiee", "bye"}
    if t in greetings:
        return False
    triggers = (
        "search", "who", "what", "where", "when", "why", "how",
        "latest", "today", "yesterday", "tomorrow", "weather", "news", "price",
        "score", "current", "update", "release", "version", "winner",
        "match", "stock", "tell me about", "find", "is it", "which",
        "who is", "what is", "info on", "about", "rate", "new",
        "ceo", "founder", "president", "launch", "versus", "vs"
    )
    return any(k in t for k in triggers) or "?" in t


class WebSearch:
    @staticmethod
    async def search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Perform web search using duckduckgo_search library with multi-tier fallback."""
        if not query or not query.strip():
            return []

        clean_query = sanitize_utf8(query.strip())
        results: List[Dict[str, str]] = []

        # 1. Primary Method: Official duckduckgo_search library (DDGS)
        if HAS_DDGS:
            try:
                def _ddgs_fetch(q: str, limit: int):
                    with DDGS() as ddgs:
                        # Try text search
                        res = list(ddgs.text(q, max_results=limit))
                        if not res:
                            # Try news search fallback
                            res = list(ddgs.news(q, max_results=limit))
                        return res

                raw_results = await asyncio.to_thread(_ddgs_fetch, clean_query, max_results)
                for item in raw_results:
                    title = sanitize_utf8(item.get("title", ""))
                    snippet = sanitize_utf8(item.get("body", item.get("snippet", "")))
                    link = sanitize_utf8(item.get("href", item.get("link", "")))
                    if title or snippet:
                        results.append({
                            "title": title,
                            "snippet": snippet,
                            "link": link
                        })
                if results:
                    logger.info(f"DDGS web search retrieved {len(results)} results for '{clean_query}'")
                    return results
            except Exception as e:
                logger.warning(f"DDGS search failed for '{clean_query}': {e}. Trying HTML fallback...")

        # 2. Secondary Fallback: DuckDuckGo HTML Scraping
        try:
            encoded_query = urllib.parse.quote(clean_query)
            url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            }
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(url, headers=headers, timeout=8.0)
                if resp.status_code == 200 and HAS_BS4:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for result in soup.find_all("div", class_="result"):
                        title_tag = result.find("a", class_="result__a")
                        snippet_tag = result.find("a", class_="result__snippet")
                        if title_tag and snippet_tag:
                            title = sanitize_utf8(title_tag.get_text(strip=True))
                            snippet = sanitize_utf8(snippet_tag.get_text(strip=True))
                            raw_link = title_tag.get("href", "")
                            link = raw_link
                            if "/uddg=" in raw_link:
                                match = re.search(r'uddg=([^&]+)', raw_link)
                                if match:
                                    link = urllib.parse.unquote(match.group(1))
                            link = sanitize_utf8(link)
                            if title or snippet:
                                results.append({
                                    "title": title,
                                    "snippet": snippet,
                                    "link": link,
                                })
                                if len(results) >= max_results:
                                    break
                if results:
                    return results
        except Exception as e:
            logger.warning(f"HTML web search fallback failed for '{clean_query}': {e}")

        # 3. Tertiary Fallback: DuckDuckGo Instant Answer API
        try:
            api_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(clean_query)}&format=json&no_html=1&skip_disambig=1"
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(api_url, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    abstract = sanitize_utf8(data.get("AbstractText", ""))
                    heading = sanitize_utf8(data.get("Heading", ""))
                    source_url = sanitize_utf8(data.get("AbstractURL", ""))
                    if abstract:
                        results.append({
                            "title": heading or clean_query,
                            "snippet": abstract,
                            "link": source_url,
                        })
                    for topic in data.get("RelatedTopics", [])[:3]:
                        if isinstance(topic, dict) and topic.get("Text"):
                            results.append({
                                "title": heading or clean_query,
                                "snippet": sanitize_utf8(topic.get("Text")),
                                "link": sanitize_utf8(topic.get("FirstURL", "")),
                            })
        except Exception as e:
            logger.warning(f"Instant Answer API fallback failed for '{clean_query}': {e}")

        return results
