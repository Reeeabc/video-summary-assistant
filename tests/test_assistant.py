import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx
import yt_dlp

spec = importlib.util.spec_from_file_location("assistant", Path(__file__).parents[1] / "video_assistant.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class AssistantTests(unittest.TestCase):
    def state(self):
        return {"model": "qwen-audio-3.1-asr-flash-filetrans", "workspace": "ws-test",
                "report_mode": "standard", "source": {
                    "video_url": "https://www.bilibili.com/video/BV1gmJX6SExT?p=1"}}

    def test_url_rejects_credentials_foreign_hosts_and_ambiguous_pages(self):
        for value in ["https://www.bilibili.com.evil.test/video/BV1gmJX6SExT",
                      "https://x@www.bilibili.com/video/BV1gmJX6SExT",
                      "https://www.bilibili.com/video/BV1gmJX6SExT?p=1&p=2",
                      "http://www.bilibili.com/video/BV1gmJX6SExT"]:
            with self.subTest(value=value), self.assertRaises(m.Problem):
                m.canonical(value)
        url, identity = m.canonical("https://www.bilibili.com/video/BV1gmJX6SExT?p=2&share=test")
        self.assertEqual(identity, "BV1gmJX6SExT-p2")
        self.assertEqual(url, "https://www.bilibili.com/video/BV1gmJX6SExT?p=2")

    def test_redaction_removes_nested_media_urls_and_auth(self):
        result = m.redact({"file_url": "https://cdn.test/private?token=abc", "children": [
            {"message": "failed https://storage.test/path?sign=abc", "Authorization": "Bearer private"}]})
        text = json.dumps(result)
        self.assertNotIn("token=", text)
        self.assertNotIn("sign=", text)
        self.assertNotIn("Bearer", text)

    def test_probe_uses_system_ca_and_does_not_pass_credentials_to_extractor(self):
        source = {"duration": 474.41, "formats": [{"format_id": "30232",
                  "vcodec": "none", "acodec": "mp4a.40.2",
                  "http_headers": {"User-Agent": "yt-browser", "Referer": "https://www.bilibili.com/", "Cookie": "private"},
                  "url": "https://cdn.test/audio?sign=private"}]}
        def extractor(command, **kwargs):
            self.assertEqual(command[1], str(m.ROOT / "bilibili_extractor.py"))
            opts = yt_dlp.parse_options(command[2:]).ydl_opts
            self.assertIn("no-certifi", opts["compat_opts"])
            self.assertFalse(opts["nocheckcertificate"])
            env = kwargs["env"]
            for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                self.assertEqual(env[name], "/system/ca.pem")
            self.assertEqual(env["HTTPS_PROXY"], "http://proxy:8080")
            self.assertNotIn("DASHSCOPE_API_KEY", env)
            self.assertNotIn("COOKIE", env)
            return subprocess.CompletedProcess(command, 0, json.dumps(source), "")
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/system/ca.pem",
                "REQUESTS_CA_BUNDLE": "/system/ca.pem", "CURL_CA_BUNDLE": "/system/ca.pem",
                "HTTPS_PROXY": "http://proxy:8080", "DASHSCOPE_API_KEY": "test-secret",
                "COOKIE": "test-cookie"}, clear=True), patch.object(m.subprocess, "run", side_effect=extractor):
            headers = {}
            metadata, _ = m.resolve(self.state()["source"]["video_url"], 60, media_headers=headers)
        self.assertEqual(metadata["audio_codec"], "mp4a.40.2")
        self.assertEqual(headers, {"user-agent": "yt-browser", "referer": "https://www.bilibili.com/"})
        self.assertNotIn("http_headers", metadata)
        self.assertNotIn("sign=", json.dumps(metadata))

    def test_probe_failures_are_specific_and_do_not_echo_extractor_secrets(self):
        for diagnostic, expected in [("CERTIFICATE_VERIFY_FAILED", "TLS"),
                                     ("HTTP Error 412", "HTTP 412"),
                                     ("Unable to extract initial state", "初始数据"),
                                     ("unknown error", "视频解析失败")]:
            result = subprocess.CompletedProcess([], 1, "", diagnostic +
                " Cookie: private-cookie Authorization: Bearer test-secret https://cdn.test/?sign=private")
            with self.subTest(diagnostic=diagnostic), patch.object(m.subprocess, "run", return_value=result):
                with self.assertRaises(m.Problem) as caught:
                    m.resolve(self.state()["source"]["video_url"], 60)
                self.assertIn(expected, str(caught.exception))
                for private in ("private-cookie", "test-secret", "sign="):
                    self.assertNotIn(private, str(caught.exception))

    def test_submit_checkpoint_survives_ambiguous_network_failure(self):
        def failure(request):
            self.assertEqual(json.loads((folder / "state.json").read_text())["status"], "SUBMISSION_UNKNOWN")
            self.assertNotIn("X-DashScope-OssResourceResolve", request.headers)
            self.assertEqual(json.loads(request.content)["input"]["file_urls"], ["https://cdn.test/audio?sign=abc"])
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            raise httpx.ReadTimeout("secret URL should never reach user", request=request)
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = self.state()
            with httpx.Client(transport=httpx.MockTransport(failure)) as client:
                with self.assertRaises(m.Problem):
                    m.submit(client, folder, state, "https://cdn.test/audio?sign=abc", {"Authorization": "Bearer test-secret"})
            saved = json.loads((folder / "state.json").read_text())
            self.assertEqual(saved["status"], "SUBMISSION_UNKNOWN")
            self.assertNotIn("sign=", (folder / "state.json").read_text())
            with patch.object(m, "credentials", side_effect=AssertionError("must not contact cloud")):
                with self.assertRaises(m.Problem):
                    m.resume_locked(folder, saved, 1)

    def test_audio_range_reads_only_small_response_and_omits_auth(self):
        class SmallStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'x' * 1024
        def media(request):
            self.assertEqual(request.headers['Range'], 'bytes=0-1023')
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('cookie', request.headers)
            self.assertEqual(request.headers['User-Agent'], 'yt-browser')
            self.assertEqual(request.headers['Referer'], 'https://www.bilibili.com/')
            return httpx.Response(206, headers={'Content-Range': 'bytes 0-1023/99999',
                'Content-Length': '1024', 'Content-Type': 'audio/mp4'}, stream=SmallStream())
        client = httpx.Client(transport=httpx.MockTransport(media))
        with patch.object(m.httpx, 'Client', return_value=client):
            result = m.check_audio_range('https://cdn.test/audio?sign=private', self.state()['source']['video_url'],
                {'User-Agent': 'yt-browser', 'Referer': 'https://www.bilibili.com/',
                 'Cookie': 'private', 'Authorization': 'Bearer private', 'Range': 'bytes=0-'})
        self.assertEqual(result['bytes_read'], 1024)
        self.assertNotIn('sign=', json.dumps(result))

    def test_ignored_or_invalid_range_is_closed_without_reading_body(self):
        class MustNotRead(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError('must close before consuming a full media response')
        for status, headers in [(200, {}), (206, {'Content-Range': 'bytes 0-99999/100000'}),
                (206, {'Content-Range': 'bytes 0-1023/99999', 'Content-Length': '99999'}),
                (206, {'Content-Range': 'bytes 0-1023/99999', 'Content-Type': 'text/html'})]:
            with self.subTest(status=status, headers=headers):
                client = httpx.Client(transport=httpx.MockTransport(lambda r:
                    httpx.Response(status, headers=headers, stream=MustNotRead())))
                with patch.object(m.httpx, 'Client', return_value=client), self.assertRaises(m.Problem):
                    m.check_audio_range('https://cdn.test/audio?sign=private', self.state()['source']['video_url'])

    def test_probe_keeps_safe_metadata_when_range_check_fails(self):
        metadata = {'video_url': self.state()['source']['video_url'], 'duration_seconds': 75,
                    'audio_codec': 'mp4a.40.2'}
        output = io.StringIO()
        with patch.object(m.sys, 'argv', ['video_assistant.py', 'probe', 'BV1gmJX6SExT', '--check-audio-range']), \
             patch.object(m, 'resolve', return_value=(metadata, 'https://cdn.test/audio?sign=private')), \
             patch.object(m, 'check_audio_range', side_effect=m.Problem('HTTP 403')), redirect_stdout(output):
            with self.assertRaises(m.Problem):
                m.main()
        data = json.loads(output.getvalue())
        self.assertEqual(data['audio_codec'], 'mp4a.40.2')
        self.assertEqual(data['audio_range_check']['status'], 'failed')
        self.assertNotIn('sign=', output.getvalue())

    def test_success_preserves_task_id_before_polling(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = self.state()
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                    "output": {"task_id": "original-123"}}))) as client:
                m.submit(client, folder, state, "https://cdn.test/audio", {"Authorization": "Bearer secret"})
            self.assertEqual(json.loads((folder / "state.json").read_text())["task_id"], "original-123")

    def test_parent_success_with_failed_subtask_does_not_complete(self):
        payload = {"output": {"task_status": "SUCCEEDED", "results": [{
            "subtask_status": "FAILED", "code": "InvalidFile.DownloadFailed"}]}}
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = {**self.state(), "task_id": "original-123"}
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
                with self.assertRaises(m.Problem):
                    m.poll(client, folder, state, {"Authorization": "Bearer secret"}, 1)
            self.assertEqual(state["status"], "ASR_FAILED")
            self.assertFalse((folder / "transcript.txt").exists())

    def test_missing_times_preserve_text_without_inventing_subtitles(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = self.state()
            m.export_sources(folder, state, {"transcripts": [{"text": "无时间戳也要保留原文。"}]})
            self.assertEqual((folder / "transcript.txt").read_text(), "无时间戳也要保留原文。")
            self.assertEqual((folder / "transcript.srt").read_text(), "")
            self.assertIsNone(json.loads((folder / "source-units.json").read_text())[0]["start_ms"])

    def test_parent_download_failure_is_preserved_and_cannot_resubmit(self):
        payload = {"output": {"task_status": "FAILED", "code": "FILE_DOWNLOAD_FAILED",
            "message": "failed https://cdn.test/audio?sign=private"}}
        requests = []
        def failed_task(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/api/v1/tasks/original-123")
            return httpx.Response(200, json=payload)
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = {**self.state(), "task_id": "original-123"}
            with httpx.Client(transport=httpx.MockTransport(failed_task)) as client:
                with self.assertRaisesRegex(m.Problem, "FILE_DOWNLOAD_FAILED"):
                    m.poll(client, folder, state, {"Authorization": "Bearer test-secret"}, 1)
            self.assertEqual(len(requests), 1)
            self.assertEqual(state["status"], "ASR_FAILED")
            self.assertEqual(state["provider_code"], "FILE_DOWNLOAD_FAILED")
            self.assertNotIn("sign=", (folder / "asr-task.json").read_text())
            self.assertFalse((folder / "transcript.txt").exists())
            with patch.object(m, "credentials", side_effect=AssertionError("must not contact provider again")):
                with self.assertRaises(m.Problem):
                    m.resume_locked(folder, state, 1)

    def test_success_download_omits_auth_and_persists_only_redacted_urls(self):
        result_url = "https://result.oss-cn-beijing.aliyuncs.com/result?signature=private"
        envelope = {"output": {"task_status": "SUCCEEDED", "results": [{
            "subtask_status": "SUCCEEDED", "transcription_url": result_url}]}}
        def storage_response(request):
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json={"file_url": "https://cdn.test/audio?sign=private",
                "transcripts": [{"sentences": [{"text": "成功的原始句子", "begin_time": 1200, "end_time": 3500}]}]})
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = {**self.state(), "task_id": "original-123"}
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope))) as cloud:
                storage = httpx.Client(transport=httpx.MockTransport(storage_response))
                with patch.object(m.httpx, "Client", return_value=storage):
                    m.poll(cloud, folder, state, {"Authorization": "Bearer secret"}, 1)
            self.assertEqual(state["status"], "TRANSCRIPT_READY")
            self.assertIn("00:00:01,200 --> 00:00:03,500", (folder / "transcript.srt").read_text())
            self.assertNotIn("signature=", (folder / "asr-task.json").read_text())
            self.assertNotIn("sign=", (folder / "asr-raw.json").read_text())

    def test_report_rejects_modified_original_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = self.state()
            m.export_sources(folder, state, {"transcripts": [{"text": "这是原文"}]})
            (folder / "report.html").write_text('<html><title>报告</title><body data-source-units="S00001">'
                    + state["source"]["video_url"] + "解释" * 600 + '</body></html>')
            (folder / "transcript.md").write_text("被改动的原文")
            with self.assertRaises(m.Problem):
                m.verify(folder)

    def test_lock_prevents_concurrent_billing(self):
        with tempfile.TemporaryDirectory() as td:
            with m.locked(Path(td)):
                with self.assertRaises(m.Problem):
                    with m.locked(Path(td)):
                        self.fail("second process acquired the lock")
            self.assertFalse((Path(td) / ".task.lock").exists())

    def test_finished_resume_does_not_access_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = self.state()
            m.export_sources(folder, state, {"transcripts": [{"text": "已经完成"}]})
            with patch.object(m, "credentials", side_effect=AssertionError("no new cloud request")):
                m.resume_locked(folder, state, 1)

    def test_report_rejects_fabricated_ids_and_external_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            state = self.state()
            m.export_sources(folder, state, {"transcripts": [{"text": "这是原文"}]})
            def html(source_id, external=""):
                return ('<!doctype html><html><title>报告</title><body>' + state["source"]["video_url"]
                        + f'<p data-source-units="{source_id}">' + "解释" * 600 + '</p>' + external + '</body></html>')
            for content in [html("S99999"), html("S00001", '<script src="https://evil.test/x.js"></script>')]:
                (folder / "report.html").write_text(content)
                with self.assertRaises(m.Problem):
                    m.verify(folder)
            (folder / "report.html").write_text(html("S00001"))
            m.verify(folder)
            self.assertEqual(json.loads((folder / "state.json").read_text())["status"], "REPORT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
