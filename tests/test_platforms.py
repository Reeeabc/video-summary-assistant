from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from yt_dlp.networking import Response

import audio_acquisition as acquisition
import generic_media as generic
import media_download
from platforms import Problem, parse_video
import temporary_audio
import video_assistant as app


YOUTUBE = 'https://www.youtube.com/watch?v=BaW_jenozKc'
VIMEO = 'https://vimeo.com/76979871'


class PlatformTests(unittest.TestCase):
    def info(self, platform='youtube', protocol='https', codec='opus'):
        video_id = 'BaW_jenozKc' if platform == 'youtube' else '76979871'
        return {'id': video_id, 'duration': 10, 'title': '音频解析样本', 'uploader': '作者',
                'formats': [{'format_id': 'audio-opus', 'vcodec': 'none', 'acodec': codec,
                    'ext': 'webm', 'url': 'https://cdn.test/audio?signature=private', 'protocol': protocol,
                    'http_headers': {'User-Agent': 'test', 'Cookie': 'private', 'Authorization': 'private'}}]}

    def test_canonical_platform_urls_and_collision_free_directories(self):
        urls = [YOUTUBE, 'https://youtu.be/BaW_jenozKc?t=2',
                'https://m.youtube.com/shorts/BaW_jenozKc', 'https://www.youtube.com/embed/BaW_jenozKc']
        for url in urls:
            ref = parse_video(url)
            self.assertEqual(ref.platform, 'youtube')
            self.assertEqual(ref.url, YOUTUBE)
            self.assertEqual(ref.identity, 'youtube-BaW_jenozKc')
        self.assertEqual(parse_video('https://player.vimeo.com/video/76979871').url, VIMEO)
        self.assertEqual(parse_video(VIMEO).identity, 'vimeo-76979871')
        self.assertEqual(parse_video('BV1gmJX6SExT').identity, 'bilibili-BV1gmJX6SExT-p1')

    def test_collections_channels_live_entries_and_unverified_platforms_are_closed(self):
        for url in ['https://youtube.com/playlist?list=private', 'https://youtube.com/@channel',
                    'https://youtube.com/live/BaW_jenozKc', 'https://vimeo.com/channels/demo',
                    'https://vimeo.com/76979871?h=private', 'https://youtube.com.evil.test/watch?v=BaW_jenozKc',
                    'https://user:private@vimeo.com/76979871', 'https://www.douyin.com/video/123',
                    'https://www.tiktok.com/@user/video/123', 'https://www.ixigua.com/123']:
            with self.subTest(url=url), self.assertRaises(Problem): parse_video(url)

    def test_opus_and_segmented_audio_do_not_use_bilibili_format_rules(self):
        for platform, url in [('youtube', YOUTUBE), ('vimeo', VIMEO)]:
            for protocol in ('https', 'm3u8_native', 'http_dash_segments'):
                info = self.info(platform, protocol)
                if protocol == 'http_dash_segments':
                    info['formats'][0]['fragments'] = [{'url': 'https://cdn.test/part?signature=private'}]
                headers = {}
                source, private = generic.normalize_info(parse_video(url), info, 300, headers)
                self.assertEqual(source['platform'], platform)
                self.assertEqual(source['audio_codec'], 'opus')
                self.assertEqual(source['canonical_url'], url)
                self.assertEqual(headers, {'user-agent': 'test'})
                self.assertNotIn('signature=', json.dumps(source))
                self.assertNotIn('Cookie', json.dumps(private))
                self.assertEqual(private['info']['protocol'], protocol)

    def test_no_audio_only_format_never_falls_back_to_video(self):
        info = self.info()
        info['formats'][0]['vcodec'] = 'avc1'
        with self.assertRaisesRegex(Problem, '独立音轨'):
            generic.normalize_info(parse_video(YOUTUBE), info, 300)
        with tempfile.TemporaryDirectory() as td, patch.object(media_download.yt_dlp, 'YoutubeDL') as ydl:
            with self.assertRaises(ValueError):
                media_download.download_selected(info['formats'][0], Path(td)/'audio', 1024)
            ydl.assert_not_called()

    def test_id_duration_live_and_playlist_guards_before_download(self):
        for changes in ({'id': 'wrong'}, {'duration': 18001}, {'duration': float('nan')},
                        {'is_live': True}, {'live_status': 'is_upcoming'}, {'_type': 'playlist'}, {'entries': []}):
            with self.subTest(changes=changes), self.assertRaises(Problem):
                generic.normalize_info(parse_video(YOUTUBE), {**self.info(), **changes}, 300)
        self.assertEqual(generic.normalize_info(parse_video(YOUTUBE), {**self.info(), 'duration': 18000}, 300)[0]['duration_seconds'], 18000)

    def test_generic_subprocess_has_ca_proxy_and_no_credentials_or_cookie_config(self):
        def run(command, **kw):
            self.assertIn('--use-extractors', command)
            self.assertEqual(command[command.index('--use-extractors')+1], 'youtube')
            self.assertIn('no-certifi', command)
            self.assertNotIn('--no-check-certificates', command)
            self.assertNotIn('DASHSCOPE_API_KEY', kw['env'])
            self.assertNotIn('COOKIE', kw['env'])
            self.assertEqual(kw['env']['HTTPS_PROXY'], 'http://proxy:8080')
            return subprocess.CompletedProcess(command, 0, json.dumps(self.info()), '')
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'secret', 'COOKIE': 'private', 'HTTPS_PROXY': 'http://proxy:8080'}), \
             patch.object(generic.subprocess, 'run', side_effect=run):
            self.assertEqual(generic.resolve_generic(parse_video(YOUTUBE), 300)[0]['platform'], 'youtube')

    def test_proxy_and_login_diagnostics_are_bounded_and_redacted(self):
        errors = ['Tunnel connection failed: 403 Forbidden https://cdn.test/a?signature=private',
                  'The web client only works when logged-in. Use --cookies private']
        for error, expected in zip(errors, ('HTTP 403', '需要登录')):
            with patch.object(generic.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', error)) as run:
                with self.assertRaises(Problem) as raised: generic.resolve_generic(parse_video(YOUTUBE), 300)
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn('signature=private', str(raised.exception))
                self.assertNotIn('--cookies private', str(raised.exception))
                self.assertEqual(run.call_count, 1)

    def test_segment_downloader_keeps_audio_and_rejects_missing_fragments(self):
        info = self.info(protocol='http_dash_segments')['formats'][0]
        info.update(id='test', title='test', fragments=[{'url': 'https://cdn.test/a'}])
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)/'source.media'
            def download(value):
                self.assertEqual(value['fragments'], info['fragments'])
                self.assertEqual(value['vcodec'], 'none')
                target.write_bytes(b'audio')
            with patch.object(media_download.yt_dlp, 'YoutubeDL') as ydl:
                ydl.return_value.__enter__.return_value.process_info.side_effect = download
                media_download.download_selected(info, target, 1024)
                opts = ydl.call_args.args[0]
                self.assertFalse(opts['skip_unavailable_fragments'])
                self.assertEqual(opts['fragment_retries'], 0)
                self.assertFalse(opts['nocheckcertificate'])
                with self.assertRaises(ValueError): opts['progress_hooks'][0]({'downloaded_bytes': 1025})
            target.unlink()
            with patch.object(media_download.yt_dlp, 'YoutubeDL') as ydl:
                ydl.return_value.__enter__.return_value.process_info.side_effect = media_download.DownloadError('fragment missing')
                with self.assertRaises(media_download.DownloadError): media_download.download_selected(info, target, 1024)

    def test_audio_worker_receives_private_info_only_via_stdin_and_cleans_failure(self):
        private = generic.normalize_info(parse_video(YOUTUBE), self.info(), 300)[1]
        def worker(command, **kw):
            self.assertNotIn('signature=private', ' '.join(command))
            self.assertIn('signature=private', kw['input'])
            self.assertNotIn('DASHSCOPE_API_KEY', kw['env'])
            return subprocess.CompletedProcess(command, 2, '', '{"reason":"HTTP 403"}')
        events = []
        with patch.object(acquisition.subprocess, 'run', side_effect=worker), \
             patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'secret'}):
            with self.assertRaises(temporary_audio.AudioUploadError):
                with temporary_audio.prepared_audio(private, {}, 10, event=lambda s,d: events.append(s)):
                    self.fail('failed audio cannot reach upload')
        self.assertEqual(events[-1], 'LOCAL_AUDIO_REMOVED')

    def test_real_opus_conversion_and_full_decode_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'source.webm', Path(td)/'audio.m4a'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                            '-c:a', 'libopus', str(source)], check=True, capture_output=True)
            result = acquisition.normalize_audio(source, output, 3, 1024*1024)
            self.assertEqual(result['codec'], 'aac')
            self.assertEqual(result['converted_from_codec'], 'opus')
            self.assertEqual(result['video_streams'], 0)
            self.assertTrue(result['full_decode_verified'])
            with self.assertRaises(temporary_audio.AudioUploadError):
                acquisition.normalize_audio(source, Path(td)/'bad.m4a', 30, 1024*1024)

    def test_real_native_dash_and_hls_assembly_with_offline_transport(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_dir = root/'fixtures'
            source_dir.mkdir()
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                            '-c:a', 'aac', '-f', 'dash', '-seg_duration', '1', str(source_dir/'audio.mpd')],
                           check=True, capture_output=True)
            chunks = sorted(source_dir.glob('chunk*.m4s'))
            init = next(source_dir.glob('init*.m4s'))
            payloads = {p.name: p.read_bytes() for p in [init, *chunks]}
            manifest = '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:2\n'
            manifest += f'#EXT-X-MAP:URI="{init.name}"\n'
            manifest += ''.join(f'#EXTINF:1.0,\n{p.name}\n' for p in chunks) + '#EXT-X-ENDLIST\n'
            payloads['audio.m3u8'] = manifest.encode()
            for protocol in ('http_dash_segments', 'm3u8_native'):
                with self.subTest(protocol=protocol):
                    directory = root/protocol
                    directory.mkdir()
                    target = directory/'source.media'
                    requests = []
                    def offline_open(ydl, request):
                        name = Path(urlsplit(request.url).path).name
                        requests.append(name)
                        body = payloads[name]
                        return Response(io.BytesIO(body), request.url, {'Content-Length': str(len(body))})
                    info = {'id': 'fixture', 'title': 'audio', 'extractor_key': 'Youtube',
                            'url': 'https://cdn.test/audio.m3u8', 'protocol': protocol,
                            'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a.40.2'}
                    if protocol == 'http_dash_segments':
                        info['fragments'] = [{'url': 'https://cdn.test/'+p.name} for p in [init, *chunks]]
                    with patch.object(media_download.yt_dlp.YoutubeDL, 'urlopen', offline_open):
                        media_download.download_selected(info, target, 1024*1024)
                    self.assertTrue({p.name for p in [init, *chunks]} <= set(requests))
                    result = acquisition.normalize_audio(target, directory/'audio.m4a', 3, 1024*1024)
                    self.assertTrue(result['full_decode_verified'])
                    self.assertEqual(result['video_streams'], 0)

    def test_audio_check_cannot_upload_or_bill_and_persists_safe_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(url=YOUTUBE, output=Path(td)/'check', max_minutes=300, max_audio_mib=1024)
            source, private = generic.normalize_info(parse_video(YOUTUBE), self.info(), 300)
            with patch.object(app, 'resolve', return_value=(source, private)), \
                 patch.object(acquisition, 'acquire_audio', return_value={'full_decode_verified': True}), \
                 patch.object(app, 'credentials', side_effect=AssertionError('no key needed')), \
                 patch.object(temporary_audio, 'get_policy', side_effect=AssertionError('no upload')), \
                 patch.object(app, 'submit', side_effect=AssertionError('no ASR')), redirect_stdout(io.StringIO()):
                app.audio_check(args)
            state = json.loads((args.output/'audio-check.json').read_text())
            self.assertEqual(state['status'], 'AUDIO_VERIFIED')
            self.assertFalse(state['transcription_submitted'])
            self.assertTrue(state['local_audio_removed'])
            self.assertNotIn('signature=private', json.dumps(state))

    def test_direct_url_rejects_segmented_audio_without_submission(self):
        _, media = generic.normalize_info(parse_video(YOUTUBE), self.info(protocol='m3u8_native'), 300)
        with self.assertRaises(Problem): app.direct_url(media)
        _, media = generic.normalize_info(parse_video(YOUTUBE), self.info(), 300)
        self.assertEqual(app.direct_url(media), media['info']['url'])


if __name__ == '__main__':
    unittest.main()
