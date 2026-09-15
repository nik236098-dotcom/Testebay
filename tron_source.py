"""
tronaccs.market через System API v2 (https://tronaccs-market.readme.io).

    GET  https://system-api.tronaccs.market/items?category=telegram&page=1&orderBy=time_add&orderType=DESC
         + фильтры: priceFrom/priceTo, contactsFrom/contactsTo, dialogsFrom/To, channelsFrom/To,
           chatsFrom/To, premium (0/1), spamblock (0/1), two_fa (0/1), ageFrom/To, starsFrom/To,
           country (числовые ID, справочника нет), origin (ID), searchString ...
    POST https://system-api.tronaccs.market/items/{id}/purchase
    Authorization: Bearer <токен из настроек аккаунта, раздел "API TronAccs">

Ответ списка: {"status": "ok", "items": [{item_id, item_url, published_date, title, price,
price_currency, price_fees, telegram_counrty (код страны, опечатка в API),
telegram_contacts_count, telegram_spam_block, telegram_premium, telegram_channels_count,
telegram_conversations_count, telegram_password (2FA), ...}]}

Фильтр пользователя (строка вида "country=UZ contacts>=100 spam=no price<=500")
разбирается в parse_filter; то, что умеет сервер, уходит параметрами запроса
(build_params), остальное проверяется на своей стороне (match_filter).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("lzt-monitor.tron")

TRON_API = "https://system-api.tronaccs.market"
TRON_SITE = "https://tronaccs.market"


class TronError(RuntimeError):
    pass


# ---------- разбор фильтра ----------

_TOKEN_RE = re.compile(r'(\S+?)(!=|>=|<=|=|>|<|~)("[^"]*"|\S+)')
TRUE_WORDS = {"yes", "true", "1", "да", "есть", "on"}
FALSE_WORDS = {"no", "false", "0", "нет", "off", "none", "null", ""}

# Синонимы ключей фильтра -> (параметр API "от", параметр API "до") для числовых диапазонов
RANGE_KEYS = {
    "price": ("priceFrom", "priceTo"), "цена": ("priceFrom", "priceTo"),
    "contacts": ("contactsFrom", "contactsTo"), "контакты": ("contactsFrom", "contactsTo"),
    "dialogs": ("dialogsFrom", "dialogsTo"), "conversations": ("dialogsFrom", "dialogsTo"), "диалоги": ("dialogsFrom", "dialogsTo"),
    "channels": ("channelsFrom", "channelsTo"), "каналы": ("channelsFrom", "channelsTo"),
    "chats": ("chatsFrom", "chatsTo"), "чаты": ("chatsFrom", "chatsTo"),
    "age": ("ageFrom", "ageTo"), "возраст": ("ageFrom", "ageTo"),
    "stars": ("starsFrom", "starsTo"),
    "idlength": ("idLengthFrom", "idLengthTo"), "id_length": ("idLengthFrom", "idLengthTo"),
    "admin_channels": ("adminChannelsFrom", "adminChannelsTo"),
    "admin_chats": ("adminChatsFrom", "adminChatsTo"),
    "gifts": ("giftsRegularFrom", "giftsRegularTo"), "nft": ("giftsNftFrom", "giftsNftTo"),
    "settle": ("settle", None), "days": ("settle", None),
}
# Булевы ключи -> параметр API
BOOL_KEYS = {
    "spam": "spamblock", "spamblock": "spamblock", "спам": "spamblock", "спамблок": "spamblock",
    "premium": "premium", "премиум": "premium",
    "2fa": "two_fa", "two_fa": "two_fa", "password": "two_fa", "пароль": "two_fa",
    "geo": "spamblock_geo", "spamblock_geo": "spamblock_geo",
    "admin": "with_admin_channels",
}
# Ключи, которые сервер принимает как ID (число); иначе проверяем сами по коду в лоте
ID_KEYS = {"country": "country", "страна": "country", "origin": "origin", "происхождение": "origin"}


def parse_filter(text: str) -> list[tuple[str, str, str]]:
    """'country=UZ contacts>=100 spam=no price<=500' -> [(key, op, value), ...]."""
    rules = []
    for m in _TOKEN_RE.finditer(text or ""):
        rules.append((m.group(1).strip().lower(), m.group(2), m.group(3).strip('"')))
    if (text or "").strip() and not rules:
        raise ValueError("Не понял фильтр. Пример: country=UZ contacts>=100 spam=no price<=500")
    return rules


def _bool_value(value: str) -> int | None:
    v = value.strip().lower()
    if v in TRUE_WORDS:
        return 1
    if v in FALSE_WORDS:
        return 0
    return None


def build_params(rules: list[tuple[str, str, str]]) -> tuple[dict, list[tuple[str, str, str]]]:
    """Разделяем правила: что умеет сервер -> параметры запроса, остальное -> проверка у себя."""
    params: dict = {}
    local: list[tuple[str, str, str]] = []
    for key, op, value in rules:
        if key in RANGE_KEYS and op in (">", ">=", "<", "<=", "="):
            lo, hi = RANGE_KEYS[key]
            num = value.replace(",", ".")
            try:
                float(num)
            except ValueError:
                local.append((key, op, value))
                continue
            if op in (">", ">=", "=") and lo:
                params[lo] = num
            if op in ("<", "<=", "=") and hi:
                params[hi] = num
            if op in (">", "<"):  # сервер знает только "от/до" включительно, уточним у себя
                local.append((key, op, value))
        elif key in BOOL_KEYS and op in ("=", "!="):
            b = _bool_value(value)
            if b is None:
                local.append((key, op, value))
                continue
            if op == "!=":
                b = 1 - b
            params[BOOL_KEYS[key]] = b
        elif key in ID_KEYS and op == "=" and re.fullmatch(r"[\d,]+", value):
            params[ID_KEYS[key]] = value
        else:
            local.append((key, op, value))
    return params, local


# ---------- проверка на своей стороне ----------

# Поле лота для ключа фильтра (после normalize)
FIELD_ALIASES = {
    "country": "telegram_country", "страна": "telegram_country",
    "contacts": "telegram_contacts_count", "контакты": "telegram_contacts_count",
    "dialogs": "telegram_conversations_count", "conversations": "telegram_conversations_count", "диалоги": "telegram_conversations_count",
    "channels": "telegram_channels_count", "каналы": "telegram_channels_count",
    "spam": "telegram_spam_block", "spamblock": "telegram_spam_block", "спам": "telegram_spam_block", "спамблок": "telegram_spam_block",
    "premium": "telegram_premium", "премиум": "telegram_premium",
    "2fa": "telegram_password", "two_fa": "telegram_password", "password": "telegram_password",
    "price": "price", "цена": "price",
    "title": "title", "название": "title", "name": "title",
    "seller": "seller_username", "продавец": "seller_username",
    "origin": "item_origin", "stars": "telegram_raiting_stars", "level": "telegram_raiting_level",
}


def _find_key(item: dict, key: str):
    if key in FIELD_ALIASES and FIELD_ALIASES[key] in item:
        return FIELD_ALIASES[key]
    keys = {k.lower(): k for k in item}
    if key in keys:
        return keys[key]
    candidates = [k for kl, k in keys.items() if key in kl]
    return sorted(candidates, key=len)[0] if candidates else None


def _to_number(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    return None


def _match_rule(item: dict, key: str, op: str, expected: str) -> bool:
    real_key = _find_key(item, key)
    if real_key is None:
        return False
    actual = item[real_key]
    if op == "~":
        return expected.lower() in str(actual).lower()

    e_bool = _bool_value(expected)
    a_num, e_num = _to_number(actual), _to_number(expected)
    if op in (">", "<", ">=", "<="):
        if a_num is None or e_num is None:
            return False
        return {">": a_num > e_num, "<": a_num < e_num, ">=": a_num >= e_num, "<=": a_num <= e_num}[op]
    if e_bool is not None and (isinstance(actual, bool) or a_num in (0.0, 1.0) or str(actual).lower() in TRUE_WORDS | FALSE_WORDS):
        a_bool = 1 if (actual is True or a_num == 1.0 or str(actual).lower() in TRUE_WORDS) else 0
        return (a_bool == e_bool) if op == "=" else (a_bool != e_bool)
    if a_num is not None and e_num is not None:
        return (a_num == e_num) if op == "=" else (a_num != e_num)
    equal = str(actual).strip().lower() == expected.strip().lower()
    return equal if op == "=" else not equal


def match_filter(item: dict, rules: list[tuple[str, str, str]]) -> bool:
    return all(_match_rule(item, k, op, v) for k, op, v in rules)


# ---------- клиент ----------

def _to_ts(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def normalize(raw: dict) -> dict | None:
    """Приводим лот tronaccs к виду лота lzt, чтобы форматирование и кнопки были общими."""
    try:
        item_id = int(raw.get("item_id") or raw.get("id"))
    except (TypeError, ValueError):
        return None
    price = raw.get("price_fees") if raw.get("price_fees") not in (None, "") else raw.get("price")
    p = _to_number(price)
    if p is not None:
        price = int(p) if p.is_integer() else p
    item = {
        "item_id": item_id,
        "title": str(raw.get("title") or f"Лот #{item_id}"),
        "price": price,
        "price_currency": str(raw.get("price_currency") or "rub").lower(),
        "published_date": _to_ts(raw.get("published_date")),
        "url": raw.get("item_url") or f"{TRON_SITE}/{raw.get('category') or 'telegram'}/{item_id}",
        "source": "tron",
        "item_origin": raw.get("item_origin"),
        "seller_username": raw.get("seller_username"),
        "raw": raw,
    }
    # telegram_* поля переносим как есть; опечатку API telegram_counrty исправляем
    for key, value in raw.items():
        if key.startswith("telegram_") and value not in (None, ""):
            item["telegram_country" if key == "telegram_counrty" else key] = value
    base = _to_number(raw.get("price"))
    if base is not None and raw.get("price_fees") not in (None, "") and base != _to_number(raw.get("price_fees")):
        item["web_details"] = f"без комиссии {int(base) if base.is_integer() else base}"
    return item


class TronApiClient:
    name = "tron"

    def __init__(self, token: str, category: str = "telegram", max_pages: int = 2) -> None:
        self.category = category
        self.max_pages = max(1, max_pages)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "lzt-market-monitor/1.0",
        })

    MIN_INTERVAL = 1.1  # сервер требует не чаще одного запроса в секунду
    _last_request_at = 0.0
    _pace_lock = threading.Lock()  # общий на все экземпляры: монитор и бот ходят в API из разных потоков

    def _request(self, method: str, path: str, **kw):
        timeout = kw.pop("timeout", 30)
        for attempt in range(1, 6):
            # Держим паузу между любыми запросами к tronaccs, из какого бы потока они ни шли
            with TronApiClient._pace_lock:
                wait = TronApiClient._last_request_at + self.MIN_INTERVAL - time.time()
                if wait > 0:
                    time.sleep(wait)
                try:
                    resp = self.session.request(method, f"{TRON_API}{path}", timeout=timeout, **kw)
                finally:
                    TronApiClient._last_request_at = time.time()
            if resp.status_code == 401:
                raise TronError("tronaccs: 401, проверьте TRON_TOKEN")
            try:
                data = resp.json()
            except ValueError:
                raise TronError(f"tronaccs: HTTP {resp.status_code}, ответ не JSON: {resp.text[:200]}")
            message = str((data.get("message") if isinstance(data, dict) else "") or "")
            limited = resp.status_code == 429 or "Попробуйте через" in message or "Превышено количество" in message
            if limited and attempt < 5:
                m = re.search(r"через\s+(\d+)", message)
                delay = int(m.group(1)) if m else 2
                log.warning("tronaccs: лимит запросов, жду %d с (попытка %d)", delay, attempt)
                time.sleep(delay + 0.3)
                continue
            if resp.status_code >= 400:
                raise TronError(f"tronaccs: HTTP {resp.status_code}: {str(data)[:200]}")
            return data
        raise TronError("tronaccs: лимит запросов не снимается, попробуйте позже")

    def countries(self) -> dict[str, int]:
        """Справочник стран сайта: код -> ID. Отдаётся публичным API сайта (api.tronaccs.market/countries)."""
        for url in ("https://api.tronaccs.market/countries", f"{TRON_API}/countries"):
            try:
                resp = requests.get(url, timeout=20, headers={"Accept": "application/json",
                                                              "User-Agent": self.session.headers["User-Agent"]})
                data = resp.json()
            except (requests.RequestException, ValueError):
                continue
            items = data
            if isinstance(data, dict):
                items = data.get("countries") or data.get("items") or data.get("data") or data.get("result") or []
            result = {}
            for c in items if isinstance(items, list) else []:
                if isinstance(c, dict) and c.get("countryCode") and c.get("id") is not None:
                    try:
                        result[str(c["countryCode"]).upper()] = int(c["id"])
                    except (TypeError, ValueError):
                        pass
            if result:
                return result
        return {}

    def me(self) -> dict:
        """GET /me: баланс и валюта."""
        data = self._request("GET", "/me")
        return data.get("user") or {} if isinstance(data, dict) else {}

    def fetch_raw_page(self, page: int = 1, params: dict | None = None):
        q = {"category": self.category, "page": page, "orderBy": "time_add", "orderType": "DESC"}
        q.update(params or {})
        return self._request("GET", "/items", params=q)

    def fetch_items(self, params: dict | None = None) -> list[dict]:
        """Лоты, новые первыми, с первых max_pages страниц."""
        items: dict[int, dict] = {}
        page_size = None
        for page in range(1, self.max_pages + 1):
            data = self.fetch_raw_page(page, params)
            if isinstance(data, dict) and str(data.get("status", "ok")).lower() not in ("ok", "true", "success", "1"):
                raise TronError(f"tronaccs: {data.get('result') or data.get('message') or data}")
            raw_list = data.get("items") if isinstance(data, dict) else data
            if not isinstance(raw_list, list) or not raw_list:
                break
            for raw in raw_list:
                if isinstance(raw, dict):
                    item = normalize(raw)
                    if item:
                        items[item["item_id"]] = item
            page_size = page_size or len(raw_list)
            if len(raw_list) < page_size:
                break
        return list(items.values())

    def item_detail(self, item_id: int) -> dict | None:
        """Данные купленного аккаунта: GET /items/{id}, запасной вариант /item/{id}."""
        for path in (f"/items/{item_id}", f"/item/{item_id}"):
            try:
                data = self._request("GET", path)
            except (TronError, requests.RequestException):
                continue
            if isinstance(data, dict):
                for key in ("item", "account", "data", "result"):
                    if isinstance(data.get(key), dict):
                        return data[key]
                return data
        return None

    def buy(self, item_id: int) -> tuple[bool, str]:
        try:
            data = self._request("POST", f"/items/{item_id}/purchase", timeout=90)
        except TronError as exc:
            return False, str(exc)
        except requests.RequestException as exc:
            return False, f"Ошибка сети: {exc}"
        status = data.get("status") if isinstance(data, dict) else None
        text = str((data.get("result") if isinstance(data, dict) else None) or data)[:300]
        ok = status is True or str(status).lower() in ("ok", "true", "success", "1")
        return ok, text if ok else (text or "Маркет отказал в покупке")
