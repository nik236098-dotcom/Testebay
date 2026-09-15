"""Offline regression checks: no Telegram or marketplace requests."""

import html
import threading
import unittest
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock, patch

import monitor
from bot_text import display_value, readable, split_html, user_error
from tron_source import _readable


class CheckedHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.feed(text)
        self.close()
        assert not self.stack, self.stack

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "a", "code", "pre"}, tag
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag

    def handle_data(self, text):
        self.text.append(text)

    @property
    def visible(self):
        return "".join(self.text)


class TextTests(unittest.TestCase):
    def test_help_has_valid_html(self):
        text = monitor.Bot.HELP + monitor.Bot.OWNER_HELP
        self.assertIn("/filter <ссылка>", CheckedHTML(text).visible)
        self.assertIn("/kick <id>", CheckedHTML(text).visible)

    def test_nested_errors_and_escaped_russian(self):
        data = {"errors": {"token": [{"message": r"\u041d\u0435\u0432\u0435\u0440\u043d\u043e"}]}}
        for fn in (readable, monitor._errors_text, _readable):
            self.assertEqual(fn(data), "Неверно")
        self.assertEqual(readable({"errors": ["Первое", {"message": "Второе"}]}), "Первое; Второе")

    def test_escaped_emoji(self):
        self.assertEqual(readable(r"\ud83d\udc0a"), "🐊")

    def test_error_localization(self):
        for source in ("HTTPSConnectionPool(host='example.org')", "HTTP 503 <h1>Bad Gateway</h1>",
                       "Read timed out", "HTTP 401 Unauthorized", "HTTP 429 Too Many Requests", "UNKNOWN_ERROR"):
            actual = user_error(source)
            self.assertRegex(actual, "[А-Яа-я]")
            self.assertNotIn("example.org", actual)
            self.assertNotIn("HTTP", actual)
        self.assertEqual(user_error({"errors": [{"message": "Недостаточно средств"}]}), "Недостаточно средств")

    def test_missing_values_preserve_zero(self):
        self.assertEqual(display_value(0), "0")
        self.assertEqual(display_value(None), "не указан")
        self.assertEqual(display_value(""), "не указан")

    def test_html_chunks_preserve_text_and_balanced_tags(self):
        visible = ("Привет & <мир> 🐊\n" * 700)
        source = '<b><i>' + html.escape(visible) + '</i></b>'
        chunks = split_html(source)
        self.assertGreater(len(chunks), 1)
        texts = [CheckedHTML(chunk).visible for chunk in chunks]
        self.assertEqual("".join(texts), visible)
        self.assertTrue(all(len(t.encode('utf-16-le')) // 2 <= 4096 for t in texts))

    def test_unknown_placeholder_and_chunk_boundary(self):
        self.assertEqual(CheckedHTML(split_html('/help <placeholder>')[0]).visible, '/help <placeholder>')
        source = '<pre>' + '&amp;' * 4096 + '🐊</pre>'
        chunks = split_html(source)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(CheckedHTML(chunks[-1]).visible, '🐊')

    def test_send_and_edit_keep_keyboard_on_last_chunk(self):
        tg = monitor.Telegram('offline-test')
        tg.call = Mock(return_value={})
        keyboard = {"inline_keyboard": [[{"text": "Меню", "callback_data": "menu"}]]}
        source = '<b>' + '🐊' * 3000 + '</b>'
        self.assertTrue(tg.send(1, source, keyboard))
        calls = tg.call.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotIn('reply_markup', calls[0].args[1])
        self.assertEqual(calls[-1].args[1]['reply_markup'], keyboard)
        tg.call.reset_mock()
        tg.edit_text(1, 10, source, keyboard)
        calls = tg.call.call_args_list
        self.assertEqual(calls[0].args[0], 'editMessageText')
        self.assertEqual(calls[-1].args[0], 'sendMessage')
        for call in calls:
            CheckedHTML(call.args[1]['text'])

    def test_failed_chunk_stops_sending(self):
        tg = monitor.Telegram('offline-test')
        tg.call = Mock(return_value=None)
        self.assertFalse(tg.send(1, 'x' * 9000))
        self.assertEqual(tg.call.call_count, 1)

    def test_missing_account_fields(self):
        client = monitor.LztClient('offline-test')
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = {"user": {"user_id": 1, "balance": None}}
        client.session.get = Mock(return_value=response)
        self.assertNotIn('None', client.check_token()[1])
        tron = monitor.TronClient('offline-test', '', {}, pages=1)
        tron.api.me = Mock(return_value={"id": 1, "balance": 0})
        self.assertNotIn('None', tron.check_token()[1])
        self.assertEqual(tron.balance(), '0')


class TokenTests(unittest.TestCase):
    def make_bot(self):
        bot = monitor.Bot.__new__(monitor.Bot)
        profile = {"user_id": 1, "lzt_token": "old", "tron_token": "existing"}
        bot.tg = Mock()
        bot.state = SimpleNamespace(save=Mock(), country_ids={})
        bot.cfg = SimpleNamespace(tron_category='telegram')
        bot.jobs, bot.awaiting = {}, {}
        bot.jobs_lock, bot.flow_lock = threading.RLock(), threading.RLock()
        bot.profile = Mock(return_value=profile)
        bot.finish_setup = Mock()
        return bot, profile

    def test_slow_token_does_not_block_and_duplicate_is_ignored(self):
        bot, profile = self.make_bot()
        entered, release = threading.Event(), threading.Event()
        def check():
            entered.set()
            self.assertTrue(release.wait(2))
            return True, 'Пользователь'
        with patch.object(monitor.LztClient, 'check_token', side_effect=check) as checker:
            bot.receive_token(profile, 10, 'lzt_token', 'new', 5)
            self.assertTrue(entered.wait(1))
            bot.receive_token(profile, 10, 'lzt_token', 'duplicate', 6)
            self.assertEqual(profile['lzt_token'], 'old')
            release.set()
            bot.jobs[(1, '/token')].join(2)
            self.assertEqual(profile['lzt_token'], 'new')
            self.assertEqual(checker.call_count, 1)
            bot.finish_setup.assert_called_once()

    def test_cancelled_token_result_does_not_change_profile(self):
        bot, profile = self.make_bot()
        entered, release = threading.Event(), threading.Event()
        def check():
            entered.set()
            release.wait(2)
            return True, 'Пользователь'
        with patch.object(monitor.LztClient, 'check_token', side_effect=check):
            bot.receive_token(profile, 10, 'lzt_token', 'new', 5)
            self.assertTrue(entered.wait(1))
            with bot.flow_lock:
                bot.awaiting.pop(10)
            release.set()
            bot.jobs[(1, '/token')].join(2)
            self.assertEqual(profile['lzt_token'], 'old')
            bot.state.save.assert_not_called()
            bot.finish_setup.assert_not_called()

    def test_failed_token_is_russian_and_can_be_retried(self):
        bot, profile = self.make_bot()
        with patch.object(monitor.LztClient, 'check_token', return_value=(False, 'Read timed out')):
            bot.receive_token(profile, 10, 'lzt_token', 'new', 5)
            bot.jobs[(1, '/token')].join(2)
        self.assertEqual(profile['lzt_token'], 'old')
        self.assertEqual(bot.awaiting[10], (1, 'lzt_token'))
        self.assertIn('не ответил вовремя', bot.tg.send.call_args.args[1])

    def test_start_and_help_reply(self):
        bot, profile = self.make_bot()
        profile['chats'] = [10]
        bot.runtime = Mock(return_value=None)
        bot.is_owner = Mock(return_value=False)
        for command in ('/start', '/help', '/unknown'):
            bot.handle({'message': {'chat': {'id': 10}, 'from': {'id': 1}, 'text': command}})
            self.assertIn('/settings', CheckedHTML(bot.tg.send.call_args.args[1]).visible)

    def test_command_exception_notifies_user(self):
        bot, _ = self.make_bot()
        bot.state.tg_offset = 0
        update = {'update_id': 1, 'message': {'chat': {'id': 10}, 'from': {'id': 1}, 'text': '/help'}}
        bot.state.lock = threading.RLock()
        bot.tg.get_updates.side_effect = [[update], KeyboardInterrupt]
        bot.handle = Mock(side_effect=ValueError('technical details'))
        with self.assertLogs('lzt-monitor', level='ERROR'):
            with self.assertRaises(KeyboardInterrupt):
                bot.run_forever()
        self.assertIn('Не удалось выполнить команду', bot.tg.send.call_args.args[1])
        self.assertNotIn('technical details', bot.tg.send.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
