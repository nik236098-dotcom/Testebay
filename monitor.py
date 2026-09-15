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
    EVA_URL          - база EVA TG spammer-api, по умолчанию https://api.eva-sms.cc/api/v1
    EVA_TOKEN        - X-EVANGELION токен владельца (необязательно)
    EVA_ENV          - prod | dev
    EVA_RUN_SPAM     - 1 = upload-run (сразу спам), 0 = только залив
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

from bot_text import display_value, readable, split_html, user_error
from bot_menu import country_name, criteria, lzt_criteria, money, price_text, settings_from_query, tron_criteria

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

EVA_PROD_BASE = "https://api.eva-sms.cc/api/v1"
EVA_DEV_BASE = "/spammer-api/api/v1"


def _ago(ts) -> str:
    if not ts:
        return "ещё не было"
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta} с назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    return f"{delta // 3600} ч {delta % 3600 // 60} мин назад"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


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
        self.session_only = os.getenv("SESSION_ONLY", "1") == "1"  # присылать только .session, без JSON и сводки
        # Внешняя панель для автозагрузки .session (evangelion-best.to и т.п.)
        self.panel_url = os.getenv("PANEL_URL", "").strip()
        self.panel_token = os.getenv("PANEL_TOKEN", "").strip()
        self.panel_field = os.getenv("PANEL_FIELD", "file").strip() or "file"
        # EVA TG (spammer-api) — автозалив архива .session после покупки
        self.eva_url = os.getenv("EVA_URL", EVA_PROD_BASE).strip()
        self.eva_token = os.getenv("EVA_TOKEN", "").strip()
        self.eva_env = os.getenv("EVA_ENV", "prod").strip().lower()
        self.eva_run_spam = os.getenv("EVA_RUN_SPAM", "1") == "1"
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


def autobuy_lzt_params(s: dict) -> list[tuple[str, str]]:
    """Параметры запроса к lzt для автопокупки."""
    params: list[tuple[str, str]] = []
    if s.get("country") and s["country"] != "any":
        params.append(("country[]", s["country"]))
    if s.get("contacts", 0) > 0:
        params.append(("min_contacts", str(s["contacts"])))
    if s.get("spam") and s["spam"] != "any":
        params.append(("spam", s["spam"]))
    if s.get("price_min") is not None:
        params.append(("pmin", str(s["price_min"])))
    if s.get("price_max") is not None:
        params.append(("pmax", str(s["price_max"])))
    params.append(("order_by", "pdate_to_down"))
    return params


def autobuy_tron_filter(s: dict) -> str:
    """Текстовый фильтр tronaccs для автопокупки."""
    parts: list[str] = []
    if s.get("country") and s["country"] != "any":
        parts.append(f"country={s['country']}")
    if s.get("contacts", 0) > 0:
        parts.append(f"contacts>={s['contacts']}")
    if s.get("spam") and s["spam"] != "any":
        parts.append(f"spam={s['spam']}")
    if s.get("price_min") is not None:
        parts.append(f"price>={s['price_min']}")
    if s.get("price_max") is not None:
        parts.append(f"price<={s['price_max']}")
    return " ".join(parts)


AUTOBUY_DEFAULT_SETTINGS = {
    "country": "UZ",
    "contacts": 100,
    "spam": "no",
    "price_min": None,
    "price_max": None,
    "lzt": True,
    "tron": False,
}

AUTOBUY_SITES = ("lzt", "tron")
AUTOBUY_SITE_LABELS = {"lzt": "lzt.market", "tron": "tronaccs"}


def new_autobuy() -> dict:
    return {
        "enabled": False,
        "sources": ["lzt"],                    # площадки автопокупки, можно обе
        "settings": dict(AUTOBUY_DEFAULT_SETTINGS),
        "seen": {"lzt": [], "tron": []},       # ID лотов по площадкам (у lzt и tronaccs ID могут совпадать)
        "attempts": {},                        # "lzt:123" -> ts последней попытки (анти-retry)
        "init": {"lzt": False, "tron": False}, # тихая первая проверка по каждой площадке
    }


def autobuy_upgrade(ab: dict) -> dict:
    """Старый формат (одна площадка в "source", общий список seen) -> новый, по площадкам."""
    old_source = ab.pop("source", None) or "lzt"
    if not isinstance(ab.get("sources"), list):
        ab["sources"] = [old_source]
    ab["sources"] = [x for x in ab["sources"] if x in AUTOBUY_SITES]
    if not isinstance(ab.get("seen"), dict):
        ab["seen"] = {old_source: list(ab.get("seen") or [])}
    for site in AUTOBUY_SITES:
        ab["seen"].setdefault(site, [])
    if not isinstance(ab.get("init"), dict):
        ab["init"] = {old_source: bool(ab.get("init"))}
    for site in AUTOBUY_SITES:
        ab["init"].setdefault(site, False)
    ab.setdefault("enabled", False)
    ab.setdefault("attempts", {})
    return ab


def autobuy_reset(ab: dict, attempts: bool = True) -> None:
    """Условия изменились: забываем историю, следующая проверка каждой площадки — тихая."""
    ab["seen"] = {site: [] for site in AUTOBUY_SITES}
    ab["init"] = {site: False for site in AUTOBUY_SITES}
    if attempts:
        ab["attempts"] = {}


def autobuy_sources_label(ab: dict) -> str:
    return " + ".join(AUTOBUY_SITE_LABELS[x] for x in ab.get("sources") or []) or "не выбрана"


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
        # >>> AutoBuy <<<
        "autobuy": new_autobuy(),
        # Автозагрузка купленной .session во внешнюю панель (evangelion и т.п.)
        "upload_url": cfg.panel_url if with_env_tokens else "",
        "upload_token": cfg.panel_token if with_env_tokens else "",
        "upload_field": cfg.panel_field,
        # EVA TG (spammer-api)
        "eva_url": cfg.eva_url if with_env_tokens else EVA_PROD_BASE,
        "eva_token": cfg.eva_token if with_env_tokens else "",
        "eva_env": cfg.eva_env,
        "eva_run_spam": cfg.eva_run_spam,
        "eva_auto": bool(cfg.eva_token) if with_env_tokens else False,
    }


def _trim_list(lst: list, limit: int = MAX_SEEN_IDS) -> None:
    """Оставляет последние limit элементов, не создавая новый список."""
    if len(lst) > limit:
        del lst[:-limit]


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
        # Профили из старого state: достраиваем блок autobuy и переводим его на формат с несколькими площадками
        for p in self.users.values():
            p["autobuy"] = autobuy_upgrade(p.get("autobuy") or new_autobuy())

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
                # Списки урезаем НА МЕСТЕ: рассылка и AutoBuy держат ссылку на этот же список и
                # дописывают в него между сохранениями; копия (срез) оставила бы их со старым объектом
                for k in ("lzt", "tron"):
                    _trim_list(p["seen"][k])
                ab = p.get("autobuy") or {}
                if ab:
                    autobuy_upgrade(ab)
                    for site in AUTOBUY_SITES:
                        _trim_list(ab["seen"][site])
                    attempts = ab.get("attempts") or {}
                    if len(attempts) > MAX_SEEN_IDS:
                        for key, _ in sorted(attempts.items(), key=lambda kv: kv[1])[:len(attempts) - MAX_SEEN_IDS]:
                            del attempts[key]
            data = {
                "users": self.users,
                "invites": self.invites,
                "tg_offset": self.tg_offset,
                "country_ids": self.country_ids,
                "owner_id": self.owner_id,
            }
            # Профили правятся из потоков проверки без этого замка; если словарь изменился
            # прямо во время сериализации — просто пробуем ещё раз
            for attempt in range(5):
                try:
                    payload = json.dumps(data, ensure_ascii=False)
                    break
                except RuntimeError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
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

def _errors_text(data) -> str:
    """Unwrap nested service errors without Python container syntax."""
    return readable(data)


def _body_snippet(resp, limit: int = 150, with_server: bool = True) -> str:
    """Короткий читаемый текст ответа для сообщения об ошибке: из JSON берём поле errors/message
    (без экранированных кодов символов), у HTML-страниц (DDoS-Guard, заглушка техработ) убираем теги."""
    text = resp.text or ""
    server = resp.headers.get("Server", "") if with_server else ""
    try:
        text = _errors_text(json.loads(text)) or text
    except ValueError:
        pass
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return (f"[{server}] " if server else "") + (text[:limit] or "пустой ответ")


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
            return False, f"HTTP {resp.status_code}: {_body_snippet(resp, with_server=False)}"
        user = data.get("user") if isinstance(data, dict) else None
        if isinstance(user, dict):
            return True, f"{display_value(user.get('username') or user.get('user_id'), 'пользователь')}, баланс {display_value(user.get('balance'), 'недоступен')} ₽"
        return True, "ок"

    def balance(self) -> str:
        try:
            resp = self.session.get(f"{API_BASE}/me", timeout=30)
            resp.raise_for_status()
            data = resp.json()
            user = data.get("user") if isinstance(data, dict) else None
            if not isinstance(user, dict):
                return "Не удалось получить баланс. Попробуйте позже."
            return money(user.get("balance"), user.get("currency") or "RUB")
        except (requests.RequestException, ValueError) as exc:
            log.warning("Не удалось получить баланс lzt: %s", exc)
            return user_error(exc)

    def fetch_items(self, category: str, params: list[tuple[str, str]], retries: int = 5) -> list[dict]:
        """retries=1 — один быстрый запрос без пауз (для команд из чата), 5 — с повторами (для монитора)."""
        url = f"{API_BASE}/{category}"
        backoff = 5
        for attempt in range(1, retries + 1):
            last = attempt >= retries
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                log.warning("lzt: ошибка сети (попытка %d): %s", attempt, exc)
                self._err(f"сеть: {exc}")
                if last:
                    return []
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                self._err("lzt: превышен лимит запросов (429)")
                if last:
                    return []
                time.sleep(retry_after)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code in (401, 403):
                self._err(f"lzt {resp.status_code}: проверьте токен. {_body_snippet(resp, with_server=False)}")
                log.error("lzt вернул %d: %s", resp.status_code, _body_snippet(resp, limit=300))
                return []
            if resp.status_code >= 500:
                # 5xx (чаще всего 503 на техработах) держится долго: одна повторная попытка,
                # иначе проверка занимает замок минутами и /check не может вклиниться
                self._err(f"lzt вернул {resp.status_code}: {_body_snippet(resp)}")
                if last or attempt >= 2:
                    return []
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 400:
                self._err(f"lzt {resp.status_code}: {_body_snippet(resp, with_server=False)}")
                return []
            try:
                data = resp.json()
            except ValueError:
                self._err(f"lzt: ответ не JSON: {_body_snippet(resp)}")
                return []
            if isinstance(data, dict) and data.get("errors"):
                self._err(f"lzt: {_errors_text(data)}")
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
            return False, _errors_text(errors)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {_body_snippet(resp, with_server=False)}"
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

    def fetch_items(self, category: str, params: list[tuple[str, str]], retries: int = 5) -> list[dict]:
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
        return True, f"{display_value(user.get('username'), 'пользователь')}, баланс {display_value(user.get('balance'), 'недоступен')} {str(user.get('currency') or '').upper()}"

    def fetch_items(self, retries: int = 5) -> list[dict]:
        try:
            items = self.api.fetch_items(self.params, retries=retries)
        except (TronError, requests.RequestException) as exc:
            self.last_error = str(exc)[:300]
            log.error("%s", exc)
            return []
        self.last_total = len(items)
        if self.local_rules:
            items = [i for i in items if match_filter(i, self.local_rules)]
        return items

    def fetch_raw_sample(self) -> tuple[dict | None, int]:
        data = self.api.fetch_raw_page(1, self.params, retries=1)
        raw = data.get("items") if isinstance(data, dict) else data
        raw = raw if isinstance(raw, list) else []
        return (raw[0] if raw else (data if isinstance(data, dict) else None)), len(raw)

    def balance(self) -> str | None:
        try:
            user = self.api.me()
        except (TronError, requests.RequestException) as exc:
            return user_error(exc)
        return money(user.get("balance"), user.get("currency") or "RUB") if user else "Баланс недоступен"

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
        self.result_state = threading.local()

    def call(self, method: str, payload: dict) -> dict | None:
        self.result_state.description = ""
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
            if method in ("editMessageText", "editMessageReplyMarkup") and "message is not modified" in str(data.get("description", "")).lower():
                return {}
            self.result_state.description = str(data.get("description", ""))
            if method != "deleteMessage":
                log.error("Telegram %s: ответ %d: %s", method, resp.status_code, resp.text[:300])
            return None
        return None

    def send(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> bool:
        chunks = split_html(text, TELEGRAM_MESSAGE_LIMIT)
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            if self.call("sendMessage", payload) is None:
                return False
        return bool(chunks)

    def can_recreate_screen(self) -> bool:
        description = getattr(self.result_state, "description", "").lower()
        return "message to edit not found" in description or "message can't be edited" in description

    def send_screen(self, chat_id: int, text: str, reply_markup: dict) -> int | None:
        # Menu pages are deliberately compact; longer content still keeps valid HTML.
        chunks = split_html(text, TELEGRAM_MESSAGE_LIMIT)
        message_id = None
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
            if index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            result = self.call("sendMessage", payload)
            if result is None:
                return None
            message_id = result.get("message_id")
        return message_id

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

    def edit_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> bool:
        chunks = split_html(text, TELEGRAM_MESSAGE_LIMIT)
        if not chunks:
            return False
        payload = {"chat_id": chat_id, "message_id": message_id, "text": chunks[0],
                   "parse_mode": "HTML", "disable_web_page_preview": True,
                   "reply_markup": reply_markup if len(chunks) == 1 and reply_markup else {"inline_keyboard": []}}
        if self.call("editMessageText", payload) is None:
            return False
        for index, chunk in enumerate(chunks[1:], start=1):
            if not self.send(chat_id, chunk, reply_markup if index == len(chunks) - 1 else None):
                return False
        return True

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


# Боевые дата-центры Telegram: dc_id -> адрес (порт 443)
TG_DC = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100",
         4: "149.154.167.91", 5: "91.108.56.130"}


