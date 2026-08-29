import logging
import re
import urllib.parse
from typing import List, Dict

import httpx
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

logger = logging.getLogger(__name__)


def clean_text_no_links(text: str) -> str:
    """Remove any URLs or web links from text."""
    if not text:
        return ""
    cleaned = re.sub(r'https?://\S+|www\.\S+', '', text)
    return cleaned.strip()


class WebSearch:
    @staticmethod
    async def search(query: str, max_results: int = 4) -> List[Dict[str, str]]:
        """Perform an async web search on DuckDuckGo and return search results with title, snippet, and link."""
        if not query or not query.strip():
            return []

        results: List[Dict[str, str]] = []
        encoded_query = urllib.parse.quote(query.strip())
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(url, headers=headers, timeout=8.0)
                if resp.status_code == 200:
                    if HAS_BS4:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for result in soup.find_all("div", class_="result"):
                            title_tag = result.find("a", class_="result__a")
                            snippet_tag = result.find("a", class_="result__snippet")
                            if title_tag and snippet_tag:
                                title = title_tag.get_text(strip=True)
                                snippet = snippet_tag.get_text(strip=True)
                                raw_link = title_tag.get("href", "")
                                # Extract actual destination URL if DuckDuckGo redirect link
                                link = raw_link
                                if "/uddg=" in raw_link:
                                    match = re.search(r'uddg=([^&]+)', raw_link)
                                    if match:
                                        link = urllib.parse.unquote(match.group(1))

                                if title and snippet:
                                    results.append({
                                        "title": title,
                                        "snippet": snippet,
                                        "link": link,
                                    })
                                    if len(results) >= max_results:
                                        break
        except Exception as e:
            logger.warning(f"Web search failed for query '{query}': {e}")

        return results
