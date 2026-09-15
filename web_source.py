"""
Получение лотов со страницы lzt.market без платного API.

Загружает обычную HTML-страницу каталога (как браузер) и вытаскивает лоты
по ссылкам вида https://lzt.market/<item_id>/ . Разметка сайта может
меняться, поэтому парсер опирается не на CSS-классы, а на ссылки лотов и
текст вокруг них.

Два способа загрузки:
  1. requests с заголовками браузера и, при необходимости, cookies из вашего
     браузера (LZT_COOKIES) - быстро и без зависимостей.
  2. Playwright + Chromium (USE_BROWSER=1) - если сайт отдаёт проверку
     DDoS-Guard/Cloudflare и вариант 1 не проходит.
"""

from __future__ import annotations

import logging
import re
import time
from html.parser import HTMLParser

import requests

log = logging.getLogger("lzt-monitor.web")

MARKET_URL = "https://lzt.market"
ITEM_LINK_RE = re.compile(r"^(?:https?://lzt\.market)?/(\d{3,})/?(?:[?#].*)?$")
PRICE_RE = re.compile(r"(\d[\d\s  ]*(?:[.,]\d+)?)\s*(₽|\$|€|руб|USD|EUR)", re.IGNORECASE)
CHALLENGE_MARKERS = ("ddos-guard", "__ddg", "cf-challenge", "challenge-platform", "just a moment", "checking your browser")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


class WebSourceError(RuntimeError):
    pass


# ---------- разбор HTML ----------

class _Node:
    __slots__ = ("tag", "attrs", "parent", "children", "text")

    def __init__(self, tag: str, attrs: dict, parent: "_Node | None"):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list["_Node"] = []
        self.text: list[str] = []

    def all_text(self) -> str:
        parts = list(self.text)
        for child in self.children:
            parts.append(child.all_text())
        return " ".join(p for p in parts if p)


class _TreeBuilder(HTMLParser):
    VOID = {"br", "img", "input", "meta", "link", "hr", "area", "base", "col", "embed", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, None)
        self.cur = self.root
        self.anchors: list[_Node] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        node = _Node(tag, dict(attrs), self.cur)
        self.cur.children.append(node)
        if tag == "a":
            self.anchors.append(node)
        if tag not in self.VOID:
            self.cur = node

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        if self._skip:
            return
        text = " ".join(data.split())
        if text:
            self.cur.text.append(text)


def _parse_price(text: str) -> tuple[int | float | None, str | None]:
    m = PRICE_RE.search(text)
    if not m:
        return None, None
    raw = re.sub(r"[\s  ]", "", m.group(1)).replace(",", ".")
    try:
        value: int | float = float(raw)
        if value.is_integer():
            value = int(value)
    except ValueError:
        return None, None
    cur = m.group(2).lower()
    currency = {"₽": "rub", "руб": "rub", "$": "usd", "usd": "usd", "€": "eur", "eur": "eur"}.get(cur, cur)
    return value, currency


def _item_block(anchor: _Node, link_re: re.Pattern = ITEM_LINK_RE) -> _Node:
    """Поднимаемся от ссылки к блоку лота: до элемента, где встречается цена,
    но не дальше блока, в котором есть ссылки на другие лоты."""
    node = anchor
    own_id = link_re.match(anchor.attrs.get("href", "")).group(1)
    for _ in range(8):
        parent = node.parent
        if parent is None or parent.tag == "root":
            break
        other_ids = {
            m.group(1)
            for a in _anchors_in(parent)
            if (m := link_re.match(a.attrs.get("href", ""))) and m.group(1) != own_id
        }
        if other_ids:
            break
        node = parent
        if PRICE_RE.search(node.all_text()):
            cls = node.attrs.get("class", "")
            if "item" in cls.lower() or node.tag in ("article", "li"):
                break
    return node


def _anchors_in(node: _Node) -> list[_Node]:
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.tag == "a":
            out.append(n)
        stack.extend(n.children)
    return out


