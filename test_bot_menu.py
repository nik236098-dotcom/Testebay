"""Offline menu, status and message-reuse regressions."""
from __future__ import annotations

import ast
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import monitor
from bot_menu import criteria, lzt_criteria, money, settings_from_query, tron_criteria
from test_bot_text import CheckedHTML


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.p = {"user_id": 1, "name": "owner", "chats": [10], "query": "country[]=UZ&min_contacts=80&spam=no&pmax=200",
                  "tron_filter": "country=UZ contacts>=80 spam=no price<=200", "category": "telegram",
                  "seen": {"lzt": [], "tron": []}, "init": {"lzt": False, "tron": False}}
        self.tg = Mock()
        self.tg.send_screen.return_value = 100
        self.tg.edit_text.return_value = True
        self.tg.can_recreate_screen.return_value = False
        self.state = SimpleNamespace(users={}, get=lambda uid: self.p if uid == 1 else None, save=Mock(), owner_id=1,
                                     country_ids={"UZ": 1}, lock=threading.RLock())
        self.cfg = SimpleNamespace(poll_interval=30, owner_ids={1}, tron_category='telegram')
        self.bot = monitor.Bot(self.cfg, self.state, self.tg)
        self.rt = SimpleNamespace(lzt=Mock(), tron=Mock(), stats={"last_items": 0, "last_new": 0,
                                  "tron_items": 0, "tron_new": 0, "lzt_last_ok": True, "tron_last_ok": True},
                                  lock=threading.Lock(), record_error=Mock())
        self.bot.runtime = Mock(return_value=self.rt)

    def message(self, text):
        self.bot.handle({"message": {"chat": {"id": 10}, "from": {"id": 1}, "text": text}})

    def test_text_opens_new_menu_and_screen_commands_edit(self):
        for cmd in ('/start', '/menu', '/settings', '/status', '/help', 'меню', '/unknown'):
            self.message(cmd)
        self.assertEqual(self.tg.send_screen.call_count, 4)
        self.assertEqual(self.tg.edit_text.call_count, 3)
        self.assertTrue(all(call.args[1] == 100 for call in self.tg.edit_text.call_args_list))
        self.tg.send.assert_not_called()
        self.assertEqual(self.p['menu_messages'], {'10': 100})

    def test_message_id_survives_bot_restart(self):
        self.bot.show_menu(self.p, 10)
        second = monitor.Bot(self.cfg, self.state, self.tg)
        second.show_menu(self.p, 10)
        self.assertEqual(self.tg.send_screen.call_count, 1)
        self.assertEqual(self.tg.edit_text.call_args.args[1], 100)

    def test_balance_updates_existing_screen_for_both_sites(self):
        self.p.update(lzt_token='test-lzt', tron_token='test-tron')
        self.bot.show_menu(self.p, 10)
        with patch.object(monitor.LztClient, 'balance', return_value='1 250 ₽') as lzt, patch.object(monitor.TronClient, 'balance', return_value='455 ₽') as tron:
            self.bot.show_balances(self.p, 10)
            for job in list(self.bot.jobs.values()):
                job.join(2)
            text = self.tg.edit_text.call_args.args[2]
            self.assertIn('1 250 ₽', text)
            self.assertIn('455 ₽', text)
            lzt.assert_called_once()
            tron.assert_called_once()
        self.assertEqual(self.tg.send_screen.call_count, 1)
        self.tg.send.assert_not_called()

    def test_slow_balance_does_not_overwrite_navigation(self):
        self.p['lzt_token'] = 'test-lzt'
        entered, release = threading.Event(), threading.Event()
        def balance():
            entered.set()
            release.wait(2)
            return '1 ₽'
        with patch.object(monitor.LztClient, 'balance', side_effect=balance):
            self.bot.show_balances(self.p, 10)
            self.assertTrue(entered.wait(1))
            self.bot.show_menu(self.p, 10)
            count = self.tg.edit_text.call_count
            release.set()
            for job in list(self.bot.jobs.values()):
                job.join(2)
            self.assertEqual(self.tg.edit_text.call_count, count)
            self.assertIn('Главное меню', self.tg.edit_text.call_args.args[2])

    def test_status_shows_applied_criteria_not_unsaved_draft(self):
        self.p['settings'] = {"country": 'RU', "contacts": 0, "spam": 'any', "price_max": None}
        self.bot.cmd_status(self.p, self.rt, 10)
        text = self.tg.send_screen.call_args.args[1]
        self.assertIn('Узбекистан', text)
        self.assertIn('200 ₽', text)
        self.assertNotIn('Россия', text)
        self.assertNotIn('country=', text)
        self.assertNotIn('pmax=', text)
        CheckedHTML(text)

    def test_status_failure_does_not_claim_zero_results(self):
        self.rt.stats.update(lzt_last_ok=False, tron_last_ok=False, lzt_last_error='HTTP 503 Bad Gateway', tron_last_error='Read timed out')
        self.bot.cmd_status(self.p, self.rt, 10)
        text = self.tg.send_screen.call_args.args[1]
        self.assertIn('Количество аккаунтов сейчас неизвестно', text)
        self.assertNotIn('Подходящих в последней проверке: <b>0', text)
        self.assertNotIn('HTTP', text)

    def test_settings_import_price_limit_from_link(self):
        s = self.bot.current_settings(self.p)
        self.assertEqual(s['price_max'], 200)
        self.assertIn('до 200 ₽', self.bot.settings_text(self.p))

    def test_any_country_remains_any(self):
        self.bot.handle_settings_callback({'id': 'q', 'from': {'id': 1}, 'message': {'chat': {'id': 10}, 'message_id': 100}}, ['set', 'country', 'any'])
        self.assertEqual(self.p['settings']['country'], 'any')
        self.assertIn('любая страна', self.tg.edit_text.call_args.args[2])

    def test_clear_price_and_save_edits_existing_screen(self):
        self.bot.show_menu(self.p, 10)
        s = self.bot.current_settings(self.p)
        s['price_min'] = s['price_max'] = None
        self.state.reset_seen = Mock()
        self.rt.rebuild = Mock()
        self.bot.apply_settings(self.p, 10)
        self.assertNotIn('pmax', self.p['query'])
        self.assertNotIn('price', self.p['tron_filter'])
        self.assertIn('без ограничений', self.tg.edit_text.call_args.args[2])
        self.assertEqual(self.tg.send_screen.call_count, 1)

    def test_disabled_sites_stay_disabled_in_runtime(self):
        self.p.update(lzt_enabled=False, tron_enabled=False, lzt_token='test', tron_token='test')
        rt = monitor.UserRuntime(self.cfg, self.state, self.p)
        self.assertIsNone(rt.lzt)
        self.assertIsNone(rt.tron)

    def test_network_edit_failure_does_not_create_duplicate(self):
        self.p['menu_messages'] = {'10': 100}
        self.tg.edit_text.return_value = False
        self.bot.show_menu(self.p, 10)
        self.tg.send_screen.assert_not_called()

    def test_deleted_screen_is_replaced_and_saved(self):
        self.p['menu_messages'] = {'10': 50}
        self.tg.edit_text.return_value = False
        self.tg.can_recreate_screen.return_value = True
        self.bot.show_menu(self.p, 10)
        self.tg.send_screen.assert_called_once()
        self.assertEqual(self.p['menu_messages']['10'], 100)

    def test_read_only_check_renders_one_screen(self):
        self.rt.lzt.last_error = None
        self.rt.lzt.fetch_items.return_value = []
        self.rt.tron.last_error = None
        self.rt.tron.last_total = 0
        self.rt.tron.fetch_items.return_value = []
        self.bot.show_menu(self.p, 10)
        self.bot.start_check(self.p, self.rt, 10)
        for job in list(self.bot.jobs.values()):
            job.join(2)
        self.assertEqual(self.tg.send_screen.call_count, 1)
        self.tg.send.assert_not_called()
        self.assertIn('Результат проверки', self.tg.edit_text.call_args.args[2])

    def test_unauthorized_navigation_cannot_read_balances(self):
        self.bot.handle_callback({'id': 'q', 'from': {'id': 2}, 'message': {'chat': {'id': 10}, 'message_id': 100}, 'data': 'nav:balances'})
        self.assertEqual(self.bot.jobs, {})
        self.tg.send_screen.assert_not_called()
        self.tg.edit_text.assert_not_called()


