#!/usr/bin/env python3
"""
Мониторинг новых лотов на lzt.market и tronaccs.market с уведомлениями в Telegram.

Многопользовательский: у каждого пользователя свои токены API, свой фильтр,
свои настройки и свой список уже отправленных лотов. Доступ выдаётся владельцем
одноразовым кодом: владелец делает /invite <код>, новый пользователь пишет боту
/pin <код> и вводит свои токены прямо в чате.

Настройка через переменные окружения (см. .env.example):
    TG_BOT_TOKEN     - токен Telegram-бота от @BotFather
    TG_USER_ID       - Telegram ID владельца (можно несколько через запятую)
    LZT_TOKEN        - токен API Lolzteam владельца (https://lolz.live/account/api)
    TRON_TOKEN       - токен API tronaccs владельца (ник -> "API TronAccs")
    LZT_QUERY        - фильтр lzt по умолчанию, "country[]=UZ&min_contacts=100&spam=no"
    TRON_FILTER      - фильтр tronaccs по умолчанию, "country=UZ contacts>=100 spam=no"
    POLL_INTERVAL    - период опроса в секундах, по умолчанию 60
    STATE_FILE       - путь к файлу состояния, по умолчанию state.json
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import requests

from tron_source import TronApiClient, TronError, build_params, match_filter, parse_filter
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
DEFAULT_TRON_FILTER = "country=UZ contacts>=100 spam=no"
MAX_SEEN_IDS = 5000
TELEGRAM_MESSAGE_LIMIT = 4096
MSK = timezone(timedelta(hours=3))


def _ago(ts) -> str:
    if not ts:
        return "ещё не было"
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta} с назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    return f"{delta // 3600} ч {delta % 3600 // 60} мин назад"


def _mask(token: str | None) -> str:
    if not token:
        return "не задан"
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "задан"


class Config:
    def __init__(self) -> None:
        self.tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
        self.owner_ids = {
            int(x) for x in os.getenv("TG_USER_ID", "").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()
        }
        self.tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
        # Значения по умолчанию для профиля владельца
        self.lzt_token = os.getenv("LZT_TOKEN", "").strip()
        self.source = os.getenv("SOURCE", "api" if self.lzt_token else "web").strip().lower()
        self.cookies = os.getenv("LZT_COOKIES", "").strip() or None
        self.use_browser = os.getenv("USE_BROWSER", "0") == "1"
        self.category = os.getenv("LZT_CATEGORY", "telegram").strip().strip("/")
        self.query = os.getenv("LZT_QUERY", DEFAULT_QUERY).strip().lstrip("?")
        self.tron_token = os.getenv("TRON_TOKEN", "").strip()
        self.tron_filter = os.getenv("TRON_FILTER", DEFAULT_TRON_FILTER).strip()
        self.tron_pages = max(1, int(os.getenv("TRON_PAGES", "2")))
        self.tron_category = os.getenv("TRON_CATEGORY", "telegram").strip() or "telegram"
        # Общее
        self.poll_interval = max(5, int(os.getenv("POLL_INTERVAL", "60")))
        self.notify_on_first_run = os.getenv("NOTIFY_ON_FIRST_RUN", "0") == "1"
        self.notify_on_startup = os.getenv("NOTIFY_ON_STARTUP", "1") == "1"
        self.buy_confirm = os.getenv("BUY_CONFIRM", "0") == "1"
        self.send_session_files = os.getenv("SEND_SESSION_FILES", "1") == "1"
        self.state_file = Path(os.getenv("STATE_FILE", "state.json"))

    def validate(self) -> None:
        if self.source not in ("api", "web"):
            raise SystemExit("SOURCE должен быть 'web' или 'api'")
        if not self.tg_bot_token:
            raise SystemExit("Не задан TG_BOT_TOKEN. Скопируйте .env.example в .env и заполните.")


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
    return category, parsed.query.strip()


def lzt_params(query: str) -> list[tuple[str, str]]:
    """Фильтры из ссылки + принудительная сортировка по дате публикации (новые сверху)."""
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in ("order_by", "page")]
    params.append(("order_by", "pdate_to_down"))
    return params


def lzt_site_url(profile: dict) -> str:
    return f"{MARKET_URL}/{profile.get('category') or 'telegram'}/?{profile.get('query') or ''}"


# ---------- профили и состояние ----------

def new_profile(user_id: int, name: str, chat_id: int | None, cfg: Config, with_env_tokens: bool = False) -> dict:
    return {
        "user_id": user_id,
        "name": name,
        "chats": [chat_id] if chat_id else [],
        "created_at": int(time.time()),
        "lzt_token": cfg.lzt_token if with_env_tokens else "",
        "source": cfg.source if with_env_tokens else "api",
        "category": cfg.category,
        "query": cfg.query,
        "tron_token": cfg.tron_token if with_env_tokens else "",
        "tron_enabled": True,
        "tron_filter": cfg.tron_filter,
        "settings": {},
        "seen": {"lzt": [], "tron": []},
        "init": {"lzt": False, "tron": False},
    }


class State:
    """Состояние на диске: профили пользователей, коды приглашений, смещение обновлений бота."""

    def __init__(self, path: Path, cfg: Config) -> None:
        self.path = path
        self.cfg = cfg
        self.lock = threading.RLock()
        self.users: dict[str, dict] = {}
        self.invites: list[str] = []
        self.tg_offset = 0
        self.country_ids: dict[str, int] = {}
        self.owner_id: int | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("Не удалось прочитать %s: %s. Начинаю с пустого состояния.", self.path, exc)
            return
        self.tg_offset = int(data.get("tg_offset", 0))
        self.country_ids = {str(k).upper(): int(v) for k, v in (data.get("country_ids") or {}).items()}
        self.invites = [str(x) for x in data.get("invites", [])]
        self.owner_id = data.get("owner_id")
        self.users = {str(k): v for k, v in (data.get("users") or {}).items()}
        if "subscribers" in data and not self.users:
            self._migrate_legacy(data)

    def _migrate_legacy(self, data: dict) -> None:
        """Старый однопользовательский state.json -> профиль владельца."""
        owner = next(iter(self.cfg.owner_ids), None)
        if owner is None and data.get("subscribers"):
            owner = int(data["subscribers"][0])
        if owner is None:
            return
        p = new_profile(owner, "owner", None, self.cfg, with_env_tokens=True)
        p["chats"] = [int(x) for x in data.get("subscribers", [])] or [owner]
        if data.get("category") and data.get("query") is not None:
            p["category"], p["query"] = data["category"], data["query"]
        if data.get("tron_filter") is not None:
            p["tron_filter"] = data["tron_filter"]
        if data.get("tron_enabled") is not None:
            p["tron_enabled"] = bool(data["tron_enabled"])
        p["settings"] = data.get("settings") or {}
        p["seen"] = {"lzt": [int(x) for x in data.get("seen_ids", [])],
                     "tron": [int(x) for x in ((data.get("extra") or {}).get("tron") or {}).get("seen", [])]}
        p["init"] = {"lzt": bool(data.get("initialized")),
                     "tron": bool(((data.get("extra") or {}).get("tron") or {}).get("initialized"))}
        self.users[str(owner)] = p
        self.owner_id = owner
        log.info("Перенёс старые настройки в профиль владельца %s", owner)

    def save(self) -> None:
        with self.lock:
            for p in self.users.values():
                for k in ("lzt", "tron"):
                    p["seen"][k] = p["seen"][k][-MAX_SEEN_IDS:]
            data = {
                "users": self.users,
                "invites": self.invites,
                "tg_offset": self.tg_offset,
                "country_ids": self.country_ids,
                "owner_id": self.owner_id,
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, user_id: int) -> dict | None:
        return self.users.get(str(user_id))

    def add_user(self, profile: dict) -> None:
        with self.lock:
            self.users[str(profile["user_id"])] = profile
            self.save()

    def remove_user(self, user_id: int) -> bool:
        with self.lock:
            if str(user_id) not in self.users:
                return False
            del self.users[str(user_id)]
            self.save()
            return True

    def reset_seen(self, profile: dict, name: str) -> None:
        """После смены фильтра лоты забываем, первая проверка пройдёт молча."""
        with self.lock:
            profile["seen"][name] = []
            profile["init"][name] = False
            self.save()


# ---------- клиенты сайтов ----------

class LztClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "lzt-market-monitor/2.0",
        })
        self.last_error: str | None = None
        self.last_meta: dict = {}

    def _err(self, text: str) -> None:
        self.last_error = text[:300]

    def check_token(self) -> tuple[bool, str]:
        """GET /me: валиден ли токен."""
        try:
            resp = self.session.get(f"{API_BASE}/me", timeout=30)
            data = resp.json() if resp.content else {}
        except requests.RequestException as exc:
            return False, f"Ошибка сети: {exc}"
        except ValueError:
            return False, f"Ответ не JSON (HTTP {resp.status_code})"
        if resp.status_code in (401, 403):
            return False, "Маркет не принял токен (401/403)"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
        user = data.get("user") if isinstance(data, dict) else None
        if isinstance(user, dict):
            return True, f"{user.get('username') or user.get('user_id')}, баланс {user.get('balance')} ₽"
        return True, "ок"

    def fetch_items(self, category: str, params: list[tuple[str, str]]) -> list[dict]:
        url = f"{API_BASE}/{category}"
        backoff = 5
        for attempt in range(1, 6):
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                log.warning("lzt: ошибка сети (попытка %d): %s", attempt, exc)
                self._err(f"сеть: {exc}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                self._err("lzt: превышен лимит запросов (429)")
                time.sleep(retry_after)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code in (401, 403):
                self._err(f"lzt {resp.status_code}: проверьте токен. {resp.text[:150]}")
                log.error("lzt вернул %d: %s", resp.status_code, resp.text[:300])
                return []
            if resp.status_code >= 500:
                self._err(f"lzt вернул {resp.status_code}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 400:
                self._err(f"lzt {resp.status_code}: {resp.text[:150]}")
                return []
            try:
                data = resp.json()
            except ValueError:
                self._err(f"lzt: ответ не JSON: {resp.text[:150]}")
                return []
            if isinstance(data, dict) and data.get("errors"):
                self._err(f"lzt: {data['errors']}")
                return []
            items = data.get("items", []) if isinstance(data, dict) else []
            if isinstance(data, dict):
                self.last_meta = {k: data.get(k) for k in ("totalItems", "perPage", "wasCached") if k in data}
            return [i for i in items if isinstance(i, dict) and "item_id" in i]
        self._err("lzt: не удалось получить список лотов после нескольких попыток")
        return []

    def fast_buy(self, item_id: int, price=None) -> tuple[bool, str]:
        """POST /{item_id}/fast-buy: проверка аккаунта и покупка одним запросом."""
        body = {"price": price} if price is not None else {}
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

    def account_data(self, item_id: int) -> dict | None:
        """GET /{item_id}: данные купленного аккаунта (loginData, tdata и т.п.)."""
        try:
            resp = self.session.get(f"{API_BASE}/{item_id}", timeout=60)
            data = resp.json()
        except (requests.RequestException, ValueError):
            return None
        if isinstance(data, dict):
            return data.get("item") if isinstance(data.get("item"), dict) else data
        return None

    def download(self, url: str) -> bytes | None:
        try:
            resp = self.session.get(url, timeout=120)
            return resp.content if resp.ok else None
        except requests.RequestException:
            return None


class WebClient:
    """lzt без API: разбор HTML-страницы каталога (см. web_source.py)."""

    def __init__(self, cookies: str | None, use_browser: bool) -> None:
        self.cookies = cookies
        self.use_browser = use_browser
        self.session = requests.Session()
        self.last_error: str | None = None
        self.last_meta: dict = {}

    def fetch_items(self, category: str, params: list[tuple[str, str]]) -> list[dict]:
        try:
            return fetch_items_web(category, urlencode(params), self.cookies, self.use_browser, self.session)
        except (WebSourceError, requests.RequestException) as exc:
            self.last_error = f"страница: {exc}"[:300]
            log.error("Не удалось загрузить страницу: %s", exc)
            return []


class TronClient:
    """tronaccs.market через API + фильтр на своей стороне."""

    def __init__(self, token: str, filter_text: str, country_ids: dict, category: str = "telegram", pages: int = 2) -> None:
        self.api = TronApiClient(token, category, pages)
        self.country_ids = country_ids
        self.last_total = 0
        self.last_error: str | None = None
        self.set_filter(filter_text)

    def set_filter(self, text: str) -> None:
        self.rules = parse_filter(text)  # ValueError, если не разобрали
        self.params, self.local_rules = build_params(self.rules)
        codes = [v.upper() for k, op, v in self.local_rules if k in ("country", "страна") and op == "="]
        if any(c not in self.country_ids for c in codes):
            try:
                fetched = self.api.countries()
            except Exception as exc:  # noqa: BLE001
                log.warning("tronaccs: справочник стран не загрузился: %s", exc)
                fetched = {}
            if fetched:
                self.country_ids.update(fetched)
        known = [str(self.country_ids[c]) for c in codes if c in self.country_ids]
        if known and "country" not in self.params:
            self.params["country"] = ",".join(known)

    def check_token(self) -> tuple[bool, str]:
        try:
            user = self.api.me()
        except (TronError, requests.RequestException) as exc:
            return False, str(exc)
        if not user:
            return False, "Пустой ответ /me"
        return True, f"{user.get('username')}, баланс {user.get('balance')} {str(user.get('currency') or '').upper()}"

    def fetch_items(self) -> list[dict]:
        try:
            items = self.api.fetch_items(self.params)
        except (TronError, requests.RequestException) as exc:
            self.last_error = str(exc)[:300]
            log.error("%s", exc)
            return []
        self.last_total = len(items)
        if self.local_rules:
            items = [i for i in items if match_filter(i, self.local_rules)]
        return items

    def fetch_raw_sample(self) -> tuple[dict | None, int]:
        data = self.api.fetch_raw_page(1, self.params)
        raw = data.get("items") if isinstance(data, dict) else data
        raw = raw if isinstance(raw, list) else []
        return (raw[0] if raw else (data if isinstance(data, dict) else None)), len(raw)

    def balance(self) -> str | None:
        try:
            user = self.api.me()
        except (TronError, requests.RequestException) as exc:
            return f"ошибка: {exc}"
        return f"{user.get('balance')} {str(user.get('currency') or '').upper()}".strip() if user else None

    def find_country_id(self, code: str, max_id: int = 300, progress=None) -> int | None:
        code = code.upper()
        for cid in range(1, max_id + 1):
            data = self.api.fetch_raw_page(1, {"country": str(cid)})
            raw = data.get("items") if isinstance(data, dict) else None
            if raw:
                codes = {str(r.get("telegram_counrty") or r.get("telegram_country") or "").upper() for r in raw if isinstance(r, dict)}
                if codes == {code}:
                    return cid
            if progress and cid % 50 == 0:
                progress(cid)
        return None

    def buy(self, item_id: int) -> tuple[bool, str]:
        return self.api.buy(item_id)

    def account_data(self, item_id: int) -> dict | None:
        return self.api.item_detail(item_id)

    def download(self, url: str) -> bytes | None:
        try:
            resp = self.api.session.get(url, timeout=120)
            return resp.content if resp.ok else None
        except requests.RequestException:
            return None


# ---------- Telegram ----------

class Telegram:
    def __init__(self, bot_token: str) -> None:
        self.base = f"{TG_API}/bot{bot_token}"
        self.session = requests.Session()

    def call(self, method: str, payload: dict) -> dict | None:
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
                time.sleep(int(retry_after))
                continue
            if resp.ok and data.get("ok"):
                return data.get("result") or {}
            if method != "deleteMessage":
                log.error("Telegram %s: ответ %d: %s", method, resp.status_code, resp.text[:300])
            return None
        return None

    def send(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> bool:
        payload = {"chat_id": chat_id, "text": text[:TELEGRAM_MESSAGE_LIMIT], "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload) is not None

    def send_all(self, chat_ids: list[int], text: str, reply_markup: dict | None = None) -> int:
        return sum(1 for c in list(chat_ids) if self.send(c, text, reply_markup))

    def send_document(self, chat_id: int | str, filename: str, content: bytes, caption: str = "") -> bool:
        for attempt in range(1, 4):
            try:
                resp = self.session.post(
                    f"{self.base}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption[:1024]},
                    files={"document": (filename, content)},
                    timeout=120,
                )
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram sendDocument: ошибка (попытка %d): %s", attempt, exc)
                time.sleep(3 * attempt)
                continue
            if resp.status_code == 429:
                time.sleep(int(data.get("parameters", {}).get("retry_after", 5)))
                continue
            if resp.ok and data.get("ok"):
                return True
            log.error("Telegram sendDocument: %d: %s", resp.status_code, resp.text[:300])
            return False
        return False

    def edit_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:TELEGRAM_MESSAGE_LIMIT],
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                             "reply_markup": reply_markup or {"inline_keyboard": []}})

    def delete(self, chat_id: int, message_id: int) -> None:
        self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert})

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        try:
            resp = self.session.get(f"{self.base}/getUpdates",
                                    params={"offset": offset, "timeout": timeout,
                                            "allowed_updates": '["message","callback_query"]'},
                                    timeout=timeout + 10)
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


# ---------- форматирование лота ----------

FIELD_LABELS: list[tuple[tuple[str, ...], str, str]] = [
    (("telegram_country",), "🌍 Страна", "text"),
    (("telegram_contacts", "telegram_contacts_count"), "👥 Контакты", "num"),
    (("telegram_spam_block",), "🚫 Спамблок", "spam"),
    (("telegram_premium",), "⭐ Premium", "bool"),
    (("telegram_premium_expires",), "⭐ Premium до", "date"),
    (("telegram_password",), "🔐 Облачный пароль (2FA)", "bool"),
    (("telegram_chats_count",), "💬 Чаты", "num"),
    (("telegram_channels_count",), "📣 Каналы", "num"),
    (("telegram_conversations_count",), "✉️ Диалоги", "num"),
    (("telegram_admin_groups_count", "telegram_admin_chats_count"), "👑 Админ в группах", "num"),
    (("telegram_admin_channels_count",), "👑 Админ в каналах", "num"),
    (("telegram_stars_count", "telegram_stars"), "✨ Stars", "num"),
    (("telegram_gifts_count", "telegram_gifts"), "🎁 Подарки", "num"),
    (("telegram_nft_gifts_count", "telegram_nft_gifts"), "🎁 NFT-подарки", "num"),
    (("telegram_raiting_level", "telegram_rating_level"), "🏅 Уровень рейтинга", "num"),
    (("telegram_raiting_stars", "telegram_rating_stars"), "🏅 Звёзды рейтинга", "num"),
    (("telegram_id_count", "telegram_id_length"), "🔢 Длина ID", "num"),
    (("telegram_dc_id",), "🖥 Дата-центр", "num"),
    (("telegram_reg_date", "telegram_register_date", "telegram_birthday"), "📅 Регистрация", "date"),
    (("telegram_last_seen",), "👀 Был онлайн", "date"),
    (("telegram_full_name",), "🙍 Имя в профиле", "text"),
]
ORIGIN_LABELS = {
    "autoreg": "авторег", "self_registration": "саморег", "brute": "брут", "stealer": "стилер",
    "personal": "личный", "resale": "перепродажа", "fishing": "фишинг", "retrive": "восстановленный",
    "dummy": "пустышка", "phishing": "фишинг", "farm": "ферма",
}


def _first(item: dict, *keys: str):
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


# ---------- файлы сессии купленного аккаунта ----------

# Ключи с данными для входа: подпись -> список подстрок в имени поля
SESSION_KEYS = {
    "tdata": ("tdata",),
    "telethon": ("telethon",),
    "pyrogram": ("pyrogram",),
    "session": ("session_string", "stringsession", "string_session", "session"),
    "json": ("telegram_json", "tg_json", "account_json"),
}
# Короткие поля в текстовую сводку
INFO_KEYS = {
    "phone": ("phone", "telegram_phone", "number"),
    "authkey": ("auth_key", "authkey"),
    "dc_id": ("dc_id", "dcid", "telegram_dc_id"),
    "user_id": ("telegram_id", "tg_id", "user_id"),
    "username": ("telegram_username", "username"),
    "2fa / пароль": ("twofa", "two_fa", "password", "cloud_password"),
    "first_name": ("first_name", "telegram_full_name"),
}
_B64_RE = re.compile(r"^[A-Za-z0-9+/=\r\n]+$")


def _maybe_binary(value: str) -> bytes | None:
    """Если строка — base64 файла, вернуть (байты, расширение). Иначе None."""
    s = value.strip()
    if len(s) < 100 or not _B64_RE.match(s):
        return None
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:  # noqa: BLE001
        return None
    if raw[:16].startswith(b"SQLite format 3"):  # готовый .session (Telethon/Pyrogram - SQLite)
        return raw, "session"
    if raw[:2] == b"PK" or raw[:4] == b"Rar!" or raw[:2] == b"\x1f\x8b":  # zip, rar, gzip
        return raw, "zip"
    return None


def stringsession_to_session(session_str: str) -> bytes | None:
    """Строковую сессию Telethon -> настоящий .session (SQLite). Нужен пакет telethon."""
    try:
        import tempfile
        from telethon.sessions import StringSession, SQLiteSession
    except Exception:  # noqa: BLE001 - telethon не установлен
        return None
    try:
        ss = StringSession(session_str)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s")
            sql = SQLiteSession(path)
            sql.set_dc(ss.dc_id, ss.server_address, ss.port)
            sql.auth_key = ss.auth_key
            sql.save()
            sql.close()
            with open(path + ".session", "rb") as f:
                return f.read()
    except Exception:  # noqa: BLE001
        return None


def _flatten(data, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out.update(_flatten(v, f"{prefix}{i}."))
    else:
        out[prefix.rstrip(".")] = data
    return out


def build_account_files(item_id: int, data, downloader=None) -> tuple[list[tuple[str, bytes]], str]:
    """Из данных купленного аккаунта собирает файлы (имя, байты) и текстовую сводку.

    Формат ответа маркетов заранее не известен, поэтому: полный ответ всегда
    сохраняется в JSON, известные поля сессии выкладываются отдельными файлами,
    ссылки на файлы (tdata.zip и т.п.) скачиваются, короткие поля идут в сводку.
    """
    files: list[tuple[str, bytes]] = []
    used_names: set[str] = set()

    def add(name: str, content: bytes) -> None:
        base, dot, ext = name.rpartition(".")
        stem = base or name
        final = name
        n = 2
        while final in used_names:
            final = f"{stem}_{n}{dot}{ext}"
            n += 1
        used_names.add(final)
        files.append((final, content))

    flat = _flatten(data)
    info_lines: list[str] = []
    got_session = False
    for key, value in flat.items():
        low = key.lower()
        short = key.split(".")[-1]
        # Короткие информационные поля
        for label, subs in INFO_KEYS.items():
            if any(low == s or low.endswith("." + s) for s in subs) and value not in (None, "", 0):
                info_lines.append(f"{label}: {value}")
                break
        if not isinstance(value, str) or len(value) < 20:
            continue
        # Ссылка на файл — скачиваем как есть (.session, .zip, .json)
        if value.startswith(("http://", "https://")) and downloader is not None:
            low_url = value.lower().split("?")[0]
            if any(low_url.endswith(e) for e in (".session", ".zip", ".rar", ".7z", ".json")) or "download" in low_url:
                blob = downloader(value)
                if blob:
                    ext = low_url.rsplit(".", 1)[-1] if "." in low_url else "bin"
                    add(f"{item_id}.{ext}" if ext == "session" else f"{item_id}_{short}.{ext}", blob)
                    got_session = got_session or ext == "session"
                continue
        # base64 файла: .session (SQLite) или архив (tdata)
        binary = _maybe_binary(value)
        if binary is not None:
            raw, ext = binary
            if ext == "session":
                add(f"{item_id}.session", raw)
                got_session = True
            else:
                add(f"{item_id}_tdata.zip" if "tdata" in low else f"{item_id}_{short}.zip", raw)
            continue
        # Строковая сессия -> пробуем собрать настоящий .session
        if any(s in low for s in ("telethon", "session_string", "stringsession", "string_session")) or low.endswith("session"):
            real = stringsession_to_session(value)
            if real:
                add(f"{item_id}.session", real)
                got_session = True
            else:
                add(f"{item_id}_telethon_string.txt", value.encode("utf-8"))  # строка, не файл
            continue
        # Прочие известные форматы
        for label, subs in SESSION_KEYS.items():
            if any(s in low for s in subs):
                ext = "json" if label == "json" else "txt"
                add(f"{item_id}_{label}.{ext}", value.encode("utf-8"))
                break

    # Полный ответ прикладываем как запасной, если готового .session не нашлось
    if not got_session:
        add(f"{item_id}.json", json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    summary = "\n".join(dict.fromkeys(info_lines))  # без дублей, порядок сохранён
    return files, summary


def _fmt_date(ts) -> str:
    try:
        ts = int(ts)
        if ts > 10**11:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=MSK).strftime("%d.%m.%Y %H:%M МСК")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _yes_no(value) -> str:
    if value in (True, 1, "1", "yes", "true"):
        return "да ✅"
    if value in (False, 0, "0", "no", "false"):
        return "нет ❌"
    return "неизвестно ❔"


def _spam(value) -> str:
    """lzt: -1 нет спамблока, 1 есть, 0 не проверялся; tronaccs: true/false."""
    if isinstance(value, bool):
        return "есть ⛔" if value else "нет ✅"
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("yes", "true"):
            return "есть ⛔"
        if s in ("no", "false"):
            return "нет ✅"
        try:
            value = int(s)
        except ValueError:
            return f"{html.escape(value)} ❔"
    if isinstance(value, (int, float)):
        n = int(value)
        if n == 1:
            return "есть ⛔"
        if n == -1:
            return "нет ✅"
        if n == 0:
            return "не проверялся ❔"
        if n > 10**8:
            return f"до {_fmt_date(n)} ⛔"
    return f"{html.escape(str(value))} ❔"


def _fmt_value(value, kind: str) -> str | None:
    if kind == "bool":
        return _yes_no(value)
    if kind == "spam":
        return _spam(value)
    if kind == "date":
        return _fmt_date(value)
    if kind == "num" and isinstance(value, bool):
        return _yes_no(value)
    return html.escape(str(value))


def format_item(item: dict) -> str:
    item_id = item["item_id"]
    title = html.escape(str(_first(item, "title", "title_en") or f"Лот #{item_id}"))
    price = _first(item, "price", "rub_price")
    currency = str(_first(item, "price_currency", "currency") or "rub").lower()
    currency = {"rub": "₽", "usd": "$", "eur": "€"}.get(currency, currency.upper())
    price_str = f"{price} {currency}" if price is not None else "?"
    site = "tronaccs" if item.get("source") == "tron" else "lzt.market"
    lines = [f"🆕 <b>{title}</b>", f"🏪 {site}", f"💰 Цена: <b>{html.escape(price_str)}</b>"]
    for keys, label, kind in FIELD_LABELS:
        value = _first(item, *keys)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = _fmt_value(value, kind)
        if text and text != "?":
            lines.append(f"{label}: {text}")
    details = _first(item, "web_details")
    if details:
        lines.append(f"ℹ️ {html.escape(str(details))}")
    origin = _first(item, "item_origin")
    if origin:
        lines.append(f"📦 Происхождение: {html.escape(ORIGIN_LABELS.get(str(origin).lower(), str(origin)))}")
    seller = _first(item, "seller_username")
    if seller:
        lines.append(f"👤 Продавец: {html.escape(str(seller))}")
    published = _first(item, "published_date")
    if published:
        lines.append(f"🕒 Опубликован: {_fmt_date(published)}")
    lines.append(f"🔗 {item.get('url') or f'{MARKET_URL}/{item_id}'}")
    return "\n".join(lines)


# ---------- рабочее состояние пользователя ----------

class UserRuntime:
    """Клиенты и статистика одного пользователя (не сохраняются на диск)."""

    def __init__(self, cfg: Config, state: State, profile: dict) -> None:
        self.cfg = cfg
        self.state = state
        self.profile = profile
        self.lzt: LztClient | WebClient | None = None
        self.tron: TronClient | None = None
        self.stats: dict = {"checks": 0, "last_check_at": None, "last_items": None, "last_new": 0,
                            "tron_total": None, "tron_items": None, "tron_new": 0,
                            "last_error": None, "last_error_at": None}
        self.rebuild()

    def rebuild(self) -> None:
        p = self.profile
        if p.get("source") == "web":
            self.lzt = WebClient(self.cfg.cookies, self.cfg.use_browser)
        elif p.get("lzt_token"):
            self.lzt = LztClient(p["lzt_token"])
        else:
            self.lzt = None
        self.tron = None
        if p.get("tron_enabled", True) and p.get("tron_token"):
            try:
                self.tron = TronClient(p["tron_token"], p.get("tron_filter") or "", self.state.country_ids,
                                       self.cfg.tron_category, self.cfg.tron_pages)
            except ValueError as exc:
                log.error("tronaccs: фильтр пользователя %s не разобран: %s", p["user_id"], exc)

    def record_error(self, text: str | None) -> None:
        if text:
            self.stats["last_error"] = text[:300]
            self.stats["last_error_at"] = time.time()

    @property
    def chats(self) -> list[int]:
        return list(self.profile.get("chats") or [])


# ---------- бот ----------

class Bot:
    HELP = (
        "Команды:\n"
        "/settings — настроить фильтр кнопками: страна, контакты, спамблок\n"
        "/check — проверить прямо сейчас и показать последние лоты\n"
        "/status — что мониторится, балансы, последняя проверка\n"
        "/token — показать или сменить токены API\n"
        "/filter <ссылка> — фильтр lzt.market ссылкой с сайта\n"
        "/tron on|off, /tronfilter … — tronaccs вручную\n"
        "/tronid UZ — узнать ID страны на tronaccs\n"
        "/lztdump, /trondump — лот в сыром виде, как отдаёт сайт\n"
        "/stop — отписать этот чат, /start — подписать снова\n"
        "/id — ваш Telegram ID"
    )
    OWNER_HELP = (
        "\n\nКоманды владельца:\n"
        "/invite [код] — создать одноразовый код доступа\n"
        "/users — список пользователей\n"
        "/kick <id> — удалить пользователя"
    )
    COUNTRIES = ["UZ", "RU", "KZ", "UA", "BY", "KG", "TJ", "US", "IN", "ID"]
    CONTACTS = [0, 50, 100, 200, 300, 500, 1000]
    PRICES = [100, 200, 300, 500, 1000, 2000]

    def __init__(self, cfg: Config, state: State, tg: Telegram) -> None:
        self.cfg = cfg
        self.state = state
        self.tg = tg
        self.runtimes: dict[int, UserRuntime] = {}
        self.awaiting: dict[int, tuple[int, str]] = {}  # chat_id -> (user_id, что ждём текстом)
        for p in list(state.users.values()):
            self.runtime(p["user_id"])

    # --- доступ ---

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.cfg.owner_ids or user_id == self.state.owner_id

    def profile(self, user_id: int) -> dict | None:
        return self.state.get(user_id)

    def runtime(self, user_id: int) -> UserRuntime | None:
        p = self.profile(user_id)
        if p is None:
            self.runtimes.pop(user_id, None)
            return None
        rt = self.runtimes.get(user_id)
        if rt is None or rt.profile is not p:
            rt = UserRuntime(self.cfg, self.state, p)
            self.runtimes[user_id] = rt
        return rt

    def ensure_owner_profile(self) -> None:
        """Профиль владельца из .env, если его ещё нет."""
        owner = next(iter(self.cfg.owner_ids), None) or self.state.owner_id
        if owner is None:
            return
        if self.state.owner_id is None:
            self.state.owner_id = owner
        if self.profile(owner) is None:
            chat = int(self.cfg.tg_chat_id) if self.cfg.tg_chat_id.lstrip("-").isdigit() else owner
            p = new_profile(owner, "owner", chat, self.cfg, with_env_tokens=True)
            self.state.add_user(p)
            self.runtime(owner)
            log.info("Создан профиль владельца %s из .env", owner)

    # --- меню /settings ---

    def current_settings(self, p: dict) -> dict:
        s = p.get("settings") or {}
        if not s:
            q = dict(parse_qsl(p.get("query") or "", keep_blank_values=True))
            country = (q.get("country[]") or "").upper() or "any"
            try:
                contacts = int(q.get("min_contacts") or 0)
            except ValueError:
                contacts = 0
            spam = q.get("spam") if q.get("spam") in ("yes", "no") else "any"
            s = {"country": country, "contacts": contacts, "spam": spam, "lzt": True, "tron": True}
            p["settings"] = s
        return s

    @staticmethod
    def _price_text(s: dict) -> str:
        lo, hi = s.get("price_min"), s.get("price_max")
        if lo is None and hi is None:
            return "любая"
        if lo is not None and hi is not None:
            return f"от {lo} до {hi} ₽"
        return f"от {lo} ₽" if lo is not None else f"до {hi} ₽"

    @classmethod
    def _label(cls, s: dict) -> str:
        country = "любая" if s["country"] == "any" else s["country"]
        spam = {"no": "нет", "yes": "есть", "any": "любой"}[s["spam"]]
        return f"Страна: {country}\nКонтактов от: {s['contacts']}\nСпамблок: {spam}\nЦена: {cls._price_text(s)}"

    def settings_keyboard(self, p: dict, view: str = "main") -> dict:
        s = self.current_settings(p)
        if view == "country":
            rows, row = [], []
            for c in self.COUNTRIES:
                row.append({"text": ("✅ " if s["country"] == c else "") + c, "callback_data": f"set:country:{c}"})
                if len(row) == 5:
                    rows.append(row); row = []
            if row:
                rows.append(row)
            rows.append([{"text": ("✅ " if s["country"] == "any" else "") + "Любая", "callback_data": "set:country:any"},
                         {"text": "✏️ Другая", "callback_data": "set:country:ask"}])
            rows.append([{"text": "← Назад", "callback_data": "set:menu:0"}])
            return {"inline_keyboard": rows}
        if view == "contacts":
            row = [{"text": ("✅ " if s["contacts"] == n else "") + str(n), "callback_data": f"set:contacts:{n}"} for n in self.CONTACTS]
            return {"inline_keyboard": [row[:4], row[4:], [{"text": "✏️ Другое число", "callback_data": "set:contacts:ask"},
                                                             {"text": "← Назад", "callback_data": "set:menu:0"}]]}
        if view == "spam":
            return {"inline_keyboard": [[
                {"text": ("✅ " if s["spam"] == "no" else "") + "Без спамблока", "callback_data": "set:spam:no"},
                {"text": ("✅ " if s["spam"] == "yes" else "") + "Со спамблоком", "callback_data": "set:spam:yes"},
                {"text": ("✅ " if s["spam"] == "any" else "") + "Любой", "callback_data": "set:spam:any"},
            ], [{"text": "← Назад", "callback_data": "set:menu:0"}]]}
        if view == "price":
            lo, hi = s.get("price_min"), s.get("price_max")
            row = [{"text": ("✅ " if hi == n else "") + f"до {n}", "callback_data": f"set:price_max:{n}"} for n in self.PRICES]
            return {"inline_keyboard": [
                row[:3], row[3:],
                [{"text": f"✏️ От… ({lo if lo is not None else 'нет'})", "callback_data": "set:price_min:ask"},
                 {"text": f"✏️ До… ({hi if hi is not None else 'нет'})", "callback_data": "set:price_max:ask"}],
                [{"text": ("✅ " if lo is None and hi is None else "") + "Любая цена", "callback_data": "set:price_clear:0"},
                 {"text": "← Назад", "callback_data": "set:menu:0"}],
            ]}
        country = "любая" if s["country"] == "any" else s["country"]
        spam = {"no": "нет", "yes": "есть", "any": "любой"}[s["spam"]]
        return {"inline_keyboard": [
            [{"text": f"🌍 Страна: {country}", "callback_data": "set:view:country"}],
            [{"text": f"👥 Контактов от: {s['contacts']}", "callback_data": "set:view:contacts"}],
            [{"text": f"🚫 Спамблок: {spam}", "callback_data": "set:view:spam"}],
            [{"text": f"💰 Цена: {self._price_text(s)}", "callback_data": "set:view:price"}],
            [{"text": ("✅" if s.get("lzt", True) else "☐") + " lzt.market", "callback_data": "set:site:lzt"},
             {"text": ("✅" if s.get("tron", True) else "☐") + " tronaccs", "callback_data": "set:site:tron"}],
            [{"text": "💾 Применить", "callback_data": "set:apply:0"}],
        ]}

    def settings_text(self, p: dict, view: str = "main") -> str:
        s = self.current_settings(p)
        hints = {"main": "\n\nНажмите на строку, чтобы изменить. Потом «Применить».",
                 "country": "\n\nВыберите страну аккаунта.", "contacts": "\n\nМинимальное число контактов.",
                 "spam": "\n\nСпамблок на аккаунте.",
                 "price": "\n\nЦена в рублях. На tronaccs считается с комиссией."}
        return "⚙️ <b>Настройки фильтра</b>\n" + html.escape(self._label(s)) + hints.get(view, "")

    def show_settings(self, p: dict, chat_id: int, message_id: int | None = None, view: str = "main") -> None:
        if message_id:
            self.tg.edit_text(chat_id, message_id, self.settings_text(p, view), self.settings_keyboard(p, view))
        else:
            self.tg.send(chat_id, self.settings_text(p, view), self.settings_keyboard(p, view))

    def apply_settings(self, p: dict, chat_id: int) -> None:
        s = self.current_settings(p)
        rt = self.runtime(p["user_id"])
        applied = []
        if s.get("lzt", True):
            params = []
            if s["country"] != "any":
                params.append(("country[]", s["country"]))
            if s["contacts"] > 0:
                params.append(("min_contacts", str(s["contacts"])))
            if s["spam"] != "any":
                params.append(("spam", s["spam"]))
            if s.get("price_min") is not None:
                params.append(("pmin", str(s["price_min"])))
            if s.get("price_max") is not None:
                params.append(("pmax", str(s["price_max"])))
            p["category"] = "telegram"
            p["query"] = "&".join(f"{k}={v}" for k, v in params)
            self.state.reset_seen(p, "lzt")
            applied.append("lzt.market: " + html.escape(lzt_site_url(p)))
        if s.get("tron", True):
            parts = []
            if s["country"] != "any":
                parts.append(f"country={s['country']}")
            if s["contacts"] > 0:
                parts.append(f"contacts>={s['contacts']}")
            if s["spam"] != "any":
                parts.append(f"spam={s['spam']}")
            if s.get("price_min") is not None:
                parts.append(f"price>={s['price_min']}")
            if s.get("price_max") is not None:
                parts.append(f"price<={s['price_max']}")
            p["tron_filter"] = " ".join(parts)
            self.state.reset_seen(p, "tron")
            applied.append("tronaccs: " + (html.escape(p["tron_filter"]) or "без фильтра")
                           + ("" if p.get("tron_token") else " (нет токена tronaccs, см. /token)"))
        self.state.save()
        if rt:
            rt.rebuild()
        log.info("Пользователь %s применил настройки: %s", p["user_id"], s)
        self.tg.send(chat_id, "✅ Применил.\n" + html.escape(self._label(s)) + "\n\n" + "\n".join(applied)
                     + "\n\nТекущие лоты запомню молча, дальше буду присылать только новые. Проверить: /check")

    def handle_settings_callback(self, cq: dict, parts: list[str]) -> None:
        cq_id = cq.get("id", "")
        user_id = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        p = self.profile(user_id)
        if p is None:
            self.tg.answer_callback(cq_id, "⛔ Нет доступа. Введите код: /pin <код>", alert=True)
            return
        _, kind, value = (parts + ["", ""])[:3]
        s = self.current_settings(p)
        if kind == "view":
            self.show_settings(p, chat_id, message_id, value)
        elif kind == "menu":
            self.awaiting.pop(chat_id, None)
            self.show_settings(p, chat_id, message_id, "main")
        elif kind == "country":
            if value == "ask":
                self.awaiting[chat_id] = (user_id, "country")
                self.tg.answer_callback(cq_id)
                self.tg.send(chat_id, "Пришлите код страны двумя буквами, например <code>UZ</code>. Отмена: /settings")
                return
            s["country"] = value.upper()
            self.state.save()
            self.show_settings(p, chat_id, message_id, "main")
        elif kind == "contacts":
            if value == "ask":
                self.awaiting[chat_id] = (user_id, "contacts")
                self.tg.answer_callback(cq_id)
                self.tg.send(chat_id, "Пришлите минимальное число контактов, например <code>150</code>. Отмена: /settings")
                return
            s["contacts"] = int(value)
            self.state.save()
            self.show_settings(p, chat_id, message_id, "main")
        elif kind == "spam":
            s["spam"] = value
            self.state.save()
            self.show_settings(p, chat_id, message_id, "main")
        elif kind in ("price_min", "price_max"):
            if value == "ask":
                self.awaiting[chat_id] = (user_id, kind)
                self.tg.answer_callback(cq_id)
                self.tg.send(chat_id, ("Пришлите минимальную цену в рублях, например <code>100</code>."
                                       if kind == "price_min" else
                                       "Пришлите максимальную цену в рублях, например <code>500</code>.")
                             + "\n0 — убрать это ограничение. Отмена: /settings")
                return
            s[kind] = int(value)
            self.state.save()
            self.show_settings(p, chat_id, message_id, "price")
        elif kind == "price_clear":
            s["price_min"] = None
            s["price_max"] = None
            self.state.save()
            self.show_settings(p, chat_id, message_id, "main")
        elif kind == "site":
            s[value] = not s.get(value, True)
            self.state.save()
            self.show_settings(p, chat_id, message_id, "main")
        elif kind == "apply":
            self.tg.edit_markup(chat_id, message_id, None)
            self.apply_settings(p, chat_id)
        self.tg.answer_callback(cq_id)

    # --- кнопки под лотом ---

    @staticmethod
    def _price_label(price, currency) -> str:
        cur = {"rub": "₽", "usd": "$", "eur": "€"}.get(str(currency or "rub").lower(), str(currency or ""))
        return f"{price} {cur}".strip()

    def item_keyboard(self, rt: UserRuntime | None, item: dict, stage: str = "buy") -> dict:
        item_id = int(item["item_id"])
        price = item.get("price")
        rows = []
        is_tron = item.get("source") == "tron"
        if is_tron:
            can_buy = rt is not None and rt.tron is not None
            p = "t"
        else:
            can_buy = rt is not None and isinstance(rt.lzt, LztClient) and price is not None
            p = ""
        price_s = price if price is not None else 0
        label = self._price_label(price, item.get("price_currency"))
        if can_buy and stage == "buy":
            rows.append([{"text": f"🛒 Купить за {label}", "callback_data": f"{p}buy:{item_id}:{price_s}"}])
        elif can_buy and stage == "confirm":
            rows.append([{"text": f"✅ Подтвердить за {label}", "callback_data": f"{p}confirm:{item_id}:{price_s}"},
                         {"text": "❌ Отмена", "callback_data": f"{p}cancel:{item_id}:{price_s}"}])
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
        if parts[0] == "set" and chat_id is not None:
            self.handle_settings_callback(cq, parts)
            return
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
        is_tron = action.startswith("t")
        if is_tron:
            action = action[1:]
        item = {"item_id": item_id, "price": price}
        if is_tron:
            item["source"] = "tron"
            item["url"] = f"https://tronaccs.market/{self.cfg.tron_category}/{item_id}"
        item_url = item.get("url") or f"{MARKET_URL}/{item_id}/"

        rt = self.runtime(user_id)
        if rt is None:
            self.tg.answer_callback(cq_id, "⛔ Нет доступа. Введите код: /pin <код>", alert=True)
            return
        if is_tron and rt.tron is None:
            self.tg.answer_callback(cq_id, "tronaccs выключен или нет токена (/token tron)", alert=True)
            return
        if not is_tron and not isinstance(rt.lzt, LztClient):
            self.tg.answer_callback(cq_id, "Для покупки нужен токен lzt (/token lzt)", alert=True)
            return

        if action == "buy" and self.cfg.buy_confirm:
            self.tg.edit_markup(chat_id, message_id, self.item_keyboard(rt, item, "confirm"))
            self.tg.answer_callback(cq_id, "Подтвердите покупку")
        elif action == "cancel":
            self.tg.edit_markup(chat_id, message_id, self.item_keyboard(rt, item, "buy"))
            self.tg.answer_callback(cq_id, "Отменено")
        elif action in ("buy", "confirm"):
            self.tg.answer_callback(cq_id, "Покупаю…")
            self.tg.edit_markup(chat_id, message_id, self.item_keyboard(rt, item, "done"))
            client = rt.tron if is_tron else rt.lzt
            ok, text = client.buy(item_id) if is_tron else client.fast_buy(item_id, price)
            site = "tronaccs" if is_tron else "lzt.market"
            log.info("%s: покупка лота %d за %s пользователем %s: %s — %s", site, item_id, price, user_id, ok, text)
            if ok:
                self.tg.send(chat_id, f"✅ {site}: куплен лот {item_id} за {self._price_label(price, None)}.\n"
                                      f"{html.escape(text)}\n{item_url}")
                self.send_account_files(rt, chat_id, item_id, site)
            else:
                self.tg.send(chat_id, f"❌ {site}: не удалось купить лот {item_id}: {html.escape(text)}\n{item_url}",
                             self.item_keyboard(rt, item, "buy"))
        else:
            self.tg.answer_callback(cq_id)

    def send_account_files(self, rt: UserRuntime, chat_id: int, item_id: int, site: str) -> None:
        """После покупки выгружает данные аккаунта (сессия, tdata и т.п.) файлами."""
        if not self.cfg.send_session_files:
            return
        client = rt.tron if site == "tronaccs" else rt.lzt
        if client is None or not hasattr(client, "account_data"):
            return
        data = client.account_data(item_id)
        if not data:
            self.tg.send(chat_id, "📎 Не удалось автоматически выгрузить данные аккаунта. "
                                  "Заберите их на странице лота или в «Мои покупки».")
            return
        try:
            files, summary = build_account_files(item_id, data, getattr(client, "download", None))
        except Exception:  # noqa: BLE001
            log.exception("Не удалось собрать файлы аккаунта %s", item_id)
            files, summary = [], ""
        if summary:
            self.tg.send(chat_id, f"🔑 Данные аккаунта {item_id}:\n<pre>{html.escape(summary)}</pre>")
        sent = 0
        for name, content in files:
            if len(content) > 49 * 1024 * 1024:  # лимит Telegram на документ
                continue
            if self.tg.send_document(chat_id, name, content, caption=f"{site}: аккаунт {item_id}"):
                sent += 1
        if sent:
            log.info("%s: выгружено %d файлов аккаунта %d пользователю %s", site, sent, item_id, rt.profile["user_id"])
        else:
            self.tg.send(chat_id, "📎 Файлы аккаунта отправить не удалось, данные во вложенном JSON или на странице лота.")

    # --- регистрация и токены ---

    def start_registration(self, user: dict, chat_id: int) -> None:
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username") or str(user["id"])
        p = new_profile(int(user["id"]), name, chat_id, self.cfg)
        self.state.add_user(p)
        self.runtime(p["user_id"])
        self.awaiting[chat_id] = (p["user_id"], "lzt_token")
        self.tg.send(chat_id, "✅ Код принят, доступ открыт.\n\n"
                              "Шаг 1 из 2. Пришлите ваш токен API <b>lzt.market</b>.\n"
                              "Где взять: https://lolz.live/account/api → создать токен с областью market.\n"
                              "Если lzt не нужен, напишите /skip")

    def receive_token(self, p: dict, chat_id: int, kind: str, text: str, message_id: int | None) -> None:
        token = text.strip()
        if message_id:
            self.tg.delete(chat_id, message_id)  # токен в чате лучше не оставлять
        if kind == "lzt_token":
            client = LztClient(token)
            ok, info = client.check_token()
            if not ok:
                self.tg.send(chat_id, "❌ lzt.market не принял токен: " + html.escape(info) + "\nПришлите ещё раз или /skip")
                return
            p["lzt_token"] = token
            p["source"] = "api"
            self.state.save()
            self.tg.send(chat_id, "✅ Токен lzt.market принят: " + html.escape(info))
            if not p.get("tron_token"):
                self.awaiting[chat_id] = (p["user_id"], "tron_token")
                self.tg.send(chat_id, "Шаг 2 из 2. Пришлите токен API <b>tronaccs.market</b>.\n"
                                      "Где взять: на сайте нажмите на ник → «API TronAccs» → создать токен.\n"
                                      "Если tronaccs не нужен, напишите /skip")
                return
        else:
            try:
                client = TronClient(token, "", self.state.country_ids, self.cfg.tron_category, 1)
            except ValueError:
                client = None
            ok, info = client.check_token() if client else (False, "ошибка")
            if not ok:
                self.tg.send(chat_id, "❌ tronaccs не принял токен: " + html.escape(info) + "\nПришлите ещё раз или /skip")
                return
            p["tron_token"] = token
            p["tron_enabled"] = True
            self.state.save()
            self.tg.send(chat_id, "✅ Токен tronaccs принят: " + html.escape(info))
        self.awaiting.pop(chat_id, None)
        self.finish_setup(p, chat_id)

    def finish_setup(self, p: dict, chat_id: int) -> None:
        rt = self.runtime(p["user_id"])
        if rt:
            rt.rebuild()
        self.tg.send(chat_id, "Готово. Настройте фильтр кнопками и нажмите «Применить»:")
        self.show_settings(p, chat_id)

    # --- обработка обновлений ---

    def handle(self, update: dict) -> None:
        if update.get("callback_query"):
            self.handle_callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        chat_id = chat.get("id")
        user_id = user.get("id")
        message_id = msg.get("message_id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or user_id is None:
            return
        p = self.profile(user_id)

        # Ожидаем текст: токен, код страны, число контактов
        if not text.startswith("/"):
            pending = self.awaiting.get(chat_id)
            if pending and pending[0] == user_id and p is not None:
                self.handle_awaiting(p, chat_id, pending[1], text, message_id)
            return

        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "/id":
            self.tg.send(chat_id, f"Ваш Telegram ID: <code>{user_id}</code>\nID этого чата: <code>{chat_id}</code>")
            return
        if command == "/pin":
            self.cmd_pin(user, chat_id, arg, message_id)
            return
        if command == "/skip" and self.awaiting.get(chat_id, (None, ""))[1] in ("lzt_token", "tron_token") and p is not None:
            kind = self.awaiting.pop(chat_id)[1]
            if kind == "lzt_token" and not p.get("tron_token"):
                self.awaiting[chat_id] = (user_id, "tron_token")
                self.tg.send(chat_id, "Пропустил lzt. Шаг 2 из 2. Пришлите токен API tronaccs или /skip")
                return
            self.finish_setup(p, chat_id)
            return
        if command == "/start" and p is None and not self.state.users and not self.cfg.owner_ids:
            # Самый первый пользователь без настроенного владельца становится владельцем
            self.state.owner_id = user_id
            self.start_registration(user, chat_id)
            return
        if p is None:
            self.tg.send(chat_id, "⛔ Этот бот приватный. Введите код доступа: <code>/pin ваш_код</code>")
            return
        self.awaiting.pop(chat_id, None)
        rt = self.runtime(user_id)

        if command in ("/settings", "/menu"):
            self.show_settings(p, chat_id)
        elif command == "/start":
            if chat_id not in p["chats"]:
                p["chats"].append(chat_id)
                self.state.save()
            self.tg.send(chat_id, "✅ Этот чат подписан на ваши лоты.\n" + self.HELP
                         + (self.OWNER_HELP if self.is_owner(user_id) else ""))
        elif command == "/stop":
            if chat_id in p["chats"]:
                p["chats"].remove(chat_id)
                self.state.save()
                self.tg.send(chat_id, "🔕 Отписал этот чат. Вернуть: /start")
            else:
                self.tg.send(chat_id, "Этот чат и так не подписан. Подписать: /start")
        elif command == "/help":
            self.tg.send(chat_id, self.HELP + (self.OWNER_HELP if self.is_owner(user_id) else ""))
        elif command == "/token":
            self.cmd_token(p, chat_id, arg, message_id)
        elif command == "/filter":
            self.cmd_filter(p, chat_id, arg)
        elif command == "/check":
            self.cmd_check(p, rt, chat_id)
        elif command == "/status":
            self.cmd_status(p, rt, chat_id)
        elif command == "/tron":
            self.cmd_tron(p, rt, chat_id, arg)
        elif command == "/tronfilter":
            self.cmd_tronfilter(p, rt, chat_id, arg)
        elif command == "/tronid":
            self.cmd_tronid(p, rt, chat_id, arg)
        elif command == "/lztdump":
            items = rt.lzt.fetch_items(p["category"], lzt_params(p["query"])) if rt and rt.lzt else []
            if not items:
                self.tg.send(chat_id, "lzt.market: лотов нет, нет токена или сайт не ответил. Подробности в /status.")
                return
            raw = {k: v for k, v in items[0].items() if not isinstance(v, (dict, list))}
            self.tg.send(chat_id, "lzt.market: первый лот как отдаёт API:\n<pre>"
                         + html.escape(json.dumps(raw, ensure_ascii=False, indent=1)[:3500]) + "</pre>")
        elif command == "/trondump":
            if not rt or rt.tron is None:
                self.tg.send(chat_id, "tronaccs выключен или нет токена. /tron on, /token tron")
                return
            try:
                raw, count = rt.tron.fetch_raw_sample()
            except (TronError, requests.RequestException) as exc:
                self.tg.send(chat_id, "⚠️ " + html.escape(str(exc)))
                return
            if raw is None:
                self.tg.send(chat_id, "tronaccs вернул пустой список.")
                return
            self.tg.send(chat_id, f"tronaccs: лотов на первой странице {count}. Первый как есть:\n<pre>"
                         + html.escape(json.dumps(raw, ensure_ascii=False, indent=1)[:3500]) + "</pre>")
        elif command == "/invite" and self.is_owner(user_id):
            code = arg.strip() or secrets.token_urlsafe(6)
            if " " in code:
                self.tg.send(chat_id, "Код без пробелов, например /invite yazaurkainu")
                return
            with self.state.lock:
                if code not in self.state.invites:
                    self.state.invites.append(code)
                self.state.save()
            self.tg.send(chat_id, f"🎟 Код создан: <code>{html.escape(code)}</code>\n"
                                  f"Пусть друг напишет боту: <code>/pin {html.escape(code)}</code>\n"
                                  "Код одноразовый, после использования сгорает.")
        elif command == "/users" and self.is_owner(user_id):
            lines = []
            for p2 in self.state.users.values():
                lines.append(f"• {html.escape(p2['name'])} — ID <code>{p2['user_id']}</code>, чатов: {len(p2['chats'])}, "
                             f"lzt: {'✅' if p2.get('lzt_token') else '—'}, tronaccs: {'✅' if p2.get('tron_token') else '—'}"
                             + (" 👑" if self.is_owner(p2['user_id']) else ""))
            lines.append(f"\nАктивных кодов: {len(self.state.invites)}")
            self.tg.send(chat_id, "Пользователи:\n" + "\n".join(lines))
        elif command == "/kick" and self.is_owner(user_id):
            if not arg.strip().lstrip("-").isdigit():
                self.tg.send(chat_id, "Укажите ID: /kick 123456789 (список: /users)")
                return
            uid = int(arg)
            if self.is_owner(uid):
                self.tg.send(chat_id, "Владельца удалить нельзя.")
                return
            if self.state.remove_user(uid):
                self.runtimes.pop(uid, None)
                self.tg.send(chat_id, f"Пользователь {uid} удалён.")
            else:
                self.tg.send(chat_id, "Такого пользователя нет.")
        else:
            self.tg.send(chat_id, self.HELP + (self.OWNER_HELP if self.is_owner(user_id) else ""))

    def handle_awaiting(self, p: dict, chat_id: int, kind: str, text: str, message_id: int | None) -> None:
        if kind in ("lzt_token", "tron_token"):
            self.receive_token(p, chat_id, kind, text, message_id)
            return
        s = self.current_settings(p)
        if kind == "country":
            code = text.strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", code):
                self.tg.send(chat_id, "Нужен код из двух латинских букв, например UZ. Или /settings для отмены.")
                return
            s["country"] = code
        elif kind == "contacts":
            if not text.strip().isdigit():
                self.tg.send(chat_id, "Нужно число, например 150. Или /settings для отмены.")
                return
            s["contacts"] = int(text.strip())
        elif kind in ("price_min", "price_max"):
            digits = text.strip().replace(" ", "")
            if not digits.isdigit():
                self.tg.send(chat_id, "Нужно число в рублях, например 500. 0 — убрать ограничение. Или /settings для отмены.")
                return
            s[kind] = int(digits) or None
        self.awaiting.pop(chat_id, None)
        self.state.save()
        self.show_settings(p, chat_id)

    def cmd_pin(self, user: dict, chat_id: int, code: str, message_id: int | None) -> None:
        user_id = int(user["id"])
        if message_id:
            self.tg.delete(chat_id, message_id)
        if self.profile(user_id) is not None:
            self.tg.send(chat_id, "У вас уже есть доступ. Команды: /help")
            return
        code = code.strip()
        with self.state.lock:
            valid = bool(code) and code in self.state.invites
            if valid:
                self.state.invites.remove(code)  # одноразовый
                self.state.save()
        if not valid:
            log.info("Неверный код от пользователя %s", user_id)
            self.tg.send(chat_id, "❌ Неверный или уже использованный код.")
            return
        log.info("Пользователь %s вошёл по коду", user_id)
        self.start_registration(user, chat_id)

    def cmd_token(self, p: dict, chat_id: int, arg: str, message_id: int | None) -> None:
        parts = arg.split(maxsplit=1)
        if not parts:
            self.tg.send(chat_id, "Токены:\n"
                                  f"lzt.market: <code>{html.escape(_mask(p.get('lzt_token')))}</code>\n"
                                  f"tronaccs: <code>{html.escape(_mask(p.get('tron_token')))}</code>\n\n"
                                  "Сменить: /token lzt или /token tron, затем пришлите токен сообщением.")
            return
        kind = parts[0].lower()
        if kind not in ("lzt", "tron"):
            self.tg.send(chat_id, "Укажите сайт: /token lzt или /token tron")
            return
        if len(parts) > 1:
            if message_id:
                self.tg.delete(chat_id, message_id)
            self.receive_token(p, chat_id, f"{kind}_token", parts[1], None)
            return
        self.awaiting[chat_id] = (p["user_id"], f"{kind}_token")
        self.tg.send(chat_id, f"Пришлите токен {'lzt.market' if kind == 'lzt' else 'tronaccs'} одним сообщением.")

    def cmd_filter(self, p: dict, chat_id: int, arg: str) -> None:
        if not arg:
            self.tg.send(chat_id, "Текущий фильтр lzt.market:\n" + html.escape(lzt_site_url(p))
                         + "\n\nСменить: пришлите <code>/filter https://lzt.market/telegram/?country[]=UZ&amp;spam=no</code>"
                           "\nИли проще: /settings")
            return
        try:
            category, query = parse_market_url(arg)
        except ValueError as exc:
            self.tg.send(chat_id, "⚠️ " + html.escape(str(exc)))
            return
        p["category"], p["query"] = category, query
        p["settings"] = {}
        self.state.reset_seen(p, "lzt")
        self.tg.send(chat_id, "✅ Фильтр lzt.market обновлён:\n" + html.escape(lzt_site_url(p))
                     + "\n\nТекущие лоты запомню молча, дальше буду присылать только новые.")

    def cmd_tron(self, p: dict, rt: UserRuntime | None, chat_id: int, arg: str) -> None:
        a = arg.lower()
        if a in ("on", "вкл", "включить"):
            if not p.get("tron_token"):
                self.tg.send(chat_id, "⚠️ Нет токена tronaccs. Добавьте: /token tron")
                return
            p["tron_enabled"] = True
            self.state.reset_seen(p, "tron")
            if rt:
                rt.rebuild()
            self.tg.send(chat_id, "✅ tronaccs включён. Фильтр: " + (html.escape(p.get("tron_filter") or "") or "нет"))
        elif a in ("off", "выкл", "стоп"):
            p["tron_enabled"] = False
            self.state.save()
            if rt:
                rt.rebuild()
            self.tg.send(chat_id, "🔕 tronaccs выключен.")
        else:
            self.tg.send(chat_id, ("tronaccs: включён ✅" if rt and rt.tron else "tronaccs: выключен")
                         + "\nФильтр: " + (html.escape(p.get("tron_filter") or "") or "нет")
                         + "\n\n/tron on — включить, /tron off — выключить\n/tronfilter … — фильтр вручную, /trondump — поля лота")

    def cmd_tronfilter(self, p: dict, rt: UserRuntime | None, chat_id: int, arg: str) -> None:
        if not arg:
            self.tg.send(chat_id, "Фильтр tronaccs: " + (html.escape(p.get("tron_filter") or "") or "нет")
                         + "\n\nЗадать: <code>/tronfilter country=UZ contacts>=100 spam=no price<=500</code>\n"
                           "Поля: price, contacts, dialogs, channels, chats, age, stars, spam, premium, 2fa, country, seller, title~слово.\n"
                           "Условия: = != > < >= <= и ~ (содержит). Снять: /tronfilter off\nИли проще: /settings")
            return
        text = "" if arg.lower() in ("off", "нет", "снять") else arg
        try:
            parse_filter(text)
        except ValueError as exc:
            self.tg.send(chat_id, "⚠️ " + html.escape(str(exc)))
            return
        p["tron_filter"] = text
        p["settings"] = {}
        self.state.reset_seen(p, "tron")
        if rt:
            rt.rebuild()
        self.tg.send(chat_id, "✅ Фильтр tronaccs: " + (html.escape(text) or "снят")
                     + "\nТекущие подходящие лоты запомню молча, дальше буду присылать новые.")

    def cmd_tronid(self, p: dict, rt: UserRuntime | None, chat_id: int, arg: str) -> None:
        if not rt or rt.tron is None:
            self.tg.send(chat_id, "tronaccs выключен или нет токена. /tron on, /token tron")
            return
        code = arg.strip().upper()
        if code == "RESET":
            with self.state.lock:
                self.state.country_ids.clear()
                self.state.save()
            self.tg.send(chat_id, "Список ID стран очищен.")
            return
        manual = re.fullmatch(r"([A-Z]{2})[\s=:]+(\d+)", code)
        if manual:
            code, cid = manual.group(1), int(manual.group(2))
            with self.state.lock:
                self.state.country_ids[code] = cid
                self.state.save()
            rt.rebuild()
            self.tg.send(chat_id, f"✅ {code} = ID {cid}. Запомнил.")
            return
        if not re.fullmatch(r"[A-Z]{2}", code):
            known = ", ".join(f"{k}={v}" for k, v in sorted(self.state.country_ids.items())) or "пока нет"
            self.tg.send(chat_id, "Найти перебором: <code>/tronid UZ</code>\nВписать вручную: <code>/tronid UZ 443</code>\n"
                                  "Известные ID: " + html.escape(known))
            return
        if code in self.state.country_ids:
            self.tg.send(chat_id, f"{code} = ID {self.state.country_ids[code]} (уже известен).")
            return
        self.tg.send(chat_id, f"🔎 Ищу ID страны {code} перебором через API tronaccs, до 5 минут…")
        try:
            cid = rt.tron.find_country_id(code, progress=lambda n: self.tg.send(chat_id, f"…проверил {n} ID"))
        except (TronError, requests.RequestException) as exc:
            self.tg.send(chat_id, "⚠️ " + html.escape(str(exc)))
            return
        if cid is None:
            self.tg.send(chat_id, f"Не нашёл ID для {code}. Можно подсмотреть на сайте и вписать: /tronid {code} <число>")
            return
        with self.state.lock:
            self.state.country_ids[code] = cid
            self.state.save()
        rt.rebuild()
        self.tg.send(chat_id, f"✅ {code} = ID {cid}. Запомнил.")

    def cmd_check(self, p: dict, rt: UserRuntime | None, chat_id: int) -> None:
        if rt is None:
            return
        self.tg.send(chat_id, "🔎 Проверяю…")
        if rt.lzt is None:
            self.tg.send(chat_id, "lzt.market: нет токена. Добавить: /token lzt")
        else:
            items = rt.lzt.fetch_items(p["category"], lzt_params(p["query"]))
            if not items:
                self.tg.send(chat_id, "lzt.market: по фильтру ничего не найдено или сайт не ответил. Подробности в /status.")
            else:
                latest = sorted(items, key=lambda i: int(i.get("published_date") or 0), reverse=True)[:3]
                self.tg.send(chat_id, f"lzt.market: лотов на первой странице {len(items)}. Последние:")
                for item in latest:
                    self.tg.send(chat_id, format_item(item), self.item_keyboard(rt, item))
        if rt.tron is not None:
            items = rt.tron.fetch_items()
            if not items:
                self.tg.send(chat_id, f"tronaccs: получено лотов {rt.tron.last_total}, под фильтр не подошёл ни один "
                                      "или сайт не ответил. Подробности в /status, поля лота: /trondump")
            else:
                self.tg.send(chat_id, f"tronaccs: получено лотов {rt.tron.last_total}, под фильтр подходят {len(items)}. Последние:")
                for item in items[:3]:
                    self.tg.send(chat_id, format_item(item), self.item_keyboard(rt, item))

    def cmd_status(self, p: dict, rt: UserRuntime | None, chat_id: int) -> None:
        if rt is None:
            return
        st = rt.stats
        lines = [
            f"👤 {html.escape(p['name'])}" + (" 👑" if self.is_owner(p['user_id']) else ""),
            "🏪 lzt.market: " + ("выключен, нет токена" if rt.lzt is None else ("страница сайта" if isinstance(rt.lzt, WebClient) else "API"))
            + "\n   фильтр: " + html.escape(lzt_site_url(p)),
            "🏪 tronaccs: " + (("включён, фильтр: " + (html.escape(p.get("tron_filter") or "") or "нет")) if rt.tron
                              else ("выключен" if p.get("tron_token") else "нет токена")),
            f"💬 Подписанных чатов: {len(p['chats'])}" + (" (этот чат подписан ✅)" if chat_id in p["chats"] else " (этот чат не подписан)"),
            f"⏱ Проверка каждые {self.cfg.poll_interval} с, проверок: {st['checks']}, последняя: {_ago(st['last_check_at'])}",
        ]
        if st["last_items"] is not None:
            lines.append(f"📡 lzt.market: лотов по фильтру {st['last_items']}, новых в последней проверке {st['last_new']}")
            meta = rt.lzt.last_meta if rt.lzt else {}
            if meta.get("totalItems") is not None:
                lines.append(f"   всего на маркете по фильтру: {meta['totalItems']}, из кэша: {'да' if meta.get('wasCached') else 'нет'}")
        if st["tron_items"] is not None:
            lines.append(f"📡 tronaccs: получено {st['tron_total']}, под фильтр {st['tron_items']}, новых {st['tron_new']}")
        if rt.tron is not None:
            bal = rt.tron.balance()
            if bal:
                lines.append(f"💳 Баланс tronaccs: {html.escape(bal)}")
        if st["last_error"]:
            lines.append(f"⚠️ Последняя ошибка ({_ago(st['last_error_at'])}): " + html.escape(str(st["last_error"])))
        self.tg.send(chat_id, "\n".join(lines))

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


# ---------- мониторинг ----------

def deliver_new(bot: Bot, rt: UserRuntime, items: list[dict], new_items: list[dict], name: str) -> int:
    """Первый запуск молчит, дальше шлём новые лоты в чаты пользователя."""
    p, state, cfg, tg = rt.profile, bot.state, bot.cfg, bot.tg
    seen = p["seen"][name]
    if not p["init"][name]:
        p["init"][name] = True
        if not cfg.notify_on_first_run:
            for item in items:
                if int(item["item_id"]) not in seen:
                    seen.append(int(item["item_id"]))
            state.save()
            log.info("%s/%s: первый запуск, запомнил %d лотов", p["user_id"], name, len(items))
            return 0
    new_items.sort(key=lambda i: int(i.get("published_date") or 0))
    chats = rt.chats
    if not chats:
        for item in new_items:
            seen.append(int(item["item_id"]))
        state.save()
        return 0
    sent = 0
    for item in new_items:
        item_id = int(item["item_id"])
        if tg.send_all(chats, format_item(item), bot.item_keyboard(rt, item)):
            sent += 1
            log.info("%s/%s: отправлен лот %d (%s)", p["user_id"], name, item_id, item.get("price"))
        else:
            log.error("%s/%s: не удалось отправить лот %d", p["user_id"], name, item_id)
            continue
        seen.append(item_id)
        state.save()
        time.sleep(1)
    return sent


def check_user(bot: Bot, rt: UserRuntime) -> None:
    p, st = rt.profile, rt.stats
    if rt.lzt is not None:
        items = rt.lzt.fetch_items(p["category"], lzt_params(p["query"]))
        st["checks"] += 1
        st["last_check_at"] = time.time()
        st["last_items"] = len(items)
        st["last_new"] = 0
        rt.record_error(rt.lzt.last_error)
        rt.lzt.last_error = None
        if items:
            seen = p["seen"]["lzt"]
            new_items = [i for i in items if int(i["item_id"]) not in seen]
            st["last_new"] = len(new_items)
            log.info("%s/lzt: лотов %d, новых %d", p["user_id"], len(items), len(new_items))
            deliver_new(bot, rt, items, new_items, "lzt")
        else:
            log.warning("%s/lzt: лотов нет (ошибка или пустой фильтр)", p["user_id"])
    if rt.tron is not None:
        items = rt.tron.fetch_items()
        st["tron_total"] = rt.tron.last_total
        st["tron_items"] = len(items)
        st["tron_new"] = 0
        rt.record_error(rt.tron.last_error)
        rt.tron.last_error = None
        if items:
            seen = p["seen"]["tron"]
            new_items = [i for i in items if int(i["item_id"]) not in seen]
            st["tron_new"] = len(new_items)
            log.info("%s/tron: всего %d, под фильтр %d, новых %d", p["user_id"], rt.tron.last_total, len(items), len(new_items))
            deliver_new(bot, rt, items, new_items, "tron")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    cfg.validate()
    tg = Telegram(cfg.tg_bot_token)
    state = State(cfg.state_file, cfg)
    bot = Bot(cfg, state, tg)
    bot.ensure_owner_profile()

    if "--test" in argv:
        chats = [c for p in state.users.values() for c in p["chats"]]
        if not chats:
            print("Подписчиков нет: напишите боту /start")
            return 1
        ok = tg.send_all(chats, "✅ Монитор подключён.")
        print(f"Тестовое сообщение отправлено в {ok} из {len(chats)} чатов")
        return 0 if ok else 1

    if "--dump" in argv:
        rt = next(iter(bot.runtimes.values()), None)
        if rt is None or rt.lzt is None:
            print("Нет профиля с токеном lzt. Заполните LZT_TOKEN и TG_USER_ID в .env")
            return 1
        items = rt.lzt.fetch_items(rt.profile["category"], lzt_params(rt.profile["query"]))
        print(json.dumps(items[:3], ensure_ascii=False, indent=2))
        print(f"\nВсего лотов на первой странице: {len(items)}")
        return 0

    threading.Thread(target=bot.run_forever, name="telegram-bot", daemon=True).start()
    log.info("Пользователей: %d, проверка каждые %d с", len(state.users), cfg.poll_interval)
    if not state.users:
        log.info("Задайте TG_USER_ID в .env или напишите боту /start, чтобы стать владельцем")
    if cfg.notify_on_startup:
        for p in list(state.users.values()):
            if p["chats"]:
                tg.send_all(p["chats"], "🟢 Монитор запущен. Фильтр lzt: " + html.escape(lzt_site_url(p))
                            + "\nПришлю сообщение, как только появится новый лот. Проверить сейчас: /check")

    once = "--once" in argv
    while True:
        for uid in list(bot.runtimes):
            rt = bot.runtime(uid)
            if rt is None:
                continue
            try:
                check_user(bot, rt)
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка при проверке пользователя %s", uid)
                rt.record_error(f"{type(exc).__name__}: {exc}")
        if once:
            return 0
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
