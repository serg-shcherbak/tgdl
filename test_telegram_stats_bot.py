import asyncio
import csv
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import telegram_stats_bot as bot


def dt(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class FakeMessage:
    def __init__(self, message_id, day, text=None, *, media=None, replies=None, sender_id=None, reply_to=None, noforwards=False):
        self.id = message_id
        self.date = dt(day)
        self.edit_date = None
        self.text = text
        self.message = text
        self.raw_text = text or ""
        self.media = media
        self.replies = replies
        self.sender_id = sender_id
        self.sender = SimpleNamespace(id=sender_id, username="sender") if sender_id else None
        self.reply_to = reply_to
        self.views = 10
        self.forwards = 2
        self.reactions = None
        self.grouped_id = None
        self.pinned = False
        self.post_author = None
        self.noforwards = noforwards


class FakeClient:
    def __init__(self, entities, posts, comments=None, *, fail_channels=()):
        self.entities = entities
        self.posts = posts
        self.comments = comments or {}
        self.fail_channels = set(fail_channels)
        self.entity_calls = []
        self.iter_calls = []
        self.download_calls = []

    async def get_entity(self, value):
        self.entity_calls.append(value)
        if value in self.fail_channels:
            raise RuntimeError("unavailable")
        return self.entities[value]

    async def get_full_channel(self, entity):
        return SimpleNamespace(
            full_chat=SimpleNamespace(
                about="description",
                participants_count=7,
                linked_chat_id=99,
            )
        )

    async def iter_messages(self, entity, reply_to=None):
        if reply_to is not None and getattr(entity, "id", entity) == 99:
            raise AssertionError("comment iterator must receive source channel entity")
        key = (getattr(entity, "id", entity), reply_to)
        self.iter_calls.append(key)
        values = self.comments.get(key, []) if reply_to is not None else self.posts.get(getattr(entity, "id", entity), [])
        for value in values:
            yield value

    async def download_media(self, message, file):
        self.download_calls.append(message.id)
        with open(file, "wb") as stream:
            stream.write(b"media")
        return file


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"flood wait {seconds}")
        self.seconds = seconds


class LongWaitClient(FakeClient):
    async def iter_messages(self, entity, reply_to=None):
        key = (getattr(entity, "id", entity), reply_to)
        self.iter_calls.append(key)
        if reply_to is None and getattr(entity, "id", None) == 1:
            raise FloodWaitError(120)
        values = self.comments.get(key, []) if reply_to is not None else self.posts.get(getattr(entity, "id", entity), [])
        for value in values:
            yield value


