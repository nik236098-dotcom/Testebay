"""Russian labels for Telegram menu screens (Python 3.8 compatible)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs


COUNTRIES = {
    "UZ": "Узбекистан", "RU": "Россия", "KZ": "Казахстан", "UA": "Украина",
    "BY": "Беларусь", "KG": "Кыргызстан", "TJ": "Таджикистан", "US": "США",
    "IN": "Индия", "ID": "Индонезия", "VN": "Вьетнам", "TH": "Таиланд",
    "TR": "Турция", "DE": "Германия", "GB": "Великобритания", "FR": "Франция",
    "AZ": "Азербайджан", "AM": "Армения", "GE": "Грузия", "CN": "Китай",
}


def country_name(code):
    code = str(code or "any").upper()
    return "любая страна" if code == "ANY" else COUNTRIES.get(code, "страна с кодом " + code)


def number(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite():
            raise InvalidOperation
        formatted = format(n, ",f")
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted.replace(",", " ").replace(".", ",")
    except (InvalidOperation, ValueError, TypeError):
        return "неизвестно"


def money(value, currency="RUB"):
    if value is None or value == "":
        return "баланс недоступен"
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(str(currency or "RUB").upper(), str(currency))
    return number(value) + " " + symbol


def price_text(settings):
    lo, hi = settings.get("price_min"), settings.get("price_max")
    if lo is None and hi is None:
        return "без ограничений"
    if lo is not None and hi is not None:
        return "от {} до {} ₽".format(number(lo), number(hi))
    return "от {} ₽".format(number(lo)) if lo is not None else "до {} ₽".format(number(hi))


def criteria(settings):
    contacts = settings.get("contacts", 0)
    spam = {"no": "только без спамблока", "yes": "только со спамблоком", "any": "не имеет значения"}.get(settings.get("spam"), "не имеет значения")
    return "\n".join([
        "🌍 Страна: " + country_name(settings.get("country")),
        "👥 Контакты: " + ("не меньше " + number(contacts) if contacts else "без ограничений"),
        "🚫 Спамблок: " + spam,
        "💰 Цена: " + price_text(settings),
    ])


def settings_from_query(query):
    q = parse_qs(query or "", keep_blank_values=True)
    def first(key, default=None):
        return q.get(key, [default])[0]
    def numeric(key, default=None):
        try:
            n = float(first(key))
            if not Decimal(str(n)).is_finite():
                return default
            return int(n) if n.is_integer() else n
        except (TypeError, ValueError, OverflowError):
            return default
    return {"country": first("country[]", "any") or "any", "contacts": numeric("min_contacts", 0),
            "spam": first("spam") if first("spam") in ("yes", "no") else "any", "price_min": numeric("pmin"), "price_max": numeric("pmax")}


def lzt_criteria(query):
    text = criteria(settings_from_query(query))
    q = parse_qs(query or "", keep_blank_values=True)
    codes = q.get("country[]", [])
    if len(codes) > 1:
        text = text.replace("🌍 Страна: " + country_name(codes[0]), "🌍 Страны: " + ", ".join(country_name(c) for c in codes), 1)
    extra = set(q) - {"country[]", "min_contacts", "spam", "pmin", "pmax", "order_by", "page"}
    if extra:
        text += "\nДополнительные условия заданы ссылкой на площадку."
    return text


def tron_criteria(rules):
    labels = {"country": "🌍 Страна", "страна": "🌍 Страна", "contacts": "👥 Контакты",
              "контакты": "👥 Контакты", "spam": "🚫 Спамблок", "спам": "🚫 Спамблок", "price": "💰 Цена", "цена": "💰 Цена",
              "premium": "Премиум", "two_fa": "Двухэтапная защита", "dialogs": "Диалоги", "channels": "Каналы", "chats": "Чаты"}
    ops = {"=": "", "!=": "не ", ">=": "не меньше ", "<=": "не больше ", ">": "больше ", "<": "меньше ", "~": "содержит "}
    lines = []
    present = set()
    extras = False
    for key, op, value in rules:
        if key not in labels:
            extras = True
            continue
        canonical = {"страна": "country", "контакты": "contacts", "спам": "spam", "цена": "price"}.get(key, key)
        present.add(canonical)
        rendered = str(value)
        if canonical == "country":
            rendered = country_name(value)
        elif canonical == "spam":
            rendered = {"no": "без спамблока", "false": "без спамблока", "0": "без спамблока", "yes": "со спамблоком", "true": "со спамблоком", "1": "со спамблоком"}.get(str(value).lower(), "неизвестное значение")
        elif canonical == "price":
            rendered = money(value)
        elif canonical in ("contacts", "dialogs", "channels", "chats"):
            rendered = number(value)
        else:
            rendered = {"1": "да", "true": "да", "yes": "да", "0": "нет", "false": "нет", "no": "нет"}.get(str(value).lower(), str(value))
        lines.append(labels[key] + ": " + ops.get(op, "") + rendered)
    for key, label, default in (("country", "🌍 Страна", "любая страна"), ("contacts", "👥 Контакты", "без ограничений"), ("spam", "🚫 Спамблок", "не имеет значения"), ("price", "💰 Цена", "без ограничений")):
        if key not in present:
            lines.append(label + ": " + default)
    if extras:
        lines.append("Также заданы дополнительные условия расширенного фильтра.")
    return "\n".join(lines)
