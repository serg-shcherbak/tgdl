import asyncio
import csv
import errno
import io
from itertools import chain, permutations
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
from telethon.tl import types as tl

import telegram_stats_bot as bot


def dt(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def photo_media(media_id=11):
    return tl.MessageMediaPhoto(photo=tl.Photo(
        media_id, 0, b'', dt('2026-08-20'), [tl.PhotoSize('x', 10, 10, 100)], 1))


def document_media(media_id, mime_type, attributes=()):
    return tl.MessageMediaDocument(document=tl.Document(
        media_id, 0, b'', dt('2026-08-20'), mime_type, 100, 1, list(attributes)))


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
    def test_polls_are_text_in_every_media_mode_for_posts_and_comments(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel', noforwards=True)
        question = tl.TextWithEntities('🙂 Question?', [tl.MessageEntityBold(3, 8)])
        answers = [tl.PollAnswer(tl.TextWithEntities('Yes', [tl.MessageEntityItalic(0, 3)]), b'1'),
                   tl.PollAnswer(tl.TextWithEntities('No', []), b'2')]
        for quiz in (False, True):
            poll = tl.Poll(77, question, answers, hash=0, quiz=quiz, closed=quiz, multiple_choice=not quiz)
            results = (tl.PollResults(total_voters=3, results=[
                tl.PollAnswerVoters(b'1', voters=3, correct=True),
                tl.PollAnswerVoters(b'2', voters=0)], solution='Explanation',
                solution_entities=[tl.MessageEntityBold(0, 11)]) if quiz else tl.PollResults())
            media = tl.MessageMediaPoll(poll, results)
            for mode in 'NPVA':
                with self.subTest(quiz=quiz, mode=mode), tempfile.TemporaryDirectory() as output:
                    post = FakeMessage(1, '2026-08-20', media=media, replies=SimpleNamespace(replies=1, channel_id=99))
                    comment = FakeMessage(101, '2026-08-20', 'caption', media=media)
                    client = FakeClient({'channel': entity}, {1: [post]}, {(1, 1): [comment]})
                    result = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'),
                        output_dir=output, media_mode=mode, comments_requested=True))
                    folder = Path(result['folder'])
                    with (folder / 'posts.csv').open(newline='') as stream:
                        reader = csv.DictReader(stream)
                        row = next(reader)
                        self.assertEqual(reader.fieldnames[-1], 'poll_text')
                    records = [json.loads(line) for line in (folder / 'comments.jsonl').read_text().splitlines()]
                    self.assertEqual(row['text'], '')
                    self.assertEqual(records[1]['text'], 'caption')
                    self.assertEqual(client.download_calls, [])
                    self.assertEqual(result['channel']['media_export_status'], 'not_requested' if mode == 'N' else 'complete')
                    for item in (row, records[1]):
                        self.assertEqual(item['media_type'], 'poll')
                        self.assertEqual(item['media_order'], '')
                        self.assertEqual(item['media_local_path'], '')
                        self.assertEqual(item['media_status'], 'metadata_only')
                        self.assertIn('🙂 Question?\n1. Yes', item['poll_text'])
                        details = json.loads(item['media_extra_json'])['poll']
                        self.assertEqual(details['id'], 77)
                        self.assertEqual(details['question_entities'][0]['offset'], 3)
                        self.assertEqual(details['options'][0]['text_entities'][0]['_'], 'MessageEntityItalic')
                        self.assertEqual(details['options'][0]['data'], b'1'.hex())
                        self.assertEqual(details['quiz'], quiz)
                        self.assertEqual(details['multiple_choice'], not quiz)
                        if quiz:
                            self.assertEqual(details['options'][1]['voters'], 0)
                            self.assertIn('Всего проголосовало: 3', item['poll_text'])
                            self.assertIn('[правильный ответ]', item['poll_text'])
                            self.assertIn('Опрос закрыт', item['poll_text'])
                            self.assertIn('Пояснение: Explanation', item['poll_text'])
                        else:
                            self.assertIsNone(details['total_voters'])
                            self.assertIsNone(details['options'][0]['voters'])
                            self.assertNotIn('голоса:', item['poll_text'])
                            self.assertNotIn('правильный ответ', item['poll_text'])
                    self.assertIn('poll_text', (folder / 'AGENTS.md').read_text())
                    self.assertTrue(bot.verify_export_generation(str(folder)))

    def test_document_types_do_not_depend_on_attribute_order(self):
        sticker = tl.DocumentAttributeSticker('🙂', tl.InputStickerSetEmpty())
        cases = [
            ('sticker', 'video/webm', [sticker, tl.DocumentAttributeVideo(1, 10, 10), tl.DocumentAttributeAnimated()]),
            ('sticker', 'image/webp', [sticker, tl.DocumentAttributeImageSize(10, 10)]),
            ('animation', 'video/mp4', [tl.DocumentAttributeAnimated(), tl.DocumentAttributeVideo(1, 10, 10)]),
            ('video_note', 'video/mp4', [tl.DocumentAttributeVideo(1, 10, 10, round_message=True), tl.DocumentAttributeFilename('round.mp4')]),
        ]
        for expected, mime, attributes in cases:
            for attrs in permutations(attributes):
                with self.subTest(expected=expected, attributes=attrs):
                    msg = FakeMessage(1, '2026-08-20', media=document_media(11, mime, attrs))
                    info, downloadable = bot._media_metadata(msg)
                    self.assertTrue(downloadable)
                    self.assertEqual(info['media_type'], expected)
                    self.assertEqual(bot._media_selected(msg, info, 'V'), expected == 'video_note')
                    self.assertTrue(bot._media_selected(msg, info, 'A'))
                    if expected == 'video_note':
                        self.assertTrue(json.loads(info['media_extra_json'])['round_message'])

    def test_non_file_media_and_locations_do_not_queue_downloads(self):
        geo = tl.GeoPoint(43.21, 54.32, 0)
        media = [
            tl.MessageMediaDice(6, '🎲'),
            tl.MessageMediaGiveaway([1], 2, dt('2026-08-21'), prize_description='Prize'),
            tl.MessageMediaGiveawayResults(1, 5, 1, 0, [10], dt('2026-08-21')),
            tl.MessageMediaStory(tl.PeerChannel(1), 5),
            tl.MessageMediaPaidMedia(10, [tl.MessageExtendedMedia(photo_media())]),
            tl.MessageMediaWebPage(None),
            tl.MessageMediaPhoto(), tl.MessageMediaPhoto(photo=tl.PhotoEmpty(1)),
            tl.MessageMediaDocument(), tl.MessageMediaDocument(document=tl.DocumentEmpty(1)),
            tl.MessageMediaGeo(geo), tl.MessageMediaGeoLive(geo, 60),
            tl.MessageMediaVenue(geo, 'Place', 'Private address', 'x', 'x', 'x'),
            tl.MessageMediaUnsupported(),
            tl.MessageMediaContact('123', 'Name', 'Surname', '', 10),
        ]
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        for mode in 'NPVA':
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as output:
                posts = [FakeMessage(i, '2026-08-20', 'original', media=item) for i, item in enumerate(media, 1)]
                posts[0].replies = SimpleNamespace(replies=len(media), channel_id=99)
                comments = [FakeMessage(i + 100, '2026-08-20', 'comment', media=item) for i, item in enumerate(media, 1)]
                client = FakeClient({'channel': entity}, {1: posts}, {(1, 1): comments})
                result = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'),
                    output_dir=output, media_mode=mode, comments_requested=True))
                self.assertEqual(client.download_calls, [])
                self.assertEqual(result['channel']['media_export_status'], 'not_requested' if mode == 'N' else 'complete')
                records = [json.loads(line) for line in (Path(result['folder']) / 'comments.jsonl').read_text().splitlines()][1:]
                for row in result['posts'] + records:
                    self.assertEqual(row['media_status'], 'metadata_only')
                    self.assertEqual(row['media_order'], '')
                    self.assertEqual(row['media_local_path'], '')
                    details = json.loads(row['media_extra_json'])
                    if row['media_type'] == 'location':
                        self.assertEqual(set(details), {'source_type'})
                        self.assertIn(row['text'], ('original', 'comment'))
                self.assertEqual(json.loads(result['posts'][0]['media_extra_json'])['value'], 6)
                self.assertEqual(json.loads(result['posts'][1]['media_extra_json'])['prize_description'], 'Prize')
                self.assertEqual(json.loads(result['posts'][3]['media_extra_json'])['story_id'], 5)
                self.assertEqual(json.loads(result['posts'][4]['media_extra_json'])['attachment_count'], 1)

    def test_comment_counts_can_grow_without_masking_errors(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        for count, exported, fail, status in [(1, 2, False, 'complete'), (1, 2, True, 'partial'), (3, 1, False, 'partial')]:
            class Client(FakeClient):
                async def iter_messages(self, entity, reply_to=None, **kwargs):
                    async for row in super().iter_messages(entity, reply_to, **kwargs):
                        yield row
                    if reply_to is not None and fail:
                        raise RuntimeError('injected reply failure')
            with self.subTest(count=count, exported=exported, fail=fail), tempfile.TemporaryDirectory() as output:
                post = FakeMessage(7, '2026-08-20', 'post', replies=SimpleNamespace(replies=count, channel_id=99))
                replies = [FakeMessage(101+i, '2026-08-20', 'reply') for i in range(exported)]
                result = asyncio.run(bot.process_channel(Client({'channel': entity}, {1: [post]}, {(1, 7): replies}),
                    'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output, comments_requested=True))
                self.assertEqual(result['posts'][0]['comments_export_status'], status)
                self.assertEqual(result['posts'][0]['comments_exported'], exported)
                self.assertEqual(result['posts'][0]['comments_count'], count)
                if status == 'complete':
                    before = {name: (Path(result['folder']) / name).read_bytes() for name in bot._DATA_FILES}
                    again = FakeClient({'channel': entity}, {})
                    asyncio.run(bot.resume_comments(again, result['folder']))
                    self.assertEqual(again.iter_calls, [])
                    self.assertEqual(before, {name: (Path(result['folder']) / name).read_bytes() for name in bot._DATA_FILES})

    def test_comment_roots_use_the_discussion_message_id(self):
        entity = SimpleNamespace(id=1)
        info, _ = bot._media_metadata(SimpleNamespace(media=None))
        for parent, top, root in [(500, None, 500), (501, 500, 500), (None, None, '')]:
            comment = FakeMessage(502, '2026-08-20', 'reply', reply_to=tl.MessageReplyHeader(
                reply_to_msg_id=parent, reply_to_top_id=top))
            row = bot._comment_row(comment, entity, 7, 99, info)
            self.assertEqual(row['discussion_thread_root_id'], root)
            self.assertEqual(row['source_post_id'], 7)
            self.assertEqual(row['reply_to_comment_id'], parent or '')

    def test_resume_publishes_local_changes_without_comment_requests(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        for posts in ([], [FakeMessage(1, '2026-08-20', 'post', replies=SimpleNamespace(replies=0, channel_id=99)),
                           FakeMessage(2, '2026-08-20', 'disabled')]):
            with self.subTest(posts=len(posts)), tempfile.TemporaryDirectory() as output:
                saved = asyncio.run(bot.process_channel(FakeClient({'channel': entity}, {1: posts}), 'channel',
                    dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
                folder = Path(saved['folder'])
                client = FakeClient({'channel': entity}, {})
                result = asyncio.run(bot.resume_comments(client, str(folder)))
                self.assertEqual(client.iter_calls, [])
                self.assertNotEqual(result['export_id'], saved['export_id'])
                self.assertEqual([p['comments_export_status'] for p in result['posts']], ['complete', 'disabled'] if posts else [])
                with (folder / 'posts.csv').open(newline='') as stream:
                    self.assertEqual([p['comments_export_status'] for p in csv.DictReader(stream)], ['complete', 'disabled'] if posts else [])
                self.assertTrue(json.loads((folder / 'comments.jsonl').read_text().splitlines()[0])['comments_requested'])
                self.assertFalse(list(folder.glob('.text-ready-*')))
                before = {name: (folder / name).read_bytes() for name in (*bot._DATA_FILES, 'AGENTS.md')}
                asyncio.run(bot.resume_comments(FakeClient({'channel': entity}, {}), str(folder)))
                self.assertEqual(before, {name: (folder / name).read_bytes() for name in before})

    def test_resume_saves_deduplication_of_already_complete_threads(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'post', replies=SimpleNamespace(replies=1, channel_id=99))
        client = FakeClient({'channel': entity}, {1: [post]}, {(1, 1): [FakeMessage(101, '2026-08-20', 'reply')]})
        with tempfile.TemporaryDirectory() as output:
            saved = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'),
                output_dir=output, comments_requested=True))
            path = Path(saved['folder']) / 'comments.jsonl'
            lines = path.read_text().splitlines()
            path.write_text('\n'.join(lines + [lines[1]]) + '\n')
            again = FakeClient({'channel': entity}, {})
            result = asyncio.run(bot.resume_comments(again, saved['folder']))
            self.assertEqual(again.iter_calls, [])
            self.assertEqual(len(path.read_text().splitlines()), 2)
            self.assertEqual(result['posts'][0]['comments_exported'], 1)
            self.assertNotEqual(result['export_id'], saved['export_id'])
            self.assertTrue(bot.verify_export_generation(saved['folder']))

    def test_resume_no_request_update_rolls_back_on_disk_failure(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'post', replies=SimpleNamespace(replies=0, channel_id=99))
        with tempfile.TemporaryDirectory() as output:
            saved = asyncio.run(bot.process_channel(FakeClient({'channel': entity}, {1: [post]}), 'channel',
                dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            folder = Path(saved['folder'])
            before = {name: (folder / name).read_bytes() for name in bot._DATA_FILES}
            replace = os.replace
            failed = False
            def fail_once(source, target):
                nonlocal failed
                if target == str(folder / 'posts.csv') and not failed:
                    failed = True
                    raise OSError(errno.ENOSPC, 'injected disk full')
                return replace(source, target)
            client = FakeClient({'channel': entity}, {})
            with patch.object(bot.os, 'replace', side_effect=fail_once):
                with self.assertRaises(OSError):
                    asyncio.run(bot.resume_comments(client, str(folder)))
            self.assertEqual(client.iter_calls, [])
            self.assertEqual(before, {name: (folder / name).read_bytes() for name in before})
            self.assertTrue(bot.verify_export_generation(str(folder)))

    def test_resume_accepts_legacy_records_without_poll_text(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'post', replies=SimpleNamespace(replies=2, channel_id=99))
        with tempfile.TemporaryDirectory() as output:
            saved = asyncio.run(bot.process_channel(FakeClient({'channel': entity}, {1: [post]}, {
                (1, 1): [FakeMessage(102, '2026-08-20', 'old reply')]}), 'channel', dt('2026-08-20'), dt('2026-08-20'),
                output_dir=output, comments_requested=True))
            folder = Path(saved['folder'])
            with (folder / 'posts.csv').open(newline='') as stream:
                reader = csv.DictReader(stream)
                fields = [field for field in reader.fieldnames if field != 'poll_text']
                posts = [{key: value for key, value in row.items() if key != 'poll_text'} for row in reader]
            with (folder / 'posts.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fields)
                writer.writeheader()
                writer.writerows(posts)
            records = [json.loads(line) for line in (folder / 'comments.jsonl').read_text().splitlines()]
            records[1].pop('poll_text')
            (folder / 'comments.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records))
            client = FakeClient({'channel': entity}, {}, {(1, 1): [FakeMessage(101, '2026-08-20', 'new reply')]})
            result = asyncio.run(bot.resume_comments(client, str(folder)))
            self.assertEqual(client.iter_calls, [(1, 1)])
            self.assertEqual(client.iter_options, [{'offset_id': 102}])
            self.assertEqual(result['posts'][0]['poll_text'], '')
            self.assertEqual(result['posts'][0]['text'], 'post')
            comments = [json.loads(line) for line in (folder / 'comments.jsonl').read_text().splitlines()][1:]
            self.assertEqual([row['poll_text'] for row in comments], ['', ''])
            self.assertEqual([row['text'] for row in comments], ['old reply', 'new reply'])
            self.assertEqual(result['posts'][0]['comments_export_status'], 'complete')
            self.assertTrue(bot.verify_export_generation(str(folder)))

    def test_equal_dates_sort_by_id_and_keep_album_order(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        posts = [FakeMessage(i, '2026-08-20', media=photo_media(i)) for i in (3, 2, 1)]
        for post in posts:
            post.grouped_id = 777
        with tempfile.TemporaryDirectory() as output:
            result = asyncio.run(bot.process_channel(FakeClient({'channel': entity}, {1: posts}), 'channel',
                dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            self.assertEqual([row['post_id'] for row in result['posts']], [1, 2, 3])
            self.assertEqual([row['media_order'] for row in result['posts']], [1, 2, 3])

    def test_common_media_suffixes_and_original_filename(self):
        for mime, suffix in {'audio/ogg': '.ogg', 'image/webp': '.webp', 'video/webm': '.webm',
                             'application/x-tgsticker': '.tgs', 'video/quicktime': '.mov',
                             'audio/mp4': '.m4a', 'audio/wav': '.wav', 'video/mp4': '.mp4',
                             'unknown/type': '.bin'}.items():
            self.assertEqual(bot._media_suffix({'media_mime_type': mime}), suffix)
            self.assertEqual(bot._media_suffix({'media_mime_type': mime, 'media_name': 'original.ext'}), '.ext')

    def test_resume_adds_comments_without_fetching_posts_or_media(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'saved post', media=photo_media(11),
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
                with bot._lock_export_folder(folder):
                    pass

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
                     patch('builtins.input', side_effect=['2', saved['folder']] if interactive else AssertionError('no prompts expected')), \
                     patch('sys.stdout', new_callable=io.StringIO) as console:
                    result = asyncio.run(bot.main([] if interactive else ['--resume-comments', saved['folder']]))
                self.assertEqual(result, 0)
                self.assertEqual(client.iter_calls, [(1, 1)])
                self.assertIn('Продолжается сохраненный срез', console.getvalue())
                self.assertIn('Канал: Channel; постов: 1; период UTC:', console.getvalue())
                self.assertIn('Папка будет заблокирована', console.getvalue())
                self.assertIn('Папка: ', console.getvalue())
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
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        media = [
            photo_media(11),
            document_media(12, 'image/png'),
            document_media(13, 'video/mp4', [tl.DocumentAttributeVideo(1, 1, 1)]),
            document_media(14, 'audio/ogg', [tl.DocumentAttributeAudio(1, voice=True)]),
            document_media(15, 'application/pdf'),
            document_media(16, 'video/mp4', [tl.DocumentAttributeAnimated(), tl.DocumentAttributeVideo(1, 1, 1)]),
            document_media(17, 'video/mp4', [tl.DocumentAttributeVideo(1, 1, 1, round_message=True)]),
        ]
        expected = {'N': set(), 'P': {1, 2, 101}, 'V': {3, 7}, 'A': {1, 2, 3, 4, 5, 6, 7, 101}}
        for mode, ids in expected.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as output:
                posts = [FakeMessage(index, '2026-08-20', media=item) for index, item in enumerate(media, 1)]
                posts[0].replies = SimpleNamespace(replies=1, channel_id=99)
                comment = FakeMessage(101, '2020-01-01', media=photo_media(111))
                client = FakeClient({'channel': entity}, {1: posts}, {(1, 1): [comment]})
                result = asyncio.run(bot.run_exports(client, ['channel'], dt('2026-08-20'), dt('2026-08-20'),
                    media_mode=mode, comments_requested=True, output_dir=output))
                self.assertEqual(result['exit_code'], 0, result['failures'])
                self.assertEqual(set(client.download_calls), ids)
                rows = result['successes'][0]['posts']
                self.assertEqual([row['media_type'] for row in rows],
                                 ['photo', 'image', 'video', 'voice', 'document', 'animation', 'video_note'])
                if mode == 'P':
                    self.assertEqual(rows[2]['media_status'], 'skipped_by_filter')

    def test_album_order_captions_and_native_telegram_entities(self):
        from telethon.tl.types import Message, MessageEntityTextUrl, PeerChannel, InputPeerChannel
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        posts = [FakeMessage(index, '2026-08-20', media=photo_media(index)) for index in (3, 2, 1)]
        for post in posts:
            post.grouped_id = 777
            post.forwards = post.id
        posts[1] = Message(id=2, peer_id=PeerChannel(1), date=dt('2026-08-20'), message='🙂 abc',
            grouped_id=777, entities=[MessageEntityTextUrl(offset=3, length=3, url='https://example.com')], forwards=2)
        posts[1]._input_chat = InputPeerChannel(1, 2)
        posts[1].media = photo_media(2)
        posts[0].replies = SimpleNamespace(replies=2, channel_id=99)
        comments = [FakeMessage(index, '2020-01-01', media=photo_media(index)) for index in (103, 101)]
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
            posts = [FakeMessage(index, '2026-08-20', f'text {index}', media=document_media(index, 'video/mp4', [tl.DocumentAttributeVideo(1, 10, 10)])) for index in range(1, 7)]
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
        post = FakeMessage(1, '2026-08-20', 'keep me', media=photo_media(123))
        client = FakeClient({'channel': entity}, {1: [post]})
        with tempfile.TemporaryDirectory() as output:
            first = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            first_path = first['posts'][0]['media_local_path']
            post.media.photo.id = 456
            second = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            self.assertNotEqual(second['posts'][0]['media_local_path'], first_path)
            self.assertTrue(os.path.isfile(os.path.join(first['folder'], first_path)))
            post.media.photo.id = 789
            with patch.object(client, 'download_media', side_effect=OSError('broken attachment')):
                failed = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), media_mode='A', output_dir=output))
            self.assertEqual(failed['channel']['media_export_status'], 'partial')
            self.assertEqual(failed['posts'][0]['text'], 'keep me')
            self.assertEqual(failed['posts'][0]['media_status'], 'download_failed')
            self.assertEqual(failed['posts'][0]['media_local_path'], '')

    def test_mixed_revisions_are_detected_and_snapshot_survives_final_disk_failure(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        post = FakeMessage(1, '2026-08-20', 'keep me', media=photo_media(11))
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
                    yield FakeMessage(1, '2026-08-20', 'text', media=document_media(11, 'video/mp4', [tl.DocumentAttributeVideo(1, 10, 10)]))
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
            client = FatalClient({'channel': entity, 'later': SimpleNamespace(id=2)}, {1: [FakeMessage(1, '2026-08-20', 'saved text', media=document_media(11, 'video/mp4', [tl.DocumentAttributeVideo(1, 10, 10)]))]})
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
            second = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            self.assertNotEqual(second['folder'], result['folder'])
            with self.assertRaises(bot.ChannelFolderError):
                bot._write_export_agents(result['folder'], 'new')
            with open(path, encoding='utf-8') as stream:
                self.assertEqual(stream.read(), 'custom instructions')

    def test_snapshot_creation_failure_keeps_published_text(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity, 'later': SimpleNamespace(id=2)}, {
            1: [FakeMessage(1, '2026-08-20', 'saved text', media=photo_media(11))],
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
            client = CancelClient({'channel': entity}, {1: [FakeMessage(1, '2026-08-20', 'saved text', media=document_media(11, 'video/mp4', [tl.DocumentAttributeVideo(1, 10, 10)]))]})
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
        for value in ('@channel', 'channel', 'https://t.me/channel', 't.me/channel',
                      'https://t.me/channel/123', 't.me/channel/123/', 'https://t.me/s/channel', 't.me/s/channel/123'):
            with self.subTest(value=value):
                self.assertEqual(bot.parse_channel_source(value), ['channel'])
        self.assertEqual(bot.parse_channel_source('@one, t.me/two https://t.me/s/one/123'), ['one', 'two'])
        self.assertEqual(bot.parse_channel_input('t.me/+secret'), 'https://t.me/+secret')
        for value in ('https://t.me/joinchat/secret', 'https://t.me/c/123/456', 'https://t.me/c/123',
                      'https://t.me/share/url?url=example.com'):
            self.assertEqual(bot.parse_channel_input(value), value)
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
        media = photo_media()
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

    def test_new_exports_preserve_full_dataset_for_narrow_and_empty_periods(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        latest = FakeMessage(1001, '2026-08-20', 'latest', media=photo_media(123),
                             replies=SimpleNamespace(replies=1, channel_id=99))
        posts = [latest] + [FakeMessage(index, '2026-08-19', 'older') for index in range(1000, 0, -1)]
        client = FakeClient({'channel': entity}, {1: posts}, {(1, 1001): [FakeMessage(101, '2026-08-20', 'reply')]})
        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            first = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-19'), dt('2026-08-20'),
                                comments_requested=True, media_mode='A', output_dir=output))
            self.assertEqual(len(first['posts']), 1001)
            original = {path.relative_to(first['folder']): path.read_bytes()
                        for path in Path(first['folder']).rglob('*') if path.is_file()}
            folders = {first['folder']}
            for day, expected_count in [('2026-08-20', 1), ('2026-08-21', 0), ('2026-08-20', 1)]:
                result = asyncio.run(bot.run_exports(client, ['channel'], dt(day), dt(day), output_dir=output))
                self.assertEqual(result['exit_code'], 0)
                saved = result['successes'][0]
                self.assertNotIn(saved['folder'], folders)
                self.assertNotEqual(saved['export_id'], first['export_id'])
                folders.add(saved['folder'])
                self.assertEqual(len(saved['posts']), expected_count)
                self.assertTrue(bot.verify_export_generation(saved['folder']))
                self.assertEqual({path.relative_to(first['folder']): path.read_bytes()
                                  for path in Path(first['folder']).rglob('*') if path.is_file()}, original)

    def test_failed_new_publication_preserves_previous_dataset(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity}, {1: [FakeMessage(1, '2026-08-20', 'saved')]})
        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            first = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            original = {path.name: path.read_bytes() for path in Path(first['folder']).iterdir() if path.is_file()}
            replace = os.replace

            def fail_posts(source, target):
                if os.path.basename(target) == 'posts.csv':
                    raise OSError(errno.ENOSPC, 'disk full')
                return replace(source, target)

            with patch.object(bot.os, 'replace', side_effect=fail_posts):
                result = asyncio.run(bot.run_exports(client, ['channel'], dt('2026-08-21'), dt('2026-08-21'), output_dir=output))
            self.assertEqual(result['exit_code'], 1)
            self.assertTrue(result['fatal_error'])
            self.assertTrue(bot.verify_export_generation(first['folder']))
            self.assertEqual({path.name: path.read_bytes() for path in Path(first['folder']).iterdir() if path.is_file()}, original)

    def test_new_folder_reservation_does_not_reuse_empty_directory(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        with tempfile.TemporaryDirectory() as output:
            first = bot.select_channel_folder(output, entity)
            second = bot.select_channel_folder(output, entity)
            third = bot.select_channel_folder(output, entity)
            self.assertEqual(len({first, second, third}), 3)
            self.assertTrue(all(os.path.isdir(folder) for folder in (first, second, third)))

    def test_new_export_can_follow_metadata_failure_with_only_lock_file_left(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity}, {1: [FakeMessage(1, '2026-08-20', 'saved')]})
        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            with patch.object(client, 'get_full_channel', side_effect=OSError('metadata unavailable')):
                failed = asyncio.run(bot.run_exports(client, ['channel'], dt('2026-08-20'), dt('2026-08-20'),
                                                     comments_requested=True, output_dir=output))
            self.assertEqual(failed['exit_code'], 1)
            self.assertEqual(os.listdir(os.path.join(output, 'channel')), ['.export.lock'])
            result = asyncio.run(bot.run_exports(client, ['channel'], dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            self.assertEqual(result['exit_code'], 0)
            self.assertNotEqual(result['successes'][0]['folder'], os.path.realpath(os.path.join(output, 'channel')))

    def test_resume_legacy_without_history_status_defaults_to_complete(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity}, {1: [FakeMessage(1, '2026-08-20', 'saved')]})
        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            saved = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            folder = saved['folder']
            with Path(folder, 'channel.csv').open(encoding='utf-8', newline='') as stream:
                channel = next(csv.DictReader(stream))
            channel.pop('posts_export_status')
            with Path(folder, 'channel.csv').open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(channel))
                writer.writeheader()
                writer.writerow(channel)
            with Path(folder, 'comments.jsonl').open(encoding='utf-8') as stream:
                control = json.loads(stream.readline())
            control.pop('posts_export_status')
            Path(folder, 'comments.jsonl').write_text(json.dumps(control) + '\n', encoding='utf-8')
            legacy_client = FakeClient({'channel': entity}, {})
            result = asyncio.run(bot.resume_comments(legacy_client, folder))
            self.assertEqual(result['channel']['posts_export_status'], 'complete')
            self.assertEqual(legacy_client.iter_calls, [])

    def test_cancelling_fresh_export_preserves_previous_dataset(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity}, {1: [FakeMessage(1, '2026-08-20', 'saved')]})

        async def scenario(output):
            first = await bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output)
            original = {path.name: path.read_bytes() for path in Path(first['folder']).iterdir() if path.is_file()}
            entered = asyncio.Event()

            async def blocked_history(*args, **kwargs):
                yield FakeMessage(2, '2026-08-20', 'new')
                entered.set()
                await asyncio.Event().wait()

            with patch.object(client, 'iter_messages', side_effect=blocked_history):
                task = asyncio.create_task(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(bot.verify_export_generation(first['folder']))
            self.assertEqual({path.name: path.read_bytes() for path in Path(first['folder']).iterdir() if path.is_file()}, original)
            partial = next(path for path in Path(output).iterdir() if str(path.resolve()) != first['folder'])
            self.assertTrue(bot.verify_export_generation(partial))
            with (partial / 'channel.csv').open(encoding='utf-8', newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['posts_export_status'], 'interrupted')
            with (partial / 'posts.csv').open(encoding='utf-8', newline='') as stream:
                self.assertEqual([row['text'] for row in csv.DictReader(stream)], ['new'])

        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            asyncio.run(scenario(output))

    def test_parallel_resumes_are_rejected_before_reading_or_telegram_requests(self):
        async def scenario(output):
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            post = FakeMessage(1, '2026-08-20', 'saved', replies=SimpleNamespace(replies=1, channel_id=99))
            saved = await bot.process_channel(FakeClient({'channel': entity}, {1: [post]}), 'channel',
                                             dt('2026-08-20'), dt('2026-08-20'), output_dir=output)
            entered, release = asyncio.Event(), asyncio.Event()

            class PausedClient(FakeClient):
                async def iter_messages(self, entity, reply_to=None, **kwargs):
                    entered.set()
                    await release.wait()
                    yield FakeMessage(101, '2026-08-20', 'first writer')

            first = PausedClient({'channel': entity}, {})
            second = FakeClient({'channel': entity}, {})
            task = asyncio.create_task(bot.resume_comments(first, saved['folder']))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                before = {name: Path(saved['folder'], name).read_bytes() for name in bot._DATA_FILES}
                with patch.object(bot, '_load_saved_export', side_effect=AssertionError('must lock before reading')):
                    with self.assertRaisesRegex(bot.ChannelFolderError, 'занята'):
                        await bot.resume_comments(second, saved['folder'])
                self.assertEqual(second.entity_calls, [])
                self.assertEqual(second.iter_calls, [])
                self.assertEqual(before, {name: Path(saved['folder'], name).read_bytes() for name in bot._DATA_FILES})
                release.set()
                await asyncio.wait_for(task, 2)
                with Path(saved['folder'], 'comments.jsonl').open(encoding='utf-8') as stream:
                    self.assertEqual([row['comment_id'] for row in map(json.loads, stream)
                                      if row['record_type'] == 'comment'], [101])
                await bot.resume_comments(second, saved['folder'])
                self.assertEqual(second.iter_calls, [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            asyncio.run(scenario(output))

    def test_fresh_export_blocks_resume_until_writer_finishes(self):
        async def scenario(output):
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            post = FakeMessage(1, '2026-08-20', 'saved', replies=SimpleNamespace(replies=1, channel_id=99))
            entered, release = asyncio.Event(), asyncio.Event()

            class PausedClient(FakeClient):
                async def iter_messages(self, entity, reply_to=None, **kwargs):
                    if reply_to is None:
                        yield post
                    else:
                        entered.set()
                        await release.wait()
                        yield FakeMessage(101, '2026-08-20', 'saved reply')

            task = asyncio.create_task(bot.process_channel(PausedClient({'channel': entity}, {}), 'channel',
                                       dt('2026-08-20'), dt('2026-08-20'), comments_requested=True, output_dir=output))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                folder = os.path.join(output, 'channel')
                self.assertTrue(bot.verify_export_generation(folder))
                other = FakeClient({'channel': entity}, {})
                with self.assertRaisesRegex(bot.ChannelFolderError, 'занята'):
                    await bot.resume_comments(other, folder)
                self.assertEqual(other.entity_calls, [])
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await bot.resume_comments(other, folder)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            asyncio.run(scenario(output))

    def test_writer_lock_blocks_other_process_and_releases_after_abrupt_exit(self):
        contender = '''
import sys
import telegram_stats_bot as bot
try:
    with bot._lock_export_folder(sys.argv[1]):
        raise AssertionError('second process acquired writer lock')
except bot.ChannelFolderError as exc:
    assert 'занята' in str(exc)
    print('blocked')
'''
        crash = '''
import os, sys
import telegram_stats_bot as bot
lock = bot._lock_export_folder(sys.argv[1])
os._exit(23)
'''
        with tempfile.TemporaryDirectory() as folder:
            with bot._lock_export_folder(folder):
                run = subprocess.run([sys.executable, '-c', contender, folder], capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout.strip(), 'blocked')
            run = subprocess.run([sys.executable, '-c', crash, folder], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 23, run.stderr)
            self.assertTrue(Path(folder, '.export.lock').is_file())
            with bot._lock_export_folder(folder):
                pass

    def test_busy_resume_cli_exits_before_authorization(self):
        config = bot.RuntimeConfig(123, 'offline', 'offline', '/unused')
        with tempfile.TemporaryDirectory() as folder, bot._lock_export_folder(folder), \
             patch.object(bot, 'load_runtime_config', return_value=config), \
             patch.object(bot, '_load_saved_export', side_effect=AssertionError('must lock before reading')), \
             patch.object(bot, 'ExportClient', side_effect=AssertionError('must not authorize busy writer')), \
             patch('sys.stdout', new_callable=io.StringIO) as console:
            self.assertEqual(asyncio.run(bot.main(['--resume-comments', folder])), 1)
            self.assertIn('занята', console.getvalue())

    def test_cmd_c_before_posts_ready_saves_history_and_keeps_partial_status_on_resume(self):
        script = '''
import asyncio, os, signal, sys
from types import SimpleNamespace
from test_telegram_stats_bot import FakeClient, FakeMessage, dt
import telegram_stats_bot as bot
entity = SimpleNamespace(id=1, title='Channel', username='channel')
class InterruptClient(FakeClient):
    async def iter_messages(self, entity, reply_to=None, **kwargs):
        assert reply_to is None
        yield FakeMessage(1, '2026-08-20', 'saved before ready', replies=SimpleNamespace(replies=1, channel_id=99))
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
        await asyncio.Event().wait()
try:
    asyncio.run(bot.process_channel(InterruptClient({'channel': entity}, {}), 'channel',
                dt('2026-08-20'), dt('2026-08-20'), comments_requested=True, output_dir=sys.argv[1]))
except KeyboardInterrupt:
    pass
'''
        with tempfile.TemporaryDirectory() as output:
            run = subprocess.run([sys.executable, '-c', script, output], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn('never retrieved', run.stderr)
            self.assertNotIn('Task was destroyed', run.stderr)
            self.assertNotIn('Посты готовы', run.stdout)
            self.assertIn('Сохранен прогресс', run.stdout)
            folder = os.path.join(output, 'channel')
            self.assertTrue(bot.verify_export_generation(folder))
            channel, posts = bot._load_saved_export(folder)
            self.assertEqual(channel['posts_export_status'], 'interrupted')
            self.assertEqual(posts[0]['text'], 'saved before ready')
            self.assertEqual(posts[0]['comments_export_status'], 'interrupted')
            with Path(folder, 'comments.jsonl').open(encoding='utf-8') as stream:
                self.assertEqual(json.loads(stream.readline())['posts_export_status'], 'interrupted')
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            client = FakeClient({'channel': entity}, {}, {(1, 1): [FakeMessage(101, '2026-08-20', 'reply')]})
            with patch('sys.stdout', new_callable=io.StringIO) as console:
                saved = asyncio.run(bot.resume_comments(client, folder))
                bot._print_export_summary({'successes': [saved], 'failures': []})
            self.assertEqual(client.iter_calls, [(1, 1)])
            self.assertEqual(saved['channel']['posts_export_status'], 'interrupted')
            self.assertIn('История постов неполная', console.getvalue())

    def test_early_cancellation_retains_completed_media_and_cleans_partial_downloads(self):
        async def scenario(output):
            entity = SimpleNamespace(id=1, title='Channel', username='channel')
            blocked = asyncio.Event()

            class MediaClient(FakeClient):
                async def iter_messages(self, entity, reply_to=None, **kwargs):
                    self.iter_calls.append((entity.id, reply_to))
                    for index in (2, 1):
                        post = FakeMessage(index, '2026-08-20', f'post {index}',
                                           media=photo_media(index),
                                           replies=SimpleNamespace(replies=1, channel_id=99))
                        post.grouped_id = 123
                        yield post
                    await asyncio.Event().wait()

                async def download_media(self, message, file):
                    if message.id == 2:
                        return await super().download_media(message, file)
                    Path(file).write_bytes(b'partial download')
                    blocked.set()
                    await asyncio.Event().wait()

            client = MediaClient({'channel': entity}, {})
            task = asyncio.create_task(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'),
                                       comments_requested=True, media_mode='A', output_dir=output))
            try:
                await asyncio.wait_for(blocked.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                folder = os.path.join(output, 'channel')
                self.assertTrue(bot.verify_export_generation(folder))
                channel, posts = bot._load_saved_export(folder)
                self.assertEqual(channel['posts_export_status'], 'interrupted')
                self.assertEqual(channel['media_export_status'], 'interrupted')
                by_id = {row['post_id']: row for row in posts}
                self.assertEqual(by_id[1]['media_status'], 'interrupted')
                self.assertEqual(by_id[1]['media_local_path'], '')
                self.assertEqual(by_id[2]['media_status'], 'downloaded')
                self.assertEqual(Path(folder, by_id[2]['media_local_path']).read_bytes(), b'media')
                self.assertEqual([by_id[i]['media_order'] for i in (1, 2)], [1, 2])
                self.assertTrue(all(row['comments_export_status'] == 'interrupted' for row in posts))
                self.assertEqual(client.iter_calls, [(1, None)])
                self.assertFalse(any(path.name.endswith('.tmp') for path in Path(folder).rglob('*')))
                self.assertEqual([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()], [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO):
            asyncio.run(scenario(output))

    def test_checkpoint_disk_full_stops_before_next_channel(self):
        class HistoryFailure(FakeClient):
            async def iter_messages(self, entity, reply_to=None, **kwargs):
                yield FakeMessage(1, '2026-08-20', 'read before failure')
                raise RuntimeError('history failed')

        entities = {'channel': SimpleNamespace(id=1, title='Channel', username='channel'),
                    'later': SimpleNamespace(id=2, title='Later', username='later')}
        client = HistoryFailure(entities, {})
        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO), \
             patch('sys.stderr', new_callable=io.StringIO) as errors, \
             patch.object(bot, 'publish_export', side_effect=OSError(errno.ENOSPC, 'disk full')):
            result = asyncio.run(bot.run_exports(client, ['channel', 'later'], dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            self.assertTrue(result['fatal_error'])
            self.assertEqual(client.entity_calls, ['channel'])
            self.assertIn('Не удалось сохранить прогресс', errors.getvalue())
            with bot._lock_export_folder(os.path.join(output, 'channel')):
                pass

    def test_repeat_media_has_independent_files_in_each_export(self):
        entity = SimpleNamespace(id=1, title="Channel", username="channel")
        post = FakeMessage(1, "2026-08-20", None, media=photo_media(123))
        client = FakeClient({"channel": entity}, {1: [post]})
        with tempfile.TemporaryDirectory() as output:
            first = asyncio.run(bot.run_exports(client, ["channel"], dt("2026-08-20"), dt("2026-08-20"), download_media=True, output_dir=output))['successes'][0]
            second = asyncio.run(bot.run_exports(client, ["channel"], dt("2026-08-20"), dt("2026-08-20"), download_media=True, output_dir=output))['successes'][0]
            self.assertNotEqual(first['folder'], second['folder'])
            for saved in (first, second):
                self.assertTrue(os.path.isfile(os.path.join(saved['folder'], saved['posts'][0]['media_local_path'])))
        self.assertEqual(client.download_calls, [1, 1])

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
        protected = FakeMessage(1, "2026-08-20", None, media=document_media(11, 'application/pdf'), noforwards=True)
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
        post = FakeMessage(1, "2026-08-20", None, media=photo_media())
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
        self.assertIn("Папка: /tmp/channel", rendered)
        self.assertIn("Всего постов:     1", rendered)
        self.assertIn("Пересылок: 2, Реакций: 3, Просмотров: 10", rendered)
        self.assertIn("Текст: hello", rendered)

    def test_summary_warns_about_empty_or_incomplete_exports(self):
        saved = {
            'channel': {'title': 'Channel', 'media_export_status': 'partial'},
            'folder': '/tmp/separate-export',
            'posts': [{'comments_export_status': 'partial'}, {'comments_export_status': 'unavailable'}],
        }
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            bot._print_export_summary({'successes': [saved], 'failures': []})
        rendered = output.getvalue()
        self.assertIn('Комментарии неполные: веток 2', rendered)
        self.assertIn('Часть медиа не загружена', rendered)
        self.assertNotIn('Успешно', rendered)
        saved['posts'] = []
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            bot._print_export_summary({'successes': [saved], 'failures': []})
        self.assertIn('За выбранный период постов нет', output.getvalue())
        saved['channel']['posts_export_status'] = 'interrupted'
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            bot._print_export_summary({'successes': [saved], 'failures': []})
        self.assertIn('История постов неполная', output.getvalue())
        self.assertNotIn('За выбранный период постов нет', output.getvalue())

    def test_summary_latest_posts_are_actually_latest(self):
        posts = [{'text': f'post-{index}', 'date': f'2026-08-{index + 10}'} for index in range(1, 7)]
        result = {'successes': [{'channel': {'title': 'Channel'}, 'folder': '/tmp/channel', 'posts': posts}],
                  'failures': [], 'sort_key': 'date'}
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            bot._print_export_summary(result)
        rendered = output.getvalue()
        self.assertLess(rendered.index('Текст: post-6'), rendered.index('Текст: post-5'))
        self.assertNotIn('Текст: post-1', rendered)

    def test_new_export_cli_preserves_existing_folder_and_explains_utc(self):
        entity = SimpleNamespace(id=1, title='Channel', username='channel')
        client = FakeClient({'channel': entity}, {1: [FakeMessage(1, '2026-08-20', 'saved')]})
        client.start = AsyncMock()
        client.disconnect = AsyncMock()
        with tempfile.TemporaryDirectory() as output, patch('sys.stdout', new_callable=io.StringIO) as console:
            saved = asyncio.run(bot.process_channel(client, 'channel', dt('2026-08-20'), dt('2026-08-20'), output_dir=output))
            original = Path(saved['folder'], 'posts.csv').read_bytes()
            config = bot.RuntimeConfig(123, 'offline', 'offline', output)
            with patch.object(bot, 'ExportClient', return_value=client), \
                 patch.object(bot, 'load_runtime_config', return_value=config), \
                 patch('builtins.input', side_effect=['', 'channel', '20-08-2026', '20-08-2026', '', '', '']):
                result = asyncio.run(bot.main([]))
            self.assertEqual(result, 0)
            self.assertEqual(Path(saved['folder'], 'posts.csv').read_bytes(), original)
            self.assertEqual(len(os.listdir(output)), 2)
        rendered = console.getvalue()
        self.assertIn('новая выгрузка в отдельную папку', rendered)
        self.assertIn('границы суток — UTC', rendered)
        self.assertIn('Папка:', rendered)

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