class TelegramExportTests(unittest.TestCase):
    def test_day_first_dates_and_media_prompt(self):
        for value in ('21-08-2026', '21.08.2026', '21/08/2026'):
            self.assertEqual(bot.parse_date_value(value), dt('2026-08-21'))
        self.assertEqual(bot.parse_date_value('29-02-2024'), dt('2024-02-29'))
        for value in ('29-02-2026', '31-02-2026', '2026-08-2026', '1-08-2026'):
            with self.assertRaises(ValueError):
                bot.parse_date_value(value)
        with patch('builtins.input', side_effect=['wrong', 'p']):
            self.assertEqual(bot.ask_media_mode(), 'P')
        with patch('builtins.input', return_value=''):
            self.assertEqual(bot.ask_media_mode(), 'N')
        with patch('builtins.input', return_value=''), patch('sys.stdout', new_callable=io.StringIO):
            today = bot.ask_end_date()
        self.assertEqual(today.hour, 23)

    def test_media_modes_apply_to_posts_and_comments(self):
        from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeVideo
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        media = [
            SimpleNamespace(kind='photo', id=11),
            SimpleNamespace(kind='document', id=12, mime_type='image/png'),
            SimpleNamespace(kind='video', id=13, mime_type='video/mp4'),
            SimpleNamespace(kind='voice', id=14),
            SimpleNamespace(kind='document', id=15, mime_type='application/pdf'),
            SimpleNamespace(kind='video', id=16, document=SimpleNamespace(attributes=[DocumentAttributeAnimated()])),
            SimpleNamespace(kind='video', id=17, document=SimpleNamespace(attributes=[DocumentAttributeVideo(1, 1, 1, round_message=True)])),
        ]
        expected = {'N': set(), 'P': {1, 2, 101}, 'V': {3, 7}, 'A': {1, 2, 3, 4, 5, 6, 7, 101}}
        for mode, ids in expected.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as output:
                posts = [FakeMessage(index, '2026-08-20', media=item) for index, item in enumerate(media, 1)]
                posts[0].replies = SimpleNamespace(replies=1, channel_id=99)
                comment = FakeMessage(101, '2020-01-01', media=SimpleNamespace(kind='photo', id=111))
                client = FakeClient({'channel': entity}, {1: posts}, {(1, 1): [comment]})
                result = asyncio.run(bot.run_exports(client, ['channel'], dt('2026-08-20'), dt('2026-08-20'),
                    media_mode=mode, comments_requested=True, output_dir=output))
                self.assertEqual(result['exit_code'], 0, result['failures'])
                self.assertEqual(set(client.download_calls), ids)
                rows = result['successes'][0]['posts']
                self.assertTrue(all(row['media_type'] for row in rows))
                if mode == 'P':
                    self.assertEqual(rows[2]['media_status'], 'skipped_by_filter')

    def test_album_order_captions_and_native_telegram_entities(self):
        from telethon.tl.types import Message, MessageEntityTextUrl, PeerChannel, InputPeerChannel
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        posts = [FakeMessage(index, '2026-08-20', media=SimpleNamespace(kind='photo', id=index)) for index in (3, 2, 1)]
        for post in posts:
            post.grouped_id = 777
            post.forwards = post.id
        posts[1] = Message(id=2, peer_id=PeerChannel(1), date=dt('2026-08-20'), message='🙂 abc',
            grouped_id=777, entities=[MessageEntityTextUrl(offset=3, length=3, url='https://example.com')], forwards=2)
        posts[1]._input_chat = InputPeerChannel(1, 2)
        posts[1].media = SimpleNamespace(kind='photo', id=2)
        posts[0].replies = SimpleNamespace(replies=2, channel_id=99)
        comments = [FakeMessage(index, '2020-01-01', media=SimpleNamespace(kind='photo', id=index)) for index in (103, 101)]
        for comment in comments:
            comment.grouped_id = 888
        class ContextClient(FakeClient):
            async def download_media(self, message, file):
                if message.id == 2:
                    self_context.assertEqual(message.message, '')
                    self_context.assertEqual(message.input_chat, posts[1].input_chat)
                    self_context.assertEqual(message.date, posts[1].date)
                return await super().download_media(message, file)
        self_context = self
        client = ContextClient({'channel': entity}, {1: posts}, {(1, 3): comments})
        with tempfile.TemporaryDirectory() as output:
            result = asyncio.run(bot.run_exports(client, ['channel'], dt('2026-08-20'), dt('2026-08-20'),
                sort_key='forwards', comments_requested=True, media_mode='P', output_dir=output))
            self.assertEqual(result['exit_code'], 0, result['failures'])
            rows = result['successes'][0]['posts']
            self.assertEqual([row['media_order'] for row in rows], [3, 2, 1])
            self.assertEqual(rows[1]['text'], '🙂 abc')
            self.assertEqual(json.loads(rows[1]['text_entities_json'])[0]['offset'], 3)
            self.assertEqual(posts[1].message, '🙂 abc')
            folder = result['successes'][0]['folder']
            with open(os.path.join(folder, 'comments.jsonl'), encoding='utf-8') as stream:
                records = [json.loads(line) for line in stream][1:]
            self.assertEqual([row['media_order'] for row in records], [2, 1])
            with open(os.path.join(folder, 'AGENTS.md'), encoding='utf-8') as stream:
                guide = stream.read()
            for field in ('grouped_id', 'media_order', 'UTF-16', 'source_post_id', 'export_revision'):
                self.assertIn(field, guide)

    def test_text_is_readable_while_media_is_blocked_and_limit_is_three(self):
        async def scenario(output):
            ready, release = asyncio.Event(), asyncio.Event()
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            posts = [FakeMessage(index, '2026-08-20', f'text {index}', media=SimpleNamespace(kind='video', id=index)) for index in range(1, 7)]
            class SlowClient(FakeClient):
                active = 0
                maximum = 0
                async def download_media(self, message, file):
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                    try:
                        await release.wait()
                        return await super().download_media(message, file)
                    finally:
                        self.active -= 1
            client = SlowClient({'channel': entity}, {1: posts})
            original = bot.publish_export
            def publish(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs['export_revision'] == 1:
                    ready.set()
                return result
            with patch.object(bot, 'publish_export', side_effect=publish):
                task = asyncio.create_task(bot.run_exports(client, ['channel'], dt('2026-08-20'), dt('2026-08-20'), media_mode='V', output_dir=output))
                try:
                    await asyncio.wait_for(ready.wait(), 3)
                    folder = os.path.join(output, 'channel')
                    self.assertTrue(bot.verify_export_generation(folder))
                    self.assertFalse(task.done())
                    self.assertEqual(client.active, 3)
                    with open(os.path.join(folder, 'posts.csv'), encoding='utf-8', newline='') as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), 6)
                    self.assertTrue(all(row['text'] and row['media_status'] == 'pending' and not row['media_local_path'] for row in rows))
                    self.assertTrue(os.path.isfile(os.path.join(folder, 'AGENTS.md')))
                    release.set()
                    result = await asyncio.wait_for(task, 3)
                    self.assertEqual(result['exit_code'], 0, result['failures'])
                    self.assertEqual(client.maximum, 3)
                    self.assertEqual(client.active, 0)
                    self.assertEqual(result['successes'][0]['channel']['export_revision'], 2)
                    for row in result['successes'][0]['posts']:
                        self.assertTrue(os.path.isfile(os.path.join(folder, row['media_local_path'])))
                    self.assertFalse(any(name.startswith('.text-ready-') for name in os.listdir(folder)))
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        with tempfile.TemporaryDirectory() as output:
            asyncio.run(scenario(output))

    def test_replaced_media_gets_new_path_and_failure_preserves_text(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'keep me', media=SimpleNamespace(kind='photo', id=123))
        client = FakeClient({'channel': entity}, {1: [post]})
        with tempfile.TemporaryDirectory() as output:
            first = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            first_path = first['posts'][0]['media_local_path']
            post.media.id = 456
            second = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            self.assertNotEqual(second['posts'][0]['media_local_path'], first_path)
            self.assertTrue(os.path.isfile(os.path.join(first['folder'], first_path)))
            post.media.id = 789
            with patch.object(client, 'download_media', side_effect=OSError('broken attachment')):
                failed = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            self.assertEqual(failed['channel']['media_export_status'], 'partial')
            self.assertEqual(failed['posts'][0]['text'], 'keep me')
            self.assertEqual(failed['posts'][0]['media_status'], 'download_failed')
            self.assertEqual(failed['posts'][0]['media_local_path'], '')

    def test_mixed_revisions_are_detected_and_snapshot_survives_final_disk_failure(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'keep me', media=SimpleNamespace(kind='photo', id=11))
        client = FakeClient({'channel': entity, 'later': SimpleNamespace(id=2)}, {1: [post]})
        with tempfile.TemporaryDirectory() as output:
            real_replace = bot.os.replace
            replacements = 0
            def replace(source, target):
                nonlocal replacements
                if os.path.basename(target) in bot._DATA_FILES:
                    replacements += 1
                    if replacements >= 5:
                        raise OSError(errno.ENOSPC, 'disk full')
                return real_replace(source, target)
            with patch.object(bot.os, 'replace', side_effect=replace):
                result = asyncio.run(bot.run_exports(client, ['channel', 'later'], dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            self.assertEqual(result['exit_code'], 1)
            self.assertEqual(client.entity_calls, ['channel'])
            folder = os.path.join(output, 'channel')
            self.assertFalse(bot.verify_export_generation(folder))
            snapshot = next(name for name in os.listdir(folder) if name.startswith('.text-ready-'))
            self.assertTrue(bot.verify_export_generation(os.path.join(folder, snapshot)))
            with open(os.path.join(folder, snapshot, 'posts.csv'), encoding='utf-8', newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['text'], 'keep me')
            self.assertFalse(any(name.endswith('.tmp') for _, _, names in os.walk(folder) for name in names))

    def test_media_flood_wait_cancels_collection_and_stops_batch(self):
        async def scenario(output):
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            class FatalClient(FakeClient):
                async def iter_messages(self, entity, reply_to=None):
                    self.iter_calls.append((entity.id, reply_to))
                    yield FakeMessage(1, '2026-08-20', 'text', media=SimpleNamespace(kind='video', id=11))
                    await asyncio.Event().wait()
                async def download_media(self, message, file):
                    with open(file, 'wb') as stream:
                        stream.write(b'partial')
                    raise FloodWaitError(120)
            client = FatalClient({'channel': entity, 'later': SimpleNamespace(id=2)}, {})
            result = await asyncio.wait_for(bot.run_exports(client, ['channel', 'later'], dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output), 3)
            self.assertTrue(result['fatal_error'])
            self.assertEqual(client.entity_calls, ['channel'])
            self.assertFalse(any(name.endswith('.tmp') for _, _, names in os.walk(output) for name in names))
            self.assertEqual([task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()], [])
        with tempfile.TemporaryDirectory() as output:
            asyncio.run(scenario(output))

    def test_interruption_after_text_ready_keeps_text_and_cleans_partial_media(self):
        async def scenario(output):
            ready = asyncio.Event()
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            class FatalClient(FakeClient):
                async def download_media(self, message, file):
                    with open(file, 'wb') as stream:
                        stream.write(b'partial')
                    await ready.wait()
                    raise FloodWaitError(120)
            client = FatalClient({'channel': entity, 'later': SimpleNamespace(id=2)}, {1: [FakeMessage(1, '2026-08-20', 'saved text', media=SimpleNamespace(kind='video', id=11))]})
            original = bot.publish_export
            def publish(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs['export_revision'] == 1:
                    ready.set()
                return result
            with patch.object(bot, 'publish_export', side_effect=publish):
                result = await asyncio.wait_for(bot.run_exports(client, ['channel', 'later'], dt('2026-08-20'), dt('2026-08-20'), media_mode='V', output_dir=output), 3)
            self.assertTrue(result['fatal_error'])
            self.assertEqual(client.entity_calls, ['channel'])
            folder = os.path.join(output, 'channel')
            self.assertTrue(bot.verify_export_generation(folder))
            with open(os.path.join(folder, 'channel.csv'), encoding='utf-8', newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['media_export_status'], 'interrupted')
            with open(os.path.join(folder, 'posts.csv'), encoding='utf-8', newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['text'], 'saved text')
            self.assertEqual(row['media_status'], 'interrupted')
            self.assertFalse(any(name.endswith('.tmp') for _, _, names in os.walk(output) for name in names))
        with tempfile.TemporaryDirectory() as output:
            asyncio.run(scenario(output))

    def test_generated_agents_exists_for_empty_export_and_user_file_is_preserved(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity}, {1: []})
        with tempfile.TemporaryDirectory() as output:
            result = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            path = os.path.join(result['folder'], 'AGENTS.md')
            self.assertTrue(os.path.isfile(path))
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('custom instructions')
            with self.assertRaises(bot.ChannelFolderError):
                asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            with open(path, encoding='utf-8') as stream:
                self.assertEqual(stream.read(), 'custom instructions')

    def test_snapshot_creation_failure_keeps_published_text(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity, 'later': SimpleNamespace(id=2)}, {
            1: [FakeMessage(1, '2026-08-20', 'saved text', media=SimpleNamespace(kind='photo', id=11))],
        })
        with tempfile.TemporaryDirectory() as output:
            with patch.object(bot, '_save_text_snapshot', side_effect=OSError(errno.ENOSPC, 'disk full')):
                result = asyncio.run(bot.run_exports(client, ['channel', 'later'], dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            self.assertTrue(result['fatal_error'])
            self.assertEqual(client.entity_calls, ['channel'])
            folder = os.path.join(output, 'channel')
            self.assertTrue(bot.verify_export_generation(folder))
            with open(os.path.join(folder, 'posts.csv'), encoding='utf-8', newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['text'], 'saved text')
            self.assertEqual(row['export_revision'], '1')

    def test_cancelling_export_after_text_ready_leaves_no_background_downloads(self):
        async def scenario(output):
            ready = asyncio.Event()
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            class CancelClient(FakeClient):
                async def download_media(self, message, file):
                    with open(file, 'wb') as stream:
                        stream.write(b'partial')
                    await asyncio.Event().wait()
            client = CancelClient({'channel': entity}, {1: [FakeMessage(1, '2026-08-20', 'saved text', media=SimpleNamespace(kind='video', id=11))]})
            original = bot.publish_export
            def publish(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs['export_revision'] == 1:
                    ready.set()
                return result
            with patch.object(bot, 'publish_export', side_effect=publish):
                task = asyncio.create_task(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), media_mode='V', output_dir=output))
                await asyncio.wait_for(ready.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(bot.verify_export_generation(os.path.join(output, 'channel')))
            self.assertFalse(any(name.endswith('.tmp') for _, _, names in os.walk(output) for name in names))
            self.assertEqual([task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()], [])
        with tempfile.TemporaryDirectory() as output:
            asyncio.run(scenario(output))

    def test_legacy_snapshot_without_revision_is_still_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            bot.publish_export(folder, {'channel_id': 1}, [{'post_id': 1}])
            for name in ('channel.csv', 'posts.csv'):
                path = os.path.join(folder, name)
                with open(path, encoding='utf-8', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                for row in rows:
                    row.pop('export_revision')
                with open(path, 'w', encoding='utf-8', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
            path = os.path.join(folder, 'comments.jsonl')
            with open(path, encoding='utf-8') as stream:
                control = json.loads(stream.readline())
            control.pop('export_revision')
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write(json.dumps(control) + '\n')
            self.assertTrue(bot.verify_export_generation(folder))

    def test_boolean_prompts_use_y_n_with_n_as_default(self):
        prompts = []
        with patch.object(bot, "ask_dates", return_value=(dt("2026-08-20"), dt("2026-08-20"))), \
                patch.object(bot, "ask_sort", return_value=("date", "по дате")), \
                patch("builtins.input", side_effect=lambda prompt: prompts.append(prompt) or ""):
            settings = bot.collect_run_settings()
        self.assertFalse(settings.comments_requested)
        self.assertEqual(settings.media_mode, "N")
        self.assertEqual(prompts[-2:], [
            "Экспортировать комментарии? [y/N]: ",
            "Медиа: P — фото, V — видео, A — все, N — ничего [N]: ",
        ])

    def test_import_without_environment(self):
        env = {"PATH": os.environ.get("PATH", "")}
        result = subprocess.run(
            [sys.executable, "-c", "import telegram_stats_bot"],
            cwd=os.path.dirname(__file__),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_config_validation_is_deferred(self):
        with self.assertRaises(bot.RuntimeConfigError):
            bot.load_runtime_config({})
        with self.assertRaisesRegex(bot.RuntimeConfigError, "API_ID"):
            bot.load_runtime_config({"API_ID": "not-an-int", "API_HASH": "hash", "PHONE": "+1"})
        config = bot.load_runtime_config({"API_ID": "123", "API_HASH": "hash", "PHONE": "+1"})
        self.assertEqual(config.api_id, 123)

    def test_exact_date_boundaries_and_reversed_range(self):
        self.assertEqual(bot.parse_date_value("2026-08-21"), dt("2026-08-21"))
        self.assertEqual(bot.parse_date_value("2026.08.21"), dt("2026-08-21"))
        self.assertEqual(bot.parse_date_value("2026/08/21", end=True).hour, 23)
        with self.assertRaises(ValueError):
            bot.parse_date_range("2026-08-22", "2026-08-21")
        with self.assertRaises(ValueError):
            bot.parse_date_value("2026-08-2")

    def test_channel_and_date_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "channels with spaces.txt")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("\n@one\nhttps://t.me/two\none\n")
            self.assertEqual(bot.parse_channel_source(f'"{path}"'), ["one", "two"])
            self.assertEqual(bot.parse_channel_source("@one, https://t.me/two two"), ["one", "two"])
            with self.assertRaises(FileNotFoundError):
                bot.parse_channel_source(os.path.join(directory, "missing.txt"))
        self.assertEqual(bot.parse_date_value("2026.08.21"), dt("2026-08-21"))
        self.assertEqual(bot.parse_date_value("2026/08/21", end=True).hour, 23)
        with self.assertRaises(ValueError):
            bot.parse_date_value("2026-02-30")

    def test_export_files_comments_media_and_metadata(self):
        entity = SimpleNamespace(id=1, title="Channel", username="channel", broadcast=True)
        media = SimpleNamespace(kind="photo")
        post = FakeMessage(1, "2026-08-20", "hello", media=media, replies=SimpleNamespace(replies=2, channel_id=99))
        media_only = FakeMessage(2, "2026-08-19", None, media=media)
        comment1 = FakeMessage(101, "2020-01-01", "first", sender_id=10, reply_to=SimpleNamespace(reply_to_msg_id=1, reply_to_top_id=1))
        comment2 = FakeMessage(102, "2020-01-02", "second", sender_id=11, reply_to=SimpleNamespace(reply_to_msg_id=101, reply_to_top_id=1))
        client = FakeClient({"channel": entity}, {1: [post, media_only]}, {(1, 1): [comment1, comment2]})
        with tempfile.TemporaryDirectory() as output:
            result = asyncio.run(bot.run_exports(client, ["@channel", "channel"], dt("2026-08-19"), dt("2026-08-21"), comments_requested=True, download_media=True, output_dir=output))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(client.entity_calls, ["channel"])
            folder = result["successes"][0]["folder"]
            self.assertTrue(bot.verify_export_generation(folder))
            with open(os.path.join(folder, "channel.csv"), encoding="utf-8", newline="") as stream:
                channel = next(csv.DictReader(stream))
            self.assertEqual(channel["description"], "description")
            self.assertEqual(channel["subscriber_count"], "7")
            with open(os.path.join(folder, "posts.csv"), encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            rows_by_id = {row["post_id"]: row for row in rows}
            self.assertEqual(rows_by_id["2"]["text"], "")
            self.assertEqual(rows_by_id["1"]["comments_count"], "2")
            self.assertEqual(rows_by_id["1"]["comments_exported"], "2")
            self.assertEqual(rows_by_id["1"]["comments_export_status"], "complete")
            self.assertEqual(rows_by_id["2"]["comments_export_status"], "disabled")
            self.assertTrue(rows_by_id["1"]["media_local_path"].startswith("media/"))
            with open(os.path.join(folder, "comments.jsonl"), encoding="utf-8") as stream:
                comments = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(comments[0]["record_type"], "run_control")
            self.assertEqual(len(comments), 3)
            self.assertEqual(comments[1]["source_post_id"], 1)
            self.assertEqual(comments[1]["date"], "2020-01-01T00:00:00+00:00")
            self.assertIn(1, client.download_calls)

    def test_repeat_media_uses_existing_deterministic_file(self):
        entity = SimpleNamespace(id=1, title="Channel", username="channel")
        post = FakeMessage(1, "2026-08-20", None, media=SimpleNamespace(kind="photo", id=123))
        client = FakeClient({"channel": entity}, {1: [post]})
        with tempfile.TemporaryDirectory() as output:
            asyncio.run(bot.run_exports(client, ["channel"], dt("2026-08-20"), dt("2026-08-20"), download_media=True, output_dir=output))
            asyncio.run(bot.run_exports(client, ["channel"], dt("2026-08-20"), dt("2026-08-20"), download_media=True, output_dir=output))
        self.assertEqual(client.download_calls, [1])

    def test_comment_spool_is_consumed_lazily(self):
        entity = SimpleNamespace(id=1, title="Channel", username="channel")
        post = FakeMessage(1, "2026-08-20", "post", replies=SimpleNamespace(replies=3, channel_id=99))
        comments = {
            (1, 1): (FakeMessage(index, "2020-01-01", f"comment-{index}") for index in range(3)),
        }
        client = FakeClient({"channel": entity}, {1: [post]}, comments)
        captured = {}

        def capture(folder, channel_row, posts, comment_records=(), **kwargs):
            captured["is_list"] = isinstance(comment_records, list)
            captured["first"] = next(comment_records)
            return kwargs.get("export_id", "captured")

        with tempfile.TemporaryDirectory() as output:
            with patch.object(bot, "publish_export", side_effect=capture):
                asyncio.run(bot.process_channel(client, "channel", dt("2026-08-20"), dt("2026-08-20"), comments_requested=True, output_dir=output))
        self.assertFalse(captured["is_list"])
        self.assertEqual(captured["first"]["comment_id"], 0)

    def test_protected_media_and_folder_collision(self):
        entity1 = SimpleNamespace(id=1, title="Same title", username=None)
        protected = FakeMessage(1, "2026-08-20", None, media=SimpleNamespace(kind="document"), noforwards=True)
        client = FakeClient({"one": entity1}, {1: [protected]})
        with tempfile.TemporaryDirectory() as output:
            first = asyncio.run(bot.run_exports(client, ["one"], dt("2026-08-20"), dt("2026-08-20"), download_media=True, output_dir=output))
            self.assertEqual(first["successes"][0]["posts"][0]["media_status"], "protected")
            self.assertFalse(client.download_calls)
            entity2 = SimpleNamespace(id=2, title="Same title", username=None)
            client.entities["two"] = entity2
            client.posts[2] = []
            second = asyncio.run(bot.run_exports(client, ["two"], dt("2026-08-20"), dt("2026-08-20"), output_dir=output))
            self.assertEqual(second["exit_code"], 1)
            self.assertIn("channel folder belongs", second["failures"][0]["error"])

    def test_missing_channel_isolated_and_comments_control_only(self):
        entity = SimpleNamespace(id=1, title="Good", username="good")
        post = FakeMessage(1, "2026-08-20", "hello")
        client = FakeClient({"bad": entity, "good": entity}, {1: [post]}, fail_channels={"bad"})
        with tempfile.TemporaryDirectory() as output:
            result = asyncio.run(bot.run_exports(client, ["bad", "good"], dt("2026-08-20"), dt("2026-08-20"), output_dir=output))
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(len(result["successes"]), 1)
            with open(os.path.join(result["successes"][0]["folder"], "comments.jsonl"), encoding="utf-8") as stream:
                records = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(records), 1)
            self.assertFalse(records[0]["comments_requested"])

    def test_mixed_generations_are_detectable(self):
        with tempfile.TemporaryDirectory() as output:
            folder = os.path.join(output, "x")
            os.makedirs(folder)
            row = {"export_id": "a", "channel_id": 1}
            bot.publish_export(folder, row, [{"export_id": "a", "channel_id": 1, "post_id": 1}], comment_records=[])
            with open(os.path.join(folder, "comments.jsonl"), "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"record_type": "run_control", "export_id": "b"}) + "\n")
            self.assertFalse(bot.verify_export_generation(folder))
            with self.assertRaises(ValueError):
                bot.assert_generation_consistent(folder)

    def test_atomic_publish_cleans_sibling_temps_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as output:
            folder = os.path.join(output, "channel")
            os.makedirs(folder)
            bot.publish_export(folder, {"channel_id": 1}, [])
            with open(os.path.join(folder, "channel.csv"), "rb") as stream:
                previous = stream.read()
            with patch.object(bot.os, "replace", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    bot.publish_export(folder, {"channel_id": 1}, [], export_id="new")
            with open(os.path.join(folder, "channel.csv"), "rb") as stream:
                self.assertEqual(stream.read(), previous)
            self.assertEqual([name for name in os.listdir(folder) if name.endswith(".tmp")], [])

    def test_long_flood_wait_stops_before_next_channel(self):
        entities = {
            "one": SimpleNamespace(id=1, title="One", username="one"),
            "two": SimpleNamespace(id=2, title="Two", username="two"),
        }
        client = LongWaitClient(entities, {1: [], 2: [FakeMessage(2, "2026-08-20", "later")]})
        with tempfile.TemporaryDirectory() as output:
            result = asyncio.run(bot.run_exports(client, ["one", "two"], dt("2026-08-20"), dt("2026-08-20"), output_dir=output))
            self.assertEqual(result["exit_code"], 1)
            self.assertTrue(result["fatal_error"])
            self.assertEqual(client.entity_calls, ["one"])

    def test_disk_failure_cleans_current_media_and_stops_batch(self):
        entities = {
            "one": SimpleNamespace(id=1, title="One", username="one"),
            "two": SimpleNamespace(id=2, title="Two", username="two"),
        }
        post = FakeMessage(1, "2026-08-20", None, media=SimpleNamespace(kind="photo"))
        client = FakeClient(entities, {1: [post], 2: [FakeMessage(2, "2026-08-20", "later")]})
        with tempfile.TemporaryDirectory() as output:
            with patch.object(bot, "publish_export", side_effect=OSError(errno.ENOSPC, "disk full")):
                result = asyncio.run(bot.run_exports(client, ["one", "two"], dt("2026-08-20"), dt("2026-08-20"), download_media=True, output_dir=output))
            self.assertEqual(result["exit_code"], 1)
            self.assertTrue(result["fatal_error"])
            self.assertEqual(client.entity_calls, ["one"])
            media_dir = os.path.join(output, "one", "media")
            if os.path.isdir(media_dir):
                self.assertEqual(os.listdir(media_dir), [])
            self.assertEqual([name for root, _, names in os.walk(output) for name in names if name.endswith(".tmp")], [])

    def test_single_summary_keeps_top_post_console_output(self):
        result = {
            "successes": [{
                "channel": {"title": "Channel", "channel_id": 1},
                "folder": "/tmp/channel",
                "posts": [{
                    "forwards": 2, "reactions": 3, "views": 10,
                    "text": "hello", "date": "2026-08-20T00:00:00+00:00",
                }],
            }],
            "failures": [],
            "sort_key": "date",
            "sort_label": "по дате",
        }
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            bot._print_export_summary(result)
        rendered = output.getvalue()
        self.assertIn("• Channel — постов: 1", rendered)
        self.assertNotIn("/tmp/channel", rendered)
        self.assertIn("Всего постов:     1", rendered)
        self.assertIn("Пересылок: 2, Реакций: 3, Просмотров: 10", rendered)
        self.assertIn("Текст: hello", rendered)

    def test_export_prints_live_post_counter(self):
        entity = SimpleNamespace(id=1, title="Channel", username="channel")
        client = FakeClient({"channel": entity}, {1: [
            FakeMessage(1, "2026-08-20", "one"),
            FakeMessage(2, "2026-08-20", "two"),
        ]})
        with tempfile.TemporaryDirectory() as output:
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                asyncio.run(bot.run_exports(
                    client, ["channel"], dt("2026-08-20"), dt("2026-08-20"), output_dir=output,
                ))
        rendered = stdout.getvalue()
        self.assertIn("Выгружаю Channel — постов: 0", rendered)
        self.assertIn("Выгружаю Channel — постов: 2", rendered)


if __name__ == "__main__":
    unittest.main()
