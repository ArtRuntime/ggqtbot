import asyncio
import logging
import random
import re
import urllib.parse
from typing import List, Dict, Any

import httpx

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except (ImportError, SyntaxError, Exception):
    HAS_BS4 = False

try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except (ImportError, SyntaxError, Exception):
    HAS_DDGS = False

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except (ImportError, SyntaxError, Exception):
    HAS_CURL_CFFI = False

try:
    import nodriver as uc
    from pyvirtualdisplay import Display
    HAS_NODRIVER = True
except (ImportError, SyntaxError, Exception):
    HAS_NODRIVER = False

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
    # Strip Telegram bot commands and trigger prefixes (e.g. /chat, /ask, /ai, !ask, /search)
    q = re.sub(r'^[/!](?:chat|ask|ai|search|find|tell)\b', '', q, flags=re.IGNORECASE).strip()
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

    # Skip pure date and time inquiries (handled directly by real-time Indian Standard Time context)
    time_phrases = (
        "what is the time", "what time", "current time", "what's the time", "time now", "time right now",
        "what is the date", "what date", "current date", "today's date", "todays date", "date today",
        "what day is today", "what day is it", "what day is this", "what is today",
        "what year is it", "what month is it", "what is current time", "tell me time", "tell me date"
    )
    if any(tp in t for tp in time_phrases) or t in ("time", "date", "clock", "today"):
        return False

    # Currency and exchange rate patterns (e.g. "1 usd in inr", "usd to inr", "btc price", "gold rate")
    currencies = (
        "usd", "inr", "eur", "gbp", "jpy", "cad", "aud", "cny", "rub", "aed", "sar",
        "btc", "eth", "sol", "usdt", "crypto", "bitcoin", "dollar", "rupee", "euro",
        "gold", "silver", "petrol", "diesel"
    )
    if any(c in t for c in currencies) and any(w in t for w in ("in", "to", "price", "rate", "value", "worth", "exchange", "convert", "today", "now", "vs", "cost")):
        return True

    if any(sym in t for sym in ("$", "₹", "€", "£", "¥")):
        return True

    triggers = (
        "search", "who", "what", "where", "when", "why", "how",
        "latest", "today", "yesterday", "tomorrow", "weather", "news", "price",
        "score", "current", "update", "release", "version", "winner",
        "match", "stock", "tell me about", "find", "is it", "which",
        "who is", "what is", "info on", "about", "rate", "new",
        "ceo", "founder", "president", "launch", "versus", "vs",
        "conversion", "exchange rate", "market", "stands for", "meaning",
        "height of", "net worth", "capital of", "population", "gdp", "stats"
    )
    return any(k in t for k in triggers) or "?" in t


