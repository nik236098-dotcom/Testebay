"""Human-readable service errors and Telegram-safe message formatting."""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser


def readable(value) -> str:
    """Unwrap nested error responses without displaying Python containers."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, dict):
        for key in ("message", "errors", "error", "result"):
            if value.get(key):
                return readable(value[key])
        return "; ".join(filter(None, (readable(v) for v in value.values())))
    if isinstance(value, (list, tuple)):
        return "; ".join(filter(None, (readable(v) for v in value)))
    text = str(value)
    if text.lstrip().startswith(('{', '[', '"')):
        try:
            decoded = json.loads(text)
            if decoded != value:
                return readable(decoded)
        except (ValueError, TypeError):
            pass
    text = re.sub(r"(?:\\u[0-9a-fA-F]{4})+", lambda m: json.loads('"' + m[0] + '"'), text)
    text = text.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def user_error(value) -> str:
    """Keep transport diagnostics and untranslated service codes out of chats."""
    text = readable(value)
    low = text.lower()
    if any(s in low for s in ("401", "403", "unauthorized", "forbidden", "invalid token", "invalid api", "unknown api", "tron_token")):
        return "Сервис не принял ключ доступа. Проверьте токен и его права."
    if any(s in low for s in ("429", "too many", "rate limit", "лимит запросов")):
        return "Слишком много запросов. Подождите немного и повторите попытку."
    if any(s in low for s in ("timeout", "timed out", "readtimeout")):
        return "Сайт не ответил вовремя. Попробуйте ещё раз позже."
    if any(s in low for s in ("connection", "httpsconnectionpool", "ssl", "сеть", "502", "503", "504", "cloudflare", "ddos", "maintenance", "bad gateway")):
        return "Сайт временно недоступен. Попробуйте ещё раз позже."
    if any(s in low for s in ("json", "decode", "traceback")):
        return "Сайт прислал непонятный ответ. Попробуйте ещё раз позже."
    if re.search(r"http\s*[: ]?\s*[45][0-9]{2}", low):
        return "Сервис отклонил запрос. Попробуйте ещё раз позже."
    if re.search(r"[а-яё]", low) and not re.search(r"https?://|[{}\[\]]|\\[a-z]", low):
        return text[:300]
    return "Не удалось выполнить запрос к сервису. Попробуйте ещё раз позже."


def display_value(value, default="не указан") -> str:
    return default if value is None or value == "" else str(value)


class _MessageParser(HTMLParser):
    tags = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "span", "tg-spoiler", "tg-emoji", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        self.events.append(("open", tag, raw) if tag in self.tags else ("text", raw))

    def handle_endtag(self, tag):
        self.events.append(("close", tag))

    def handle_data(self, data):
        self.events.append(("text", data))


def split_html(text: str, limit: int = 4096) -> list[str]:
    """Split visible UTF-16 text, closing/reopening formatting at each boundary."""
    if limit < 2:
        raise ValueError("Message limit must be at least two")
    parser = _MessageParser()
    parser.feed(text)
    parser.close()
    chunks, current, stack = [], [], []
    size = 0

    def flush():
        nonlocal current, size
        if size:
            chunks.append("".join(current) + "".join(f"</{tag}>" for tag, _ in reversed(stack)))
        current = [raw for _, raw in stack]
        size = 0

    def add_text(value):
        nonlocal size
        for char in value:
            units = 2 if ord(char) > 0xFFFF else 1
            if size + units > limit:
                flush()
            current.append(html.escape(char, quote=False))
            size += units

    for event in parser.events:
        kind, value = event[:2]
        if kind == "text":
            add_text(value)
        elif kind == "open":
            stack.append((value, event[2]))
            current.append(event[2])
        elif stack and stack[-1][0] == value:
            current.append(f"</{value}>")
            stack.pop()
        else:
            add_text(f"</{value}>")
    flush()
    return chunks
