"""Извлечение полного текста новости (CONTRACTS §2)."""
from __future__ import annotations

import html as html_module
import logging
import re
from urllib.parse import urlparse

import httpx

from ..config import NewsSource
from .sources import RawItem

logger = logging.getLogger(__name__)


def html_to_text(html: str) -> str:
    """HTML -> текст: убрать script/style, взять содержимое <p>, join через \\n, unescape."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.DOTALL | re.IGNORECASE)
    paragraphs = [re.sub(r"<[^>]+>", "", p) for p in paragraphs]
    text = "\n".join(paragraphs)
    return html_module.unescape(text)


class RobotsCache:
    """Кэш правил robots.txt по хосту."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str) -> None:
        self._client = client
        self._user_agent = user_agent
        self._cache: dict[str, list[str]] = {}

    async def _disallowed(self, host: str) -> list[str]:
        if host in self._cache:
            return self._cache[host]
        rules: list[str] = []
        try:
            resp = await self._client.get(f"{host}/robots.txt")
        except httpx.RequestError:
            self._cache[host] = rules
            return rules
        if resp.status_code == 200:
            current_ua_match = False
            for line in resp.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("user-agent:"):
                    ua = line.split(":", 1)[1].strip()
                    current_ua_match = ua in ("*", self._user_agent)
                elif current_ua_match and line.lower().startswith("disallow:"):
                    path = line.split(":", 1)[1].strip()
                    if path:
                        rules.append(path)
        self._cache[host] = rules
        return rules

    async def allowed(self, url: str) -> bool:
        """Можно ли сканировать URL по robots.txt. Ошибка/404 -> True."""
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        rules = await self._disallowed(host)
        path = parsed.path or "/"
        return not any(path.startswith(rule) for rule in rules)


async def get_fulltext(
    client: httpx.AsyncClient,
    robots: RobotsCache,
    item: RawItem,
    src: NewsSource,
    max_bytes: int,
) -> str | None:
    """Получить полный текст статьи. 'rss' -> из content_html; 'page' -> с сайта."""
    if src.fulltext == "rss":
        if item.content_html is None:
            return None
        return html_to_text(item.content_html)[:max_bytes]
    # page
    if not await robots.allowed(item.url):
        return None
    try:
        resp = await client.get(item.url)
    except httpx.RequestError as exc:
        logger.warning("не удалось загрузить %s: %s", item.url, exc)
        return None
    if resp.status_code != 200:
        return None
    return html_to_text(resp.text)[:max_bytes]