# ==========================================
# 1. Google Search Engine (nodriver + HTTP)
# ==========================================
class GoogleSearchEngine:
    @staticmethod
    async def search_nodriver(query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Scrape Google Search using nodriver and virtual display."""
        if not HAS_NODRIVER:
            return []
        
        def _run_nodriver():
            async def _inner():
                with Display(visible=0, size=(1920, 1080)):
                    browser = await uc.start(headless=False)
                    try:
                        encoded = urllib.parse.quote(query)
                        page = await browser.get(f"https://www.google.com/search?q={encoded}&hl=en")
                        await asyncio.sleep(3.5)
                        containers = await page.select_all("div.MjjYud")
                        res_list = []
                        for container in containers:
                            try:
                                title_el = await container.query_selector("h3")
                                link_el = await container.query_selector("a")
                                snippet_el = await container.query_selector("div[style*='-webkit-line-clamp']") or \
                                             await container.query_selector(".VwiC3b")
                                if title_el and link_el:
                                    attrs = link_el.attributes
                                    link = ""
                                    for i in range(len(attrs)):
                                        if attrs[i] == "href":
                                            link = attrs[i + 1]
                                            break
                                    if not link or any(x in link for x in ["youtube.com", "youtu.be", "google.com", "/search"]):
                                        continue
                                    res_list.append({
                                        "title": sanitize_utf8(title_el.text.strip()),
                                        "link": sanitize_utf8(link),
                                        "snippet": sanitize_utf8(snippet_el.text.replace('\n', ' ').strip() if snippet_el else "No snippet.")
                                    })
                                    if len(res_list) >= max_results:
                                        break
                            except Exception:
                                continue
                        return res_list
                    finally:
                        browser.stop()
            return uc.loop().run_until_complete(_inner())

        try:
            return await asyncio.to_thread(_run_nodriver)
        except Exception as e:
            logger.debug(f"Google nodriver search failed: {e}")
            return []

    @staticmethod
    def _parse_google_html(html: str, max_results: int = 5) -> List[Dict[str, str]]:
        if not HAS_BS4 or not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for container in soup.select("div.MjjYud, div.g"):
            title_tag = container.select_one("h3")
            link_tag = container.select_one("a")
            snippet_tag = container.select_one("div[style*='-webkit-line-clamp'], .VwiC3b, span.aCOpRe")
            if title_tag and link_tag and link_tag.get("href"):
                link = link_tag["href"]
                if link.startswith("/url?q="):
                    link = link.split("/url?q=")[1].split("&")[0]
                link = urllib.parse.unquote(link)
                if not link.startswith("http") or any(x in link for x in ["google.com", "youtube.com", "/search"]):
                    continue
                title = sanitize_utf8(title_tag.get_text(strip=True))
                snippet = sanitize_utf8(snippet_tag.get_text(strip=True) if snippet_tag else "")
                if title:
                    results.append({
                        "title": title,
                        "link": link,
                        "snippet": snippet or title
                    })
                    if len(results) >= max_results:
                        break
        return results

    @classmethod
    async def search_cffi(cls, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Scrape Google Search using curl_cffi Chrome impersonation."""
        if not HAS_CURL_CFFI:
            return []
        try:
            def _fetch():
                session = cffi_requests.Session()
                encoded = urllib.parse.quote(query)
                url = f"https://www.google.com/search?q={encoded}&hl=en"
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                }
                resp = session.get(url, headers=headers, impersonate="chrome120", timeout=10)
                if resp.status_code == 200:
                    return cls._parse_google_html(resp.text, max_results=max_results)
                return []
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.debug(f"Google curl_cffi search notice: {e}")
            return []

    @classmethod
    async def search_http(cls, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Scrape Google Search using HTTP request impersonation."""
        encoded = urllib.parse.quote(query)
        url = f"https://www.google.com/search?q={encoded}&hl=en"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return cls._parse_google_html(resp.text, max_results=max_results)
        except Exception as e:
            logger.debug(f"Google HTTP search fallback notice: {e}")
        return []

    @classmethod
    async def search(cls, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        if HAS_NODRIVER:
            try:
                res = await cls.search_nodriver(query, max_results=max_results)
                if res:
                    return res
            except Exception as e:
                logger.debug(f"Google nodriver search exception: {e}")
        
        if HAS_CURL_CFFI:
            res = await cls.search_cffi(query, max_results=max_results)
            if res:
                return res

        return await cls.search_http(query, max_results=max_results)


# ==========================================
# 2. Bing Search Engine (curl_cffi + httpx)
# ==========================================
class BingSearchEngine:
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ]

    @classmethod
    def _get_headers(cls) -> dict:
        return {
            "User-Agent": random.choice(cls.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.bing.com/",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }

    @classmethod
    def _parse_bing_html(cls, html: str, max_results: int = 5) -> List[Dict[str, str]]:
        if not HAS_BS4 or not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("li.b_algo"):
            title_node = item.select_one("h2 a")
            snippet_node = item.select_one(".b_caption p, .b_algoSlug, .b_snippet")
            if title_node and title_node.get("href"):
                link = title_node["href"]
                if link.startswith("/ck/") or "bing.com" in link:
                    continue
                title = sanitize_utf8(title_node.get_text(strip=True))
                snippet = sanitize_utf8(snippet_node.get_text(strip=True) if snippet_node else "")
                if title:
                    results.append({
                        "title": title,
                        "link": sanitize_utf8(link),
                        "snippet": snippet or title
                    })
                    if len(results) >= max_results:
                        break
        return results

    @classmethod
    async def search(cls, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Search Bing with curl_cffi Chrome impersonation and async httpx fallback."""
        clean_query = sanitize_utf8(query.strip())
        if not clean_query:
            return []

        # 1. Try curl_cffi with Chrome impersonation
        if HAS_CURL_CFFI:
            try:
                def _cffi_fetch():
                    session = cffi_requests.Session()
                    params = {"q": clean_query, "first": 1}
                    resp = session.get(
                        "https://www.bing.com/search",
                        params=params,
                        headers=cls._get_headers(),
                        impersonate="chrome120",
                        timeout=10
                    )
                    if resp.status_code == 200:
                        return cls._parse_bing_html(resp.text, max_results=max_results)
                    return []

                cffi_res = await asyncio.to_thread(_cffi_fetch)
                if cffi_res:
                    return cffi_res
            except Exception as e:
                logger.debug(f"Bing curl_cffi search notice: {e}")

        # 2. Try httpx fallback
        try:
            encoded_query = urllib.parse.quote(clean_query)
            url = f"https://www.bing.com/search?q={encoded_query}&first=1"
            async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
                resp = await client.get(url, headers=cls._get_headers())
                if resp.status_code == 200:
                    return cls._parse_bing_html(resp.text, max_results=max_results)
        except Exception as e:
            logger.debug(f"Bing httpx search fallback notice: {e}")

        return []


# ==========================================
# 3. DuckDuckGo Search Engine
# ==========================================
class DuckDuckGoSearchEngine:
    @staticmethod
    async def search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Search DuckDuckGo with DDGS, HTML scraping, and Instant Answer API."""
        clean_query = sanitize_utf8(query.strip())
        if not clean_query:
            return []

        results = []
        # 1. Official DDGS library
        if HAS_DDGS:
            try:
                def _ddgs_fetch(q: str, limit: int):
                    with DDGS() as ddgs:
                        res = list(ddgs.text(q, max_results=limit))
                        if not res:
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
                    return results
            except Exception as e:
                logger.debug(f"DDGS search notice: {e}")

        # 2. DuckDuckGo HTML scraping fallback
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
            async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
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
            logger.debug(f"DuckDuckGo HTML fallback notice: {e}")

        # 3. DuckDuckGo Instant Answer API fallback
        try:
            api_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(clean_query)}&format=json&no_html=1&skip_disambig=1"
            async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
                resp = await client.get(api_url)
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
        except Exception as e:
            logger.debug(f"DuckDuckGo API fallback notice: {e}")

        return results


# ==========================================
# 4. Multi-Engine Combiner & Aggregator
# ==========================================
class WebSearch:
    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL to identify duplicates across different search engines."""
        if not url:
            return ""
        url = url.strip().rstrip("/")
        # Remove common tracking parameters
        url = re.sub(r'[?&](?:utm_[^&]+|ref=[^&]+|fbclid=[^&]+)', '', url)
        return url.lower()

    @classmethod
    async def search(cls, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """
        Execute parallel web search across Google, Bing, and DuckDuckGo.
        Deduplicates, merges, and compiles the most relevant organic results.
        """
        if not query or not query.strip():
            return []

        clean_query = sanitize_utf8(query.strip())
        logger.info(f"Executing multi-engine web search for query: '{clean_query}'")

        # Query all three search engines concurrently with a safety timeout
        engines_tasks = [
            asyncio.create_task(GoogleSearchEngine.search(clean_query, max_results=max_results)),
            asyncio.create_task(BingSearchEngine.search(clean_query, max_results=max_results)),
            asyncio.create_task(DuckDuckGoSearchEngine.search(clean_query, max_results=max_results)),
        ]

        engine_outputs = await asyncio.gather(*engines_tasks, return_exceptions=True)

        combined_results: List[Dict[str, str]] = []
        seen_urls = set()
        seen_titles = set()

        # Interleave and merge results for maximum diversity and accuracy
        # Extract engine lists safely
        valid_lists: List[List[Dict[str, str]]] = []
        for out in engine_outputs:
            if isinstance(out, list) and out:
                valid_lists.append(out)

        # Round-robin collection across search engines
        max_depth = max((len(l) for l in valid_lists), default=0)
        for depth in range(max_depth):
            for eng_list in valid_lists:
                if depth < len(eng_list):
                    item = eng_list[depth]
                    title = sanitize_utf8(item.get("title", "")).strip()
                    link = sanitize_utf8(item.get("link", "")).strip()
                    snippet = sanitize_utf8(item.get("snippet", "")).strip()

                    norm_url = cls._normalize_url(link)
                    norm_title = re.sub(r'[^a-zA-Z0-9]', '', title.lower())[:40]

                    # Skip duplicate URLs and identical titles
                    if norm_url and norm_url in seen_urls:
                        continue
                    if norm_title and norm_title in seen_titles:
                        continue

                    if title and (snippet or link):
                        if norm_url:
                            seen_urls.add(norm_url)
                        if norm_title:
                            seen_titles.add(norm_title)

                        combined_results.append({
                            "title": title,
                            "link": link,
                            "snippet": snippet
                        })

                        if len(combined_results) >= max_results:
                            break
            if len(combined_results) >= max_results:
                break

        logger.info(f"Multi-engine search returned {len(combined_results)} compiled results for '{clean_query}'")
        return combined_results
