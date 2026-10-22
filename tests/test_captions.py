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
import zipfile

import httpx
import caption_extractor as extractor
import captions
from platforms import parse_video
import video_assistant as app

URL = 'https://www.youtube.com/watch?v=BaW_jenozKc'
BODY = json.dumps({'events': [{'tStartMs': 1000, 'dDurationMs': 3000,
                              'segs': [{'utf8': '原始字幕与 https://example.com 来源链接。'}]}]})


class CaptionTests(unittest.TestCase):
    def args(self, **changes):
        return SimpleNamespace(**{**dict(url=URL, mode='standard', output=None, input_mode='bailian-upload',
            max_minutes=300, max_audio_mib=1024, wait_seconds=1, source_strategy='auto',
            subtitle_langs='zh-Hans,zh,en', reuse_from=None, new_attempt=False), **changes})

    def result(self):
        source = extractor.public_source(parse_video(URL), '字幕里的信息与来源', '作者', 10)
        source['subtitles'] = {'language': 'zh-Hans', 'kind': 'manual', 'format': 'json3', 'cue_count': 1}
        return {'status': 'available', 'source': source, 'selected': source['subtitles'],
                'raw': captions.parse_captions(BODY, 'json3', 10)}

    def prepare_free(self, root, **changes):
        output = io.StringIO()
        with patch.object(app, 'ROOT', root), patch.object(app, 'fetch_captions', return_value=self.result()), \
             patch.object(app, 'credentials', side_effect=AssertionError('no ASR credential access')), \
             patch.object(app, 'resolve', side_effect=AssertionError('no audio extraction')), \
             patch.object(app, 'submit', side_effect=AssertionError('no paid submission')), redirect_stdout(output):
            app.prepare(self.args(**changes))
        return Path(json.loads(output.getvalue())['directory'])

    def test_human_before_automatic_native_languages_and_no_danmaku_or_translation(self):
        manual = {'en': [{'ext': 'vtt', 'url': 'https://www.youtube.com/captions'}],
                  'danmaku': [{'ext': 'vtt', 'url': 'https://www.youtube.com/danmaku'}]}
        automatic = {'zh-Hans': [{'ext': 'json3', 'url': 'https://www.youtube.com/auto'}]}
        selected = captions.choose_track(manual, automatic)
        self.assertEqual((selected['language'], selected['kind']), ('en', 'manual'))
        translated = {'zh-Hans': [{'ext': 'json3', 'url': 'https://www.youtube.com/captions?tlang=zh-Hans'}]}
        self.assertIsNone(captions.choose_track(translated, {}))
        self.assertEqual(captions.choose_track({}, automatic)['kind'], 'automatic')
        fallback = captions.choose_track({'ja': manual['en']}, {}, ('zh', 'en'))
        self.assertFalse(fallback['language_preference_matched'])

    def test_json3_bili_srt_and_vtt_keep_times_and_source_text(self):
        raw = captions.parse_captions(BODY, 'json3', 10)
        self.assertEqual(raw['transcripts'][0]['sentences'][0]['begin_time'], 1000)
        bili = captions.parse_captions(json.dumps({'body': [{'from': 1.25, 'to': 2.5, 'content': '字幕正文'}]}), 'bilibili-json', 10)
        self.assertEqual(bili['transcripts'][0]['sentences'][0]['begin_time'], 1250)
        for ext, text in [('vtt', 'WEBVTT\n\n00:01.000 --> 00:03.000 align:start\n<b>字幕</b> &amp; 信息\n\n'),
                          ('srt', '1\n00:00:01,000 --> 00:00:03,000\n<b>字幕</b> &amp; 信息\n\n')]:
            sentence = captions.parse_captions(text, ext, 10)['transcripts'][0]['sentences'][0]
            self.assertEqual(sentence, {'text': '字幕 & 信息', 'begin_time': 1000, 'end_time': 3000})
        for body in ('{}', '<html>verification</html>', '{"events":[]}'):
            with self.assertRaises(ValueError): captions.parse_captions(body, 'json3', 10)

    def test_bili_subtitle_lookup_uses_selected_cid_without_playback_or_danmaku(self):
        ref = parse_video('https://www.bilibili.com/video/BV1gmJX6SExT?p=2')
        def detail(ie):
            ie._detail_pages = [{'cid': 10, 'duration': 5}, {'cid': 20, 'duration': 6}]
            return {'videoData': {'title': '分 P 样本', 'owner': {'name': '作者'}}}
        def api(ie, endpoint, video_id, **kw):
            self.assertEqual(endpoint, 'https://api.bilibili.com/x/player/wbi/v2')
            self.assertEqual(kw['query']['cid'], 20)
            return {'code': 0, 'data': {'subtitle': {'subtitles': [
                {'lan': 'ai-zh', 'ai_type': 1, 'subtitle_url': '//aisubtitle.hdslb.com/captions.json'}]}}}
        with patch.object(extractor.ProjectBiliBiliIE, '_detail_state', detail), \
             patch.object(extractor.ProjectBiliBiliIE, '_download_json', api), \
             patch.object(extractor.ProjectBiliBiliIE, '_download_playinfo', side_effect=AssertionError('no playback')):
            source, manual, automatic, _ = extractor.bili_catalog(ref, {'quiet': True})
        self.assertEqual(source['cid'], 20)
        self.assertEqual(source['duration_seconds'], 6)
        self.assertEqual(manual, {})
        self.assertIn('ai-zh', automatic)
        self.assertNotIn('danmaku', manual)

    def test_youtube_catalog_works_without_audio_formats(self):
        info = {'id': 'BaW_jenozKc', 'duration': 10, 'title': '字幕样本', 'subtitles': {}, 'automatic_captions': {}}
        with patch.object(extractor.yt_dlp, 'YoutubeDL') as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = info
            source, _, _, _ = extractor.youtube_catalog(parse_video(URL), {})
            self.assertTrue(ydl.call_args.args[0]['ignore_no_formats_error'])
            self.assertFalse(ydl.call_args.args[0]['nocheckcertificate'] if 'nocheckcertificate' in ydl.call_args.args[0] else False)
            self.assertFalse(ydl.return_value.__enter__.return_value.extract_info.call_args.kwargs['download'])
        self.assertEqual(source['video_id'], 'BaW_jenozKc')

    def test_caption_body_is_bounded_tls_request_without_authorization_or_cookie(self):
        def response(request):
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('cookie', request.headers)
            return httpx.Response(200, text=BODY)
        track = {'url': 'https://www.youtube.com/api/timedtext?signature=private',
                 'http_headers': {'Authorization': 'private', 'Cookie': 'private'}}
        client = httpx.Client(transport=httpx.MockTransport(response))
        with patch.object(extractor.httpx, 'Client', return_value=client):
            self.assertEqual(extractor.caption_body(track, parse_video(URL)), BODY)
        with self.assertRaises(captions.CaptionBlocked):
            extractor.caption_body({'url': 'https://evil.test/captions'}, parse_video(URL))
        client = httpx.Client(transport=httpx.MockTransport(response))
        with patch.object(extractor.httpx, 'Client', return_value=client), patch.object(extractor, 'MAX_BODY', 8):
            with self.assertRaises(captions.CaptionBlocked): extractor.caption_body(track, parse_video(URL))

    def test_catalog_empty_login_and_http_failure_are_distinct(self):
        source = self.result()['source']
        with patch.object(extractor, 'youtube_catalog', return_value=(source, {}, {}, {})):
            self.assertEqual(extractor.collect(parse_video(URL), 300, ('zh',))['status'], 'unavailable')
        with patch.object(extractor, 'bili_catalog', return_value=(source, {}, {}, {'subtitle_login_required': True})):
            result = extractor.collect(parse_video('BV1gmJX6SExT'), 300, ('zh',))
            self.assertEqual(result['reason'], 'subtitle_login_required')
            self.assertEqual(result['status'], 'restricted')
        catalog = {'zh': [{'ext': 'json3', 'url': 'https://www.youtube.com/captions'}]}
        with patch.object(extractor, 'youtube_catalog', return_value=(source, catalog, {}, {})), \
             patch.object(extractor, 'caption_body', side_effect=captions.CaptionBlocked('HTTP 403')):
            with self.assertRaises(captions.CaptionBlocked): extractor.collect(parse_video(URL), 300, ('zh',))
        with patch.object(extractor, 'youtube_catalog', return_value=(source, catalog, {}, {})), \
             patch.object(extractor, 'caption_body', return_value='<html>verification required</html>'):
            with self.assertRaises(captions.CaptionBlocked): extractor.collect(parse_video(URL), 300, ('zh',))

    def test_free_subtitles_create_sources_without_key_audio_upload_or_task_id(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self.prepare_free(Path(td))
            state = json.loads((folder/'state.json').read_text())
            self.assertEqual(state['source_kind'], 'platform_subtitles')
            self.assertNotIn('task_id', state)
            self.assertFalse((folder/'asr-task.json').exists())
            self.assertEqual(json.loads((folder/'input.json').read_text())['source_kind'], 'platform_subtitles')
            self.assertIn('https://example.com', (folder/'transcript.txt').read_text())
            self.assertTrue(app.intact_sources(folder, state))

    def test_subtitles_only_does_not_relabel_existing_asr_as_platform_captions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root/'runs'/'old-asr'
            old.mkdir(parents=True)
            state = {'source': self.result()['source'], 'report_mode': 'standard', 'task_id': 'old-task'}
            app.write_json(old/'asr-task.json', {'output': {'task_status': 'SUCCEEDED'}})
            app.write_json(old/'asr-raw.json', self.result()['raw'])
            app.export_sources(old, state, self.result()['raw'])
            before = (old/'state.json').read_bytes()
            caption_folder = self.prepare_free(root, source_strategy='subtitles-only')
            self.assertNotEqual(caption_folder, old)
            self.assertEqual(json.loads((caption_folder/'state.json').read_text())['source_kind'], 'platform_subtitles')
            self.assertEqual((old/'state.json').read_bytes(), before)

    def test_missing_captions_only_then_enters_existing_audio_backend(self):
        with tempfile.TemporaryDirectory() as td, patch.object(app, 'ROOT', Path(td)), \
             patch.object(app, 'fetch_captions', return_value={'status': 'unavailable', 'reason': 'no_captions'}) as fetched, \
             patch.object(app, 'credentials', return_value={}) as credentials, \
             patch.object(app, 'resolve', return_value=({'video_url': URL, 'duration_seconds': 10}, 'https://cdn.test/audio')), \
             patch.object(app, 'stage_for_bailian', return_value='oss://temporary/audio.m4a') as staged, \
             patch.object(app, 'submit') as submit, patch.object(app, 'poll'), redirect_stdout(io.StringIO()):
            app.prepare(self.args())
            self.assertEqual(fetched.call_count, 1)
            self.assertEqual(credentials.call_count, 1)
            self.assertEqual(staged.call_count, 1)
            self.assertEqual(submit.call_count, 1)

    def test_blocked_and_subtitles_only_never_fall_back_to_paid_audio(self):
        for result, strategy in [({'status': 'blocked', 'reason': 'HTTP 403'}, 'auto'),
                                 ({'status': 'blocked', 'reason': 'subtitle_login_required'}, 'auto'),
                                 ({'status': 'restricted', 'reason': 'other_access_restriction'}, 'auto'),
                                 ({'status': 'restricted', 'reason': 'subtitle_login_required'}, 'subtitles-only'),
                                 ({'status': 'unavailable', 'reason': 'none'}, 'subtitles-only')]:
            with tempfile.TemporaryDirectory() as td, patch.object(app, 'ROOT', Path(td)), \
                 patch.object(app, 'fetch_captions', return_value=result), \
                 patch.object(app, 'credentials', side_effect=AssertionError('no billing')):
                with self.assertRaises(app.Problem): app.prepare(self.args(source_strategy=strategy))

    def test_login_only_caption_restriction_uses_selected_audio_backend(self):
        result = {'status': 'restricted', 'reason': 'subtitle_login_required'}
        url = parse_video('BV1gmJX6SExT').url
        for mode in ('bailian-upload', 'direct-url'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td, \
                 patch.object(app, 'ROOT', Path(td)), patch.object(app, 'fetch_captions', return_value=result), \
                 patch.object(app, 'credentials', return_value={}), \
                 patch.object(app, 'resolve', return_value=({'video_url': url, 'duration_seconds': 10}, 'https://cdn.test/audio')), \
                 patch.object(app, 'stage_for_bailian', return_value='oss://temporary/audio.m4a') as staged, \
                 patch.object(app, 'submit') as submit, patch.object(app, 'poll'), redirect_stdout(io.StringIO()):
                app.prepare(self.args(url=url, input_mode=mode))
                staged.assert_called_once() if mode == 'bailian-upload' else staged.assert_not_called()
                submit.assert_called_once()
                self.assertEqual(submit.call_args.args[3],
                                 'oss://temporary/audio.m4a' if mode == 'bailian-upload' else 'https://cdn.test/audio')
                checks = list(Path(td).rglob('caption-check.json'))
                self.assertEqual(len(checks), 1)
                self.assertEqual(json.loads(checks[0].read_text())['status'], 'restricted')
                self.assertEqual(json.loads(checks[0].read_text())['reason'], 'subtitle_login_required')

    def test_login_caption_fallback_stops_when_public_audio_unavailable(self):
        with tempfile.TemporaryDirectory() as td, patch.object(app, 'ROOT', Path(td)), \
             patch.object(app, 'fetch_captions', return_value={'status': 'restricted', 'reason': 'subtitle_login_required'}), \
             patch.object(app, 'credentials', return_value={}), \
             patch.object(app, 'resolve', side_effect=app.Problem('audio_requires_login')), \
             patch.object(app, 'stage_for_bailian') as staged, patch.object(app, 'submit') as submit:
            with self.assertRaisesRegex(app.Problem, 'audio_requires_login'):
                app.prepare(self.args(url='BV1gmJX6SExT'))
            staged.assert_not_called()
            submit.assert_not_called()

    def test_bili_human_and_ai_tracks_share_platform_catalog_but_keep_provenance(self):
        ref = parse_video('BV1gmJX6SExT')
        def detail(ie):
            ie._detail_pages = [{'cid': 10, 'duration': 10}]
            return {'videoData': {'title': '字幕分类样本', 'owner': {'name': '作者'}}}
        payload = {'code': 0, 'data': {'subtitle': {'subtitles': [
            {'lan': 'zh-CN', 'ai_type': 0, 'subtitle_url': '//example.hdslb.com/human.json'},
            {'lan': 'ai-zh', 'ai_type': 1, 'subtitle_url': '//example.hdslb.com/ai.json'}]}}}
        with patch.object(extractor.ProjectBiliBiliIE, '_detail_state', detail), \
             patch.object(extractor.ProjectBiliBiliIE, '_download_json', return_value=payload):
            _, manual, automatic, _ = extractor.bili_catalog(ref, {'quiet': True})
        self.assertIn('zh-CN', manual)
        self.assertIn('ai-zh', automatic)
        self.assertEqual(captions.choose_track(manual, automatic)['kind'], 'manual')
        self.assertEqual(captions.choose_track({}, automatic)['kind'], 'automatic')

    def test_quality_diagnostics_do_not_treat_silence_as_caption_absence(self):
        source = extractor.public_source(parse_video(URL), '配乐中的一段讲话', '作者', 100)
        catalog = {'zh': [{'ext': 'json3', 'url': 'https://www.youtube.com/captions'}]}
        with patch.object(extractor, 'youtube_catalog', return_value=(source, catalog, {}, {})), \
             patch.object(extractor, 'caption_body', return_value=BODY):
            result = extractor.collect(parse_video(URL), 300, ('zh',))
        self.assertEqual(result['status'], 'available')
        quality = result['selected']['quality']
        self.assertEqual(quality['timing_coverage_ratio'], .03)
        self.assertEqual(quality['largest_gap_seconds'], 96)
        self.assertTrue(quality['content_review_required'])

    def test_quality_measures_union_overlap_and_duplicates_without_rewriting_source(self):
        raw = {'transcripts': [{'sentences': [
            {'begin_time': 0, 'end_time': 4000, 'text': '第一句'},
            {'begin_time': 0, 'end_time': 4000, 'text': '第一句'},
            {'begin_time': 3000, 'end_time': 6000, 'text': '重叠的第二句'},
            {'begin_time': 8000, 'end_time': 9000, 'text': '结尾'}]}]}
        before = json.dumps(raw, ensure_ascii=False)
        quality = captions.caption_quality(raw, 10)
        self.assertEqual(quality['covered_seconds'], 7)
        self.assertEqual(quality['timing_coverage_ratio'], .7)
        self.assertEqual(quality['overlapping_cues'], 2)
        self.assertEqual(quality['exact_duplicate_cues'], 1)
        self.assertEqual(quality['largest_gap_seconds'], 2)
        self.assertEqual(json.dumps(raw, ensure_ascii=False), before)

    def test_caption_checkpoint_resume_and_mode_reuse_do_not_bill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(app, 'export_sources', side_effect=RuntimeError('interrupted')):
                with self.assertRaises(RuntimeError): self.prepare_free(root)
            folder = next((root/'runs').glob('*/state.json')).parent
            state = json.loads((folder/'state.json').read_text())
            self.assertEqual(state['status'], 'SOURCE_EXPORT_PENDING')
            with patch.object(app, 'credentials', side_effect=AssertionError('no billing')):
                app.resume_locked(folder, state, 1)
            self.assertIn('https://example.com', (folder/'transcript.txt').read_text())
            original_srt = (folder/'transcript.srt').read_bytes()
            (folder/'transcript.srt').unlink()
            with patch.object(app, 'credentials', side_effect=AssertionError('no billing')), redirect_stdout(io.StringIO()):
                app.resume_locked(folder, state, 1)
            self.assertEqual((folder/'transcript.srt').read_bytes(), original_srt)
            with patch.object(app, 'ROOT', root), patch.object(app, 'fetch_captions', side_effect=AssertionError('reuse captions')), \
                 patch.object(app, 'credentials', side_effect=AssertionError('no billing')), redirect_stdout(io.StringIO()):
                app.prepare(self.args(mode='brief'))
            states = [json.loads(p.read_text()) for p in (root/'runs').glob('*/state.json')]
            self.assertTrue(any(s['report_mode']=='brief' and s['source_kind']=='platform_subtitles' for s in states))

    def test_caption_report_zip_contains_real_caption_evidence_not_fake_asr(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self.prepare_free(Path(td))
            html = '<html><head><title>字幕报告</title></head><body><h1>字幕里的信息与来源</h1><p>'+URL+'</p><p data-source-units="S00001">'+'根据字幕整理来源。'*150+'</p></body></html>'
            (folder/'report.html').write_text(html)
            with redirect_stdout(io.StringIO()): app.deliver(folder)
            with zipfile.ZipFile(next(folder.glob('*.zip'))) as z:
                self.assertIn('caption-raw.json', z.namelist())
                self.assertIn('caption-check.json', z.namelist())
                self.assertNotIn('asr-task.json', z.namelist())
                self.assertNotIn('asr-raw.json', z.namelist())
                self.assertTrue(any(n.endswith('_原始字幕.txt') for n in z.namelist()))
                self.assertIsNone(z.testzip())
                restored = Path(td)/'restored'
                z.extractall(restored)
            with redirect_stdout(io.StringIO()): app.verify(restored)
            self.assertTrue(app.intact_sources(restored, json.loads((restored/'state.json').read_text())))

    def test_worker_has_only_ca_proxy_environment_and_safe_result(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'private', 'COOKIE': 'private'}):
            def run(command, **kw):
                self.assertNotIn('DASHSCOPE_API_KEY', kw['env'])
                self.assertNotIn('COOKIE', kw['env'])
                return subprocess.CompletedProcess(command, 0, '{"status":"blocked","reason":"HTTP 403"}', 'https://cdn.test/?signature=private')
            with patch.object(captions.subprocess, 'run', side_effect=run):
                result = captions.fetch_captions(parse_video(URL))
            self.assertNotIn('signature', json.dumps(result))
            with patch.object(captions.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                    '{"status":"restricted","reason":"subtitle_login_required"}', '')):
                self.assertEqual(captions.fetch_captions(parse_video(URL))['status'], 'restricted')


if __name__ == '__main__': unittest.main()