class LabelTests(unittest.TestCase):
    def test_money_zero_and_decimal(self):
        self.assertEqual(money(0), '0 ₽')
        self.assertEqual(money('1234.50'), '1 234,5 ₽')
        self.assertEqual(money(None), 'баланс недоступен')

    def test_criteria_in_russian(self):
        text = lzt_criteria('country[]=UZ&min_contacts=80&spam=no&pmax=200')
        self.assertIn('Узбекистан', text)
        self.assertIn('не меньше 80', text)
        self.assertIn('без спамблока', text)
        self.assertIn('до 200 ₽', text)
        text = tron_criteria(monitor.parse_filter('country=UZ contacts>=80 spam=no price<=200'))
        self.assertIn('Узбекистан', text)
        self.assertIn('не больше 200 ₽', text)

    def test_future_annotations_and_python38_syntax(self):
        for path in ('bot_menu.py', 'bot_text.py', 'monitor.py'):
            source = Path(path).read_text()
            ast.parse(source, feature_version=(3, 8))
            self.assertIn('from __future__ import annotations', source)

    def test_unchanged_telegram_edit_is_success(self):
        tg = monitor.Telegram('offline')
        response = Mock(status_code=400, ok=False)
        response.json.return_value = {'ok': False, 'description': 'Bad Request: message is not modified'}
        tg.session.post = Mock(return_value=response)
        self.assertTrue(tg.edit_text(10, 100, 'Меню'))


