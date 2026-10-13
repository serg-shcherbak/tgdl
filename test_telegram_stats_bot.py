import asyncio
import csv
import errno
import io
from itertools import chain
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        self.iter_options = []

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

    async def iter_messages(self, entity, reply_to=None, **kwargs):
        if reply_to is not None and getattr(entity, "id", entity) == 99:
            raise AssertionError("comment iterator must receive source channel entity")
        key = (getattr(entity, "id", entity), reply_to)
        self.iter_calls.append(key)
        self.iter_options.append(kwargs)
        values = self.comments.get(key, []) if reply_to is not None else self.posts.get(getattr(entity, "id", entity), [])
        for value in values:
            if reply_to is not None and kwargs.get('offset_id') and value.id >= kwargs['offset_id']:
                continue
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
    async def iter_messages(self, entity, reply_to=None, **kwargs):
        key = (getattr(entity, "id", entity), reply_to)
        self.iter_calls.append(key)
        if reply_to is None and getattr(entity, "id", None) == 1:
            raise FloodWaitError(120)
        values = self.comments.get(key, []) if reply_to is not None else self.posts.get(getattr(entity, "id", entity), [])
        for value in values:
            yield value


class TelegramExportTests(unittest.TestCase):
    def test_resume_adds_comments_without_fetching_posts_or_media(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'saved post', media=SimpleNamespace(kind='photo', id=11),
                           replies=SimpleNamespace(replies=3, channel_id=99))
        disabled = FakeMessage(2, '2026-08-20', 'no comments')
        original = FakeClient({'channel': entity}, {1: [post, disabled]})
        comments = [FakeMessage(i, '2020-01-01', f'comment {i}') for i in (103, 102, 101)]
        client = FakeClient({'channel': entity}, {}, {(1, 1): comments})
        with tempfile.TemporaryDirectory() as output:
            saved = asyncio.run(bot.process_channel(original, 'channel', dt('2026-08-20'), dt('2026-08-20'),
                                media_mode='A', output_dir=output))
            old_media = saved['posts'][0]['media_local_path']
            with patch.object(client, 'get_full_channel', side_effect=AssertionError('metadata must not be refetched')):
                result = asyncio.run(bot.resume_comments(client, saved['folder']))
            self.assertEqual(client.iter_calls, [(1, 1)])
            self.assertEqual(client.download_calls, [])
            self.assertEqual(result['posts'][0]['text'], 'saved post')
            self.assertEqual(result['posts'][0]['media_local_path'], old_media)
            self.assertEqual(result['posts'][0]['media_status'], 'downloaded')
            self.assertEqual(result['channel']['media_export_status'], 'complete')
            self.assertTrue(os.path.isfile(os.path.join(saved['folder'], old_media)))
            self.assertEqual(result['posts'][0]['comments_exported'], 3)
            self.assertEqual(result['posts'][0]['comments_export_status'], 'complete')
            self.assertEqual(result['posts'][1]['comments_export_status'], 'disabled')
            self.assertTrue(bot.verify_export_generation(saved['folder']))
            # A second resume does no history, reply or download work and keeps the snapshot.
            before = {name: (Path(saved['folder']) / name).read_bytes() for name in bot._DATA_FILES}
            again = FakeClient({'channel': entity}, {})
            asyncio.run(bot.resume_comments(again, saved['folder']))
            self.assertEqual(again.iter_calls, [])
            self.assertEqual(before, {name: (Path(saved['folder']) / name).read_bytes() for name in bot._DATA_FILES})

    def test_resume_repeated_cancellation_uses_saved_cursor_and_skips_finished_threads(self):
        async def scenario(output):
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            posts = [FakeMessage(i, '2026-08-20', f'post {i}',
                     replies=SimpleNamespace(replies=3 if i == 1 else 1, channel_id=99)) for i in (1, 2)]
            await bot.process_channel(FakeClient({'channel': entity}, {1: posts}), 'channel',
                  dt('2026-08-20'), dt('2026-08-20'), output_dir=output)
            folder = os.path.join(output, 'channel')
            comments = {(1, 1): [FakeMessage(i, '2020-01-01', f'comment {i}') for i in (103, 102, 101)],
                        (1, 2): [FakeMessage(201, '2020-01-01', 'finished thread')]}

            class Interruptible(FakeClient):
                async def iter_messages(self, entity, reply_to=None, **kwargs):
                    async for row in super().iter_messages(entity, reply_to, **kwargs):
                        yield row
                        if reply_to == 1:
                            await blocked.wait()
                            ready.set()
                            await asyncio.Event().wait()
                    if reply_to == 2:
                        blocked.set()

            for expected_offset in (None, 103):
                ready, blocked = asyncio.Event(), asyncio.Event()
                client = Interruptible({'channel': entity}, {}, comments)
                task = asyncio.create_task(bot.resume_comments(client, folder))
                try:
                    if expected_offset:
                        blocked.set()  # The other thread was completed during the first pass.
                    await asyncio.wait_for(ready.wait(), 3)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    options = client.iter_options[client.iter_calls.index((1, 1))]
                    self.assertEqual(options.get('offset_id'), expected_offset)
                    if expected_offset:
                        self.assertNotIn((1, 2), client.iter_calls)
                    self.assertTrue(bot.verify_export_generation(folder))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            final = FakeClient({'channel': entity}, {}, comments)
            result = await bot.resume_comments(final, folder)
            self.assertEqual(final.iter_calls, [(1, 1)])
            self.assertEqual(final.iter_options, [{'offset_id': 102}])
            self.assertEqual([p['comments_exported'] for p in result['posts']], [3, 1])
            self.assertTrue(all(p['comments_export_status'] == 'complete' for p in result['posts']))
            with open(os.path.join(folder, 'comments.jsonl'), encoding='utf-8') as stream:
                rows = [json.loads(line) for line in stream][1:]
            self.assertEqual(sorted(row['comment_id'] for row in rows), [101, 102, 103, 201])
            self.assertFalse(any(name.endswith('.tmp') for _, _, names in os.walk(output) for name in names))
            self.assertEqual([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()], [])
        with tempfile.TemporaryDirectory() as output:
            asyncio.run(scenario(output))

    def test_resume_refuses_foreign_corrupt_or_user_owned_exports(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'saved', replies=SimpleNamespace(replies=1, channel_id=99))
        for failure in ('foreign', 'mixed', 'user_agents'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as output:
                saved = asyncio.run(bot.process_channel(FakeClient({'channel': entity}, {1: [post]}), 'channel',
                        dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
                folder = saved['folder']
                client = FakeClient({'channel': entity}, {}, {(1, 1): [FakeMessage(101, '2020-01-01', 'comment')]})
                if failure == 'foreign':
                    client.entities['channel'] = SimpleNamespace(id=2)
                elif failure == 'mixed':
                    path = os.path.join(folder, 'comments.jsonl')
                    with open(path, encoding='utf-8') as stream:
                        control = json.loads(stream.readline())
                    control['export_id'] = 'another-export'
                    with open(path, 'w', encoding='utf-8') as stream:
                        stream.write(json.dumps(control) + '\n')
                else:
                    with open(os.path.join(folder, 'AGENTS.md'), 'w', encoding='utf-8') as stream:
                        stream.write('user instructions')
                before = {name: (Path(folder) / name).read_bytes() for name in bot._DATA_FILES}
                with self.assertRaises((bot.ChannelFolderError, ValueError)):
                    asyncio.run(bot.resume_comments(client, folder))
                self.assertEqual(client.iter_calls, [])
                self.assertEqual(before, {name: (Path(folder) / name).read_bytes() for name in bot._DATA_FILES})

    def test_resume_rolls_back_failed_first_publication(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'saved', replies=SimpleNamespace(replies=1, channel_id=99))
        with tempfile.TemporaryDirectory() as output:
            saved = asyncio.run(bot.process_channel(FakeClient({'channel': entity}, {1: [post]}), 'channel',
                    dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            folder = saved['folder']
            before = {name: (Path(folder) / name).read_bytes() for name in bot._DATA_FILES}
            replace = bot.os.replace
            failed = False
            def fail(source, target):
                nonlocal failed
                if target == os.path.join(folder, 'posts.csv') and not failed:
                    failed = True
                    raise OSError(errno.ENOSPC, 'disk full')
                return replace(source, target)
            client = FakeClient({'channel': entity}, {})
            with patch.object(bot.os, 'replace', side_effect=fail):
                with self.assertRaises(OSError):
                    asyncio.run(bot.resume_comments(client, folder))
            self.assertTrue(bot.verify_export_generation(folder))
            self.assertEqual(before, {name: (Path(folder) / name).read_bytes() for name in bot._DATA_FILES})
            self.assertEqual(client.iter_calls, [])

    def test_resume_cli_and_interactive_mode_use_existing_folder_only(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'saved', replies=SimpleNamespace(replies=1, channel_id=99))
        for interactive in (False, True):
            with self.subTest(interactive=interactive), tempfile.TemporaryDirectory() as output:
                saved = asyncio.run(bot.process_channel(FakeClient({'channel': entity}, {1: [post]}), 'channel',
                        dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
                client = FakeClient({'channel': entity}, {}, {(1, 1): [FakeMessage(101, '2020-01-01', 'comment')]})
                client.start = AsyncMock()
                client.disconnect = AsyncMock()
                config = bot.RuntimeConfig(123, 'offline', 'offline', output)
                with patch.object(bot, 'ExportClient', return_value=client), \
                     patch.object(bot, 'load_runtime_config', return_value=config), \
                     patch('builtins.input', side_effect=['2', saved['folder']] if interactive else AssertionError('no prompts expected')):
                    result = asyncio.run(bot.main([] if interactive else ['--resume-comments', saved['folder']]))
                self.assertEqual(result, 0)
                self.assertEqual(client.iter_calls, [(1, 1)])
                client.start.assert_awaited_once_with(phone='offline')
                client.disconnect.assert_awaited_once()

    def test_resume_duplicate_saved_and_returned_comments_are_not_added_twice(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'saved', replies=SimpleNamespace(replies=3, channel_id=99))
        first = FakeClient({'channel': entity}, {1: [post]}, {(1, 1): [FakeMessage(103, '2020-01-01', 'already saved')]})
        class ReplayingClient(FakeClient):
            async def iter_messages(self, entity, reply_to=None, **kwargs):
                # A replayed boundary ID must not create a duplicate record.
                async for row in super().iter_messages(entity, reply_to):
                    yield row
        with tempfile.TemporaryDirectory() as output:
            saved = asyncio.run(bot.process_channel(first, 'channel', dt('2026-08-20'), dt('2026-08-20'),
                                   comments_requested=True, output_dir=output))
            path = os.path.join(saved['folder'], 'comments.jsonl')
            with open(path, encoding='utf-8') as stream:
                lines = list(stream)
            with open(path, 'a', encoding='utf-8') as stream:
                stream.write(lines[1])
            client = ReplayingClient({'channel': entity}, {}, {(1, 1): [FakeMessage(i, '2020-01-01', 'comment') for i in (103, 102, 101)]})
            result = asyncio.run(bot.resume_comments(client, saved['folder']))
            self.assertEqual(result['posts'][0]['comments_exported'], 3)
            with open(path, encoding='utf-8') as stream:
                rows = [json.loads(line) for line in stream][1:]
            self.assertEqual([row['comment_id'] for row in rows], [103, 102, 101])

    def test_posts_are_published_before_two_bounded_comment_workers(self):
        async def scenario(output):
            release, started = asyncio.Event(), asyncio.Event()
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            posts = [FakeMessage(i, '2026-08-20', f'post {i}',
                     replies=SimpleNamespace(replies=2, channel_id=99)) for i in range(1, 5)]
            comments = {(1, i): [FakeMessage(i * 100 + j, '2020-01-01', 'comment')
                         for j in (1, 2)] for i in range(1, 5)}

            class SlowComments(FakeClient):
                active = 0
                maximum = 0
                async def iter_messages(self, entity, reply_to=None, **kwargs):
                    if reply_to is not None:
                        self.active += 1
                        self.maximum = max(self.maximum, self.active)
                        if self.active == 2:
                            started.set()
                        try:
                            await release.wait()
                            async for row in super().iter_messages(entity, reply_to, **kwargs):
                                yield row
                        finally:
                            self.active -= 1
                    else:
                        async for row in super().iter_messages(entity, **kwargs):
                            yield row

            client = SlowComments({'channel': entity}, {1: posts}, comments)
            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                task = asyncio.create_task(bot.run_exports(client, ['channel'], dt('2026-08-20'),
                       dt('2026-08-20'), comments_requested=True, output_dir=output))
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    folder = os.path.join(output, 'channel')
                    self.assertTrue(bot.verify_export_generation(folder))
                    self.assertFalse(task.done())
                    with open(os.path.join(folder, 'posts.csv'), encoding='utf-8', newline='') as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), 4)
                    self.assertTrue(all(row['comments_export_status'] == 'pending' and
                                        row['comments_exported'] == '0' for row in rows))
                    with open(os.path.join(folder, 'comments.jsonl'), encoding='utf-8') as stream:
                        self.assertEqual(len(list(stream)), 1)
                    self.assertIn('Посты готовы:', stdout.getvalue())
                    release.set()
                    result = await asyncio.wait_for(task, 3)
                    self.assertEqual(result['exit_code'], 0, result['failures'])
                    self.assertEqual(client.maximum, 2)
                    self.assertTrue(all(row['comments_exported'] == 2 for row in result['successes'][0]['posts']))
                    self.assertIn('постов: 4 | комментов: 8 | веток: 4/4', stdout.getvalue())
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(client.active, 0)
            self.assertEqual([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()], [])

        with tempfile.TemporaryDirectory() as output:
            asyncio.run(scenario(output))

    def test_cancelling_comments_keeps_posts_and_collected_comments(self):
        async def scenario(output):
            blocked = asyncio.Event()
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            posts = [FakeMessage(i, '2026-08-20', f'post {i}',
                     replies=SimpleNamespace(replies=3, channel_id=99)) for i in (1, 2, 3)]

            class PartialComments(FakeClient):
                started = 0
                async def iter_messages(self, entity, reply_to=None, **kwargs):
                    if reply_to is None:
                        async for row in super().iter_messages(entity, **kwargs):
                            yield row
                    else:
                        yield FakeMessage(reply_to * 100, '2020-01-01', 'saved comment')
                        self.started += 1
                        if self.started == 2:
                            blocked.set()
                        await asyncio.Event().wait()

            client = PartialComments({'channel': entity}, {1: posts})
            task = asyncio.create_task(bot.process_channel(client, 'channel', dt('2026-08-20'),
                   dt('2026-08-20'), comments_requested=True, output_dir=output))
            try:
                await asyncio.wait_for(blocked.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                folder = os.path.join(output, 'channel')
                self.assertTrue(bot.verify_export_generation(folder))
                with open(os.path.join(folder, 'posts.csv'), encoding='utf-8', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 3)
                self.assertEqual(sorted(row['comments_export_status'] for row in rows), ['interrupted', 'partial', 'partial'])
                with open(os.path.join(folder, 'comments.jsonl'), encoding='utf-8') as stream:
                    comments = [json.loads(line) for line in stream]
                self.assertEqual(len(comments), 3)
                self.assertEqual(sum(int(row['comments_exported']) for row in rows), 2)
                self.assertTrue(all(row['export_revision'] == '2' for row in rows))
                self.assertFalse(any(name.endswith('.tmp') for _, _, names in os.walk(output) for name in names))
                self.assertEqual([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()], [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with tempfile.TemporaryDirectory() as output:
            asyncio.run(scenario(output))

    def test_comment_fatal_errors_keep_posts_and_stop_batch(self):
        for failure in (FloodWaitError(120), OSError(errno.ENOSPC, 'disk full')):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as output:
                entity = SimpleNamespace(id=1, title='Channel', username='channel')
                post = FakeMessage(1, '2026-08-20', 'saved post', replies=SimpleNamespace(replies=2, channel_id=99))
                class FatalComments(FakeClient):
                    async def iter_messages(self, entity, reply_to=None, **kwargs):
                        if reply_to is None:
                            async for row in super().iter_messages(entity, **kwargs):
                                yield row
                        else:
                            yield FakeMessage(101, '2020-01-01', 'saved comment')
                            raise failure
                client = FatalComments({'channel': entity, 'later': SimpleNamespace(id=2)}, {1: [post]})
                result = asyncio.run(bot.run_exports(client, ['channel', 'later'], dt('2026-08-20'),
                         dt('2026-08-20'), comments_requested=True, output_dir=output))
                self.assertTrue(result['fatal_error'])
                self.assertEqual(client.entity_calls, ['channel'])
                folder = os.path.join(output, 'channel')
                self.assertTrue(bot.verify_export_generation(folder))
                with open(os.path.join(folder, 'posts.csv'), encoding='utf-8', newline='') as stream:
                    row = next(csv.DictReader(stream))
                self.assertEqual(row['text'], 'saved post')
                self.assertEqual(row['comments_export_status'], 'partial')
                self.assertEqual(row['comments_exported'], '1')

    def test_cmd_c_after_posts_ready_keeps_export_in_cli_event_loop(self):
        script = '''
import asyncio, os, signal, sys
from types import SimpleNamespace
from test_telegram_stats_bot import FakeClient, FakeMessage, dt
import telegram_stats_bot as bot
entity = SimpleNamespace(id=1, title='Channel', username='channel')
post = FakeMessage(1, '2026-08-20', 'saved post', replies=SimpleNamespace(replies=2, channel_id=99))
class InterruptClient(FakeClient):
    async def iter_messages(self, entity, reply_to=None, **kwargs):
        if reply_to is None:
            async for row in super().iter_messages(entity, **kwargs):
                yield row
        else:
            yield FakeMessage(101, '2020-01-01', 'saved comment')
            asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
            await asyncio.Event().wait()
client = InterruptClient({'channel': entity}, {1: [post]})
try:
    asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'),
                comments_requested=True, output_dir=sys.argv[1]))
except KeyboardInterrupt:
    pass
'''
        with tempfile.TemporaryDirectory() as output:
            run = subprocess.run([sys.executable, '-c', script, output], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn('never retrieved', run.stderr)
            self.assertNotIn('Task was destroyed', run.stderr)
            folder = os.path.join(output, 'channel')
            self.assertTrue(bot.verify_export_generation(folder))
            with open(os.path.join(folder, 'posts.csv'), encoding='utf-8', newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['text'], 'saved post')
            self.assertEqual(row['comments_export_status'], 'partial')
            self.assertEqual(row['comments_exported'], '1')
            with open(os.path.join(folder, 'comments.jsonl'), encoding='utf-8') as stream:
                self.assertEqual(len(list(stream)), 2)

    def test_history_offset_preserves_inclusive_end_boundary(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        end = dt('2026-08-20') + timedelta(hours=23, minutes=59, seconds=59)
        boundary = FakeMessage(1, '2026-08-20', 'boundary')
        boundary.date = end
        newer = FakeMessage(2, '2026-08-21', 'outside period')
        class OffsetClient(FakeClient):
            async def iter_messages(self, entity, reply_to=None, **kwargs):
                async for row in super().iter_messages(entity, reply_to, **kwargs):
                    if row.date < kwargs['offset_date']:
                        yield row
        client = OffsetClient({'channel': entity}, {1: [newer, boundary]})
        with tempfile.TemporaryDirectory() as output:
            result = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), end, output_dir=output))
        self.assertEqual([row['post_id'] for row in result['posts']], [1])
        self.assertEqual(client.iter_options[0]['offset_date'], end + timedelta(seconds=1))

    def test_export_client_paces_pages_shares_cooldown_and_latches_fatal(self):
        async def scenario():
            client = bot.ExportClient(None, 123, 'offline')
            clock = SimpleNamespace(now=0.0)
            loop = SimpleNamespace(time=lambda: clock.now)
            async def sleep(delay):
                clock.now += delay
            page = bot.tl_functions.messages.GetHistoryRequest(None, 0, None, 0, 100, 0, 0, 0)
            replies = bot.tl_functions.messages.GetRepliesRequest(None, 1, 0, None, 0, 100, 0, 0, 0)
            media = bot.tl_functions.upload.GetFileRequest(None, 0, 4096)
            times = []
            async def rpc(*args, **kwargs):
                times.append(clock.now)
                return 'ok'
            with patch.object(bot.asyncio, 'get_running_loop', return_value=loop), \
                 patch.object(bot.asyncio, 'sleep', side_effect=sleep), \
                 patch.object(bot.TelegramClient, '_call', side_effect=rpc) as call:
                await client._call(None, page)
                await client._call(None, replies)
                await client._call(None, page)
                await client._call(None, media)
                self.assertEqual(times, [0, 1, 2, 2])
                self.assertEqual(call.call_args.kwargs['flood_sleep_threshold'], 0)
                call.side_effect = [FloodWaitError(4), 'ok']
                await client._call(None, replies)
                self.assertEqual(clock.now, 7)
                self.assertEqual(client._page_interval, 2)
                # Every RPC, including downloads, honors a wait raised by another worker.
                client._resume_at = 12
                call.side_effect = rpc
                await client._call(None, media)
                self.assertEqual(times[-1], 12)
                call.side_effect = FloodWaitError(120)
                with self.assertRaises(bot.RunFatalError):
                    await client._call(None, page)
                count = call.call_count
                with self.assertRaises(bot.RunFatalError):
                    await client._call(None, media)
                self.assertEqual(call.call_count, count)
                client._export_fatal = None
                call.side_effect = FloodWaitError(1)
                with self.assertRaises(bot.RunFatalError):
                    await client._call(None, replies)
                self.assertEqual(call.call_count, count + 4)
            self.assertEqual(client.flood_sleep_threshold, 0)
            self.assertTrue(client._no_updates)
        asyncio.run(scenario())

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
                async def iter_messages(self, entity, reply_to=None, **kwargs):
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
            first = next(comment_records, None)
            if first is not None:
                captured["first"] = first
            return original(folder, channel_row, posts, (() if first is None else
                chain([first], comment_records)), **kwargs)

        original = bot.publish_export
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