def parse_items(html_text: str, link_re: re.Pattern = ITEM_LINK_RE, item_url: str | None = None) -> list[dict]:
    """Ищет лоты по ссылкам, подходящим под link_re (первая группа - id лота).

    item_url - шаблон адреса лота с {id}; если задан, попадает в item["url"].
    """
    builder = _TreeBuilder()
    builder.feed(html_text)

    items: dict[int, dict] = {}
    for anchor in builder.anchors:
        m = link_re.match(anchor.attrs.get("href", "") or "")
        if not m:
            continue
        item_id = int(m.group(1))
        block = _item_block(anchor, link_re)
        block_text = block.all_text()
        price, currency = _parse_price(block_text)
        title = anchor.all_text().strip() or anchor.attrs.get("title", "").strip()

        item = items.setdefault(
            item_id,
            {"item_id": item_id, "title": "", "price": None, "price_currency": None, "web_details": ""},
        )
        # Самая длинная текстовая ссылка на лот - обычно его заголовок
        if len(title) > len(item["title"]) and not PRICE_RE.fullmatch(title):
            item["title"] = title
        if item["price"] is None and price is not None:
            item["price"] = price
            item["price_currency"] = currency
        if len(block_text) > len(item["web_details"]):
            item["web_details"] = block_text

    result = []
    for item in items.values():
        details = item["web_details"]
        if item["title"]:
            details = details.replace(item["title"], " ")
        details = re.sub(r"\s+", " ", details).strip()
        if PRICE_RE.fullmatch(details):  # кроме цены ничего нет, цена и так выводится
            details = ""
        item["web_details"] = details[:300]
        if not item["title"]:
            item["title"] = f"Лот #{item['item_id']}"
        if item_url:
            item["url"] = item_url.format(id=item["item_id"])
        result.append(item)
    return result




# ---------- загрузка ----------

def _looks_like_challenge(status: int, text: str) -> bool:
    low = text[:20000].lower()
    return status in (403, 503) or (len(text) < 20000 and any(m in low for m in CHALLENGE_MARKERS))


def fetch_html_requests(url: str, cookies: str | None, session: requests.Session | None = None) -> str:
    session = session or requests.Session()
    headers = dict(BROWSER_HEADERS)
    if cookies:
        headers["Cookie"] = cookies
    resp = session.get(url, headers=headers, timeout=30)
    if _looks_like_challenge(resp.status_code, resp.text):
        raise WebSourceError(
            f"Сайт вернул проверку/блокировку (HTTP {resp.status_code}). "
            "Скопируйте cookies из браузера в LZT_COOKIES или включите USE_BROWSER=1."
        )
    if resp.status_code >= 400:
        raise WebSourceError(f"HTTP {resp.status_code} при загрузке {url}")
    return resp.text


def fetch_html_browser(url: str, cookies: str | None, user_data_dir: str = ".browser-profile") -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise WebSourceError(
            "Для USE_BROWSER=1 установите: pip install playwright && playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir,
            headless=True,
            user_agent=BROWSER_HEADERS["User-Agent"],
            locale="ru-RU",
        )
        try:
            if cookies:
                ctx.add_cookies(
                    [
                        {"name": k.strip(), "value": v.strip(), "domain": ".lzt.market", "path": "/"}
                        for k, v in (c.split("=", 1) for c in cookies.split(";") if "=" in c)
                    ]
                )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Даём DDoS-Guard/Cloudflare время пройти проверку и странице догрузиться
            for _ in range(20):
                html_text = page.content()
                if re.search(r'href="(?:https?://lzt\.market)?/\d{3,}/?', html_text):
                    return html_text
                time.sleep(1.5)
            html_text = page.content()
            if _looks_like_challenge(200, html_text):
                raise WebSourceError("Браузер не смог пройти проверку сайта. Попробуйте задать LZT_COOKIES.")
            return html_text
        finally:
            ctx.close()


def fetch_items_web(
    category: str,
    query: str,
    cookies: str | None = None,
    use_browser: bool = False,
    session: requests.Session | None = None,
) -> list[dict]:
    url = f"{MARKET_URL}/{category}/?{query}"
    if use_browser:
        html_text = fetch_html_browser(url, cookies)
    else:
        html_text = fetch_html_requests(url, cookies, session)
    items = parse_items(html_text)
    if not items:
        log.warning("На странице не найдено ни одного лота. Проверьте фильтры или включите USE_BROWSER=1.")
    return items


def fetch_page_items(
    url: str,
    link_re: re.Pattern,
    item_url: str,
    cookies: str | None = None,
    use_browser: bool = False,
    session: requests.Session | None = None,
    user_data_dir: str = ".browser-profile",
) -> list[dict]:
    """Универсальная загрузка каталога другого сайта: адрес страницы + регулярка ссылок на лот."""
    if use_browser:
        html_text = fetch_html_browser(url, cookies, user_data_dir)
    else:
        html_text = fetch_html_requests(url, cookies, session)
    items = parse_items(html_text, link_re, item_url)
    if not items:
        log.warning("На странице %s не найдено ни одного лота.", url)
    return items