if __name__ == '__main__':
    unittest.main()


class OriginTests(unittest.TestCase):
    """Происхождение аккаунта печатается для лотов обеих площадок."""

    def test_lzt_code_is_translated(self):
        text = monitor.format_item({"item_id": 1, "price": 10, "item_origin": "brute"})
        self.assertIn("📦 Происхождение: брут", text)

    def test_lzt_phrase_used_when_code_unknown(self):
        text = monitor.format_item({"item_id": 1, "price": 10, "item_origin": "something_new",
                                    "itemOriginPhrase": "Новый тип"})
        self.assertIn("📦 Происхождение: Новый тип", text)

    def test_lzt_phrase_only(self):
        text = monitor.format_item({"item_id": 1, "price": 10, "itemOriginPhrase": "Брут"})
        self.assertIn("📦 Происхождение: Брут", text)

    def test_lzt_resale_shows_original_origin(self):
        text = monitor.format_item({"item_id": 1, "price": 10, "item_origin": "resale", "resale_item_origin": "stealer"})
        self.assertIn("📦 Происхождение: перепродажа (стилер)", text)

    def test_tron_origin_under_other_keys(self):
        from tron_source import normalize
        for raw, expected in (
            ({"item_id": 5, "price": 1, "origin": "autoreg"}, "авторег"),
            ({"item_id": 5, "price": 1, "origin_name": "Брут"}, "Брут"),
            ({"item_id": 5, "price": 1, "origin": {"id": 3, "name": "stealer"}}, "стилер"),
            ({"item_id": 5, "price": 1, "origin_id": 7}, "ID 7"),
            ({"item_id": 5, "price": 1, "accountOrigin": "personal"}, "личный"),
            ({"item_id": 5, "price": 1, "original_price": 99, "origin": "farm"}, "ферма"),
        ):
            with self.subTest(raw=raw):
                item = normalize(raw)
                self.assertIn(f"📦 Происхождение: {expected}", monitor.format_item(item))
                self.assertIn("🏪 tronaccs", monitor.format_item(item))

    def test_no_origin_no_line(self):
        from tron_source import normalize
        self.assertNotIn("Происхождение", monitor.format_item({"item_id": 1, "price": 10}))
        self.assertNotIn("Происхождение", monitor.format_item(normalize({"item_id": 5, "price": 1, "item_origin": ""})))

    def test_origin_is_html_escaped(self):
        text = monitor.format_item({"item_id": 1, "price": 10, "item_origin": "<b>x</b>"})
        self.assertIn("📦 Происхождение: &lt;b&gt;x&lt;/b&gt;", text)


