#!/usr/bin/env python3
"""
Мониторинг новых лотов на lzt.market и отправка уведомлений в Telegram.

Использует официальный API Lolzteam Market (https://lzt-market.readme.io),
которое принимает те же фильтры, что и ссылка на сайте:
    https://lzt.market/telegram/?country[]=UZ&min_contacts=100&spam=no

Настройка через переменные окружения (см. .env.example):
    SOURCE           - "web" (страница сайта, без платного API, по умолчанию)
                       или "api" (официальный API, нужен LZT_TOKEN)
    LZT_COOKIES      - (web) строка Cookie из браузера, если сайт блокирует запросы
    USE_BROWSER      - (web) "1" чтобы грузить страницу через Playwright/Chromium
    LZT_TOKEN        - (api) токен API Lolzteam (https://lolz.live/account/api)
    TG_BOT_TOKEN     - токен Telegram-бота от @BotFather
    TG_CHAT_ID       - id чата, куда слать уведомления (python get_chat_id.py)
    LZT_CATEGORY     - категория, по умолчанию "telegram"
    LZT_QUERY        - строка фильтров из ссылки, по умолчанию
                       "country[]=UZ&min_contacts=100&spam=no"
    POLL_INTERVAL    - период опроса в секундах, по умолчанию 60
    NOTIFY_ON_FIRST_RUN - "1" чтобы отправить уже существующие лоты при первом запуске
    STATE_FILE       - путь к файлу состояния, по умолчанию state.json
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import requests

from web_source import WebSourceError, fetch_items_web

try:  # .env необязателен, но удобен для локального запуска
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger("lzt-monitor")

API_BASE = os.getenv("LZT_API_BASE", "https://prod-api.lzt.market").rstrip("/")
MARKET_URL = "https://lzt.market"
TG_API = "https://api.telegram.org"

DEFAULT_QUERY = "country[]=UZ&min_contacts=100&spam=no"
MAX_SEEN_IDS = 5000
TELEGRAM_MESSAGE_LIMIT = 4096


class Config:
    def __init__(self) -> None:
        self.lzt_token = os.getenv("LZT_TOKEN", "").strip()
        self.source = os.getenv("SOURCE", "api" if self.lzt_token else "web").strip().lower()
        self.cookies = os.getenv("LZT_COOKIES", "").strip() or None
        self.use_browser = os.getenv("USE_BROWSER", "0") == "1"
        self.tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
        self.tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
        self.category = os.getenv("LZT_CATEGORY", "telegram").strip().strip("/")
        self.query = os.getenv("LZT_QUERY", DEFAULT_QUERY).strip().lstrip("?")
        self.poll_interval = max(5, int(os.getenv("POLL_INTERVAL", "60")))
        self.notify_on_first_run = os.getenv("NOTIFY_ON_FIRST_RUN", "0") == "1"
        self.state_file = Path(os.getenv("STATE_FILE", "state.json"))

    def validate(self) -> None:
        if self.source not in ("api", "web"):
            raise SystemExit("SOURCE должен быть 'web' или 'api'")
        required = [("TG_BOT_TOKEN", self.tg_bot_token), ("TG_CHAT_ID", self.tg_chat_id)]
        if self.source == "api":
            required.append(("LZT_TOKEN", self.lzt_token))
        missing = [name for name, value in required if not value]
        if missing:
            raise SystemExit(
                "Не заданы переменные окружения: "
                + ", ".join(missing)
                + ". Скопируйте .env.example в .env и заполните."
            )

    def api_params(self) -> list[tuple[str, str]]:
        """Фильтры из ссылки + принудительная сортировка по дате публикации.

        Сортировка по цене (как в исходной ссылке) не годится для поиска
        новых лотов: свежий лот может оказаться не на первой странице.
        Цена всё равно приходит в каждом лоте и показывается в сообщении.
        """
        params = [(k, v) for k, v in parse_qsl(self.query, keep_blank_values=True)
                  if k not in ("order_by", "page")]
        params.append(("order_by", "pdate_to_down"))
        return params

    def web_query(self) -> str:
        """Та же строка фильтров для страницы сайта, с сортировкой по дате."""
        return urlencode(self.api_params())

    def site_url(self) -> str:
        return f"{MARKET_URL}/{self.category}/?{self.query}"


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen_ids: list[int] = []
        self.initialized = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.seen_ids = [int(x) for x in data.get("seen_ids", [])]
            self.initialized = bool(data.get("initialized", False))
        except (ValueError, OSError) as exc:
            log.warning("Не удалось прочитать %s: %s. Начинаю с пустого состояния.", self.path, exc)

    def save(self) -> None:
        self.seen_ids = self.seen_ids[-MAX_SEEN_IDS:]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"seen_ids": self.seen_ids, "initialized": self.initialized}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def is_seen(self, item_id: int) -> bool:
        return item_id in self.seen_ids

    def mark_seen(self, item_id: int) -> None:
        if item_id not in self.seen_ids:
            self.seen_ids.append(item_id)


class LztClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "lzt-market-monitor/1.0",
            }
        )

    def fetch_items(self, category: str, params: list[tuple[str, str]]) -> list[dict]:
        url = f"{API_BASE}/{category}"
        backoff = 5
        for attempt in range(1, 6):
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                log.warning("Ошибка сети (попытка %d): %s", attempt, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                log.warning("Лимит запросов API, жду %d с", retry_after)
                time.sleep(retry_after)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code in (401, 403):
                raise SystemExit(
                    f"API вернул {resp.status_code}: проверьте LZT_TOKEN. Ответ: {resp.text[:300]}"
                )
            if resp.status_code >= 500:
                log.warning("API вернул %d, повтор через %d с", resp.status_code, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("errors"):
                raise RuntimeError(f"API вернул ошибку: {data['errors']}")
            # Ответ: {"items": [...], "totalItems": N, "perPage": N, "hasNextPage": bool, ...}
            items = data.get("items", []) if isinstance(data, dict) else []
            return [i for i in items if isinstance(i, dict) and "item_id" in i]

        log.error("Не удалось получить список лотов после нескольких попыток")
        return []


class WebClient:
    """Источник без API: разбор HTML-страницы каталога (см. web_source.py)."""

    def __init__(self, cookies: str | None, use_browser: bool) -> None:
        self.cookies = cookies
        self.use_browser = use_browser
        self.session = requests.Session()

    def fetch_items(self, category: str, params: list[tuple[str, str]]) -> list[dict]:
        try:
            return fetch_items_web(
                category, urlencode(params), self.cookies, self.use_browser, self.session
            )
        except (WebSourceError, requests.RequestException) as exc:
            log.error("Не удалось загрузить страницу: %s", exc)
            return []


class Telegram:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.url = f"{TG_API}/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.session = requests.Session()

    def send(self, text: str) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text[:TELEGRAM_MESSAGE_LIMIT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(1, 4):
            try:
                resp = self.session.post(self.url, json=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("Telegram: ошибка сети (попытка %d): %s", attempt, exc)
                time.sleep(3 * attempt)
                continue
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram: лимит, жду %s с", retry_after)
                time.sleep(int(retry_after))
                continue
            if resp.ok and resp.json().get("ok"):
                return True
            log.error("Telegram: ответ %d: %s", resp.status_code, resp.text[:300])
            return False
        return False


# ---------- форматирование ----------

# Поля, которые уже выводятся отдельными строками
KNOWN_TELEGRAM_KEYS = {
    "telegram_country", "telegram_contacts", "telegram_contacts_count",
    "telegram_spam_block", "telegram_premium", "telegram_chats_count",
    "telegram_channels_count", "telegram_conversations_count",
}
# Служебные и потенциально чувствительные поля, их в чат не шлём
HIDDEN_KEYS = {
    "telegram_client", "telegram_json", "telegram_api_id", "telegram_api_hash",
    "telegram_phone", "telegram_password", "telegram_id", "telegram_username",
}

def _first(item: dict, *keys: str):
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


def _fmt_date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "?"


def _yes_no(value) -> str:
    if value in (True, 1, "1", "yes"):
        return "да"
    if value in (False, 0, "0", "no"):
        return "нет"
    return str(value)


def format_item(item: dict) -> str:
    item_id = item["item_id"]
    title = html.escape(str(_first(item, "title", "title_en") or f"Лот #{item_id}"))
    # В ответе API: price + price_currency (например "rub"), дублируется в rub_price
    price = _first(item, "price", "rub_price")
    currency = str(_first(item, "price_currency", "currency") or "rub").lower()
    currency = {"rub": "₽", "usd": "$", "eur": "€"}.get(currency, currency.upper())
    price_str = f"{price} {currency}" if price is not None else "?"

    lines = [f"🆕 <b>{title}</b>", f"💰 Цена: <b>{html.escape(price_str)}</b>"]

    country = _first(item, "telegram_country")
    if country:
        lines.append(f"🌍 Страна: {html.escape(str(country))}")
    contacts = _first(item, "telegram_contacts", "telegram_contacts_count")
    if contacts is not None:
        lines.append(f"👥 Контакты: {contacts}")
    spam = _first(item, "telegram_spam_block")
    if spam is not None:
        lines.append(f"🚫 Спамблок: {_yes_no(spam)}")
    premium = _first(item, "telegram_premium")
    if premium is not None:
        lines.append(f"⭐ Premium: {_yes_no(premium)}")
    for label, keys in (
        ("Чаты", ("telegram_chats_count",)),
        ("Каналы", ("telegram_channels_count",)),
        ("Диалоги", ("telegram_conversations_count",)),
    ):
        value = _first(item, *keys)
        if value is not None:
            lines.append(f"• {label}: {value}")
    # Остальные telegram_* поля выводим как есть: точный набор полей в
    # спецификации API не типизирован, так ничего не потеряется.
    for key in sorted(item):
        if not key.startswith("telegram_") or key in KNOWN_TELEGRAM_KEYS or key in HIDDEN_KEYS:
            continue
        value = item[key]
        if value in (None, "", [], {}) or isinstance(value, (dict, list)):
            continue
        label = key[len("telegram_"):].replace("_", " ")
        lines.append(f"• {html.escape(label)}: {html.escape(str(value))}")
    details = _first(item, "web_details")
    if details:
        lines.append(f"ℹ️ {html.escape(str(details))}")
    origin = _first(item, "item_origin")
    if origin:
        lines.append(f"📦 Происхождение: {html.escape(str(origin))}")
    published = _first(item, "published_date")
    if published:
        lines.append(f"🕒 Опубликован: {_fmt_date(published)}")

    lines.append(f"🔗 {MARKET_URL}/{item_id}")
    return "\n".join(lines)


# ---------- основной цикл ----------

def check_once(cfg: Config, state: State, lzt: "LztClient | WebClient", tg: Telegram) -> int:
    items = lzt.fetch_items(cfg.category, cfg.api_params())
    if not items:
        return 0

    new_items = [i for i in items if not state.is_seen(int(i["item_id"]))]

    if not state.initialized:
        state.initialized = True
        if not cfg.notify_on_first_run:
            for item in items:
                state.mark_seen(int(item["item_id"]))
            state.save()
            log.info("Первый запуск: запомнил %d текущих лотов, уведомлять буду о новых", len(items))
            return 0

    # Самые старые сначала, чтобы сообщения шли в хронологическом порядке
    new_items.sort(key=lambda i: int(i.get("published_date") or 0))

    sent = 0
    for item in new_items:
        item_id = int(item["item_id"])
        if tg.send(format_item(item)):
            sent += 1
            log.info("Отправлен лот %d (%s)", item_id, item.get("price"))
        else:
            log.error("Не удалось отправить лот %d, попробую в следующий раз", item_id)
            continue
        state.mark_seen(item_id)
        state.save()
        time.sleep(1)  # мягкий лимит Telegram на сообщения в один чат
    return sent


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = Config()
    cfg.validate()

    tg = Telegram(cfg.tg_bot_token, cfg.tg_chat_id)

    if "--test" in argv:
        ok = tg.send("✅ Монитор lzt.market подключён.\nСлежу за: " + html.escape(cfg.site_url()))
        print("Тестовое сообщение отправлено" if ok else "Не удалось отправить тестовое сообщение")
        return 0 if ok else 1

    if cfg.source == "api":
        lzt = LztClient(cfg.lzt_token)
    else:
        lzt = WebClient(cfg.cookies, cfg.use_browser)

    if "--dump" in argv:
        # Показать сырой ответ API: удобно, чтобы увидеть реальные названия полей
        items = lzt.fetch_items(cfg.category, cfg.api_params())
        print(json.dumps(items[:3], ensure_ascii=False, indent=2))
        print(f"\nВсего лотов на первой странице: {len(items)}")
        return 0

    state = State(cfg.state_file)

    log.info(
        "Слежу за %s каждые %d с (источник: %s)", cfg.site_url(), cfg.poll_interval, cfg.source
    )
    once = "--once" in argv
    while True:
        try:
            check_once(cfg, state, lzt, tg)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001 - монитор не должен падать из-за одной ошибки
            log.exception("Ошибка при проверке")
        if once:
            return 0
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
