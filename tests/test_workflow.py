from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import report_delivery as delivery
from runtime_config import configure_ca
import video_assistant as app


URL = 'https://www.bilibili.com/video/BV1gmJX6SExT?p=1'


class WorkflowTests(unittest.TestCase):
    def arguments(self, **changes):
        values = dict(url=URL, mode='standard', output=None, input_mode='bailian-upload', max_minutes=300,
                      max_audio_mib=1024, wait_seconds=1, reuse_from=None, new_attempt=False)
        return SimpleNamespace(**{**values, **changes})

    def successful_task(self, folder, mode='standard'):
        folder.mkdir(parents=True, exist_ok=True)
        raw = {'transcripts': [{'sentences': [{'text': '完整来源正文', 'begin_time': 1000, 'end_time': 5000}]}]}
        state = {'model': 'qwen-audio-3.1-asr-flash-filetrans', 'workspace': '', 'input_mode': 'bailian-upload',
                 'task_id': 'saved-task', 'source': {'video_url': URL, 'duration_seconds': 4}, 'report_mode': mode}
        app.write_json(folder / 'asr-task.json', {'output': {'task_status': 'SUCCEEDED', 'task_id': 'saved-task'}})
        app.write_json(folder / 'asr-raw.json', raw)
        app.export_sources(folder, state, raw)
        return state

    def html(self, source_id='S00001'):
        return ('<!doctype html><html><head><title>报告</title><style>p{color:#333}</style></head><body>'
            '<main><header><h1>芯片连接方式与带宽瓶颈</h1><p>' + URL + '</p></header>'
            '<section><h2><span class="num">1</span><span class="section-title">关系与条件</span>'
            '<span class="section-time">00:01–00:05</span></h2>'
            f'<p data-source-units="{source_id}">' + '完整解释' * 250 + '</p>'
            '<table><thead><tr><th>对象</th><th>结论</th></tr></thead><tbody>'
            f'<tr><td>甲</td><td><p data-source-units="{source_id}">带条件的结果</p></td></tr></tbody></table>'
            f'<figure data-source-units="{source_id}"><svg><title>甲连接到乙</title><text>约 8 倍（预计）</text></svg>'
            '<figcaption>定性关系示意</figcaption></figure></section>'
            '<details><summary>来源说明</summary><p>原始来源保留。</p></details>'
            '<footer>视频报告·精读</footer></main><script>window.secret="不应进入MD"</script></body></html>')

    def test_cli_defaults_and_manual_direct_url(self):
        for command, handler in [('prepare', 'prepare'), ('upload-check', 'upload_check')]:
            with self.subTest(command=command), patch.object(app.sys, 'argv', ['video_assistant.py', command, 'BV1gmJX6SExT']), \
                 patch.object(app, handler) as called:
                app.main()
                args = called.call_args.args[0]
                self.assertEqual(args.max_minutes, 300)
                self.assertEqual(args.max_audio_mib, 1024)
                if command == 'prepare': self.assertEqual(args.input_mode, 'bailian-upload')
        with patch.object(app.sys, 'argv', ['video_assistant.py', 'prepare', 'BV1gmJX6SExT', '--input-mode', 'direct-url']), \
             patch.object(app, 'prepare') as called:
            app.main()
            self.assertEqual(called.call_args.args[0].input_mode, 'direct-url')

    def test_limits_reject_values_outside_requested_bounds_before_work(self):
        for flag, value in [('--max-minutes', '0'), ('--max-minutes', '301'),
                            ('--max-audio-mib', '0'), ('--max-audio-mib', '1025')]:
            with self.subTest(flag=flag, value=value), \
                 patch.object(app.sys, 'argv', ['video_assistant.py', 'prepare', 'BV1gmJX6SExT', flag, value]), \
                 patch.object(app, 'prepare', side_effect=AssertionError('must reject before work')):
                with self.assertRaises(app.Problem): app.main()
        with patch.object(app.sys, 'argv', ['video_assistant.py', 'prepare', 'BV1gmJX6SExT', '--new-attempt']), \
             patch.object(app, 'prepare', side_effect=AssertionError('must require independent output')):
            with self.assertRaises(app.Problem): app.main()

    def test_300_minute_source_is_accepted_without_old_duration_limit(self):
        info = {'duration': 18000, 'formats': [{'url': 'https://cdn.test/audio', 'vcodec': 'none', 'acodec': 'mp4a.40.2'}]}
        result = SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr='')
        with patch.object(app.subprocess, 'run', return_value=result):
            self.assertEqual(app.resolve(URL)[0]['duration_seconds'], 18000)
        info['duration'] = 18001
        result.stdout = json.dumps(info)
        with patch.object(app.subprocess, 'run', return_value=result), self.assertRaises(app.Problem): app.resolve(URL)

    def test_success_is_reused_before_credentials_or_any_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'runs' / 'earlier-success'
            self.successful_task(source)
            before = (source / 'state.json').read_bytes()
            output = io.StringIO()
            with patch.object(app, 'ROOT', root), patch.object(app, 'credentials', side_effect=AssertionError('no credentials')), \
                 patch.object(app, 'resolve', side_effect=AssertionError('no network')), redirect_stdout(output):
                app.prepare(self.arguments())
            result = json.loads(output.getvalue())
            self.assertEqual(result['directory'], str(source))
            self.assertTrue(result['reused'])
            self.assertEqual((source / 'state.json').read_bytes(), before)

    def test_changed_reading_mode_reuses_transcript_without_new_asr(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / 'runs' / 'old'
            self.successful_task(original)
            (original / 'report.html').write_text(self.html())
            app.deliver(original, title='原模式内容主题')
            before = (original / 'transcript.md').read_bytes()
            target = root / 'runs' / 'brief'
            with patch.object(app, 'ROOT', root), patch.object(app, 'credentials', side_effect=AssertionError('no ASR')):
                app.prepare(self.arguments(mode='brief', output=target))
            self.assertEqual((target / 'transcript.md').read_bytes(), before)
            self.assertEqual(json.loads((target / 'input.json').read_text())['report_mode'], 'brief')
            self.assertEqual(json.loads((target / 'state.json').read_text())['task_id'], 'saved-task')
            self.assertNotIn('report_files', json.loads((target / 'state.json').read_text()))
            (target / 'report.html').write_text(self.html())
            app.deliver(target)
            self.assertEqual(json.loads((target / 'state.json').read_text())['report_files']['html'],
                             '芯片连接方式与带宽瓶颈_速览.html')

    def test_failed_and_unknown_tasks_do_not_trigger_new_charge_or_fallback(self):
        for status in ('ASR_FAILED', 'SUBMISSION_UNKNOWN'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                old = root / 'runs' / 'old'
                old.mkdir(parents=True)
                app.write_json(old / 'state.json', {'status': status, 'source': {'video_url': URL}})
                before = (old / 'state.json').read_bytes()
                with patch.object(app, 'ROOT', root), patch.object(app, 'credentials', side_effect=AssertionError('no charge')):
                    with self.assertRaises(app.Problem): app.prepare(self.arguments())
                self.assertEqual((old / 'state.json').read_bytes(), before)
                if status == 'SUBMISSION_UNKNOWN':
                    with patch.object(app, 'ROOT', root), patch.object(app, 'credentials', side_effect=AssertionError('no charge')):
                        with self.assertRaises(app.Problem):
                            app.prepare(self.arguments(new_attempt=True, output=root / 'new'))

    def test_corrupted_success_and_wrong_import_are_not_retranscribed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / 'runs' / 'success'
            self.successful_task(old)
            (old / 'transcript.md').write_text('corrupted')
            with patch.object(app, 'ROOT', root), patch.object(app, 'credentials', side_effect=AssertionError('no charge')):
                with self.assertRaises(app.Problem): app.prepare(self.arguments())
                with self.assertRaises(app.Problem): app.prepare(self.arguments(reuse_from=root / 'missing'))
            self.successful_task(root / 'runs' / 'healthy')
            with patch.object(app, 'ROOT', root), patch.object(app, 'credentials', side_effect=AssertionError('no charge')):
                with self.assertRaises(app.Problem): app.prepare(self.arguments(reuse_from=old))

    def test_markdown_preserves_chapters_tables_time_diagram_and_bindings(self):
        markdown = delivery.render_markdown(self.html())
        self.assertIn('## 1. 关系与条件（00:01–00:05）', markdown)
        self.assertIn('| 对象 | 结论 | 来源 |', markdown)
        self.assertIn('带条件的结果', markdown)
        self.assertIn('甲连接到乙', markdown)
        self.assertIn('约 8 倍（预计）', markdown)
        self.assertIn('[来源：S00001]', markdown)
        self.assertNotIn('window.secret', markdown)
        self.assertNotIn('p{color', markdown)
        nested = self.html().replace('完整解释', '<strong data-source-units="S00001">完整解释</strong>', 1)
        self.assertIn('**完整解释** [来源：S00001]', delivery.render_markdown(nested))

    def test_delivery_creates_full_markdown_and_archive_with_valid_sources(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self.successful_task(folder)
            html = self.html()
            (folder / 'report.html').write_text(html)
            transcript_hash = hashlib.sha256((folder / 'transcript.md').read_bytes()).hexdigest()
            app.deliver(folder)
            state = json.loads((folder / 'state.json').read_text())
            files = state['report_files']
            self.assertEqual(files['html'], '芯片连接方式与带宽瓶颈_精读.html')
            self.assertEqual((folder / files['markdown']).read_text(), delivery.render_markdown(html))
            self.assertFalse((folder / 'report.html').exists())
            self.assertFalse((folder / 'report.md').exists())
            app.verify(folder)
            with zipfile.ZipFile(next(folder.glob('*.zip'))) as z:
                self.assertIn(files['markdown'], z.namelist())
                self.assertNotIn('report.html', z.namelist())
                self.assertNotIn('report.md', z.namelist())
                self.assertIn('transcript.md', z.namelist())
                self.assertEqual(z.read(files['markdown']), (folder / files['markdown']).read_bytes())
                self.assertEqual(z.read(files['subtitles']), z.read('transcript.srt'))
                self.assertIsNone(z.testzip())
                restored = folder / 'restored'
                z.extractall(restored)
            app.verify(restored)
            self.assertTrue(app.intact_sources(restored, json.loads((restored / 'state.json').read_text())))
            app.deliver(restored)
            self.assertEqual(hashlib.sha256((folder / 'transcript.md').read_bytes()).hexdigest(), transcript_hash)

    def test_content_names_use_meaningful_titles_and_safe_paths(self):
        self.assertEqual(delivery.content_stem(self.html(), {}), '芯片连接方式与带宽瓶颈_精读')
        self.assertEqual(delivery.content_stem(self.html(), {}, '../../存储:堆叠/键合?\n', 'brief'), '存储-堆叠-键合_速览')
        generic = self.html().replace('芯片连接方式与带宽瓶颈', 'report')
        self.assertEqual(delivery.content_stem(generic, {'video_title': '存储技术的演进'}), '存储技术的演进_精读')
        with self.assertRaises(delivery.DeliveryError): delivery.content_stem(generic, {})
        self.assertLess(len(delivery.content_stem(self.html(), {}, '芯片' * 200).encode()), 200)

    def test_explicit_content_title_is_persisted_for_redelivery(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self.successful_task(folder)
            (folder / 'report.html').write_text(self.html())
            app.deliver(folder, title='存储芯片的堆叠与键合')
            state = json.loads((folder / 'state.json').read_text())
            self.assertEqual(state['report_files']['html'], '存储芯片的堆叠与键合_精读.html')
            app.deliver(folder)
            self.assertEqual(json.loads((folder / 'state.json').read_text())['report_files'], state['report_files'])
            app.deliver(folder, title='芯片连接如何影响带宽')
            self.assertFalse((folder / state['report_files']['html']).exists())
            app.verify(folder)

    def test_missing_stale_or_fabricated_markdown_fails_verification(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self.successful_task(folder)
            (folder / 'report.html').write_text(self.html())
            with self.assertRaises(app.Problem): app.verify(folder)
            for markdown in ('# 只有简短摘要\n[来源：S00001]\n', delivery.render_markdown(self.html()).replace('S00001', 'S99999')):
                (folder / 'report.md').write_text(markdown)
                with self.assertRaises(app.Problem): app.verify(folder)

    def test_ca_defaults_are_system_trust_and_explicit_config_is_preserved(self):
        with patch.dict(os.environ, {}, clear=True):
            ca = configure_ca()
            self.assertEqual(os.environ['SSL_CERT_FILE'], ca)
            self.assertEqual(os.environ['REQUESTS_CA_BUNDLE'], ca)
        with patch.dict(os.environ, {'SSL_CERT_FILE': ca, 'REQUESTS_CA_BUNDLE': '/explicit/bundle.pem'}, clear=True):
            configure_ca()
            self.assertEqual(os.environ['REQUESTS_CA_BUNDLE'], '/explicit/bundle.pem')


if __name__ == '__main__':
    unittest.main()
