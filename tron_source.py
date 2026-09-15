"""
tronaccs.market через официальный API (https://tronaccs.readme.io).

    GET  https://system-api.tronaccs.market/items/telegram?page=N  - все аккаунты в продаже
    GET  https://system-api.tronaccs.market/item/{id}
    POST https://system-api.tronaccs.market/buy/{id}                - покупка
    Authorization: Bearer <токен из настроек аккаунта, раздел "API TronAccs">

У метода списка нет параметров фильтра, поэтому фильтруем на своей стороне
(см. parse_filter / match_filter). Формат ответа в документации не описан,
разбор сделан терпимым к названиям полей.
"""

from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger("lzt-monitor.tron")

TRON_API = "https://system-api.tronaccs.market"
TRON_SITE = "https://tronaccs.market"

ID_KEYS = ("item_id", "id", "account_id")
TITLE_KEYS = ("title", "name", "username")
PRICE_KEYS = ("price", "cost", "amount")
# Поля, которые не показываем и по которым не фильтруем (длинные/служебные/секретные)
HIDDEN_KEYS = {
    "auth_key", "authkey", "session", "tdata", "telethon", "pyrogram", "json", "password",
    "description", "user", "seller", "images", "image", "photo",
}


class TronError(RuntimeError):
    pass


# ---------- фильтр на своей стороне ----------

_OPS = ("!=", ">=", "<=", "=", ">", "<", "~")
_TOKEN_RE = re.compile(r'(\S+?)(!=|>=|<=|=|>|<|~)("[^"]*"|\S+)')

TRUE_WORDS = {"yes", "true", "1", "да", "есть", "on"}
FALSE_WORDS = {"no", "false", "0", "нет", "off", "none", "null", ""}


def parse_filter(text: str) -> list[tuple[str, str, str]]:
    """'country=UZ contacts>=100 spam=no price<=500' -> [(key, op, value), ...]."""
    rules = []
    for m in _TOKEN_RE.finditer(text or ""):
        key, op, value = m.group(1), m.group(2), m.group(3).strip('"')
        rules.append((key.strip().lower(), op, value))
    if (text or "").strip() and not rules:
        raise ValueError("Не понял фильтр. Пример: country=UZ contacts>=100 spam=no price<=500")
    return rules


def _find_key(item: dict, key: str):
    """Точное совпадение ключа, иначе ключ, содержащий искомое слово."""
    keys = {k.lower(): k for k in item}
    if key in keys:
        return keys[key]
    candidates = [k for kl, k in keys.items() if key in kl and kl not in HIDDEN_KEYS]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        # предпочитаем самый короткий ключ: "contacts" вместо "contacts_last_seen"
        return sorted(candidates, key=len)[0]
    return None


