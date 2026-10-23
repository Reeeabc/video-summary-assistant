from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import platforms
import video_assistant as app

SHORT = 'https://b23.tv/bEXkGgD'
LONG = 'https://www.bilibili.com/video/BV1THKyzBER6?p=1'
CLIENT = httpx.Client


class NoBody(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError('Short-link discovery must not read response bodies')


def client(handler):
    return patch.object(platforms.httpx, 'Client',
                        side_effect=lambda **kw: CLIENT(transport=httpx.MockTransport(handler), **kw))


class ShortLinkTests(unittest.TestCase):
    def test_redirect_keeps_part_and_canonical_identity_without_reading_body(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('cookie', request.headers)
            return httpx.Response(302, headers={'location': LONG.replace('p=1', 'p=2') + '&share_source=copy'}, stream=NoBody())
        with client(handler), patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'private'}):
            ref = platforms.resolve_video(SHORT)
        self.assertEqual(ref.url, LONG.replace('p=1', 'p=2'))
        self.assertEqual(ref.identity, 'bilibili-BV1THKyzBER6-p2')
        self.assertEqual(calls, [SHORT])

    def test_existing_full_links_and_bv_ids_do_not_request_short_link_service(self):
        with patch.object(platforms.httpx, 'Client') as called:
            self.assertEqual(platforms.resolve_video(LONG).url, LONG)
            self.assertEqual(platforms.resolve_video('BV1THKyzBER6').url, LONG)
            called.assert_not_called()

    def test_relative_redirects_clear_response_cookies_and_remain_bounded(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            self.assertNotIn('cookie', request.headers)
            target = '/next123' if len(calls) == 1 else LONG
            return httpx.Response(302, headers={'location': target, 'set-cookie': 'private=value; Path=/'}, stream=NoBody())
        with client(handler):
            self.assertEqual(platforms.resolve_video(SHORT).url, LONG)
        self.assertEqual(calls, [SHORT, 'https://b23.tv/next123'])

    def test_unsafe_or_non_video_destinations_are_rejected_before_a_second_request(self):
        targets = ['http://www.bilibili.com/video/BV1THKyzBER6', 'https://127.0.0.1/private',
                   'https://b23.tv.evil.test/next', 'https://b23.tv:443/next',
                   'https://private:secret@b23.tv/next', 'https://www.bilibili.com/playlist',
                   'https://youtu.be/BaW_jenozKc', 'https://www.bilibili.com/video/BV1THKyzBER6?p=0',
                   'https://www.bilibili.com/video/BV1THKyzBER6?p=1&p=2', '', 'bad\tlocation']
        for target in targets:
            calls = []
            def handler(request):
                calls.append(str(request.url))
                return httpx.Response(302, headers={'location': target}, stream=NoBody())
            with self.subTest(target=target), client(handler), self.assertRaises(platforms.Problem):
                platforms.resolve_video(SHORT)
            self.assertEqual(calls, [SHORT])

    def test_malformed_short_inputs_never_reach_network(self):
        for value in ['http://b23.tv/a', 'https://b23.tv/a#x', 'https://b23.tv/a/b',
                      'https://user:secret@b23.tv/a', 'https://b23.tv:443/a', 'https://b23.tv/a\n']:
            with self.subTest(value=value), patch.object(platforms.httpx, 'Client') as called:
                with self.assertRaises(platforms.Problem):
                    platforms.resolve_video(value)
                called.assert_not_called()

    def test_failures_are_bounded_and_do_not_leak_urls_or_network_errors(self):
        for kind in ['loop', 'too-many', 'http', 'transport']:
            calls = []
            def handler(request):
                calls.append(str(request.url))
                if kind == 'transport':
                    raise httpx.ReadTimeout('private-url-secret', request=request)
                if kind == 'http':
                    return httpx.Response(403, stream=NoBody())
                target = SHORT if kind == 'loop' else 'https://b23.tv/next' + str(len(calls))
                return httpx.Response(302, headers={'location': target}, stream=NoBody())
            with self.subTest(kind=kind), client(handler), self.assertRaises(platforms.Problem) as error:
                platforms.resolve_video(SHORT)
            self.assertLessEqual(len(calls), 3)
            self.assertNotIn('private-url-secret', str(error.exception))
            self.assertNotIn(SHORT, str(error.exception))

    def test_prepare_short_link_reuses_success_without_caption_fetch_or_asr(self):
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(url=SHORT, source_strategy='auto', mode='standard',
                                   input_mode='direct-url', output=Path(td), reuse_from=None)
            def reuse(tasks, folder, received):
                self.assertEqual(tasks, [])
                self.assertIs(received, args)
                return True
            with client(lambda request: httpx.Response(302, headers={'location': LONG}, stream=NoBody())), \
                 patch.object(app, 'existing_tasks', return_value=[]) as existing, \
                 patch.object(app, 'reuse_success', side_effect=reuse), \
                 patch.object(app, 'fetch_captions') as captions, patch.object(app, 'credentials') as credentials:
                app.prepare(args)
                existing.assert_called_once_with(LONG, None)
                captions.assert_not_called()
                credentials.assert_not_called()

    def test_caption_check_short_link_preserves_canonical_url_without_asr(self):
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(url=SHORT, output=Path(td), max_minutes=300, subtitle_langs='zh-CN')
            result = {'status': 'missing', 'reason': 'empty_catalog'}
            with client(lambda request: httpx.Response(302, headers={'location': LONG}, stream=NoBody())), \
                 patch.object(app, 'fetch_captions', return_value=result) as captions, \
                 patch.object(app, 'credentials') as credentials, redirect_stdout(io.StringIO()):
                app.caption_check(args)
                self.assertEqual(captions.call_args.args[0].url, LONG)
                credentials.assert_not_called()
            self.assertEqual(json.loads((Path(td)/'caption-check.json').read_text())['video_url'], LONG)
