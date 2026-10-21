import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

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

    def test_submit_checkpoint_survives_ambiguous_network_failure(self):
        def failure(request):
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