def _decode_auth_key(value: str) -> bytes | None:
    """auth_key приходит hex (512 симв.) или base64 (256 байт)."""
    s = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{512}", s):
        try:
            return bytes.fromhex(s)
        except ValueError:
            return None
    if _B64_RE.match(s):
        try:
            raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
            if len(raw) == 256:
                return raw
        except Exception:  # noqa: BLE001
            return None
    return None


def session_from_authkey(flat: dict) -> bytes | None:
    """Собрать .session из полей auth_key + dc_id (формат tronaccs: AUTHKEY, DCID)."""
    auth_key = dc_id = None
    for key, value in flat.items():
        low = key.lower().rsplit(".", 1)[-1]
        if not isinstance(value, str):
            if dc_id is None and low in ("dcid", "dc_id"):
                try:
                    n = int(value)
                    if 1 <= n <= 5:
                        dc_id = n
                except (TypeError, ValueError):
                    pass
            continue
        v = value.strip()
        # Совмещённое поле AUTHKEY:DCID вида "<hex512>:<dc>"
        combo = re.fullmatch(r"([0-9a-fA-F]{512}):([1-5])", v)
        if combo:
            key_bytes = _decode_auth_key(combo.group(1))
            if key_bytes:
                return _make_telethon_session(int(combo.group(2)), TG_DC[int(combo.group(2))], 443, key_bytes)
        if auth_key is None and ("auth" in low and "key" in low or low in ("authkey", "auth_key")):
            auth_key = _decode_auth_key(v)
        if dc_id is None and low in ("dcid", "dc_id"):
            try:
                n = int(v)
                if 1 <= n <= 5:
                    dc_id = n
            except ValueError:
                pass
    if not auth_key:
        return None
    dc_id = dc_id or 2
    return _make_telethon_session(dc_id, TG_DC.get(dc_id, TG_DC[2]), 443, auth_key)


def _parse_telethon_string(session_str: str):
    """Строковая сессия Telethon -> (dc_id, ip, port, auth_key). Без зависимостей."""
    import socket
    import struct
    s = session_str.strip()
    if not s or s[0] != "1":
        return None
    s = s[1:]
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:  # noqa: BLE001
        return None
    ip_len = 4 if len(raw) == 4 + 1 + 2 + 256 else 16
    try:
        dc_id, ip, port, auth_key = struct.unpack(f">B{ip_len}sH256s", raw)
    except struct.error:
        return None
    family = socket.AF_INET if ip_len == 4 else socket.AF_INET6
    try:
        addr = socket.inet_ntop(family, ip)
    except OSError:
        addr = ""
    return dc_id, addr, port, auth_key


def _make_telethon_session(dc_id: int, ip: str, port: int, auth_key: bytes) -> bytes | None:
    """Собирает файл .session (SQLite в формате Telethon) из данных сессии."""
    import sqlite3
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.session")
            con = sqlite3.connect(path)
            cur = con.cursor()
            cur.execute("CREATE TABLE version (version integer primary key)")
            cur.execute("CREATE TABLE sessions (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer)")
            cur.execute("CREATE TABLE entities (id integer primary key, hash integer not null, username text, phone integer, name text, date integer)")
            cur.execute("CREATE TABLE sent_files (md5_digest blob, file_size integer, type integer, id integer, hash integer, primary key(md5_digest, file_size, type))")
            cur.execute("CREATE TABLE update_state (id integer primary key, pts integer, qts integer, date integer, seq integer)")
            cur.execute("INSERT INTO version VALUES (7)")
            cur.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (dc_id, ip, port, auth_key, None))
            con.commit()
            con.close()
            with open(path, "rb") as f:
                return f.read()
    except Exception:  # noqa: BLE001
        return None


def upload_session(url: str, token: str, field: str, filename: str, content: bytes) -> tuple[bool, str]:
    """POST .session во внешнюю панель. Токен идёт и заголовком, и полем, чтобы подойти под разные панели."""
    headers = {}
    dataf: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Api-Key"] = token
        dataf["token"] = token
        dataf["api_key"] = token
    try:
        resp = requests.post(url, headers=headers, data=dataf,
                             files={field or "file": (filename, content)}, timeout=120)
    except requests.RequestException as exc:
        return False, f"сеть: {exc}"
    body = resp.text[:200]
    try:
        j = resp.json()
        if isinstance(j, dict):
            body = str(j.get("message") or j.get("error") or j.get("result") or j)[:200]
            if resp.ok and str(j.get("status", "ok")).lower() in ("ok", "true", "success", "1") and not j.get("error"):
                return True, body
    except ValueError:
        pass
    if resp.ok:
        return True, body or "загружено"
    return False, f"HTTP {resp.status_code}: {body}"


def upload_to_eva_tg(base_url: str, token: str, field: str, files: list[tuple[str, bytes]],
                     run_spam: bool = True) -> tuple[bool, str]:
    """Заливает .session-файлы архивом в EVA TG (spammer-api).

    run_spam=True  → POST /spam/upload-run   (залить и сразу в очередь)
    run_spam=False → POST /sessions/upload   (только залить)

    Content-Type не ставим — requests сам выставит multipart/form-data.
    """
    import io
    import zipfile

    if not token:
        return False, "нет X-EVANGELION токена"
    if not files:
        return False, "нет .session для отправки"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files:
            z.writestr(name, content)
    data = buf.getvalue()

    if not data:
        return False, "empty_file"
    if len(data) > 240 * 1024 * 1024:
        return False, f"too_large: {len(data) / 1048576:.1f} MB > 240 MB"

    endpoint = "/spam/upload-run" if run_spam else "/sessions/upload"
    url = base_url.rstrip("/") + endpoint
    headers = {"X-EVANGELION": token}  # Content-Type НЕ ставим

    try:
        resp = requests.post(url, headers=headers,
                             files={field or "file": ("sessions.zip", data)},
                             timeout=300)
    except requests.RequestException as exc:
        return False, f"сеть: {exc}"

    try:
        j = resp.json()
    except ValueError:
        return resp.ok, resp.text[:200]
    if not isinstance(j, dict):
        return resp.ok, str(j)[:200]

    if j.get("status") is False:
        code = str(j.get("error") or j.get("code") or "?")
        if "Too Many" in code or "blocked" in code.lower():
            return False, "rate limit: " + code
        return False, code

    if "total" in j:
        return True, f"залито сессий: {j['total']}"
    bid = j.get("batch_id") or j.get("batchId") or "?"
    uq = j.get("upload_quota") or {}
    sq = j.get("sessions_quota") or {}
    parts = [f"batch {bid}"]
    if uq:
        parts.append(f"upload {uq.get('active','?')}/{uq.get('limit','?')}")
    if sq:
        parts.append(f"sessions {sq.get('active','?')}/{sq.get('limit','?')}")
    return True, ", ".join(parts)


