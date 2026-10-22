"""Bilibili direct audio -> durable Bailian task -> sources for a Codex report.

No report-model API, media downloads, personal browser sessions, or OSS uploads.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parent
MODELS = {"qwen-audio-3.1-asr-flash-filetrans", "qwen-audio-3.0-asr-flash-filetrans",
          "paraformer-v2", "fun-asr"}


class Problem(Exception):
    """A bounded message safe to display without URLs, response bodies, or secrets."""


def redact(value):
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if any(w in str(k).lower() for w in
                ("file_url", "transcription_url", "authorization", "api_key", "access_key", "cookie"))
                else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?:https?|oss)://[^\s\"<>]+", "[redacted URL]", value)
    return value


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def canonical(value):
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        value = "https://www.bilibili.com/video/" + value
    p = urlsplit(value)
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", p.path)
    if (p.scheme != "https" or p.hostname not in {"www.bilibili.com", "bilibili.com"}
            or p.netloc not in {"www.bilibili.com", "bilibili.com"} or p.fragment or not match):
        raise Problem("请提供完整 HTTPS B站 BV 视频链接或 BV 号。")
    pages = parse_qs(p.query, keep_blank_values=True).get("p", ["1"])
    if len(pages) != 1 or not re.fullmatch(r"[1-9][0-9]{0,4}", pages[0]):
        raise Problem("分 P 参数无效。")
    bvid, page = match[1], int(pages[0])
    return f"https://www.bilibili.com/video/{bvid}?p={page}", f"{bvid}-p{page}"


def resolve(url, max_minutes, *, media_headers=None):
    # The extractor subprocess does not receive the ASR credential environment.
    env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    # yt-dlp otherwise prefers certifi and ignores the environment's system CA.
    # This supported option selects system trust; TLS verification stays enabled.
    command = [sys.executable, str(ROOT / "bilibili_extractor.py"), "--ignore-config",
               "--compat-options", "no-certifi", "--no-playlist",
               "--skip-download", "--dump-single-json", "--socket-timeout", "15",
               "--retries", "0", "--extractor-retries", "0", url]
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                timeout=90, check=False)
        if result.returncode:
            # Never expose extractor stderr: it may contain cookies or signed URLs.
            error = result.stderr or ""
            diagnostic = next((line.removeprefix("BILI_DIAGNOSTIC ") for line in error.splitlines()
                               if line.startswith("BILI_DIAGNOSTIC ")), None)
            if diagnostic:
                detail = json.loads(diagnostic)
                # The project adapter emits a bounded, sanitized diagnostic object.
                raise Problem("视频解析失败；未提交百炼。" + json.dumps(detail, ensure_ascii=False))
            if "CERTIFICATE_VERIFY_FAILED" in error:
                raise Problem("视频解析的 TLS 证书校验失败；请检查系统 CA 配置，勿关闭校验。未提交百炼。")
            if "HTTP Error 412" in error:
                raise Problem("视频解析请求返回 HTTP 412；需核实站点或代理响应，不自动重试。未提交百炼。")
            if "Unable to extract initial state" in error:
                raise Problem("视频页面初始数据解析失败；需检查页面响应及 yt-dlp 版本。未提交百炼。")
            raise Problem("视频解析失败；请检查站点响应、网络和 yt-dlp 版本。未提交百炼。")
        info = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise Problem("视频解析失败；未提交百炼。请检查云端网络权限。") from None
    if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"}:
        raise Problem("视频解析失败；未提交百炼。不支持登录受限视频或合集批量处理。")
    duration = info.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not 0 < duration <= max_minutes * 60):
        raise Problem(f"时长缺失或超过本次 {max_minutes} 分钟上限；未提交百炼。")
    audios = [f for f in info.get("formats", []) if f.get("vcodec") == "none"
              and f.get("acodec") == "mp4a.40.2" and f.get("url")]
    if not audios:
        raise Problem("没有找到可用的独立 AAC 音频直链；未下载媒体或提交百炼。")
    audio = next((f for f in audios if f.get("format_id") == "30232"), audios[0])
    media = urlsplit(audio["url"])
    if media.scheme != "https" or media.username or media.password:
        raise Problem("媒体直链不符合 HTTPS 要求；未提交百炼。")
    metadata = {"video_url": url, "video_title": info.get("title", ""),
                "uploader": info.get("uploader", ""), "duration_seconds": duration,
                "audio_format_id": audio.get("format_id"), "audio_codec": audio.get("acodec")}
    if media_headers is not None and isinstance(audio.get("http_headers"), dict):
        # Headers travel in memory, never in source metadata or task checkpoints.
        media_headers.update({k.lower(): v for k, v in audio["http_headers"].items()
            if k.lower() in {"user-agent", "referer", "origin", "accept", "accept-language", "sec-fetch-mode"}
            and isinstance(v, str)})
    return metadata, audio["url"]


def check_audio_range(direct, referer, request_headers=None):
    """Check at most 1 KiB; close before reading if the server ignores Range."""
    try:
        headers = {"referer": referer}
        headers.update({k.lower(): v for k, v in (request_headers or {}).items()
            if k.lower() in {"user-agent", "referer", "origin", "accept", "accept-language", "sec-fetch-mode"}
            and isinstance(v, str)})
        headers.update({"range": "bytes=0-1023", "accept-encoding": "identity"})
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            with client.stream("GET", direct, headers=headers) as response:
                if response.status_code != 206:
                    raise Problem(f"音频 Range 检查返回 HTTP {response.status_code}；未读取响应体，不重试。")
                match = re.fullmatch(r"bytes 0-([0-9]+)/([0-9]+)", response.headers.get("Content-Range", ""))
                if not match or not 0 <= int(match[1]) < 1024 or int(match[2]) <= int(match[1]):
                    raise Problem("音频 Range 响应范围无效；未读取响应体，不重试。")
                expected = int(match[1]) + 1
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) != expected):
                    raise Problem("音频 Range 响应长度无效；未读取响应体，不重试。")
                content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type not in {"audio/mp4", "video/mp4", "application/octet-stream"}:
                    raise Problem("音频 Range 响应不是预期媒体类型；未读取响应体，不重试。")
                data = next(response.iter_raw(chunk_size=1024), b"")
                if len(data) != expected:
                    raise Problem("音频 Range 响应体长度不符；不重试。")
                return {"http_status": 206, "bytes_read": len(data), "content_type": content_type}
    except httpx.HTTPError:
        raise Problem("音频 Range 网络或 TLS 检查失败；未输出直链，不重试。") from None


def base_url(workspace):
    if not workspace:
        return "https://dashscope.aliyuncs.com"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", workspace):
        raise Problem("BAILIAN_WORKSPACE_ID 格式无效。")
    return f"https://{workspace}.cn-beijing.maas.aliyuncs.com"


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".task.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Problem("此任务正在执行或曾异常退出。先确认没有运行中的进程，再移除 .task.lock 后恢复。") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def stamp(ms):
    seconds, millis = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def export_sources(folder, state, raw):
    units = []
    for transcript in raw.get("transcripts", []):
        sentences = transcript.get("sentences") or []
        if not sentences and transcript.get("text", "").strip():
            sentences = [{"text": transcript["text"]}]
        for sentence in sentences:
            text = str(sentence.get("text", "")).strip()
            if not text:
                continue
            start, end = sentence.get("begin_time"), sentence.get("end_time")
            valid = (all(isinstance(v, (int, float)) and not isinstance(v, bool)
                         and math.isfinite(v) for v in (start, end))
                     and 0 <= start < end)
            units.append({"id": f"S{len(units) + 1:05}", "start_ms": start if valid else None,
                          "end_ms": end if valid else None, "text": text})
    if not units:
        raise Problem("百炼结果没有可用正文，保留原始结果供检查。")
    write_json(folder / "source-units.json", units)
    text = "\n".join(u["text"] for u in units)
    (folder / "transcript.txt").write_text(text, encoding="utf-8")
    md, srt = [], []
    for unit in units:
        times = ""
        if unit["start_ms"] is not None:
            times = f" {stamp(unit['start_ms'])} --> {stamp(unit['end_ms'])}"
            srt.append(f"{len(srt) + 1}\n{times.strip()}\n{unit['text']}\n")
        md.append(f"[{unit['id']}]{times}\n{unit['text']}\n")
    (folder / "transcript.md").write_text("\n".join(md), encoding="utf-8")
    (folder / "transcript.srt").write_text("\n".join(srt), encoding="utf-8")
    write_json(folder / "input.json", {**state["source"], "report_mode": state["report_mode"]})
    state.update(status="TRANSCRIPT_READY", source_units=len(units), text_characters=len(text),
                 transcript_sha256=hashlib.sha256(text.encode()).hexdigest(),
                 source_checksums={name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                   for name in ("transcript.md", "transcript.txt", "source-units.json", "input.json")})
    write_json(folder / "state.json", state)


def credentials():
    value = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not value:
        raise Problem("请在云端 Network secrets 配置 DASHSCOPE_API_KEY；不要发到聊天或写入仓库。")
    return {"Authorization": "Bearer " + value}


def provider_code(data):
    value = data.get("code", "")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,80}", value) else "unknown"


def submit(client, folder, state, direct, headers):
    # Reserve before POST, so crashes and ambiguous responses cannot trigger a duplicate bill.
    state.update(status="SUBMISSION_UNKNOWN", submission_started=datetime.now(timezone.utc).isoformat())
    write_json(folder / "state.json", state)
    try:
        response = client.post(base_url(state["workspace"]) + "/api/v1/services/audio/asr/transcription",
                               headers={**headers, "X-DashScope-Async": "enable"},
                               json={"model": state["model"], "input": {"file_urls": [direct]},
                                     "parameters": {"channel_id": [0]}})
        data = response.json()
        task = data.get("output", {}).get("task_id")
    except (httpx.HTTPError, ValueError, AttributeError):
        raise Problem("提交结果未知。先在百炼控制台核实，再用 attach-task 绑定原任务；不会自动重提。") from None
    if task and isinstance(task, str) and re.fullmatch(r"[A-Za-z0-9_-]+", task):
        state.update(task_id=task, status="SUBMITTED")
        write_json(folder / "state.json", state)
        return
    state["http_status"] = response.status_code
    state["provider_code"] = provider_code(data)
    if 400 <= response.status_code < 500:
        state["status"] = "SUBMIT_REJECTED"
    write_json(folder / "state.json", state)
    raise Problem(f"未收到任务 ID（HTTP {response.status_code}，{provider_code(data)}）。检查任务状态；不会自动重提。")


def poll(client, folder, state, headers, wait_seconds):
    task = state["task_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task):
        raise Problem("任务 ID 格式无效。")
    deadline = time.monotonic() + wait_seconds
    previous = None
    while True:
        try:
            response = client.get(base_url(state["workspace"]) + "/api/v1/tasks/" + task, headers=headers)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise Problem("查询失败；任务 ID 已保存，运行 resume 查询原任务。") from None
        if response.status_code != 200 or not isinstance(data, dict):
            raise Problem(f"查询返回 HTTP {response.status_code}；保留原任务，稍后恢复。")
        output = data.get("output", {})
        cloud_status = output.get("task_status", "UNKNOWN")
        write_json(folder / "asr-task.json", redact(data))
        if cloud_status != previous:
            print(json.dumps({"task_id": task, "cloud_status": cloud_status}), flush=True)
            previous = cloud_status
        if cloud_status in {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}:
            results = output.get("results") or []
            result = next((r for r in results if r.get("subtask_status") == "SUCCEEDED"), None)
            if not result or not result.get("transcription_url"):
                state.update(status="ASR_FAILED", provider_code=provider_code(result or (results[0] if results else output)))
                write_json(folder / "state.json", state)
                raise Problem(f"百炼转写失败（{state['provider_code']}）；查看 asr-task.json，不自动重提。")
            url = result["transcription_url"]
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or not (parsed.hostname or "").endswith(".aliyuncs.com")):
                raise Problem("结果地址不属于预期的阿里云 HTTPS 存储；保留任务待核实。")
            try:
                # Fresh client with no Authorization header or auth propagation on redirects.
                with httpx.Client(timeout=90, follow_redirects=False) as storage:
                    download = storage.get(url)
                    download.raise_for_status()
                    raw = download.json()
            except (httpx.HTTPError, ValueError):
                raise Problem("结果下载失败；原任务已保存，resume 会重新查询，不重提。") from None
            write_json(folder / "asr-raw.json", redact(raw))
            state["usage"] = data.get("usage", {})
            export_sources(folder, state, raw)
            return
        if cloud_status not in {"PENDING", "RUNNING"}:
            raise Problem("未知云端状态；保留任务供检查。")
        if time.monotonic() >= deadline:
            raise Problem("本次等待结束，云端任务可能仍在运行；用 resume 继续查询。")
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def prepare(args):
    url, identity = canonical(args.url)
    folder = (args.output or ROOT / "runs" / (identity + "-" + args.mode)).resolve()
    with locked(folder):
        path = folder / "state.json"
        if path.exists():
            state = json.loads(path.read_text())
            if state["source"]["video_url"] != url or state["report_mode"] != args.mode:
                raise Problem("输出目录属于其他视频或模式，请使用独立目录。")
            return resume_locked(folder, state, args.wait_seconds)
        headers = credentials()
        model = os.environ.get("ASR_MODEL", "qwen-audio-3.1-asr-flash-filetrans")
        if model not in MODELS:
            raise Problem("ASR_MODEL 尚未适配。")
        workspace = os.environ.get("BAILIAN_WORKSPACE_ID", "").strip()
        base_url(workspace)
        source, direct = resolve(url, args.max_minutes)
        state = {"source": source, "report_mode": args.mode, "workspace": workspace, "model": model}
        with httpx.Client(timeout=httpx.Timeout(90, connect=15)) as client:
            submit(client, folder, state, direct, headers)
            poll(client, folder, state, headers, args.wait_seconds)
    print(json.dumps({"status": "TRANSCRIPT_READY", "directory": str(folder)}, ensure_ascii=False))


def resume_locked(folder, state, wait_seconds):
    if state.get("status") in {"TRANSCRIPT_READY", "REPORT_VERIFIED"}:
        if not all((folder / name).is_file() for name in ("transcript.md", "source-units.json", "input.json")):
            export_sources(folder, state, json.loads((folder / "asr-raw.json").read_text()))
        print(json.dumps({"status": state["status"], "directory": str(folder)}, ensure_ascii=False))
        return
    if (folder / "asr-raw.json").exists():
        export_sources(folder, state, json.loads((folder / "asr-raw.json").read_text()))
        return
    if not state.get("task_id"):
        raise Problem("没有原任务 ID；先核实百炼控制台，再用 attach-task 恢复。不会自动重提。")
    if state.get("status") == "ASR_FAILED":
        raise Problem("原转写任务已失败。查看任务证据；另建计费任务需要用户明确授权。")
    with httpx.Client(timeout=httpx.Timeout(90, connect=15)) as client:
        poll(client, folder, state, credentials(), wait_seconds)


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.source_ids, self.external, self.title, self.body = set(), [], False, False
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.title |= tag == "title"
        self.body |= tag == "body"
        self.source_ids.update(filter(None, re.split(r"[,;\s]+", attrs.get("data-source-units", ""))))
        if tag in {"script", "img", "iframe", "link", "video", "audio", "source"}:
            address = attrs.get("src") or attrs.get("href")
            if address and not address.startswith("data:"):
                self.external.append(tag)


def verify(folder):
    state = json.loads((folder / "state.json").read_text())
    if state.get("status") not in {"TRANSCRIPT_READY", "REPORT_VERIFIED"}:
        raise Problem("转写尚未完成。")
    html = (folder / "report.html").read_text(encoding="utf-8")
    parsed = ReportParser()
    parsed.feed(html)
    units = json.loads((folder / "source-units.json").read_text())
    known = {u["id"] for u in units}
    actual_text = "\n".join(u["text"] for u in units)
    if hashlib.sha256(actual_text.encode()).hexdigest() != state["transcript_sha256"]:
        raise Problem("来源正文已变化，先恢复原始转写。")
    for name, digest in state.get("source_checksums", {}).items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise Problem("来源文件已变化，先恢复原始转写。")
    if (len(html) < 1000 or not parsed.title or not parsed.body or parsed.external
            or re.search(r"\{\{[^}]+\}\}|@import\b|url\(\s*['\"]?https?://", html)
            or not parsed.source_ids or not parsed.source_ids <= known
            or state["source"]["video_url"] not in html):
        raise Problem("HTML 静态检查失败：检查来源 ID、完整来源链接、占位符及外部资源。")
    check = {"static_check": "passed", "source_binding_ids": len(parsed.source_ids),
             "content_and_visual_review": "Agent must separately review source fidelity and layout",
             "report_sha256": hashlib.sha256(html.encode()).hexdigest()}
    write_json(folder / "report-check.json", check)
    state["status"] = "REPORT_VERIFIED"
    write_json(folder / "state.json", state)
    print(json.dumps(check, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="解析直链、提交一次转写、保存报告来源")
    p.add_argument("url")
    p.add_argument("--mode", choices=["standard", "brief"], default="standard")
    p.add_argument("--output", type=Path)
    p.add_argument("--max-minutes", type=int, default=60)
    p.add_argument("--wait-seconds", type=int, default=900)
    p = commands.add_parser("probe", help="仅解析元数据和音轨，不提交百炼或下载媒体")
    p.add_argument("url")
    p.add_argument("--max-minutes", type=int, default=60)
    p.add_argument("--check-audio-range", action="store_true", help="用 Range 最多读取 1 KiB 验证音轨")
    for name in ("resume", "status", "verify", "attach-task"):
        p = commands.add_parser(name)
        p.add_argument("folder", type=Path)
        if name == "resume":
            p.add_argument("--wait-seconds", type=int, default=900)
        if name == "attach-task":
            p.add_argument("task_id", help="用户从百炼控制台核实后的原任务 ID")
    args = parser.parse_args()
    if getattr(args, "wait_seconds", 1) < 1 or not 1 <= getattr(args, "max_minutes", 1) <= 300:
        raise Problem("等待时长必须为正；单次视频上限支持 1–300 分钟。")
    if args.command == "prepare":
        prepare(args)
    elif args.command == "probe":
        media_headers = {}
        metadata, direct = resolve(canonical(args.url)[0], args.max_minutes, media_headers=media_headers)
        if args.check_audio_range:
            try:
                metadata["audio_range_check"] = check_audio_range(direct, metadata["video_url"], media_headers)
            except Problem:
                print(json.dumps({**metadata, "audio_range_check": {"status": "failed"}}, ensure_ascii=False), flush=True)
                raise
        print(json.dumps(metadata, ensure_ascii=False))
    elif args.command == "status":
        print((args.folder / "state.json").read_text(encoding="utf-8"))
    else:
        with locked(args.folder):
            state = json.loads((args.folder / "state.json").read_text())
            if args.command == "resume":
                resume_locked(args.folder, state, args.wait_seconds)
            elif args.command == "verify":
                verify(args.folder)
            elif args.command == "attach-task":
                if state.get("task_id") or not re.fullmatch(r"[A-Za-z0-9_-]+", args.task_id):
                    raise Problem("已有任务 ID 或新 ID 格式无效，未修改。")
                state.update(task_id=args.task_id, status="SUBMITTED")
                write_json(args.folder / "state.json", state)


if __name__ == "__main__":
    try:
        main()
    except Problem as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("本地等待已停止；云端任务可能继续运行，用 resume 恢复。", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Exception messages/tracebacks may carry signed URLs or HTTP headers.
        print(f"执行未完成（{type(exc).__name__}）；检查任务文件，避免重复提交。", file=sys.stderr)
        sys.exit(1)
