# Монитор новых лотов lzt.market → Telegram

Скрипт раз в минуту (настраивается) проверяет лоты на lzt.market по вашим фильтрам
и присылает каждый новый лот в Telegram: название, цена, страна, контакты, спамблок,
ссылка на лот.

По умолчанию следит за
`https://lzt.market/telegram/?country[]=UZ&min_contacts=100&spam=no`.

Данные берутся из официального API Lolzteam Market (`prod-api.lzt.market`),
а не парсингом HTML: сайт закрыт защитой от ботов, а API принимает те же
параметры фильтров, что и адресная строка сайта.

## Что нужно

1. **Токен API Lolzteam.** На https://lolz.live/account/api создайте токен
   с областью `market` и положите в `LZT_TOKEN`.
2. **Telegram-бот.** У @BotFather выполните `/newbot`, токен положите в `TG_BOT_TOKEN`.
3. **chat_id.** Напишите боту любое сообщение, затем `python get_chat_id.py`
   покажет ваш `TG_CHAT_ID`.

## Запуск

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # заполнить токены и chat_id
python monitor.py --test  # проверить, что бот пишет в чат
python monitor.py         # запустить мониторинг
```

При первом запуске скрипт запоминает уже существующие лоты и молчит.
Дальше присылает только новые. Если хотите получить и текущие лоты,
поставьте `NOTIFY_ON_FIRST_RUN=1` перед первым запуском.

## Изменить фильтры

Скопируйте часть ссылки после `?` в `LZT_QUERY`, а категорию (часть пути) в `LZT_CATEGORY`:

```
https://lzt.market/telegram/?country[]=UZ&min_contacts=100&spam=no&pmax=500
                   ^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                   LZT_CATEGORY                LZT_QUERY
```

Параметр `order_by` из ссылки игнорируется: для поиска новых лотов монитор
всегда сортирует по дате публикации. Цена приходит в каждом сообщении, а
ограничить её можно параметром `pmax=` (максимум) и `pmin=` (минимум).

## Постоянная работа

- **systemd:** пример юнита в `lzt-monitor.service`.
- **Docker:** `docker compose up -d --build` (состояние хранится в `./data`).

## Файлы

- `monitor.py` — сам монитор (`--once` для одной проверки, `--test` для тестового сообщения).
- `get_chat_id.py` — показывает chat_id.
- `state.json` — список уже отправленных лотов, создаётся автоматически.