class OriginSettingTests(unittest.TestCase):
    """Фильтр по происхождению аккаунта в /settings и AutoBuy."""

    def setUp(self):
        self.p = {"user_id": 1, "name": "owner", "chats": [10],
                  "query": "country[]=UZ&min_contacts=80&spam=no&pmax=200",
                  "tron_filter": "country=UZ contacts>=80 spam=no price<=200", "category": "telegram",
                  "seen": {"lzt": [], "tron": []}, "init": {"lzt": False, "tron": False}}
        self.tg = Mock()
        self.tg.send_screen.return_value = 100
        self.tg.edit_text.return_value = True
        self.tg.can_recreate_screen.return_value = False
        self.state = SimpleNamespace(users={}, get=lambda uid: self.p if uid == 1 else None, save=Mock(),
                                     owner_id=1, country_ids={"UZ": 1}, lock=threading.RLock(),
                                     reset_seen=Mock())
        self.cfg = SimpleNamespace(poll_interval=30, owner_ids={1}, tron_category='telegram')
        self.bot = monitor.Bot(self.cfg, self.state, self.tg)
        self.rt = SimpleNamespace(lzt=Mock(), tron=Mock(),
                                  stats={"last_items": 0, "tron_items": 0}, lock=threading.Lock(),
                                  record_error=Mock(), rebuild=Mock())
        self.bot.runtime = Mock(return_value=self.rt)

    def _cb(self, parts):
        self.bot.handle_settings_callback(
            {'id': 'q', 'from': {'id': 1}, 'message': {'chat': {'id': 10}, 'message_id': 100}}, parts)

    def test_criteria_shows_origin_line(self):
        self.assertIn("📦 Происхождение: не имеет значения", criteria(settings_from_query(self.p["query"])))

    def test_query_origins_parsed_and_named(self):
        s = settings_from_query("country[]=UZ&origin[]=brute&origin[]=dummy")
        self.assertEqual(s["origins"], ["brute", "dummy"])
        self.assertIn("📦 Происхождение: брут, пустышка", criteria(s))

    def test_pick_two_origins_accumulates(self):
        self.bot.current_settings(self.p)
        self._cb(['set', 'origin', 'autoreg'])
        self._cb(['set', 'origin', 'dummy'])
        self.assertEqual(self.p['settings']['origins'], ['autoreg', 'dummy'])
        self.assertIn('авторег, пустышка', self.tg.edit_text.call_args.args[2])

    def test_pick_same_origin_twice_toggles_off(self):
        self.bot.current_settings(self.p)
        self._cb(['set', 'origin', 'stealer'])
        self._cb(['set', 'origin', 'stealer'])
        self.assertEqual(self.p['settings']['origins'], [])

    def test_any_origin_clears_all(self):
        self.bot.current_settings(self.p)
        self.p['settings']['origins'] = ['brute', 'dummy']
        self._cb(['set', 'origin', 'any'])
        self.assertEqual(self.p['settings']['origins'], [])

    def test_legacy_single_origin_is_migrated(self):
        self.bot.current_settings(self.p)
        self.p['settings'].pop('origins', None)
        self.p['settings']['origin'] = 'brute'
        self._cb(['set', 'origin', 'dummy'])
        self.assertEqual(self.p['settings']['origins'], ['brute', 'dummy'])
        self.assertNotIn('origin', self.p['settings'])

    def test_apply_writes_multiple_origins_to_both_filters(self):
        self.bot.show_menu(self.p, 10)
        s = self.bot.current_settings(self.p)
        s['origins'] = ['autoreg', 'dummy']
        self.bot.apply_settings(self.p, 10)
        self.assertIn('origin[]=autoreg', self.p['query'])
        self.assertIn('origin[]=dummy', self.p['query'])
        self.assertIn('origin=autoreg,dummy', self.p['tron_filter'])

    def test_apply_no_origin_leaves_filters_clean(self):
        self.bot.show_menu(self.p, 10)
        self.bot.apply_settings(self.p, 10)
        self.assertNotIn('origin', self.p['query'])
        self.assertNotIn('origin', self.p['tron_filter'])

    def test_origins_not_flagged_as_link_extra(self):
        text = lzt_criteria("country[]=UZ&origin[]=brute&origin[]=dummy")
        self.assertNotIn("Дополнительные условия", text)

    def test_tron_filter_matches_any_of_several_origins(self):
        from tron_source import normalize, match_filter, parse_filter
        it = normalize({"id": 1, "price": 1, "item_origin": {"type": "stealer", "title": "Стиллер"}})
        self.assertTrue(match_filter(it, parse_filter("origin=autoreg,stealer")))
        self.assertFalse(match_filter(it, parse_filter("origin=autoreg,dummy")))

    def test_autobuy_multiple_origins_in_params_and_filter(self):
        from monitor import autobuy_lzt_params, autobuy_tron_filter
        s = {"country": "any", "contacts": 0, "spam": "any", "origins": ["stealer", "brute"],
             "price_min": None, "price_max": None}
        params = autobuy_lzt_params(s)
        self.assertIn(("origin[]", "stealer"), params)
        self.assertIn(("origin[]", "brute"), params)
        self.assertIn("origin=stealer,brute", autobuy_tron_filter(s))


