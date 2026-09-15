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
    TG_USER_ID       - (необязательно) ваш Telegram ID; если задан, бот отвечает
                       только вам. Иначе подписаться может любой, кто напишет /start
    TG_CHAT_ID       - (необязательно) чат, который подписан сразу, без /start
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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import requests
from urllib.parse import urlparse

from web_source import TRON_URL, WebSourceError, fetch_items_web, fetch_page_items, tron_link_re

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

# Диагностика последней проверки, показывается в /status
STATS: dict = {
    "started_at": time.time(),
    "checks": 0,
    "last_check_at": None,
    "last_items": None,
    "last_new": 0,
    "last_error": None,
    "last_error_at": None,
    "last_meta": {},
}


def _record_error(message: str) -> None:
    STATS["last_error"] = message[:300]
    STATS["last_error_at"] = time.time()


def _ago(ts) -> str:
    if not ts:
        return "ещё не было"
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta} с назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    return f"{delta // 3600} ч {delta % 3600 // 60} мин назад"


class Config:
    def __init__(self) -> None:
        self.lzt_token = os.getenv("LZT_TOKEN", "").strip()
        self.source = os.getenv("SOURCE", "api" if self.lzt_token else "web").strip().lower()
        self.cookies = os.getenv("LZT_COOKIES", "").strip() or None
        self.use_browser = os.getenv("USE_BROWSER", "0") == "1"
        self.tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
        self.tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
        self.tg_user_ids = {
            int(x) for x in os.getenv("TG_USER_ID", "").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()
        }
        self.category = os.getenv("LZT_CATEGORY", "telegram").strip().strip("/")
        self.query = os.getenv("LZT_QUERY", DEFAULT_QUERY).strip().lstrip("?")
        self.poll_interval = max(5, int(os.getenv("POLL_INTERVAL", "60")))
        self.notify_on_first_run = os.getenv("NOTIFY_ON_FIRST_RUN", "0") == "1"
        self.buy_confirm = os.getenv("BUY_CONFIRM", "0") == "1"  # 1 - спрашивать подтверждение перед покупкой
        # Второй сайт: страница каталога tronaccs.market (пусто - выключено)
        self.tron_url = os.getenv("TRON_URL", "").strip() or None
        self.cookies_tron = os.getenv("TRON_COOKIES", "").strip() or None
        self.state_file = Path(os.getenv("STATE_FILE", "state.json"))

    def validate(self) -> None:
        if self.source not in ("api", "web"):
            raise SystemExit("SOURCE должен быть 'web' или 'api'")
        required = [("TG_BOT_TOKEN", self.tg_bot_token)]
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

    def apply_state(self, state: "State") -> None:
        """Фильтр, заданный через /filter, важнее значений из .env."""
        if state.category and state.query is not None:
            self.category = state.category
            self.query = state.query
        if state.tron_url is not None:  # "" означает выключено командой /tron off
            self.tron_url = state.tron_url or None