def stringsession_to_session(session_str: str) -> bytes | None:
    """Строковую сессию Telethon -> настоящий .session (SQLite), без внешних библиотек."""
    parsed = _parse_telethon_string(session_str)
    if not parsed:
        return None
    dc_id, ip, port, auth_key = parsed
    if not auth_key or all(b == 0 for b in auth_key) or not ip:
        return None
    return _make_telethon_session(dc_id, ip, port, auth_key)


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

    # Не нашли готовый .session? Пробуем собрать из auth_key + dc_id (формат tronaccs)
    if not got_session:
        built = session_from_authkey(flat)
        if built:
            add(f"{item_id}.session", built)
            got_session = True

    # Полный ответ прикладываем как запасной, если .session так и не собрался
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
        # >>> AutoBuy-клиенты (отдельные экземпляры, чтобы не мешать обычной проверке) <<<
        self.lzt_ab: LztClient | None = None
        self.tron_ab: TronClient | None = None
        self.stats: dict = {"checks": 0, "last_check_at": None, "last_items": None, "last_new": 0,
                            "tron_total": None, "tron_items": None, "tron_new": 0,
                            "last_error": None, "last_error_at": None}
        # Плановая проверка и /check ходят к сайтам из разных потоков; одновременно — нельзя
        # (одна requests.Session на обоих, да и второй запрос подряд ловит 429).
        self.lock = threading.Lock()
        self.rebuild()

    def rebuild(self) -> None:
        p = self.profile
        if not p.get("lzt_enabled", True):
            self.lzt = None
        elif p.get("source") == "web":
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
        # >>> AutoBuy <<<
        self.lzt_ab = None
        self.tron_ab = None
        ab = p.get("autobuy") or {}
        if ab.get("enabled"):
            sources = autobuy_upgrade(ab)["sources"]
            s = ab.get("settings") or dict(AUTOBUY_DEFAULT_SETTINGS)
            if "lzt" in sources and p.get("lzt_token") and p.get("source") != "web":
                self.lzt_ab = LztClient(p["lzt_token"])
            if "tron" in sources and p.get("tron_token"):
                try:
                    self.tron_ab = TronClient(
                        p["tron_token"], autobuy_tron_filter(s), self.state.country_ids,
                        self.cfg.tron_category, self.cfg.tron_pages,
                    )
                except ValueError as exc:
                    log.error("autobuy tron: фильтр пользователя %s не разобран: %s", p["user_id"], exc)

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
        "/menu — главное меню с кнопками\n"
        "/balance — балансы обеих площадок\n"
        "/settings — настроить фильтр кнопками: страна, контакты, спамблок\n"
        "/autobuy — автопокупка подходящих новых лотов\n"
        "/check — проверить прямо сейчас и показать последние лоты\n"
        "/status — что мониторится, балансы, последняя проверка\n"
        "/token — показать или сменить токены API\n"
        "/panel — автозагрузка купленной сессии во внешнюю панель\n"
        "/eva — EVA TG: автозалив архива .session после покупки\n"
        "/filter &lt;ссылка&gt; — фильтр lzt.market ссылкой с сайта\n"
        "/tron on|off, /tronfilter … — tronaccs вручную\n"
        "/tronid UZ — узнать ID страны на tronaccs\n"
        "/lztdump, /trondump — лот в сыром виде, как отдаёт сайт\n"
        "/lot <номер или ссылка> — почему бот не показывает конкретный лот lzt\n"
        "/stop — отписать этот чат, /start — подписать снова\n"
        "/id — ваш Telegram ID"
    )
    OWNER_HELP = (
        "\n\nКоманды владельца:\n"
        "/invite [код] — создать одноразовый код доступа\n"
        "/users — список пользователей\n"
        "/kick &lt;id&gt; — удалить пользователя"
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
        self.jobs: dict[tuple[int, str], threading.Thread] = {}  # (user_id, команда) -> поток
        self.jobs_lock = threading.RLock()
        self.flow_lock = threading.RLock()
        self.active_pages: dict[tuple, tuple] = {}
        for p in list(state.users.values()):
            self.runtime(p["user_id"])

    def run_in_background(self, user_id: int, name: str, chat_id: int, target, *args) -> None:
        """Команды, которые ходят к сайтам, выполняем в отдельном потоке: иначе, пока сайт
        думает или отдаёт 429, бот не отвечает никому и не обрабатывает кнопки."""
        key = (user_id, name)
        with self.jobs_lock:
            job = self.jobs.get(key)
            if job is not None and job.is_alive():
                self.tg.send(chat_id, f"⏳ Команда {name} уже выполняется, подождите ответа.")
                return

            def runner() -> None:
                try:
                    target(*args)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка в команде %s пользователя %s", name, user_id)
                    p = self.profile(user_id)
                    if p is not None and name.startswith(("/balances:", "/check:")):
                        with self.flow_lock:
                            page, mid = self.active_pages.get((user_id, chat_id), (None, None))
                            if page in ("balances", "check"):
                                self.show_page(p, chat_id, "⚠️ Не удалось загрузить данные. Попробуйте ещё раз позже.",
                                               self.menu_keyboard(), "error", mid)
                    else:
                        self.tg.send(chat_id, "⚠️ Не удалось выполнить команду. Попробуйте ещё раз или откройте /help.")

            job = threading.Thread(target=runner, name=f"cmd-{name.lstrip('/').replace(' ', '_')}-{user_id}", daemon=True)
            self.jobs[key] = job
            job.start()

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

    @staticmethod
    def menu_keyboard() -> dict:
        return {"inline_keyboard": [
            [{"text": "💳 Балансы площадок", "callback_data": "nav:balances"},
             {"text": "📊 Статус", "callback_data": "nav:status"}],
            [{"text": "⚙️ Настройки поиска", "callback_data": "nav:settings"}],
            [{"text": "🤖 AutoBuy", "callback_data": "nav:autobuy"}],
            [{"text": "🔎 Проверить сейчас", "callback_data": "nav:check"}],
        ]}

    @staticmethod
    def back_keyboard(refresh: str | None = None) -> dict:
        rows = []
        if refresh:
            rows.append([{"text": "🔄 Обновить", "callback_data": "nav:" + refresh}])
        rows.append([{"text": "🏠 Главное меню", "callback_data": "nav:home"}])
        return {"inline_keyboard": rows}

    def show_page(self, p: dict, chat_id: int, text: str, keyboard: dict,
                  page: str, message_id: int | None = None, new_message: bool = False) -> int | None:
        messages = p.setdefault("menu_messages", {})
        message_id = None if new_message else (message_id or messages.get(str(chat_id)))
        self.active_pages[(p["user_id"], chat_id)] = (page, message_id)
        if message_id:
            if not self.tg.edit_text(chat_id, message_id, text, keyboard):
                if not self.tg.can_recreate_screen():
                    return None
                message_id = None
        if not message_id:
            message_id = self.tg.send_screen(chat_id, text, keyboard)
        if message_id is not None:
            self.active_pages[(p["user_id"], chat_id)] = (page, message_id)
            if messages.get(str(chat_id)) != message_id:
                messages[str(chat_id)] = message_id
                self.state.save()
        return message_id

    def show_menu(self, p: dict, chat_id: int, message_id: int | None = None, new_message: bool = False) -> None:
        self.awaiting.pop(chat_id, None)
        name = p.get("name") or "друг"
        if name == "owner":
            name = "владелец"
        enabled = chat_id in p.get("chats", [])
        ab = p.get("autobuy") or {}
        ab_line = "🤖 <b>AutoBuy</b>: " + ("включён 🟢" if ab.get("enabled") else "выключен 🔴")
        text = ("🏠 <b>Главное меню</b>\n\n"
                + "Привет, " + html.escape(name) + "!\n"
                + ("🔔 Уведомления о новых аккаунтах включены." if enabled else "🔕 Уведомления в этом чате выключены. Включить: /start")
                + "\n" + ab_line
                + "\n\n💳 <b>Балансы</b> — деньги на обеих площадках."
                + "\n⚙️ <b>Настройки</b> — страна, контакты, спамблок и цена."
                + "\n🤖 <b>AutoBuy</b> — автопокупка новых лотов по своим условиям."
                + "\n📊 <b>Статус</b> — текущие условия поиска и результаты."
                + "\n🔎 <b>Проверить сейчас</b> — показать подходящие аккаунты.")
        self.show_page(p, chat_id, text, self.menu_keyboard(), "home", message_id, new_message=new_message)

    def show_balances(self, p: dict, chat_id: int, message_id: int | None = None) -> None:
        message_id = self.show_page(p, chat_id, "💳 <b>Балансы площадок</b>\n\nЗапрашиваю актуальные данные…",
                       self.back_keyboard(), "balances", message_id)
        if message_id is None:
            return
        name = f"/balances:{chat_id}:{message_id}"
        with self.jobs_lock:
            job = self.jobs.get((p["user_id"], name))
            if job is None or not job.is_alive():
                self.run_in_background(p["user_id"], name, chat_id, self.load_balances, p, chat_id, message_id)

    def load_balances(self, p: dict, chat_id: int, message_id: int | None = None) -> None:
        lines = ["💳 <b>Балансы площадок</b>"]
        for key, label in (("lzt_token", "lzt.market"), ("tron_token", "tronaccs")):
            token = p.get(key)
            if not token:
                value = "Не подключена — добавьте ключ доступа через /token"
            else:
                try:
                    client = LztClient(token) if key == "lzt_token" else TronClient(token, "", {}, self.cfg.tron_category, 1)
                    value = client.balance() or "Баланс недоступен"
                except Exception as exc:
                    log.exception("Ошибка получения баланса %s", label)
                    value = user_error(exc)
            lines.append("\n<b>" + label + "</b>\n" + html.escape(value))
        lines.append("\nЭто отдельные балансы площадок. Деньги между ними не объединяются.")
        with self.flow_lock:
            if self.profile(p["user_id"]) is not p or self.active_pages.get((p["user_id"], chat_id)) != ("balances", message_id):
                return
            self.show_page(p, chat_id, "\n".join(lines), self.back_keyboard("balances"), "balances", message_id)

    def handle_navigation(self, cq: dict) -> None:
        uid = (cq.get("from") or {}).get("id")
        p = self.profile(uid)
        msg = cq.get("message") or {}
        chat_id, message_id = (msg.get("chat") or {}).get("id"), msg.get("message_id")
        if p is None or chat_id is None:
            self.tg.answer_callback(cq.get("id", ""), "Нет доступа к этому боту.", alert=True)
            return
        self.tg.answer_callback(cq.get("id", ""))
        self.awaiting.pop(chat_id, None)
        action = (cq.get("data") or "").split(":", 1)[-1]
        if action == "settings":
            self.show_settings(p, chat_id, message_id)
        elif action == "autobuy":
            self.show_autobuy(p, chat_id, message_id)
        elif action == "status":
            self.cmd_status(p, self.runtime(uid), chat_id, message_id)
        elif action == "balances":
            self.show_balances(p, chat_id, message_id)
        elif action == "check":
            self.start_check(p, self.runtime(uid), chat_id, message_id)
        else:
            self.show_menu(p, chat_id, message_id)

    # --- меню /settings ---

    def current_settings(self, p: dict) -> dict:
        s = p.get("settings") or {}
        if not s:
            s = settings_from_query(p.get("query") or "")
            s.update({"lzt": p.get("lzt_enabled", True), "tron": p.get("tron_enabled", True)})
            p["settings"] = s
        return s

    @staticmethod
    def _price_text(s: dict) -> str:
        return price_text(s)

    @classmethod
    def _label(cls, s: dict) -> str:
        return criteria(s)

    def settings_keyboard(self, p: dict, view: str = "main") -> dict:
        s = self.current_settings(p)
        if view == "country":
            rows, row = [], []
            for c in self.COUNTRIES:
                row.append({"text": ("✅ " if s["country"] == c else "") + country_name(c), "callback_data": f"set:country:{c}"})
                if len(row) == 2:
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
        country = country_name(s["country"])
        spam = {"no": "нет", "yes": "есть", "any": "любой"}[s["spam"]]
        return {"inline_keyboard": [
            [{"text": f"🌍 Страна: {country}", "callback_data": "set:view:country"}],
            [{"text": f"👥 Контактов от: {s['contacts']}", "callback_data": "set:view:contacts"}],
            [{"text": f"🚫 Спамблок: {spam}", "callback_data": "set:view:spam"}],
            [{"text": f"💰 Цена: {self._price_text(s)}", "callback_data": "set:view:price"}],
            [{"text": ("✅" if s.get("lzt", True) else "☐") + " lzt.market", "callback_data": "set:site:lzt"},
             {"text": ("✅" if s.get("tron", True) else "☐") + " tronaccs", "callback_data": "set:site:tron"}],
            [{"text": "💾 Сохранить условия", "callback_data": "set:apply:0"}],
            [{"text": "🏠 Главное меню", "callback_data": "nav:home"}],
        ]}

    def settings_text(self, p: dict, view: str = "main") -> str:
        s = self.current_settings(p)
        hints = {"main": "\n\nНажмите на строку, чтобы изменить. Затем нажмите «Сохранить условия».",
                 "country": "\n\nВыберите страну аккаунта.", "contacts": "\n\nМинимальное число контактов.",
                 "spam": "\n\nСпамблок на аккаунте.",
                 "price": "\n\nЦена в рублях. На tronaccs считается с комиссией."}
        return "⚙️ <b>Настройки поиска</b>\n\n" + html.escape(self._label(s)) + hints.get(view, "")

    def show_settings(self, p: dict, chat_id: int, message_id: int | None = None, view: str = "main") -> None:
        self.show_page(p, chat_id, self.settings_text(p, view), self.settings_keyboard(p, view), "settings", message_id)

    def apply_settings(self, p: dict, chat_id: int, message_id: int | None = None) -> None:
        s = self.current_settings(p)
        rt = self.runtime(p["user_id"])
        if s.get("price_min") is not None and s.get("price_max") is not None and s["price_min"] > s["price_max"]:
            self.show_page(p, chat_id, "⚠️ Минимальная цена больше максимальной. Исправьте диапазон цены.",
                           self.settings_keyboard(p, "price"), "settings", message_id)
            return
        p["lzt_enabled"] = bool(s.get("lzt", True))
        p["tron_enabled"] = bool(s.get("tron", True))
        changed = False
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
            new_query = "&".join(f"{k}={v}" for k, v in params)
            # Память лотов сбрасываем только если фильтр реально изменился: иначе повторное
            # «Сохранить» делало следующую проверку молчаливой и глотало всё, что появилось за минуту
            if (p.get("category"), p.get("query")) != ("telegram", new_query):
                p["category"], p["query"] = "telegram", new_query
                self.state.reset_seen(p, "lzt")
                changed = True
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
            new_filter = " ".join(parts)
            if (p.get("tron_filter") or "") != new_filter:
                p["tron_filter"] = new_filter
                self.state.reset_seen(p, "tron")
                changed = True
        self.state.save()
        if rt:
            rt.rebuild()
        log.info("Пользователь %s применил настройки: %s", p["user_id"], s)
        if rt:
            for key in ("last_items", "tron_items"):
                rt.stats[key] = None
            for name in ("lzt", "tron"):
                rt.stats.pop(name + "_last_ok", None)
                rt.stats.pop(name + "_checked_at", None)
        self.show_page(p, chat_id, "✅ <b>Условия поиска сохранены</b>\n\n"
                       + html.escape(self._label(s))
                       + ("\n\nСтарые аккаунты будут учтены при следующей проверке. Уведомления придут о новых."
                          if changed else "\n\nУсловия не изменились, слежу дальше без пропусков."),
                       self.menu_keyboard(), "saved", message_id)

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
                self.show_page(p, chat_id, "🌍 <b>Выбор страны</b>\n\nПришлите код страны двумя буквами, например <code>UZ</code> — Узбекистан.",
                               {"inline_keyboard": [[{"text": "← К настройкам", "callback_data": "nav:settings"}]]}, "settings", message_id)
                return
            s["country"] = "any" if value.lower() == "any" else value.upper()
            self.state.save()
            self.show_settings(p, chat_id, message_id, "main")
        elif kind == "contacts":
            if value == "ask":
                self.awaiting[chat_id] = (user_id, "contacts")
                self.tg.answer_callback(cq_id)
                self.show_page(p, chat_id, "👥 <b>Количество контактов</b>\n\nПришлите минимальное число контактов, например <code>150</code>.",
                               {"inline_keyboard": [[{"text": "← К настройкам", "callback_data": "nav:settings"}]]}, "settings", message_id)
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
                self.show_page(p, chat_id, ("Пришлите минимальную цену в рублях, например <code>100</code>."
                                            if kind == "price_min" else "Пришлите максимальную цену в рублях, например <code>500</code>.")
                               + "\n0 — убрать это ограничение.",
                               {"inline_keyboard": [[{"text": "← К настройкам", "callback_data": "nav:settings"}]]}, "settings", message_id)
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
            self.apply_settings(p, chat_id, message_id)
        self.tg.answer_callback(cq_id)

    # --- меню /autobuy ---

    def autobuy_state(self, p: dict) -> dict:
        ab = autobuy_upgrade(p.setdefault("autobuy", {}))
        if not ab.get("settings"):
            reg = self.current_settings(p)
            ab["settings"] = {
                "country": reg.get("country", "UZ"),
                "contacts": reg.get("contacts", 100),
                "spam": reg.get("spam", "no"),
                "price_min": reg.get("price_min"),
                "price_max": reg.get("price_max"),
                "lzt": True,
                "tron": False,
            }
        else:
            # Дополняем недостающие ключи значениями по умолчанию
            for k, v in AUTOBUY_DEFAULT_SETTINGS.items():
                ab["settings"].setdefault(k, v)
        return ab

    def autobuy_text(self, p: dict, view: str = "main") -> str:
        ab = self.autobuy_state(p)
        s = ab["settings"]
        status = "включён 🟢" if ab["enabled"] else "выключен 🔴"
        lines = [f"🤖 <b>AutoBuy</b> — {status}"]
        if view == "main":
            lines.append(f"🏪 Площадки: <b>{autobuy_sources_label(ab)}</b>")
            lines.append(html.escape(self._label(s)))
            lines.append("")
            lines.append("Бот сам купит новый лот, подходящий под условия.")
            lines.append("⚠️ Покупки реальные — не ставьте слишком широкий фильтр.")
            if ab["enabled"]:
                if "lzt" in ab["sources"] and not p.get("lzt_token"):
                    lines.append("⚠️ Токен lzt.market не задан — на этой площадке AutoBuy работать не будет (/token lzt).")
                if "tron" in ab["sources"] and not p.get("tron_token"):
                    lines.append("⚠️ Токен tronaccs не задан — на этой площадке AutoBuy работать не будет (/token tron).")
                if s.get("price_max") is None:
                    lines.append("⚠️ Лимит цены не задан — можно купить дорогой лот. Задайте «Цена» сверху.")
        hints = {"country": "\n\nКод страны двумя буквами.",
                 "contacts": "\n\nМинимальное число контактов.",
                 "spam": "\n\nСпамблок на аккаунте.",
                 "price": "\n\nМаксимальная цена — это ваш лимит на автопокупку.",
                 "source": "\n\nС каких площадок покупать. Можно отметить обе."}
        lines.append(hints.get(view, ""))
        return "\n".join(lines)

    def autobuy_keyboard(self, p: dict, view: str = "main") -> dict:
        ab = self.autobuy_state(p)
        s = ab["settings"]
        if view == "source":
            return {"inline_keyboard": [
                [{"text": ("✅ " if "lzt" in ab["sources"] else "☐ ") + "lzt.market",
                  "callback_data": "ab:source:lzt"}],
                [{"text": ("✅ " if "tron" in ab["sources"] else "☐ ") + "tronaccs",
                  "callback_data": "ab:source:tron"}],
                [{"text": "← Назад", "callback_data": "ab:menu:0"}],
            ]}
        if view == "country":
            rows, row = [], []
            for c in self.COUNTRIES:
                row.append({"text": ("✅ " if s["country"] == c else "") + country_name(c),
                            "callback_data": f"ab:country:{c}"})
                if len(row) == 2:
                    rows.append(row); row = []
            if row:
                rows.append(row)
            rows.append([{"text": ("✅ " if s["country"] == "any" else "") + "Любая",
                          "callback_data": "ab:country:any"},
                         {"text": "✏️ Другая", "callback_data": "ab:country:ask"}])
            rows.append([{"text": "← Назад", "callback_data": "ab:menu:0"}])
            return {"inline_keyboard": rows}
        if view == "contacts":
            row = [{"text": ("✅ " if s["contacts"] == n else "") + str(n),
                    "callback_data": f"ab:contacts:{n}"} for n in self.CONTACTS]
            return {"inline_keyboard": [row[:4], row[4:],
                    [{"text": "✏️ Другое число", "callback_data": "ab:contacts:ask"},
                     {"text": "← Назад", "callback_data": "ab:menu:0"}]]}
        if view == "spam":
            return {"inline_keyboard": [[
                {"text": ("✅ " if s["spam"] == "no" else "") + "Без спамблока", "callback_data": "ab:spam:no"},
                {"text": ("✅ " if s["spam"] == "yes" else "") + "Со спамблоком", "callback_data": "ab:spam:yes"},
                {"text": ("✅ " if s["spam"] == "any" else "") + "Любой", "callback_data": "ab:spam:any"},
            ], [{"text": "← Назад", "callback_data": "ab:menu:0"}]]}
        if view == "price":
            lo, hi = s.get("price_min"), s.get("price_max")
            row = [{"text": ("✅ " if hi == n else "") + f"до {n}",
                    "callback_data": f"ab:price_max:{n}"} for n in self.PRICES]
            return {"inline_keyboard": [
                row[:3], row[3:],
                [{"text": f"✏️ От… ({lo if lo is not None else 'нет'})", "callback_data": "ab:price_min:ask"},
                 {"text": f"✏️ До… ({hi if hi is not None else 'нет'})", "callback_data": "ab:price_max:ask"}],
                [{"text": ("✅ " if lo is None and hi is None else "") + "Без ограничения",
                  "callback_data": "ab:price_clear:0"},
                 {"text": "← Назад", "callback_data": "ab:menu:0"}],
            ]}
        # main
        toggle = "🔴 Выключить AutoBuy" if ab["enabled"] else "🟢 Включить AutoBuy"
        spam = {"no": "нет", "yes": "есть", "any": "любой"}.get(s.get("spam"), "любой")
        return {"inline_keyboard": [
            [{"text": toggle, "callback_data": "ab:toggle:0"}],
            [{"text": f"🏪 Площадки: {autobuy_sources_label(ab)}", "callback_data": "ab:view:source"}],
            [{"text": f"🌍 Страна: {country_name(s['country'])}", "callback_data": "ab:view:country"}],
            [{"text": f"👥 Контактов от: {s['contacts']}", "callback_data": "ab:view:contacts"}],
            [{"text": f"🚫 Спамблок: {spam}", "callback_data": "ab:view:spam"}],
            [{"text": f"💰 Цена: {self._price_text(s)}", "callback_data": "ab:view:price"}],
            [{"text": "💾 Сохранить условия", "callback_data": "ab:apply:0"}],
            [{"text": "🏠 Главное меню", "callback_data": "nav:home"}],
        ]}

    def show_autobuy(self, p: dict, chat_id: int, message_id: int | None = None,
                     view: str = "main") -> None:
        self.show_page(p, chat_id, self.autobuy_text(p, view),
                       self.autobuy_keyboard(p, view), "autobuy", message_id)

    def apply_autobuy_settings(self, p: dict, chat_id: int, message_id: int | None = None) -> None:
        ab = self.autobuy_state(p)
        s = ab["settings"]
        if s.get("price_min") is not None and s.get("price_max") is not None \
                and s["price_min"] > s["price_max"]:
            self.show_page(p, chat_id,
                           "⚠️ Минимальная цена больше максимальной.",
                           self.autobuy_keyboard(p, "price"), "autobuy", message_id)
            return
        # При смене условий забываем историю, первая проверка — тихая
        autobuy_reset(ab)
        self.state.save()
        rt = self.runtime(p["user_id"])
        if rt:
            rt.rebuild()
        self.show_page(p, chat_id,
                       "✅ <b>Условия AutoBuy сохранены</b>\n\n"
                       + html.escape(self._label(s))
                       + ("\n\nАвтопокупка <b>включена</b> 🟢." if ab["enabled"]
                          else "\n\nАвтопокупка сейчас <b>выключена</b> 🔴."),
                       self.autobuy_keyboard(p, "main"), "autobuy", message_id)

    def handle_autobuy_callback(self, cq: dict, parts: list[str]) -> None:
        cq_id = cq.get("id", "")
        user_id = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        p = self.profile(user_id)
        if p is None:
            self.tg.answer_callback(cq_id, "⛔ Нет доступа", alert=True)
            return
        _, kind, value = (parts + ["", ""])[:3]
        ab = self.autobuy_state(p)
        s = ab["settings"]
        if kind == "view":
            self.show_autobuy(p, chat_id, message_id, value)
        elif kind == "menu":
            self.awaiting.pop(chat_id, None)
            self.show_autobuy(p, chat_id, message_id, "main")
        elif kind == "toggle":
            # При попытке включить — предупреждаем, если нет токена нужной площадки
            if not ab["enabled"]:
                if not ab["sources"]:
                    self.tg.answer_callback(cq_id, "Выберите хотя бы одну площадку", alert=True)
                    return
                if "lzt" in ab["sources"] and not p.get("lzt_token"):
                    self.tg.answer_callback(cq_id, "Нет токена lzt.market (/token lzt)", alert=True)
                    return
                if "tron" in ab["sources"] and not p.get("tron_token"):
                    self.tg.answer_callback(cq_id, "Нет токена tronaccs (/token tron)", alert=True)
                    return
            ab["enabled"] = not ab["enabled"]
            if ab["enabled"]:
                ab["init"] = {site: False for site in AUTOBUY_SITES}  # первая проверка после включения — тихая
            self.state.save()
            rt = self.runtime(user_id)
            if rt:
                rt.rebuild()
            self.show_autobuy(p, chat_id, message_id, "main")
            self.tg.answer_callback(cq_id, "AutoBuy " + ("включён 🟢" if ab["enabled"] else "выключен 🔴"))
            return
        elif kind == "source":
            # Смену площадки разрешаем только при выключенном AutoBuy — так проще и безопаснее
            if ab["enabled"]:
                self.tg.answer_callback(cq_id, "Сначала выключите AutoBuy", alert=True)
                return
            if value not in AUTOBUY_SITES:
                self.tg.answer_callback(cq_id)
                return
            if value in ab["sources"]:
                ab["sources"].remove(value)
            else:
                ab["sources"].append(value)
            autobuy_reset(ab)
            self.state.save()
            rt = self.runtime(user_id)
            if rt:
                rt.rebuild()
            self.show_autobuy(p, chat_id, message_id, "source")
        elif kind == "country":
            if value == "ask":
                self.awaiting[chat_id] = (user_id, "ab_country")
                self.tg.answer_callback(cq_id)
                self.show_page(p, chat_id,
                               "🌍 Пришлите код страны двумя буквами, например <code>UZ</code>.",
                               {"inline_keyboard": [[{"text": "← К AutoBuy", "callback_data": "nav:autobuy"}]]},
                               "autobuy", message_id)
                return
            s["country"] = "any" if value.lower() == "any" else value.upper()
            autobuy_reset(ab, attempts=False)
            self.state.save()
            self.show_autobuy(p, chat_id, message_id, "main")
        elif kind == "contacts":
            if value == "ask":
                self.awaiting[chat_id] = (user_id, "ab_contacts")
                self.tg.answer_callback(cq_id)
                self.show_page(p, chat_id, "👥 Пришлите минимальное число контактов.",
                               {"inline_keyboard": [[{"text": "← К AutoBuy", "callback_data": "nav:autobuy"}]]},
                               "autobuy", message_id)
                return
            s["contacts"] = int(value)
            autobuy_reset(ab, attempts=False)
            self.state.save()
            self.show_autobuy(p, chat_id, message_id, "main")
        elif kind == "spam":
            s["spam"] = value
            autobuy_reset(ab, attempts=False)
            self.state.save()
            self.show_autobuy(p, chat_id, message_id, "main")
        elif kind in ("price_min", "price_max"):
            if value == "ask":
                self.awaiting[chat_id] = (user_id, f"ab_{kind}")
                self.tg.answer_callback(cq_id)
                self.show_page(p, chat_id,
                               "Пришлите цену в рублях. 0 — убрать ограничение.",
                               {"inline_keyboard": [[{"text": "← К AutoBuy", "callback_data": "nav:autobuy"}]]},
                               "autobuy", message_id)
                return
            s[kind] = int(value)
            autobuy_reset(ab, attempts=False)
            self.state.save()
            self.show_autobuy(p, chat_id, message_id, "price")
        elif kind == "price_clear":
            s["price_min"] = None
            s["price_max"] = None
            autobuy_reset(ab, attempts=False)
            self.state.save()
            self.show_autobuy(p, chat_id, message_id, "main")
        elif kind == "apply":
            self.apply_autobuy_settings(p, chat_id, message_id)
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
        if parts[0] == "nav":
            self.handle_navigation(cq)
            return
        if parts[0] == "set" and chat_id is not None:
            self.handle_settings_callback(cq, parts)
            return
        if parts[0] == "ab" and chat_id is not None:
            self.handle_autobuy_callback(cq, parts)
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
            # Покупка и выгрузка файлов сессии ходят к маркету и Telegram, это долго: в фон.
            # Ключ по лоту: повторное нажатие той же кнопки не купит лот дважды.
            self.run_in_background(user_id, f"покупка лота {item_id}", chat_id,
                                   self.do_buy, rt, chat_id, item, is_tron, item_url)
        else:
            self.tg.answer_callback(cq_id)

    def do_buy(self, rt: UserRuntime, chat_id: int, item: dict, is_tron: bool, item_url: str) -> None:
        item_id, price = item["item_id"], item["price"]
        client = rt.tron if is_tron else rt.lzt
        ok, text = client.buy(item_id) if is_tron else client.fast_buy(item_id, price)
        site = "tronaccs" if is_tron else "lzt.market"
        log.info("%s: покупка лота %d за %s пользователем %s: %s — %s", site, item_id, price, rt.profile["user_id"], ok, text)
        if ok:
            self.tg.send(chat_id, f"✅ {site}: куплен лот {item_id} за {self._price_label(price, None)}.\n"
                                  f"{item_url}")
            self.send_account_files(rt, chat_id, item_id, site)
        else:
            self.tg.send(chat_id, f"❌ {site}: не удалось купить лот {item_id}: {html.escape(user_error(text))}\n{item_url}",
                         self.item_keyboard(rt, item, "buy"))

    def send_account_files(self, rt: UserRuntime, chat_id: int, item_id: int, site: str,
                           client=None) -> None:
        """После покупки выгружает данные аккаунта (сессия, tdata и т.п.) файлами.

        client позволяет использовать конкретный клиент (например, для AutoBuy).
        """
        if not self.cfg.send_session_files:
            return
        if client is None:
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
        sessions = [(n, c) for n, c in files if n.endswith(".session")]
        # Если собрали готовый .session — шлём только его, без JSON и сводки
        if sessions and self.cfg.session_only:
            to_send, with_summary = sessions, False
        else:
            to_send, with_summary = files, True
        if with_summary and summary:
            self.tg.send(chat_id, f"🔑 Данные аккаунта {item_id}:\n<pre>{html.escape(summary)}</pre>")
        sent = 0
        for name, content in to_send:
            if len(content) > 49 * 1024 * 1024:  # лимит Telegram на документ
                continue
            if self.tg.send_document(chat_id, name, content, caption=f"{site}: аккаунт {item_id}"):
                sent += 1
        if sent:
            log.info("%s: выгружено %d файлов аккаунта %d пользователю %s", site, sent, item_id, rt.profile["user_id"])
        elif not sessions:
            self.tg.send(chat_id, "📎 Готовый .session собрать не удалось. Данные во вложенном JSON или на странице лота. "
                                  "Если нужен именно .session, пришлите структуру ответа (названия полей).")
        # Автозагрузка .session во внешнюю панель (evangelion и т.п.)
        prof = rt.profile
        url = prof.get("upload_url")
        if url and sessions:
            for name, content in sessions:
                ok2, info = upload_session(url, prof.get("upload_token") or "", prof.get("upload_field") or "file", name, content)
                log.info("panel upload %s: %s — %s", name, ok2, info)
                self.tg.send(chat_id, (f"⬆️ Панель: {name} загружен." if ok2
                                       else f"⚠️ Панель: {name} не загрузился. {html.escape(user_error(info))}"))

        # Автозалив архива .session в EVA TG (spammer-api)
        if sessions and prof.get("eva_auto") and prof.get("eva_token"):
            ok3, info3 = upload_to_eva_tg(
                prof.get("eva_url") or EVA_PROD_BASE,
                prof["eva_token"],
                "file",
                sessions,
                run_spam=bool(prof.get("eva_run_spam", True)),
            )
            log.info("eva tg upload для лота %s: %s — %s", item_id, ok3, info3)
            self.tg.send(chat_id, (f"📤 EVA TG: {html.escape(readable(info3))}" if ok3
                                   else f"⚠️ EVA TG: не загрузилось — {html.escape(user_error(info3))}"))

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
        if message_id:
            self.tg.delete(chat_id, message_id)
        if not text.strip():
            self.tg.send(chat_id, "Пришлите токен текстовым сообщением или /skip для отмены.")
            return
        with self.jobs_lock:
            job = self.jobs.get((p["user_id"], "/token"))
            if job is not None and job.is_alive():
                self.tg.send(chat_id, "⏳ Уже проверяю токен. Дождитесь результата.")
                return
            pending = (p["user_id"], kind)
            self.awaiting[chat_id] = pending
            self.tg.send(chat_id, "⏳ Проверяю ключ доступа… Можно отменить командой /settings.")
            self.run_in_background(p["user_id"], "/token", chat_id,
                                   self._receive_token, p, chat_id, kind, text, None, pending)

    def _receive_token(self, p: dict, chat_id: int, kind: str, text: str,
                       message_id: int | None, pending: tuple) -> None:
        token = text.strip()
        site = "lzt.market" if kind == "lzt_token" else "tronaccs"
        if kind == "lzt_token":
            client = LztClient(token)
        else:
            client = TronClient(token, "", self.state.country_ids, self.cfg.tron_category, 1)
        ok, info = client.check_token()
        with self.flow_lock:
            if self.awaiting.get(chat_id) is not pending or self.profile(p["user_id"]) is not p:
                return
            next_step = False
            if ok:
                p[kind] = token
                if kind == "lzt_token":
                    p["source"] = "api"
                    next_step = not p.get("tron_token")
                else:
                    p["tron_enabled"] = True
                if next_step:
                    self.awaiting[chat_id] = (p["user_id"], "tron_token")
                else:
                    self.awaiting.pop(chat_id, None)
                self.state.save()
        if not ok:
            self.tg.send(chat_id, f"❌ Не удалось проверить ключ {site}: " + html.escape(user_error(info))
                         + "\nПришлите ещё раз или /skip")
            return
        self.tg.send(chat_id, f"✅ Токен {site} принят: " + html.escape(info))
        if next_step:
            self.tg.send(chat_id, "Шаг 2 из 2. Пришлите токен API <b>tronaccs.market</b>.\n"
                                  "Где взять: на сайте нажмите на ник → «API TronAccs» → создать токен.\n"
                                  "Если tronaccs не нужен, напишите /skip")
        else:
            self.finish_setup(p, chat_id)

    def finish_setup(self, p: dict, chat_id: int) -> None:
        rt = self.runtime(p["user_id"])
        if rt:
            rt.rebuild()
        self.tg.send(chat_id, "Готово. Настройте поиск кнопками и нажмите «Сохранить условия»:")
        self.show_settings(p, chat_id)

    # --- обработка обновлений ---

    def handle(self, update: dict) -> None:
        with self.flow_lock:
            self._handle(update)

    def _handle(self, update: dict) -> None:
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
                kind = pending[1]
                valid = bool(text)
                if kind in ("country", "ab_country"):
                    valid = bool(re.fullmatch(r"[A-Za-z]{2}", text))
                elif kind in ("contacts", "ab_contacts"):
                    valid = bool(re.fullmatch(r"[0-9]+", text))
                elif kind in ("price_min", "price_max", "ab_price_min", "ab_price_max"):
                    digits = text.replace(" ", "")
                    valid = bool(re.fullmatch(r"[0-9]+", digits))
                elif kind == "panel_url":
                    valid = text.startswith(("http://", "https://"))
                else:
                    # An isolated symbol is navigation, not an opaque access key.
                    valid = len(text) > 1 and any(c.isalnum() for c in text)
                if valid:
                    self.handle_awaiting(p, chat_id, kind, text, message_id)
                else:
                    self.show_menu(p, chat_id, new_message=True)
            elif p is not None:
                self.show_menu(p, chat_id, new_message=True)
            return

        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Always allow a registered user to leave an unfinished input flow.
        # Opening the menu must not wait for marketplace client initialization.
        if p is not None and command in ("/start", "/menu"):
            if command == "/start" and chat_id not in p["chats"]:
                p["chats"].append(chat_id)
                self.state.save()
            self.show_menu(p, chat_id, new_message=True)
            return

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

        if command == "/settings":
            self.show_settings(p, chat_id)
        elif command == "/menu":
            self.show_menu(p, chat_id, new_message=True)
        elif command in ("/balance", "/balances"):
            self.show_balances(p, chat_id)
        elif command == "/autobuy":
            self.show_autobuy(p, chat_id)
        elif command == "/start":
            if chat_id not in p["chats"]:
                p["chats"].append(chat_id)
                self.state.save()
            self.show_menu(p, chat_id, new_message=True)
        elif command == "/stop":
            if chat_id in p["chats"]:
                p["chats"].remove(chat_id)
                self.state.save()
                self.show_menu(p, chat_id, new_message=True)
            else:
                self.show_menu(p, chat_id, new_message=True)
        elif command == "/help":
            self.show_page(p, chat_id, self.HELP + (self.OWNER_HELP if self.is_owner(user_id) else ""),
                           self.back_keyboard(), "help")
        elif command == "/token":
            self.cmd_token(p, chat_id, arg, message_id)
        elif command == "/panel":
            self.cmd_panel(p, chat_id, arg, message_id)
        elif command == "/eva":
            self.cmd_eva(p, chat_id, arg, message_id)
        elif command == "/filter":
            self.cmd_filter(p, chat_id, arg)
        elif command == "/check":
            self.start_check(p, rt, chat_id)
        elif command == "/status":
            self.cmd_status(p, rt, chat_id)
        elif command == "/tron":
            self.cmd_tron(p, rt, chat_id, arg)
        elif command == "/tronfilter":
            self.cmd_tronfilter(p, rt, chat_id, arg)
        elif command == "/tronid":
            self.run_in_background(user_id, command, chat_id, self.cmd_tronid, p, rt, chat_id, arg)
        elif command == "/lztdump":
            self.run_in_background(user_id, command, chat_id, self.cmd_lztdump, p, rt, chat_id)
        elif command == "/lot":
            self.run_in_background(user_id, command, chat_id, self.cmd_lot, p, rt, chat_id, arg)
        elif command == "/trondump":
            self.run_in_background(user_id, command, chat_id, self.cmd_trondump, p, rt, chat_id)
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
                ab = p2.get("autobuy") or {}
                ab_mark = (" 🤖🟢" if ab.get("enabled") else "")
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
            self.show_menu(p, chat_id, new_message=True)

    def handle_awaiting(self, p: dict, chat_id: int, kind: str, text: str, message_id: int | None) -> None:
        if kind == "eva_token":
            token = text.strip()
            if message_id:
                self.tg.delete(chat_id, message_id)
            p["eva_token"] = token
            p["eva_auto"] = True
            self.awaiting.pop(chat_id, None)
            self.state.save()
            self.tg.send(chat_id, "✅ Токен EVA TG сохранён, автозалив включён.\nПроверить: /eva")
            return
        if kind.startswith("ab_"):
            ab = self.autobuy_state(p)
            s = ab["settings"]
            sub = kind[3:]
            if sub == "country":
                code = text.strip().upper()
                if not re.fullmatch(r"[A-Z]{2}", code):
                    self.tg.send(chat_id, "Нужен код из двух латинских букв, например UZ.")
                    return
                s["country"] = code
            elif sub == "contacts":
                if not text.strip().isdigit():
                    self.tg.send(chat_id, "Нужно число.")
                    return
                s["contacts"] = int(text.strip())
            elif sub in ("price_min", "price_max"):
                digits = text.strip().replace(" ", "")
                if not digits.isdigit():
                    self.tg.send(chat_id, "Нужно число в рублях. 0 — без ограничения.")
                    return
                s[sub] = int(digits) or None
            autobuy_reset(ab)
            self.awaiting.pop(chat_id, None)
            self.state.save()
            rt = self.runtime(p["user_id"])
            if rt:
                rt.rebuild()
            self.show_autobuy(p, chat_id)
            return
        if kind in ("lzt_token", "tron_token"):
            self.receive_token(p, chat_id, kind, text, message_id)
            return
        if kind in ("panel_url", "panel_token"):
            value = text.strip()
            if kind == "panel_url":
                if not value.startswith(("http://", "https://")):
                    self.tg.send(chat_id, "Нужен адрес, начинающийся с http. Или /panel для отмены.")
                    return
                p["upload_url"] = value
                msg = "✅ Адрес панели сохранён."
            else:
                p["upload_token"] = value
                msg = "✅ Токен панели сохранён."
            self.awaiting.pop(chat_id, None)
            self.state.save()
            self.tg.send(chat_id, msg + " Текущие настройки: /panel")
            return
        s = self.current_settings(p)
        if kind == "country":
            code = text.strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", code):
                self.show_page(p, chat_id, "Нужен код из двух латинских букв, например UZ. Или /settings для отмены.", self.back_keyboard(), "settings")
                return
            s["country"] = code
        elif kind == "contacts":
            if not text.strip().isdigit():
                self.show_page(p, chat_id, "Нужно число, например 150. Или /settings для отмены.", self.back_keyboard(), "settings")
                return
            s["contacts"] = int(text.strip())
        elif kind in ("price_min", "price_max"):
            digits = text.strip().replace(" ", "")
            if not digits.isdigit():
                self.show_page(p, chat_id, "Нужно число в рублях, например 500. 0 — убрать ограничение. Или /settings для отмены.", self.back_keyboard(), "settings")
                return
            s[kind] = int(digits) or None
        self.awaiting.pop(chat_id, None)
        self.state.save()
        self.show_settings(p, chat_id)

    def cmd_panel(self, p: dict, chat_id: int, arg: str, message_id: int | None) -> None:
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts is not None and len(parts) > 0 and parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            url = p.get("upload_url") or "не задан"
            self.tg.send(chat_id,
                         "⬆️ Автозагрузка .session во внешнюю панель\n"
                         f"Адрес: <code>{html.escape(url)}</code>\n"
                         f"Токен: <code>{html.escape(_mask(p.get('upload_token')))}</code>\n"
                         f"Поле файла: <code>{html.escape(p.get('upload_field') or 'file')}</code>\n\n"
                         "Настроить:\n"
                         "/panel url &lt;адрес&gt; — куда слать файл\n"
                         "/panel token &lt;токен&gt; — ключ доступа панели\n"
                         "/panel field &lt;имя&gt; — имя поля с файлом (по умолчанию file)\n"
                         "/panel off — выключить\n\n"
                         "Точный адрес и поле возьмите из панели: F12 → Network, загрузите там сессию вручную, "
                         "правой кнопкой по запросу → Copy → Copy as cURL, и пришлите мне.")
            return
        if sub == "off":
            p["upload_url"] = ""
            self.state.save()
            self.tg.send(chat_id, "🔕 Автозагрузка в панель выключена.")
        elif sub == "url" and rest:
            if not rest.startswith(("http://", "https://")):
                self.tg.send(chat_id, "Адрес должен начинаться с http.")
                return
            p["upload_url"] = rest
            self.state.save()
            self.tg.send(chat_id, "✅ Адрес панели сохранён. Проверить: /panel")
        elif sub == "url":
            self.awaiting[chat_id] = (p["user_id"], "panel_url")
            self.tg.send(chat_id, "Пришлите адрес загрузки (URL) панели одним сообщением.")
        elif sub == "token":
            if rest:
                if message_id:
                    self.tg.delete(chat_id, message_id)
                p["upload_token"] = rest
                self.state.save()
                self.tg.send(chat_id, "✅ Токен панели сохранён.")
            else:
                self.awaiting[chat_id] = (p["user_id"], "panel_token")
                self.tg.send(chat_id, "Пришлите токен панели одним сообщением.")
        elif sub == "field" and rest:
            p["upload_field"] = rest
            self.state.save()
            self.tg.send(chat_id, f"✅ Поле файла: {html.escape(rest)}")
        else:
            self.tg.send(chat_id, "Не понял. /panel — показать настройки и подсказку.")

    def cmd_eva(self, p: dict, chat_id: int, arg: str, message_id: int | None) -> None:
        parts = arg.split(maxsplit=1) if arg else []
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            self.tg.send(chat_id,
                         "📤 <b>EVA TG (spammer-api)</b>\n"
                         f"auto: {'on ✅' if p.get('eva_auto') else 'off'}\n"
                         f"env: <code>{html.escape(p.get('eva_env') or 'prod')}</code>\n"
                         f"run_spam: {'on ✅' if p.get('eva_run_spam') else 'off'}\n"
                         f"token: <code>{html.escape(_mask(p.get('eva_token')))}</code>\n\n"
                         "Команды:\n"
                         "/eva token &lt;X-EVANGELION&gt;\n"
                         "/eva auto on|off\n"
                         "/eva env prod|dev\n"
                         "/eva run on|off  (on = upload-run + спам, off = только залив)\n"
                         "/eva url &lt;база&gt;  (переопределить URL)\n\n"
                         "При включённом auto каждый купленный .session после покупки\n"
                         "автоматически упакуется в zip и уйдёт в EVA TG.")
            return
        if sub == "token":
            if rest:
                if message_id:
                    self.tg.delete(chat_id, message_id)
                p["eva_token"] = rest
                p["eva_auto"] = True
                self.state.save()
                self.tg.send(chat_id, "✅ Токен EVA TG сохранён, автозалив включён.")
            else:
                self.awaiting[chat_id] = (p["user_id"], "eva_token")
                self.tg.send(chat_id, "Пришлите X-EVANGELION токен одним сообщением.")
        elif sub == "auto":
            p["eva_auto"] = rest.lower() == "on"
            self.state.save()
            self.tg.send(chat_id, f"✅ Автозалив EVA TG: {'on' if p['eva_auto'] else 'off'}")
        elif sub == "env":
            if rest not in ("prod", "dev"):
                self.tg.send(chat_id, "prod или dev")
                return
            p["eva_env"] = rest
            p["eva_url"] = EVA_PROD_BASE if rest == "prod" else EVA_DEV_BASE
            self.state.save()
            self.tg.send(chat_id, f"✅ env: {rest}\nURL: <code>{html.escape(p['eva_url'])}</code>")
        elif sub == "run":
            p["eva_run_spam"] = rest.lower() == "on"
            self.state.save()
            self.tg.send(chat_id, f"✅ run_spam: {'on' if p['eva_run_spam'] else 'off'}")
        elif sub == "url" and rest:
            p["eva_url"] = rest
            self.state.save()
            self.tg.send(chat_id, "✅ URL сохранён")
        else:
            self.tg.send(chat_id, "Не понял. /eva без аргументов — показать настройки.")

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
            self.tg.send(chat_id, "⚠️ " + html.escape(user_error(exc)))
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
            self.tg.send(chat_id, "⚠️ " + html.escape(user_error(exc)))
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
            self.tg.send(chat_id, "⚠️ " + html.escape(user_error(exc)))
            return
        if cid is None:
            self.tg.send(chat_id, f"Не нашёл ID для {code}. Можно подсмотреть на сайте и вписать: /tronid {code} <число>")
            return
        with self.state.lock:
            self.state.country_ids[code] = cid
            self.state.save()
        rt.rebuild()
        self.tg.send(chat_id, f"✅ {code} = ID {cid}. Запомнил.")

    def acquire_check(self, rt: UserRuntime, chat_id: int, wait: float = 20) -> bool:
        """Ждём, пока закончится плановая проверка этого пользователя (если она идёт прямо сейчас)."""
        if rt.lock.acquire(timeout=wait):
            return True
        err = rt.stats.get("last_error")
        self.tg.send(chat_id, "⏳ Сейчас идёт плановая проверка, сайт отвечает медленно. Попробуйте через минуту."
                     + (f"\nПоследняя ошибка ({_ago(rt.stats['last_error_at'])}): {html.escape(user_error(err))}" if err else ""))
        return False

    def start_check(self, p: dict, rt: UserRuntime | None, chat_id: int, message_id: int | None = None) -> None:
        message_id = self.show_page(p, chat_id, "🔎 <b>Проверка аккаунтов</b>\n\nЗапрашиваю данные площадок…",
                                    self.back_keyboard(), "check", message_id)
        if message_id is None:
            return
        name = f"/check:{chat_id}"
        with self.jobs_lock:
            job = self.jobs.get((p["user_id"], name))
            if job is None or not job.is_alive():
                self.run_in_background(p["user_id"], name, chat_id, self.cmd_check, p, rt, chat_id, message_id)

    def cmd_check(self, p: dict, rt: UserRuntime | None, chat_id: int, message_id: int | None = None) -> None:
        if rt is None:
            return
        lines = ["🔎 <b>Результат проверки</b>"]
        rows = []
        to_send: list[dict] = []
        if not rt.lock.acquire(timeout=20):
            lines.append("\nПлановая проверка ещё выполняется. Попробуйте через минуту.")
        else:
            try:
                for name, label, client in (("lzt", "lzt.market", rt.lzt), ("tron", "tronaccs", rt.tron)):
                    lines.append("\n<b>" + label + "</b>")
                    if client is None:
                        lines.append("Поиск выключен или площадка не подключена.")
                        continue
                    client.last_error = None
                    items = client.fetch_items(p["category"], lzt_params(p["query"]), retries=1) if name == "lzt" else client.fetch_items(retries=1)
                    error = client.last_error
                    rt.record_error(error)
                    rt.stats[name + "_last_ok"] = not error
                    rt.stats[name + "_last_error"] = error
                    rt.stats[name + "_checked_at"] = time.time()
                    rt.stats["last_items" if name == "lzt" else "tron_items"] = len(items)
                    rt.stats["last_new" if name == "lzt" else "tron_new"] = sum(int(i["item_id"]) not in p["seen"][name] for i in items)
                    if name == "tron":
                        rt.stats["tron_total"] = client.last_total
                    if error:
                        lines.append("⚠️ " + html.escape(user_error(error)))
                        continue
                    lines.append(f"Подходящих в полученной выборке: <b>{len(items)}</b>")
                    if not items:
                        lines.append("Ничего не найдено. Проверьте страну, контакты и ограничение цены.")
                        continue
                    latest = sorted(items, key=lambda i: int(i.get("published_date") or 0), reverse=True)[:CHECK_SHOW_ITEMS]
                    lines.append(f"Последние {len(latest)} — отдельными сообщениями ниже, с кнопкой «Купить».")
                    to_send.extend(latest)
            finally:
                rt.lock.release()
        rows.extend(self.menu_keyboard()["inline_keyboard"])
        with self.flow_lock:
            if self.profile(p["user_id"]) is p and self.active_pages.get((p["user_id"], chat_id)) == ("check", message_id):
                self.show_page(p, chat_id, "\n".join(lines), {"inline_keyboard": rows}, "check", message_id)
        # Сами лоты — обычными сообщениями в чат, как при плановой проверке: с полным описанием и кнопками
        for item in to_send:
            self.tg.send(chat_id, format_item(item), self.item_keyboard(rt, item))

    def cmd_lot(self, p: dict, rt: UserRuntime | None, chat_id: int, arg: str) -> None:
        """Диагностика одного лота lzt: видит ли его API вообще, попадает ли он под фильтр,
        и не лежит ли он уже в памяти бота как показанный."""
        m = re.search(r"(\d{3,})", arg or "")
        if not m:
            self.tg.send(chat_id, "Укажите номер лота или ссылку: <code>/lot 259639880</code>")
            return
        item_id = int(m.group(1))
        if not rt or not isinstance(rt.lzt, LztClient):
            self.tg.send(chat_id, "Для этой проверки нужен токен API lzt.market (/token lzt).")
            return
        lines = [f"🔍 <b>Лот {item_id}</b>", f"🔗 {MARKET_URL}/{item_id}/"]
        in_seen = item_id in p["seen"]["lzt"]
        ab = p.get("autobuy") or {}
        in_ab_seen = item_id in ((ab.get("seen") or {}).get("lzt") or []) if isinstance(ab.get("seen"), dict) else False
        if not self.acquire_check(rt, chat_id):
            return
        try:
            detail = rt.lzt.account_data(item_id)
            rt.lzt.last_error = None
            by_filter = rt.lzt.fetch_items(p["category"], lzt_params(p["query"]), retries=1)
            err_filter, cached = rt.lzt.last_error, rt.lzt.last_meta.get("wasCached")
            rt.lzt.last_error = None
            no_filter = rt.lzt.fetch_items(p["category"], [("order_by", "pdate_to_down")], retries=1)
            err_all = rt.lzt.last_error
        finally:
            rt.lock.release()
        in_filter = any(int(i.get("item_id") or 0) == item_id for i in by_filter)
        in_all = any(int(i.get("item_id") or 0) == item_id for i in no_filter)
        if isinstance(detail, dict) and detail.get("item_id"):
            lines.append("\n<b>Как его видит API:</b>")
            lines.append(format_item(detail))
            raw = {k: detail.get(k) for k in ("item_state", "telegram_country", "telegram_contacts", "telegram_contacts_count",
                                              "telegram_spam_block", "price", "published_date", "category_id") if k in detail}
            lines.append("<code>" + html.escape(json.dumps(raw, ensure_ascii=False)) + "</code>")
        else:
            lines.append("\n⚠️ API не отдаёт данные этого лота (нет такого номера, лот скрыт или ещё не появился в API).")
        lines.append("\n<b>Где он есть:</b>")
        lines.append(("✅" if in_filter else "❌") + " в выдаче по вашему фильтру"
                     + (f" (ошибка: {html.escape(user_error(err_filter))})" if err_filter else "")
                     + (" — ответ из кэша" if cached else ""))
        lines.append(("✅" if in_all else "❌") + " в общей выдаче категории без фильтра, первая страница"
                     + (f" (ошибка: {html.escape(user_error(err_all))})" if err_all else ""))
        lines.append(("✅" if in_seen else "❌") + " в памяти бота как уже показанный")
        if ab.get("enabled"):
            lines.append(("✅" if in_ab_seen else "❌") + " в памяти AutoBuy как уже обработанный")
        lines.append("\n<b>Вывод:</b>")
        if in_filter and in_seen:
            lines.append("Бот его видит, но уже показывал раньше, поэтому молчит. Новые лоты присылаются один раз.")
        elif in_filter:
            lines.append("Бот его видит и ещё не показывал: придёт на ближайшей плановой проверке.")
        elif in_all:
            lines.append("API отдаёт лот, но ваш фильтр его отсекает. Сравните поля выше с условиями в /settings: "
                         "страна, контакты, спамблок, цена. Спамблок «неизвестно» под «без спамблока» не подходит.")
        elif isinstance(detail, dict) and detail.get("item_id"):
            lines.append("Лот в API есть, но в списках его ещё нет: у lzt список обновляется с задержкой, "
                         "либо лот на проверке (см. item_state). Обычно появляется через несколько минут.")
        else:
            lines.append("API этого лота не знает. Проверьте номер; если на сайте он есть, у API отставание.")
        self.tg.send(chat_id, "\n".join(lines))

    def cmd_lztdump(self, p: dict, rt: UserRuntime | None, chat_id: int) -> None:
        if not rt or rt.lzt is None:
            self.tg.send(chat_id, "lzt.market: нет токена. Добавить: /token lzt")
            return
        if not self.acquire_check(rt, chat_id):
            return
        try:
            items = rt.lzt.fetch_items(p["category"], lzt_params(p["query"]), retries=1)
        finally:
            rt.lock.release()
        if not items:
            self.tg.send(chat_id, "lzt.market: лотов нет или сайт не ответил"
                         + (f": {html.escape(user_error(rt.lzt.last_error))}" if rt.lzt.last_error else "."))
            return
        raw = {k: v for k, v in items[0].items() if not isinstance(v, (dict, list))}
        self.tg.send(chat_id, "lzt.market: первый лот как отдаёт API:\n<pre>"
                     + html.escape(json.dumps(raw, ensure_ascii=False, indent=1)[:3500]) + "</pre>")

    def cmd_trondump(self, p: dict, rt: UserRuntime | None, chat_id: int) -> None:
        if not rt or rt.tron is None:
            self.tg.send(chat_id, "tronaccs выключен или нет токена. /tron on, /token tron")
            return
        if not self.acquire_check(rt, chat_id):
            return
        try:
            raw, count = rt.tron.fetch_raw_sample()
        except (TronError, requests.RequestException) as exc:
            self.tg.send(chat_id, "⚠️ " + html.escape(user_error(exc)))
            return
        finally:
            rt.lock.release()
        if raw is None:
            self.tg.send(chat_id, "tronaccs вернул пустой список.")
            return
        self.tg.send(chat_id, f"tronaccs: лотов на первой странице {count}. Первый как есть:\n<pre>"
                     + html.escape(json.dumps(raw, ensure_ascii=False, indent=1)[:3500]) + "</pre>")

    def cmd_status(self, p: dict, rt: UserRuntime | None, chat_id: int, message_id: int | None = None) -> None:
        if rt is None:
            self.show_menu(p, chat_id, message_id)
            return
        st = dict(rt.stats)
        lines = ["📊 <b>Статус мониторинга</b>",
                 "\n🔔 Уведомления: " + ("включены" if chat_id in p.get("chats", []) else "выключены в этом чате"),
                 f"⏱ Интервал проверки: {self.cfg.poll_interval} сек."]
        for name, label, client, count_key, new_key in (("lzt", "lzt.market", rt.lzt, "last_items", "last_new"),
                                                       ("tron", "tronaccs", rt.tron, "tron_items", "tron_new")):
            lines.append("\n<b>" + label + "</b>")
            if not p.get(name + "_enabled", True):
                lines.append("⏸ Поиск на площадке выключен")
                continue
            if client is None:
                lines.append("🔑 Площадка не подключена — добавьте ключ через /token")
                continue
            ok = st.get(name + "_last_ok")
            lines.append("✅ Последняя проверка успешна" if ok is True else
                         "⚠️ Не удалось выполнить последнюю проверку" if ok is False else "⏳ Ожидаю первую проверку по этим условиям")
            try:
                summary = lzt_criteria(p.get("query")) if name == "lzt" else tron_criteria(parse_filter(p.get("tron_filter") or ""))
            except ValueError:
                summary = "⚠️ Условия поиска не распознаны. Задайте их в настройках."
            lines.append(html.escape(summary))
            if ok is False:
                lines.append("Количество аккаунтов сейчас неизвестно.")
                error = st.get(name + "_last_error")
                if error:
                    lines.append(html.escape(user_error(error)))
            elif st.get(count_key) is not None:
                lines.append(f"Подходящих в последней проверке: <b>{st[count_key]}</b>")
                lines.append(f"Новых в этой проверке: <b>{st.get(new_key, 0)}</b>")
                if st[count_key] == 0:
                    lines.append("По этим условиям ничего не найдено. Проверьте ограничение цены.")
            checked_at = st.get(name + "_checked_at")
            if checked_at:
                lines.append("Проверено: " + _ago(checked_at))
        # >>> AutoBuy <<<
        ab = p.get("autobuy") or {}
        ab_on = bool(ab.get("enabled"))
        lines.append("\n🤖 <b>AutoBuy</b>: " + ("включён 🟢" if ab_on else "выключен 🔴")
                     + (f" — {autobuy_sources_label(ab)}" if ab_on else ""))
        if ab_on:
            ab_s = ab.get("settings") or {}
            try:
                lines.append(html.escape(self._label(ab_s)))
            except Exception:
                pass
            for site in ab.get("sources") or []:
                r = st.get(f"ab_{site}")
                if not r:
                    lines.append(f"   {AUTOBUY_SITE_LABELS[site]}: ещё не проверял")
                elif r.get("error"):
                    lines.append(f"   {AUTOBUY_SITE_LABELS[site]}: ⚠️ {html.escape(str(r['error']))} ({_ago(r['at'])})")
                elif r.get("quiet"):
                    lines.append(f"   {AUTOBUY_SITE_LABELS[site]}: запомнил {r.get('new', 0)} текущих лотов, покупаю следующие ({_ago(r['at'])})")
                else:
                    lines.append(f"   {AUTOBUY_SITE_LABELS[site]}: новых {r.get('new', 0)}, куплено {r.get('bought', 0)}, "
                                 f"не удалось {r.get('failed', 0)}, дороже лимита {r.get('skipped_price', 0)} ({_ago(r['at'])})")
        lines.append("\nСтарые аккаунты учитываются в результатах. Уведомления приходят только о новых.")
        keyboard = {"inline_keyboard": [
            [{"text": "🔄 Обновить статус", "callback_data": "nav:status"}, {"text": "🔎 Проверить сейчас", "callback_data": "nav:check"}],
            [{"text": "🤖 AutoBuy", "callback_data": "nav:autobuy"}, {"text": "⚙️ Изменить условия", "callback_data": "nav:settings"}],
            [{"text": "🏠 Главное меню", "callback_data": "nav:home"}],
        ]}
        self.show_page(p, chat_id, "\n".join(lines), keyboard, "status", message_id)

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
                        message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
                        chat_id = (message.get("chat") or {}).get("id")
                        if chat_id is not None:
                            try:
                                self.tg.send(chat_id, "⚠️ Не удалось выполнить команду. Попробуйте ещё раз или откройте /help.")
                            except Exception:
                                log.exception("Не удалось сообщить об ошибке команды")
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
        log.warning("%s/%s: %d новых лотов, но ни один чат не подписан (/start) — пропускаю", p["user_id"], name, len(new_items))
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
    with rt.lock:
        _check_user(bot, rt)


def check_user_safe(bot: Bot, rt: UserRuntime) -> None:
    """Плановая проверка одного пользователя; ошибка одного не мешает остальным."""
    try:
        check_user(bot, rt)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка при проверке пользователя %s", rt.profile.get("user_id"))
        rt.record_error(f"{type(exc).__name__}: {exc}")


CHECK_SHOW_ITEMS = 3  # сколько последних лотов с каждой площадки присылать по /check
DOWN_AFTER_FAILS = 2  # столько плановых проверок подряд с ошибкой = сайт «лёг»
UP_AFTER_OKS = 2      # столько удачных проверок подряд после этого = сайт «встал» (один проскочивший запрос не считаем)


def track_availability(bot: Bot, rt: UserRuntime, name: str, label: str, error: str | None) -> None:
    """Пока сайт не отвечает — молчим (ошибка видна в /status и в логе). Когда снова ответил
    после DOWN_AFTER_FAILS и более неудачных проверок подряд — одно сообщение «снова работает»."""
    st = rt.stats
    since_key, fails_key, oks_key = f"{name}_down_since", f"{name}_fails", f"{name}_oks"
    st[f"{name}_last_ok"] = not error
    st[f"{name}_last_error"] = error
    st[f"{name}_checked_at"] = time.time()
    if error:
        st[fails_key] = st.get(fails_key, 0) + 1
        st[oks_key] = 0
        if st.get(since_key) is None and st[fails_key] >= DOWN_AFTER_FAILS:
            st[since_key] = time.time()
            log.warning("%s/%s: сайт не отвечает: %s", rt.profile["user_id"], name, error)
        return
    st[fails_key] = 0
    st[oks_key] = st.get(oks_key, 0) + 1
    since = st.get(since_key)
    if since is not None and st[oks_key] >= UP_AFTER_OKS:
        st[since_key] = None
        log.info("%s/%s: сайт снова отвечает", rt.profile["user_id"], name)
        bot.tg.send_all(rt.chats, f"🟢 {label} снова работает, не отвечал {_duration(time.time() - since)}. "
                                  "Слежу за новыми лотами.")


def _check_user(bot: Bot, rt: UserRuntime) -> None:
    p, st = rt.profile, rt.stats
    ab = p.get("autobuy") or {}
    ab = autobuy_upgrade(ab) if ab.get("enabled") else None
    ab_sites = set(ab["sources"]) if ab else set()
    ab_s = (ab.get("settings") or dict(AUTOBUY_DEFAULT_SETTINGS)) if ab else {}

    if rt.lzt is not None:
        items = rt.lzt.fetch_items(p["category"], lzt_params(p["query"]))
        st["checks"] += 1
        st["last_check_at"] = time.time()
        st["last_items"] = len(items)
        st["last_new"] = 0
        rt.record_error(rt.lzt.last_error)
        track_availability(bot, rt, "lzt", "lzt.market", rt.lzt.last_error)
        error, rt.lzt.last_error = rt.lzt.last_error, None
        bought: set[int] = set()
        if "lzt" in ab_sites:
            # AutoBuy идёт ПЕРЕД рассылкой: пока лоты уходят в чаты по секунде на каждый, их скупают.
            # Если условия AutoBuy совпадают с условиями поиска, второй запрос к сайту не нужен.
            same = not error and lzt_params(p["query"]) == autobuy_lzt_params(ab_s)
            bought = run_autobuy_site(bot, rt, ab, "lzt", items if same else None)
        if items:
            seen = p["seen"]["lzt"]
            new_items = [i for i in items if int(i["item_id"]) not in seen and int(i["item_id"]) not in bought]
            for item_id in bought:
                if item_id not in seen:
                    seen.append(item_id)  # о купленном AutoBuy уже написал, второй раз с кнопкой не шлём
            st["last_new"] = len(new_items)
            log.info("%s/lzt: лотов %d, новых %d", p["user_id"], len(items), len(new_items))
            deliver_new(bot, rt, items, new_items, "lzt")
        else:
            log.warning("%s/lzt: лотов нет (ошибка или пустой фильтр)", p["user_id"])
    elif "lzt" in ab_sites:
        run_autobuy_site(bot, rt, ab, "lzt", None)

    if rt.tron is not None:
        items = rt.tron.fetch_items()
        st["tron_total"] = rt.tron.last_total
        st["tron_items"] = len(items)
        st["tron_new"] = 0
        rt.record_error(rt.tron.last_error)
        track_availability(bot, rt, "tron", "tronaccs", rt.tron.last_error)
        error, rt.tron.last_error = rt.tron.last_error, None
        bought = set()
        if "tron" in ab_sites:
            same = not error and (p.get("tron_filter") or "") == autobuy_tron_filter(ab_s)
            bought = run_autobuy_site(bot, rt, ab, "tron", items if same else None)
        if items:
            seen = p["seen"]["tron"]
            new_items = [i for i in items if int(i["item_id"]) not in seen and int(i["item_id"]) not in bought]
            for item_id in bought:
                if item_id not in seen:
                    seen.append(item_id)
            st["tron_new"] = len(new_items)
            log.info("%s/tron: всего %d, под фильтр %d, новых %d", p["user_id"], rt.tron.last_total, len(items), len(new_items))
            deliver_new(bot, rt, items, new_items, "tron")
    elif "tron" in ab_sites:
        run_autobuy_site(bot, rt, ab, "tron", None)


def run_autobuy_site(bot: Bot, rt: UserRuntime, ab: dict, site: str, items: list[dict] | None) -> set[int]:
    """AutoBuy на одной площадке. items — уже полученный список (те же условия), иначе запрос свой.
    Возвращает ID купленных лотов."""
    try:
        return _check_autobuy_site(bot, rt, ab, site, items)
    except Exception:  # noqa: BLE001
        log.exception("autobuy %s: ошибка у пользователя %s", site, rt.profile["user_id"])
        return set()


def check_autobuy(bot: Bot, rt: UserRuntime) -> None:
    """Автопокупка новых лотов по отдельным условиям профиля (блок autobuy), на каждой
    из выбранных площадок. Первая проверка площадки после включения/смены фильтра — тихая:
    запоминаем уже существующие лоты, чтобы не скупить старое. Дальше — покупаем по мере появления."""
    p = rt.profile
    ab = p.get("autobuy") or {}
    if not ab.get("enabled"):
        return
    autobuy_upgrade(ab)
    for site in ab["sources"]:
        run_autobuy_site(bot, rt, ab, site, None)


def _ab_report(rt: UserRuntime, site: str, **fields) -> None:
    """Итог последней проверки AutoBuy по площадке — для /status и лога."""
    rt.stats[f"ab_{site}"] = {"at": time.time(), **fields}


def _check_autobuy_site(bot: Bot, rt: UserRuntime, ab: dict, site: str, items: list[dict] | None = None) -> set[int]:
    p, state = rt.profile, bot.state
    s = ab.get("settings") or dict(AUTOBUY_DEFAULT_SETTINGS)
    seen = ab["seen"][site]
    attempts = ab.setdefault("attempts", {})
    seen_set = set(seen)
    label = AUTOBUY_SITE_LABELS[site]
    bought: set[int] = set()

    if items is None:
        if site == "lzt":
            client = rt.lzt_ab
            if client is None:
                _ab_report(rt, site, error="нет токена lzt.market или включён режим страницы")
                return bought
            items = client.fetch_items(p.get("category") or "telegram",
                                       autobuy_lzt_params(s), retries=2)
        else:
            client = rt.tron_ab
            if client is None:
                _ab_report(rt, site, error="нет токена tronaccs")
                return bought
            items = client.fetch_items(retries=2)
        if client.last_error:
            log.warning("%s/autobuy %s: %s", p["user_id"], site, client.last_error)
            _ab_report(rt, site, error=user_error(client.last_error))
            return bought
    else:
        client = rt.lzt_ab if site == "lzt" else rt.tron_ab
        if client is None:
            _ab_report(rt, site, error="нет токена для покупки на " + label)
            return bought

    new_items = [i for i in items if int(i["item_id"]) not in seen_set]

    # Тихий первый прогон — просто запоминаем, что уже есть.
    if not ab["init"].get(site):
        ab["init"][site] = True
        for item in new_items:
            seen.append(int(item["item_id"]))
        state.save()
        log.info("%s/autobuy %s: тихая инициализация, запомнил %d лотов", p["user_id"], site, len(new_items))
        _ab_report(rt, site, new=len(new_items), quiet=True)
        return bought

    new_items.sort(key=lambda i: int(i.get("published_date") or 0))
    skipped_price = failed = 0
    for item in new_items:
        item_id = int(item["item_id"])
        seen.append(item_id)
        seen_set.add(item_id)

        # 1) защита по цене
        price = item.get("price")
        if s.get("price_max") is not None and price is not None:
            try:
                if float(price) > float(s["price_max"]):
                    skipped_price += 1
                    continue
            except (TypeError, ValueError):
                pass

        # 2) анти-дребезг: не пытаемся купить один и тот же лот дважды в течение часа
        attempt_key = f"{site}:{item_id}"
        last = attempts.get(attempt_key)
        if last and time.time() - last < 3600:
            continue

        # 3) покупаем
        if site == "lzt":
            try:
                price_int = int(price) if price is not None else None
            except (TypeError, ValueError):
                price_int = None
            ok, msg = client.fast_buy(item_id, price_int)
        else:
            ok, msg = client.buy(item_id)
        attempts[attempt_key] = time.time()
        log.info("%s/autobuy %s: лот %d → %s (%s)", p["user_id"], label, item_id, ok, msg)

        chats = rt.chats
        if ok:
            bought.add(item_id)
            if chats:
                bot.tg.send_all(chats, f"🤖✅ <b>AutoBuy</b> купил лот {item_id} на {label}.\n\n"
                                       + format_item(item))
            target = chats[0] if chats else p["user_id"]
            try:
                bot.send_account_files(rt, target, item_id, label, client=client)
            except Exception:
                log.exception("autobuy: не удалось отправить файлы лота %d", item_id)
        else:
            failed += 1
            if chats:
                bot.tg.send_all(chats,
                    f"🤖❌ <b>AutoBuy</b>: не удалось купить лот {item_id} на {label}.\n"
                    f"Причина: {html.escape(user_error(msg))}\n"
                    f"{item.get('url') or ''}")

        # урезаем массивы
        if len(seen) > MAX_SEEN_IDS:
            del seen[:-MAX_SEEN_IDS]
        if len(attempts) > MAX_SEEN_IDS:
            keep = sorted(attempts.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SEEN_IDS]
            ab["attempts"] = dict(keep)
            attempts = ab["attempts"]
        state.save()

    _ab_report(rt, site, new=len(new_items), bought=len(bought), failed=failed, skipped_price=skipped_price)
    if new_items:
        log.info("%s/autobuy %s: новых %d, куплено %d, не удалось %d, дороже лимита %d",
                 p["user_id"], site, len(new_items), len(bought), failed, skipped_price)
    return bought


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
        # Каждого пользователя проверяем в своём потоке: иначе, пока первому рассылаются лоты
        # (по секунде на лот) или его сайт тормозит, остальные получают те же лоты с опозданием.
        threads = []
        for uid in list(bot.runtimes):
            rt = bot.runtime(uid)
            if rt is None:
                continue
            t = threading.Thread(target=check_user_safe, args=(bot, rt), name=f"check-{uid}", daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        if once:
            return 0
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))                                                                                      