class TronNestedApiTests(unittest.TestCase):
    """Разбор текущего ответа tronaccs: список в data, поля во вложенном telegram,
    происхождение объектом item_origin."""

    SAMPLE = {
        "id": 1376608, "category_id": 1, "title": "Прямо с панельки", "price": 50,
        "time_add": "2026-09-29T22:28:12.000Z",
        "telegram": {"country": {"name": "Россия", "countryCode": "RU"}, "premium": 0,
                     "spamblock": 1, "two_factor": 1, "contacts": 321, "dialogs": 267,
                     "channels": 19, "admin_channels_count": 0, "dc_id": None},
        "item_origin": {"id": 2, "type": "phish", "title": "Фишинг"},
        "seller": {"id": 883, "username": "eoy52"},
    }

    def test_normalize_maps_nested_fields(self):
        from tron_source import normalize
        it = normalize(self.SAMPLE)
        self.assertEqual(it["source"], "tron")
        self.assertEqual(it["item_id"], 1376608)
        self.assertEqual(it["telegram_country"], "RU")
        self.assertEqual(it["telegram_contacts_count"], 321)
        self.assertEqual(it["telegram_conversations_count"], 267)
        self.assertEqual(it["telegram_channels_count"], 19)
        self.assertIs(it["telegram_spam_block"], True)
        self.assertEqual(it["seller_username"], "eoy52")
        self.assertIsNotNone(it["published_date"])
        self.assertEqual(it["item_origin"], "phish")
        self.assertEqual(it["item_origin_title"], "Фишинг")

    def test_format_shows_origin_and_spam(self):
        from tron_source import normalize
        text = monitor.format_item(normalize(self.SAMPLE))
        self.assertIn("📦 Происхождение: фишинг", text)
        self.assertIn("🚫 Спамблок: есть ⛔", text)
        self.assertIn("🏪 tronaccs", text)

    def test_no_spamblock_shows_net(self):
        from tron_source import normalize
        raw = dict(self.SAMPLE, telegram=dict(self.SAMPLE["telegram"], spamblock=0))
        self.assertIn("🚫 Спамблок: нет ✅", monitor.format_item(normalize(raw)))

    def test_origin_filter_matches_across_platforms(self):
        from tron_source import normalize, match_filter, parse_filter
        it = normalize(self.SAMPLE)  # tron type "phish"
        self.assertTrue(match_filter(it, parse_filter("origin=fishing")))   # код lzt
        self.assertTrue(match_filter(it, parse_filter("origin=phish")))     # тип tronaccs
        self.assertFalse(match_filter(it, parse_filter("origin=brute")))

    def test_fetch_items_reads_data_key(self):
        from tron_source import TronApiClient
        client = TronApiClient("x", "telegram", 1)
        client.fetch_raw_page = lambda page, params, retries=5: {"data": [self.SAMPLE], "hasMore": False}
        items = client.fetch_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_origin"], "phish")

    def test_confirmed_tron_origin_types(self):
        from tron_source import normalize, match_filter, parse_filter
        for type_, picker, ru in (("phish", "fishing", "фишинг"), ("stealer", "stealer", "стилер")):
            raw = dict(self.SAMPLE, item_origin={"id": 0, "type": type_, "title": "x"})
            it = normalize(raw)
            with self.subTest(type=type_):
                self.assertIn(f"\U0001F4E6 Происхождение: {ru}", monitor.format_item(it))
                self.assertTrue(match_filter(it, parse_filter(f"origin={picker}")))

    def test_scalar_and_missing_origin_still_work(self):
        from tron_source import normalize
        self.assertEqual(normalize({"id": 1, "price": 1, "item_origin": "brute"})["item_origin"], "brute")
        self.assertIsNone(normalize({"id": 1, "price": 1})["item_origin"])