def parse_market_url(url: str) -> tuple[str, str]:
    """https://lzt.market/telegram/?country[]=UZ&spam=no -> ("telegram", "country[]=UZ&spam=no")."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in ("lzt.market", "www.lzt.market"):
        raise ValueError("Нужна ссылка на lzt.market, например https://lzt.market/telegram/?spam=no")
    category = parsed.path.strip("/").split("/")[0]
    if not category or category.isdigit():
        raise ValueError("В ссылке нет категории. Откройте раздел каталога (например /telegram/) и скопируйте адрес.")
    query = parsed.query.strip()
    return category, query


class State:
    """Состояние на диске: просмотренные лоты, подписчики, смещение обновлений бота."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.seen_ids: list[int] = []
        self.initialized = False
        self.subscribers: list[int] = []
        self.tg_offset = 0
        self.category: str | None = None   # фильтр, заданный командой /filter
        self.query: str | None = None
        self.tron_url: str | None = None   # адрес каталога tronaccs, заданный /tron
        self.extra: dict[str, dict] = {}   # просмотренные лоты других сайтов: {"tron": {"seen": [], "initialized": bool}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.seen_ids = [int(x) for x in data.get("seen_ids", [])]
            self.initialized = bool(data.get("initialized", False))
            self.subscribers = [int(x) for x in data.get("subscribers", [])]
            self.tg_offset = int(data.get("tg_offset", 0))
            self.category = data.get("category") or None
            self.query = data.get("query")
            self.tron_url = data.get("tron_url")
            self.extra = {
                name: {"seen": [int(x) for x in v.get("seen", [])], "initialized": bool(v.get("initialized"))}
                for name, v in (data.get("extra") or {}).items()
            }
        except (ValueError, OSError) as exc:
            log.warning("Не удалось прочитать %s: %s. Начинаю с пустого состояния.", self.path, exc)

    def save(self) -> None:
        with self.lock:
            self.seen_ids = self.seen_ids[-MAX_SEEN_IDS:]
            data = {
                "seen_ids": self.seen_ids,
                "initialized": self.initialized,
                "subscribers": self.subscribers,
                "tg_offset": self.tg_offset,
                "category": self.category,
                "query": self.query,
                "tron_url": self.tron_url,
                "extra": self.extra,
            }
            for v in self.extra.values():
                v["seen"] = v["seen"][-MAX_SEEN_IDS:]
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    # Просмотренные лоты. "lzt" хранится в seen_ids/initialized, остальные сайты в extra.
    def _bucket(self, name: str) -> dict:
        if name not in self.extra:
            self.extra[name] = {"seen": [], "initialized": False}
        return self.extra[name]

    def is_seen(self, item_id: int, name: str = "lzt") -> bool:
        if name == "lzt":
            return item_id in self.seen_ids
        return item_id in self._bucket(name)["seen"]

    def mark_seen(self, item_id: int, name: str = "lzt") -> None:
        seen = self.seen_ids if name == "lzt" else self._bucket(name)["seen"]
        if item_id not in seen:
            seen.append(item_id)

    def is_initialized(self, name: str = "lzt") -> bool:
        return self.initialized if name == "lzt" else self._bucket(name)["initialized"]

    def set_initialized(self, name: str = "lzt", value: bool = True) -> None:
        if name == "lzt":
            self.initialized = value
        else:
            self._bucket(name)["initialized"] = value

    def set_tron_url(self, url: str) -> None:
        """"" выключает второй сайт. Лоты по нему забываем, первая проверка пройдёт молча."""
        with self.lock:
            self.tron_url = url
            self.extra["tron"] = {"seen": [], "initialized": False}
            self.save()

    def subscribe(self, chat_id: int) -> bool:
        with self.lock:
            if chat_id in self.subscribers:
                return False
            self.subscribers.append(chat_id)
            self.save()
            return True

    def unsubscribe(self, chat_id: int) -> bool:
        with self.lock:
            if chat_id not in self.subscribers:
                return False
            self.subscribers.remove(chat_id)
            self.save()
            return True

    def set_filter(self, category: str, query: str) -> None:
        """Новый фильтр: старые лоты забываем, первая проверка пройдёт молча."""
        with self.lock:
            self.category = category
            self.query = query
            self.seen_ids = []
            self.initialized = False
            self.save()


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
                _record_error(f"сеть: {exc}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                log.warning("Лимит запросов API, жду %d с", retry_after)
                _record_error("API: превышен лимит запросов (429)")
                time.sleep(retry_after)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code in (401, 403):
                _record_error(f"API {resp.status_code}: проверьте LZT_TOKEN. {resp.text[:200]}")
                log.error("API вернул %d: проверьте LZT_TOKEN. Ответ: %s", resp.status_code, resp.text[:300])
                return []
            if resp.status_code >= 500:
                log.warning("API вернул %d, повтор через %d с", resp.status_code, backoff)
                _record_error(f"API вернул {resp.status_code}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code >= 400:
                _record_error(f"API {resp.status_code}: {resp.text[:200]}")
                log.error("API вернул %d: %s", resp.status_code, resp.text[:300])
                return []
            try:
                data = resp.json()
            except ValueError:
                _record_error(f"API вернул не JSON: {resp.text[:200]}")
                log.error("API вернул не JSON: %s", resp.text[:300])
                return []
            if isinstance(data, dict) and data.get("errors"):
                _record_error(f"API: {data['errors']}")
                log.error("API вернул ошибку: %s", data["errors"])
                return []
            # Ответ: {"items": [...], "totalItems": N, "perPage": N, "hasNextPage": bool,
            #         "cacheTTL": N, "wasCached": bool, ...}
            items = data.get("items", []) if isinstance(data, dict) else []
            if isinstance(data, dict):
                STATS["last_meta"] = {
                    k: data.get(k) for k in ("totalItems", "perPage", "wasCached", "cacheTTL", "hasNextPage")
                    if k in data
                }
            return [i for i in items if isinstance(i, dict) and "item_id" in i]

        log.error("Не удалось получить список лотов после нескольких попыток")
        return []

    def fast_buy(self, item_id: int, price=None) -> tuple[bool, str]:
        """POST /{item_id}/fast-buy: проверка аккаунта и покупка одним запросом.

        Возвращает (успех, текст для пользователя). price передаём ту, что видели:
        если продавец поднял цену, маркет откажет и деньги не спишет.
        """
        body = {}
        if price is not None:
            body["price"] = price
        try:
            resp = self.session.post(f"{API_BASE}/{item_id}/fast-buy", json=body, timeout=90)
        except requests.RequestException as exc:
            return False, f"Ошибка сети: {exc}"
        try:
            data = resp.json()
        except ValueError:
            data = {}
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            return False, "; ".join(str(e) for e in errors)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        item = data.get("item") if isinstance(data, dict) else None
        if isinstance(item, dict) and item.get("item_state") not in (None, "paid", "sold"):
            return False, f"Маркет вернул состояние лота: {item.get('item_state')}"
        return True, "Покупка прошла. Данные аккаунта на странице лота."


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
            _record_error(f"страница: {exc}")
            return []


class TronClient:
    """tronaccs.market: разбор HTML-страницы каталога, API у сайта нет."""

    name = "tron"

    def __init__(self, url: str, cookies: str | None = None, use_browser: bool = False) -> None:
        self.url = url
        self.cookies = cookies
        self.use_browser = use_browser
        self.session = requests.Session()
        # Категория из ссылки (?category=telegram) нужна для адреса лота
        cat = dict(parse_qsl(urlparse(url).query)).get("category") or "telegram"
        self.item_url = f"{TRON_URL}/{cat}/{{id}}"
        self.link_re = tron_link_re(cat)

    def fetch_items(self) -> list[dict]:
        try:
            items = fetch_page_items(
                self.url, self.link_re, self.item_url, self.cookies, self.use_browser,
                self.session, user_data_dir=".browser-profile-tron",
            )
        except (WebSourceError, requests.RequestException) as exc:
            log.error("tronaccs: не удалось загрузить страницу: %s", exc)
            _record_error(f"tronaccs: {exc}")
            return []
        for item in items:
            item["source"] = self.name
        return items


class Telegram:
    def __init__(self, bot_token: str) -> None:
        self.base = f"{TG_API}/bot{bot_token}"
        self.session = requests.Session()

    def call(self, method: str, payload: dict) -> dict | None:
        """Вызов метода Bot API с повторами. Возвращает result или None."""
        for attempt in range(1, 4):
            try:
                resp = self.session.post(f"{self.base}/{method}", json=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("Telegram: ошибка сети (попытка %d): %s", attempt, exc)
                time.sleep(3 * attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram: лимит, жду %s с", retry_after)
                time.sleep(int(retry_after))
                continue
            if resp.ok and data.get("ok"):
                return data.get("result") or {}
            log.error("Telegram %s: ответ %d: %s", method, resp.status_code, resp.text[:300])
            return None
        return None

    def send(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> bool:
        payload = {
            "chat_id": chat_id,
            "text": text[:TELEGRAM_MESSAGE_LIMIT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload) is not None

    def send_all(self, chat_ids: list[int], text: str, reply_markup: dict | None = None) -> int:
        """Отправить всем подписчикам. Возвращает число успешных отправок."""
        ok = 0
        for chat_id in list(chat_ids):
            if self.send(chat_id, text, reply_markup):
                ok += 1
        return ok

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        self.call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup or {"inline_keyboard": []}},
        )

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert})

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        try:
            resp = self.session.get(
                f"{self.base}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": timeout,
                    "allowed_updates": '["message","callback_query"]',
                },
                timeout=timeout + 10,
            )
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Telegram: getUpdates не удался: %s", exc)
            time.sleep(5)
            return []
        if not data.get("ok"):
            log.error("Telegram: getUpdates вернул ошибку: %s", str(data)[:300])
            time.sleep(5)
            return []
        return data.get("result", [])


class Bot:
    """Обработка команд в личке: /start подписывает чат, /stop отписывает."""

    HELP = (
        "Команды:\n"
        "/start — подписаться на новые лоты\n"
        "/stop — отписаться\n"
        "/status — что мониторится и сколько лотов запомнено\n"
        "/filter <ссылка> — сменить фильтр: пришлите адрес страницы lzt.market с нужными фильтрами\n"
        "/filter — показать текущий фильтр\n"
        "/check — проверить прямо сейчас и показать последние лоты по фильтру\n"
        "/tron <ссылка> — следить ещё и за tronaccs.market (адрес страницы каталога)\n"
        "/tron off — выключить tronaccs, /tron — показать текущий адрес\n"
        "/id — показать ваш Telegram ID"
    )

    def __init__(self, cfg: Config, state: State, tg: Telegram, lzt=None, tron: "TronClient | None" = None) -> None:
        self.cfg = cfg
        self.state = state
        self.tg = tg
        self.lzt = lzt
        self.tron = tron

    def allowed(self, user_id: int) -> bool:
        return not self.cfg.tg_user_ids or user_id in self.cfg.tg_user_ids

    # ---------- кнопки под лотом ----------

    @staticmethod
    def _price_label(price, currency) -> str:
        cur = {"rub": "₽", "usd": "$", "eur": "€"}.get(str(currency or "rub").lower(), str(currency or ""))
        return f"{price} {cur}".strip()

    def item_keyboard(self, item: dict, stage: str = "buy") -> dict:
        """stage: "buy" — кнопка Купить; "confirm" — Подтвердить/Отмена; "done" — только ссылка."""
        item_id = int(item["item_id"])
        price = item.get("price")
        rows = []
        can_buy = self.cfg.source == "api" and price is not None and item.get("source", "lzt") == "lzt"
        if can_buy and stage == "buy":
            rows.append([{"text": f"🛒 Купить за {self._price_label(price, item.get('price_currency'))}",
                          "callback_data": f"buy:{item_id}:{price}"}])
        elif can_buy and stage == "confirm":
            rows.append([
                {"text": f"✅ Подтвердить за {self._price_label(price, item.get('price_currency'))}",
                 "callback_data": f"confirm:{item_id}:{price}"},
                {"text": "❌ Отмена", "callback_data": f"cancel:{item_id}:{price}"},
            ])
        rows.append([{"text": "🔗 Открыть на сайте", "url": item.get("url") or f"{MARKET_URL}/{item_id}/"}])
        return {"inline_keyboard": rows}

    def handle_callback(self, cq: dict) -> None:
        cq_id = cq.get("id", "")
        user_id = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        data = cq.get("data") or ""
        parts = data.split(":")
        if len(parts) != 3 or chat_id is None:
            self.tg.answer_callback(cq_id)
            return
        action, item_id_s, price_s = parts
        try:
            item_id = int(item_id_s)
            price = float(price_s)
            price = int(price) if price.is_integer() else price
        except ValueError:
            self.tg.answer_callback(cq_id)
            return
        item = {"item_id": item_id, "price": price}

        if not self.allowed(user_id):
            self.tg.answer_callback(cq_id, "⛔ Доступ только владельцу", alert=True)
            return
        if self.cfg.source != "api" or not hasattr(self.lzt, "fast_buy"):
            self.tg.answer_callback(cq_id, "Покупка доступна только в режиме SOURCE=api", alert=True)
            return

        if action == "buy" and self.cfg.buy_confirm:
            self.tg.edit_markup(chat_id, message_id, self.item_keyboard(item, "confirm"))
            self.tg.answer_callback(cq_id, "Подтвердите покупку")
        elif action == "cancel":
            self.tg.edit_markup(chat_id, message_id, self.item_keyboard(item, "buy"))
            self.tg.answer_callback(cq_id, "Отменено")
        elif action in ("buy", "confirm"):
            self.tg.answer_callback(cq_id, "Покупаю…")
            self.tg.edit_markup(chat_id, message_id, self.item_keyboard(item, "done"))
            ok, text = self.lzt.fast_buy(item_id, price)
            log.info("Покупка лота %d за %s пользователем %s: %s — %s", item_id, price, user_id, ok, text)
            if ok:
                self.tg.send(chat_id, f"✅ Куплен лот {item_id} за {self._price_label(price, None)}.\n"
                                      f"{html.escape(text)}\n{MARKET_URL}/{item_id}/")
            else:
                self.tg.send(chat_id, f"❌ Не удалось купить лот {item_id}: {html.escape(text)}\n{MARKET_URL}/{item_id}/",
                             self.item_keyboard(item, "buy"))
        else:
            self.tg.answer_callback(cq_id)

    def handle(self, update: dict) -> None:
        if update.get("callback_query"):
            self.handle_callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        chat_id = chat.get("id")
        user_id = user.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text.startswith("/"):
            return
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "/id":
            self.tg.send(chat_id, f"Ваш Telegram ID: <code>{user_id}</code>\nID этого чата: <code>{chat_id}</code>")
            return
        if not self.allowed(user_id):
            log.info("Отказ пользователю %s (чат %s): нет в TG_USER_ID", user_id, chat_id)
            self.tg.send(chat_id, "⛔ Этот бот приватный. Доступ только владельцу.")
            return

        if command == "/start":
            if self.state.subscribe(chat_id):
                self.tg.send(
                    chat_id,
                    "✅ Подписал. Буду присылать новые лоты по фильтру:\n"
                    + html.escape(self.cfg.site_url()) + "\n\n" + self.HELP,
                )
            else:
                self.tg.send(chat_id, "Вы уже подписаны.\n\n" + self.HELP)
        elif command == "/stop":
            if self.state.unsubscribe(chat_id):
                self.tg.send(chat_id, "🔕 Отписал. Чтобы вернуть уведомления — /start")
            else:
                self.tg.send(chat_id, "Вы и так не подписаны. Подписаться — /start")
        elif command == "/filter":
            if not arg:
                self.tg.send(
                    chat_id,
                    "Текущий фильтр:\n" + html.escape(self.cfg.site_url())
                    + "\n\nЧтобы сменить, пришлите:\n<code>/filter https://lzt.market/telegram/?country[]=UZ&amp;spam=no</code>",
                )
                return
            try:
                category, query = parse_market_url(arg)
            except ValueError as exc:
                self.tg.send(chat_id, "⚠️ " + html.escape(str(exc)))
                return
            self.cfg.category = category
            self.cfg.query = query
            self.state.set_filter(category, query)
            log.info("Фильтр изменён пользователем %s: %s", user_id, self.cfg.site_url())
            self.tg.send(
                chat_id,
                "✅ Фильтр обновлён:\n" + html.escape(self.cfg.site_url())
                + "\n\nТекущие лоты по нему запомню молча, дальше буду присылать только новые.",
            )
        elif command == "/check":
            if self.lzt is None:
                self.tg.send(chat_id, "Источник лотов не подключён.")
                return
            self.tg.send(chat_id, "🔎 Проверяю…")
            items = self.lzt.fetch_items(self.cfg.category, self.cfg.api_params())
            if not items:
                self.tg.send(chat_id, "lzt.market: по фильтру ничего не найдено или сайт не ответил. Подробности в /status.")
            else:
                latest = sorted(items, key=lambda i: int(i.get("published_date") or 0), reverse=True)[:3]
                self.tg.send(chat_id, f"lzt.market: лотов на первой странице {len(items)}. Последние:")
                for item in latest:
                    self.tg.send(chat_id, format_item(item), self.item_keyboard(item))
            if self.tron is not None:
                items = self.tron.fetch_items()
                if not items:
                    self.tg.send(chat_id, "tronaccs: лоты на странице не найдены или сайт не ответил. Подробности в /status.")
                else:
                    self.tg.send(chat_id, f"tronaccs: лотов на странице {len(items)}. Первые:")
                    for item in items[:3]:
                        self.tg.send(chat_id, format_item(item), self.item_keyboard(item))
        elif command == "/tron":
            if not arg:
                url = self.cfg.tron_url
                head = ("tronaccs: слежу за\n" + html.escape(url)) if url else "tronaccs выключен."
                self.tg.send(
                    chat_id,
                    head + "\n\nВключить: <code>/tron https://tronaccs.market/?category=telegram</code>\nВыключить: /tron off",
                )
                return
            if arg.lower() in ("off", "выкл", "стоп"):
                self.cfg.tron_url = None
                self.tron = None
                self.state.set_tron_url("")
                self.tg.send(chat_id, "🔕 tronaccs выключен.")
                return
            url = arg if "://" in arg else "https://" + arg
            if "tronaccs.market" not in urlparse(url).netloc.lower():
                self.tg.send(chat_id, "⚠️ Нужна ссылка на tronaccs.market, например https://tronaccs.market/?category=telegram")
                return
            self.cfg.tron_url = url
            self.tron = TronClient(url, self.cfg.cookies_tron, self.cfg.use_browser)
            self.state.set_tron_url(url)
            log.info("tronaccs включён пользователем %s: %s", user_id, url)
            self.tg.send(chat_id, "✅ tronaccs включён:\n" + html.escape(url)
                         + "\n\nТекущие лоты запомню молча, дальше буду присылать новые. Проверить: /check")
        elif command == "/status":
            with self.state.lock:
                subs = len(self.state.subscribers)
                seen = len(self.state.seen_ids)
                subscribed = chat_id in self.state.subscribers
            last_items = STATS["last_items"]
            meta = STATS["last_meta"]
            lines = [
                f"Фильтр: {html.escape(self.cfg.site_url())}",
                f"Источник: {self.cfg.source}, интервал: {self.cfg.poll_interval} с",
                "tronaccs: " + (html.escape(self.cfg.tron_url) if self.cfg.tron_url else "выключен"),
                f"Подписчиков: {subs}, запомнено лотов: {seen}",
                f"Этот чат {'подписан ✅' if subscribed else 'не подписан'}",
                "",
                f"Работает: {_ago(STATS['started_at']).replace(' назад', '')}, проверок: {STATS['checks']}",
                f"Последняя проверка: {_ago(STATS['last_check_at'])}",
            ]
            if last_items is not None:
                lines.append(f"Лотов по фильтру: {last_items}, новых в последней проверке: {STATS['last_new']}")
            if STATS.get("tron_items") is not None:
                lines.append(f"tronaccs: лотов {STATS['tron_items']}, новых {STATS['tron_new']}")
            if meta:
                lines.append("Ответ API: " + html.escape(", ".join(f"{k}={v}" for k, v in meta.items())))
            if STATS["last_error"]:
                lines.append(
                    f"⚠️ Последняя ошибка ({_ago(STATS['last_error_at'])}): "
                    + html.escape(str(STATS["last_error"]))
                )
            self.tg.send(chat_id, "\n".join(lines))
        else:
            self.tg.send(chat_id, self.HELP)

    def run_forever(self) -> None:
        while True:
            try:
                updates = self.tg.get_updates(self.state.tg_offset)
                for update in updates:
                    with self.state.lock:
                        self.state.tg_offset = max(self.state.tg_offset, int(update["update_id"]) + 1)
                    try:
                        self.handle(update)
                    except Exception:  # noqa: BLE001
                        log.exception("Ошибка при обработке команды")
                if updates:
                    self.state.save()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в цикле бота")
                time.sleep(5)


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

    lines.append(f"🔗 {item.get('url') or f'{MARKET_URL}/{item_id}'}")
    if item.get("source") == "tron":
        lines[0] = "🆕 <b>tronaccs</b> · " + lines[0][len("🆕 "):]
    return "\n".join(lines)


# ---------- основной цикл ----------

def check_once(cfg: Config, state: State, lzt: "LztClient | WebClient", tg: Telegram, bot: "Bot | None" = None) -> int:
    items = lzt.fetch_items(cfg.category, cfg.api_params())
    STATS["checks"] += 1
    STATS["last_check_at"] = time.time()
    STATS["last_items"] = len(items)
    STATS["last_new"] = 0
    if not items:
        log.warning("Проверка #%d: лотов нет (ошибка или пустой фильтр)", STATS["checks"])
        return 0

    new_items = [i for i in items if not state.is_seen(int(i["item_id"]))]
    STATS["last_new"] = len(new_items)
    log.info(
        "Проверка #%d: лотов %d, новых %d, ответ %s",
        STATS["checks"], len(items), len(new_items), STATS["last_meta"] or "-",
    )
    return deliver_new(items, new_items, "lzt", cfg, state, tg, bot)


def check_tron(cfg: Config, state: State, tron: "TronClient", tg: Telegram, bot: "Bot | None" = None) -> int:
    items = tron.fetch_items()
    STATS["tron_items"] = len(items)
    STATS["tron_new"] = 0
    if not items:
        log.warning("tronaccs: лотов нет (ошибка или пустая страница)")
        return 0
    new_items = [i for i in items if not state.is_seen(int(i["item_id"]), "tron")]
    STATS["tron_new"] = len(new_items)
    log.info("tronaccs: лотов %d, новых %d", len(items), len(new_items))
    # На странице новые сверху, шлём в обратном порядке, чтобы самый свежий был последним
    return deliver_new(items, list(reversed(new_items)), "tron", cfg, state, tg, bot)


def deliver_new(items: list[dict], new_items: list[dict], name: str, cfg: Config, state: State,
                tg: Telegram, bot: "Bot | None") -> int:
    """Общая часть: первый запуск молчит, дальше шлёт новые лоты подписчикам."""
    if not state.is_initialized(name):
        state.set_initialized(name, True)
        if not cfg.notify_on_first_run:
            for item in items:
                state.mark_seen(int(item["item_id"]), name)
            state.save()
            log.info("%s: первый запуск, запомнил %d текущих лотов, уведомлять буду о новых", name, len(items))
            return 0

    if name == "lzt":  # самые старые сначала, чтобы сообщения шли в хронологическом порядке
        new_items.sort(key=lambda i: int(i.get("published_date") or 0))

    with state.lock:
        subscribers = list(state.subscribers)
    if not subscribers:
        if new_items:
            log.warning("Никто не подписан: напишите боту /start. Пропускаю %d лотов", len(new_items))
            for item in new_items:
                state.mark_seen(int(item["item_id"]), name)
            state.save()
        return 0

    sent = 0
    for item in new_items:
        item_id = int(item["item_id"])
        keyboard = bot.item_keyboard(item) if bot else None
        if tg.send_all(subscribers, format_item(item), keyboard):
            sent += 1
            log.info("%s: отправлен лот %d (%s)", name, item_id, item.get("price"))
        else:
            log.error("%s: не удалось отправить лот %d, попробую в следующий раз", name, item_id)
            continue
        state.mark_seen(item_id, name)
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

    tg = Telegram(cfg.tg_bot_token)
    state = State(cfg.state_file)
    cfg.apply_state(state)
    if cfg.tg_chat_id:
        state.subscribe(int(cfg.tg_chat_id))

    if "--test" in argv:
        with state.lock:
            subscribers = list(state.subscribers)
        if not subscribers:
            print("Подписчиков нет: напишите боту /start (запустите monitor.py) или задайте TG_CHAT_ID")
            return 1
        ok = tg.send_all(subscribers, "✅ Монитор lzt.market подключён.\nСлежу за: " + html.escape(cfg.site_url()))
        print(f"Тестовое сообщение отправлено в {ok} из {len(subscribers)} чатов")
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

    # Бот принимает /start и /stop в отдельном потоке, монитор крутится в основном
    tron = TronClient(cfg.tron_url, cfg.cookies_tron, cfg.use_browser) if cfg.tron_url else None
    bot = Bot(cfg, state, tg, lzt, tron)
    threading.Thread(target=bot.run_forever, name="telegram-bot", daemon=True).start()

    log.info(
        "Слежу за %s каждые %d с (источник: %s). Подписчиков: %d",
        cfg.site_url(), cfg.poll_interval, cfg.source, len(state.subscribers),
    )
    if not state.subscribers:
        log.info("Напишите боту /start в Telegram, чтобы получать уведомления")
    elif os.getenv("NOTIFY_ON_STARTUP", "1") == "1":
        tg.send_all(
            list(state.subscribers),
            "🟢 Монитор запущен. Слежу за:\n" + html.escape(cfg.site_url())
            + "\n\nПришлю сообщение, как только появится новый лот. Проверить сейчас: /check",
        )
    once = "--once" in argv
    while True:
        try:
            check_once(cfg, state, lzt, tg, bot)
            if bot.tron is not None:
                check_tron(cfg, state, bot.tron, tg, bot)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - монитор не должен падать из-за одной ошибки
            log.exception("Ошибка при проверке")
            _record_error(f"{type(exc).__name__}: {exc}")
        if once:
            return 0
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
