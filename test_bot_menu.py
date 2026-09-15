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

    def test_menu_commands_edit_same_message(self):
        for cmd in ('/start', '/menu', '/settings', '/status', '/help', 'меню', '/unknown'):
            self.message(cmd)
        self.assertEqual(self.tg.send_screen.call_count, 1)
        self.assertEqual(self.tg.edit_text.call_count, 6)
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