class LztPaginationTests(unittest.TestCase):
    """lzt перелистывает страницы, пока не дойдёт до уже виденных лотов."""

    def _client(self, pages):
        c = monitor.LztClient("t")
        def fake(category, params, retries=5):
            page = 1
            for k, v in params:
                if k == "page":
                    page = int(v)
            return pages.get(page, [])
        c._fetch_page = fake
        return c

    def test_collects_multiple_pages(self):
        pages = {1: [{"item_id": i} for i in (1, 2, 3)],
                 2: [{"item_id": i} for i in (4, 5, 6)], 3: []}
        items = self._client(pages).fetch_items("telegram", [("order_by", "x")], max_pages=3)
        self.assertEqual(sorted(i["item_id"] for i in items), [1, 2, 3, 4, 5, 6])

    def test_stops_when_seen_lot_appears(self):
        pages = {1: [{"item_id": 10}, {"item_id": 11}],
                 2: [{"item_id": 12}, {"item_id": 5}],
                 3: [{"item_id": 99}]}
        items = self._client(pages).fetch_items("telegram", [("order_by", "x")], max_pages=5, stop_ids={5})
        ids = [i["item_id"] for i in items]
        self.assertIn(12, ids)
        self.assertNotIn(99, ids)  # 3-ю страницу уже не запрашиваем

    def test_default_is_single_page(self):
        pages = {1: [{"item_id": 1}], 2: [{"item_id": 2}]}
        items = self._client(pages).fetch_items("telegram", [("order_by", "x")])
        self.assertEqual([i["item_id"] for i in items], [1])

    def test_short_page_is_last(self):
        pages = {1: [{"item_id": 1}, {"item_id": 2}], 2: [{"item_id": 3}], 3: [{"item_id": 4}]}
        items = self._client(pages).fetch_items("telegram", [("order_by", "x")], max_pages=3)
        self.assertEqual(sorted(i["item_id"] for i in items), [1, 2, 3])


class TronPaginationTests(unittest.TestCase):
    """tronaccs листает до уже виденных лотов и не превышает лимит страниц."""

    def _client(self, pages, max_pages):
        from tron_source import TronApiClient
        c = TronApiClient("x", "telegram", max_pages)
        c.fetch_raw_page = lambda page, params, retries=5: pages.get(page, {"data": []})
        return c

    def test_stops_when_seen_lot_appears(self):
        pages = {1: {"data": [{"id": 10}, {"id": 11}]},
                 2: {"data": [{"id": 12}, {"id": 5}]},
                 3: {"data": [{"id": 99}]}}
        items = self._client(pages, 5).fetch_items(stop_ids={5})
        ids = [i["item_id"] for i in items]
        self.assertIn(12, ids)
        self.assertNotIn(99, ids)

    def test_respects_page_cap(self):
        pages = {1: {"data": [{"id": 1}, {"id": 2}]},
                 2: {"data": [{"id": 3}, {"id": 4}]},
                 3: {"data": [{"id": 5}]}}
        items = self._client(pages, 2).fetch_items()
        self.assertEqual(sorted(i["item_id"] for i in items), [1, 2, 3, 4])