def _to_number(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(" ", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _match_rule(item: dict, key: str, op: str, expected: str) -> bool:
    real_key = _find_key(item, key)
    if real_key is None:
        return False  # поля нет - лот не подходит под условие
    actual = item[real_key]

    if op == "~":
        return expected.lower() in str(actual).lower()

    a_num, e_num = _to_number(actual), _to_number(expected)
    if op in (">", "<", ">=", "<=") or (a_num is not None and e_num is not None and op in ("=", "!=")):
        if a_num is None or e_num is None:
            return False
        return {
            ">": a_num > e_num, "<": a_num < e_num, ">=": a_num >= e_num,
            "<=": a_num <= e_num, "=": a_num == e_num, "!=": a_num != e_num,
        }[op]

    # строки и булевы: да/нет, yes/no, true/false
    e = expected.strip().lower()
    if isinstance(actual, bool) or e in TRUE_WORDS or e in FALSE_WORDS:
        a_bool = actual if isinstance(actual, bool) else (
            str(actual).strip().lower() in TRUE_WORDS if str(actual).strip().lower() in TRUE_WORDS | FALSE_WORDS else bool(actual)
        )
        e_bool = e in TRUE_WORDS
        return (a_bool == e_bool) if op == "=" else (a_bool != e_bool)
    equal = str(actual).strip().lower() == e
    return equal if op == "=" else not equal


def match_filter(item: dict, rules: list[tuple[str, str, str]]) -> bool:
    return all(_match_rule(item, k, op, v) for k, op, v in rules)


# ---------- клиент ----------

def _first(d: dict, keys) :
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def extract_list(data) -> list[dict]:
    """Находим список лотов в ответе неизвестной формы."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "data", "accounts", "result", "results", "list", "products"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            inner = extract_list(value)
            if inner:
                return inner
    return []


def normalize(raw: dict, category: str = "telegram") -> dict | None:
    item_id = _first(raw, ID_KEYS)
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    price = _first(raw, PRICE_KEYS)
    price_num = _to_number(price)
    if price_num is not None and price_num.is_integer():
        price = int(price_num)
    elif price_num is not None:
        price = price_num
    currency = _first(raw, ("currency", "price_currency")) or "rub"

    details = []
    for key, value in raw.items():
        kl = key.lower()
        if kl in HIDDEN_KEYS or kl in ID_KEYS or kl in TITLE_KEYS or kl in PRICE_KEYS:
            continue
        if isinstance(value, (dict, list)) or value in (None, ""):
            continue
        s = str(value)
        if len(s) > 60:
            continue
        details.append(f"{key}: {s}")

    item = {
        "item_id": item_id,
        "title": str(_first(raw, TITLE_KEYS) or f"Лот #{item_id}"),
        "price": price,
        "price_currency": str(currency).lower(),
        "web_details": "; ".join(details)[:400],
        "url": f"{TRON_SITE}/{category}/{item_id}",
        "source": "tron",
        "raw": raw,
    }
    return item


class TronApiClient:
    name = "tron"

    def __init__(self, token: str, category: str = "telegram", max_pages: int = 5) -> None:
        self.category = category
        self.max_pages = max(1, max_pages)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "lzt-market-monitor/1.0",
        })
        self.last_error: str | None = None
        self.page_size: int | None = None

    def _get(self, path: str, params: dict | None = None):
        resp = self.session.get(f"{TRON_API}{path}", params=params, timeout=30)
        if resp.status_code == 401:
            raise TronError("tronaccs: 401, проверьте TRON_TOKEN")
        if resp.status_code == 429:
            raise TronError("tronaccs: превышен лимит запросов (429)")
        if resp.status_code >= 400:
            raise TronError(f"tronaccs: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise TronError(f"tronaccs: ответ не JSON: {resp.text[:200]}")

    def fetch_raw_page(self, page: int = 1):
        return self._get(f"/items/{self.category}", {"page": page})

    def fetch_items(self) -> list[dict]:
        """Все доступные лоты с первых max_pages страниц."""
        items: dict[int, dict] = {}
        for page in range(1, self.max_pages + 1):
            data = self.fetch_raw_page(page)
            raw_list = extract_list(data)
            if not raw_list:
                break
            for raw in raw_list:
                item = normalize(raw, self.category)
                if item:
                    items[item["item_id"]] = item
            if self.page_size is None:
                self.page_size = len(raw_list)
            if len(raw_list) < (self.page_size or 1):
                break  # последняя страница
            if isinstance(data, dict):
                total_pages = _to_number(_first(data, ("total_pages", "pages", "last_page", "pageCount")))
                if total_pages is not None and page >= total_pages:
                    break
            time.sleep(0.3)
        return list(items.values())

    def buy(self, item_id: int) -> tuple[bool, str]:
        try:
            resp = self.session.post(f"{TRON_API}/buy/{item_id}", timeout=90)
        except requests.RequestException as exc:
            return False, f"Ошибка сети: {exc}"
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("message") or data.get("error") or data.get("errors") or "")
            return False, f"HTTP {resp.status_code}: {msg or resp.text[:200]}"
        if isinstance(data, dict):
            status = str(data.get("status") or "").lower()
            if status and status not in ("ok", "success", "true", "1"):
                return False, str(data.get("message") or data.get("error") or data)[:300]
        return True, "Покупка прошла. Данные аккаунта в личном кабинете tronaccs (Мои покупки)."
