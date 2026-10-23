import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yt_dlp
from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import ExtractorError

from bilibili_extractor import ExtractionFailure, ProjectBiliBiliIE, QuietLogger, media_headers


BV = 'BV1gmJX6SExT'
URL = f'https://www.bilibili.com/video/{BV}?p=2'


class DetailFallbackTests(unittest.TestCase):
    def test_media_header_precedence_preserves_public_headers_and_drops_secrets(self):
        headers = media_headers({'User-Agent': 'yt-browser', 'Accept': '*/*', 'Cookie': 'private'},
                                {'Referer': URL, 'Authorization': 'Bearer private'},
                                {'User-Agent': 'format-browser', 'X-Api-Key': 'private'})
        self.assertEqual(headers, {'user-agent': 'format-browser', 'accept': '*/*', 'referer': URL})

    def setUp(self):
        self.detail = {'code': 0, 'data': {'View': {
            'bvid': BV, 'title': '公开视频', 'aid': 123, 'duration': 999,
            'owner': {'name': '测试作者', 'mid': 42},
            # Deliberately reversed: select by page, not response list order.
            'pages': [{'page': 2, 'cid': 222, 'duration': 75, 'part': '第二部分'},
                      {'page': 1, 'cid': 111, 'duration': 60, 'part': '第一部分'}]}, 'Tags': []}}
        self.playback = {'code': 0, 'data': {'timelength': 75000, 'dash': {
            'audio': [{'id': 30232, 'baseUrl': 'https://cdn.test/audio?sign=private',
                       'mimeType': 'audio/mp4', 'codecs': 'mp4a.40.2', 'bandwidth': 132000}],
            'video': []}}}
        self.calls = []
        self.page = '<html><title></title><script>window.__INITIAL_STATE__ && window.__INITIAL_STATE__.videoData</script></html>'

    def api(self, instance, url, *args, **kwargs):
        path = url.split('api.bilibili.com')[-1]
        self.calls.append((path, kwargs.get('query', {})))
        if path == '/x/web-interface/wbi/view/detail':
            return copy.deepcopy(self.detail)
        if path == '/x/player/wbi/playurl':
            if isinstance(self.playback, Exception):
                raise self.playback
            return copy.deepcopy(self.playback)
        if path == '/x/player/pagelist':
            return {'data': sorted(self.detail['data']['View']['pages'], key=lambda p: p['page'])}
        self.fail('unexpected network endpoint: ' + path)

    def extract(self, url=URL):
        with yt_dlp.YoutubeDL({'logger': QuietLogger(), 'noplaylist': True,
                              'compat_opts': {'no-certifi'}}) as ydl:
            ie = ProjectBiliBiliIE(ydl)
            with patch.object(ie, '_download_webpage_handle', return_value=(self.page, SimpleNamespace(url=url))), \
                 patch.object(ie, '_sign_wbi', side_effect=lambda query, _: {**query, 'w_rid': 'test'}), \
                 patch.object(BiliBiliIE, '_download_json', autospec=True, side_effect=self.api), \
                 patch.object(BiliBiliIE, '_get_chapters', return_value=None):
                return ie.extract(url)

    def test_missing_initial_state_reuses_upstream_playback_and_audio_extraction(self):
        info = self.extract()
        self.assertEqual(info['id'], BV + '_p2')
        self.assertEqual(info['title'], '公开视频 p02 第二部分')
        self.assertEqual(info['uploader'], '测试作者')
        self.assertEqual(info['duration'], 75)
        self.assertEqual(info['formats'][0]['acodec'], 'mp4a.40.2')
        self.assertEqual(info['formats'][0]['vcodec'], 'none')
        self.assertEqual(info['formats'][0]['format_id'], '30232')
        self.assertEqual([p for p, _ in self.calls], [
            '/x/web-interface/wbi/view/detail', '/x/player/wbi/playurl'])
        self.assertEqual(self.calls[-1][1]['bvid'], BV)
        self.assertEqual(self.calls[-1][1]['cid'], 222)
        self.assertEqual(self.calls[-1][1]['w_rid'], 'test')

    def test_omitted_p_selects_first_part(self):
        self.playback['data']['timelength'] = 60000
        info = self.extract(f'https://www.bilibili.com/video/{BV}')
        self.assertEqual(info['duration'], 60)
        self.assertEqual(self.calls[-1][1]['cid'], 111)

    def test_same_audio_format_preserves_the_playback_api_backup_urls(self):
        backup = 'https://upos-hz-mirrorakam.akamaized.net/audio?sign=private-backup'
        self.playback['data']['dash']['audio'][0]['backupUrl'] = [backup]
        info = self.extract()
        self.assertEqual(info['formats'][0]['backup_urls'], [backup])
        self.assertEqual(info['formats'][0]['format_id'], '30232')

    def test_invalid_detail_never_reaches_playback(self):
        mutations = [
            lambda d: d.update(code=-352),
            lambda d: d.update(code='0'),
            lambda d: d['data']['View'].update(bvid='BV1GJ411x7h7'),
            lambda d: d['data']['View'].update(pages=[]),
            lambda d: d['data']['View']['pages'][0].update(page=1),
            lambda d: d['data']['View']['pages'][0].update(cid=True),
            lambda d: d['data']['View']['pages'][0].update(duration=0),
        ]
        original = copy.deepcopy(self.detail)
        for change in mutations:
            with self.subTest(change=change):
                self.detail = copy.deepcopy(original)
                change(self.detail)
                self.calls.clear()
                with self.assertRaises(ExtractionFailure):
                    self.extract()
                self.assertNotIn('/x/player/wbi/playurl', [p for p, _ in self.calls])

    def test_nonexistent_or_ambiguous_part_is_rejected(self):
        for suffix in ('?p=3', '?p=0', '?p=1&p=2'):
            with self.subTest(suffix=suffix), self.assertRaises(ExtractionFailure):
                self.extract(f'https://www.bilibili.com/video/{BV}' + suffix)

    def test_existing_initial_state_does_not_use_detail_fallback(self):
        view = copy.deepcopy(self.detail['data']['View'])
        view['pages'].sort(key=lambda p: p['page'])
        self.page = '<script>window.__INITIAL_STATE__=' + json.dumps({'videoData': view}) + ';</script>'
        info = self.extract()
        self.assertEqual(info['formats'][0]['format_id'], '30232')
        self.assertNotIn('/x/web-interface/wbi/view/detail', [p for p, _ in self.calls])

    def test_verification_page_stops_before_detail_request(self):
        self.page = '<script>window._riskdata_={"v_voucher":"private"};</script>'
        with self.assertRaises(ExtractionFailure) as caught:
            self.extract()
        self.assertEqual(caught.exception.diagnostic['stage'], 'page')
        self.assertEqual(self.calls, [])

    def test_playback_business_error_is_reported_once_without_sensitive_values(self):
        self.playback = {'code': -352, 'message': '风控 https://cdn.test/?sign=private Cookie: private'}
        with self.assertRaises(ExtractionFailure) as caught:
            self.extract()
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic['stage'], 'playback')
        self.assertEqual(diagnostic['provider_code'], -352)
        self.assertNotIn('private', json.dumps(diagnostic))
        self.assertEqual(sum(path == '/x/player/wbi/playurl' for path, _ in self.calls), 1)

    def test_playback_http_failure_reports_status_and_stops(self):
        response = Response(io.BytesIO(b'blocked'), 'https://api.bilibili.com/x/player/wbi/playurl', {}, status=412)
        self.playback = ExtractorError('HTTP Error 412', cause=HTTPError(response))
        with self.assertRaises(ExtractionFailure) as caught:
            self.extract()
        self.assertEqual(caught.exception.diagnostic['http_status'], 412)
        self.assertEqual(caught.exception.diagnostic['endpoint'], '/x/player/wbi/playurl')
        self.assertEqual(sum(path == '/x/player/wbi/playurl' for path, _ in self.calls), 1)


if __name__ == '__main__':
    unittest.main()
