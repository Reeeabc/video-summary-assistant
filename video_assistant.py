"""Public single-video audio -> durable Bailian task -> sources for a Codex report.

No report-model API or personal browser sessions. Official temporary upload by default.
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
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx
from platforms import Problem, parse_video
from report_delivery import DeliveryError, content_stem, evidence_files, markdown_source_ids, render_markdown, report_paths, write_archive
from captions import DEFAULT_LANGUAGES, fetch_captions
from runtime_config import configure_ca

ROOT = Path(__file__).resolve().parent
DEFAULT_MAX_MINUTES = 300
DEFAULT_AUDIO_MIB = 1024
MODELS = {"qwen-audio-3.1-asr-flash-filetrans", "qwen-audio-3.0-asr-flash-filetrans",
          "paraformer-v2", "fun-asr"}


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
    ref = parse_video(value)
    return ref.url, ref.identity


def resolve(url, max_minutes=DEFAULT_MAX_MINUTES, *, media_headers=None):
    ref = parse_video(url)
    if ref.platform == 'bilibili':
        from bilibili_media import resolve_bilibili
        return resolve_bilibili(ref.url, max_minutes, media_headers=media_headers)
    from generic_media import resolve_generic
    return resolve_generic(ref, max_minutes, media_headers=media_headers)


def check_audio_range(direct, referer, request_headers=None):
    """Check at most 1 KiB; close before reading if the server ignores Range."""
    direct = direct_url(direct)
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
                if content_type not in {"audio/mp4", "video/mp4", "application/octet-stream",
                                        "audio/webm", "video/webm", "audio/mpeg", "audio/ogg"}:
                    raise Problem("音频 Range 响应不是预期媒体类型；未读取响应体，不重试。")
                data = next(response.iter_raw(chunk_size=1024), b"")
                if len(data) != expected:
                    raise Problem("音频 Range 响应体长度不符；不重试。")
                return {"http_status": 206, "bytes_read": len(data), "content_type": content_type}
    except httpx.HTTPError:
        raise Problem("音频 Range 网络或 TLS 检查失败；未输出直链，不重试。") from None


def direct_url(media):
    if isinstance(media, str):
        return media
    info = media['info']
    if info.get('protocol', 'https') != 'https' or info.get('fragments'):
        raise Problem('分段音轨不能直接作为单文件地址；请用默认上传模式或 audio-check。')
    return info['url']


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
        raise Problem("来源没有可用正文，保留原始结果供检查。")
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
    write_json(folder / "input.json", {**state["source"], "report_mode": state["report_mode"],
                                      "source_kind": state.get('source_kind', 'asr')})
    state.update(status="TRANSCRIPT_READY", source_units=len(units), text_characters=len(text),
                 transcript_sha256=hashlib.sha256(text.encode()).hexdigest(),
                 source_checksums={name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                   for name in ("transcript.md", "transcript.txt", "source-units.json", "input.json")})
    if state.get('source_kind') == 'platform_subtitles':
        state['source_checksums'].update({n: hashlib.sha256((folder / n).read_bytes()).hexdigest()
                                         for n in evidence_files(state)})
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
    submission_headers = {**headers, "X-DashScope-Async": "enable"}
    if state.get("input_mode", "direct-url") == "bailian-upload":
        if not direct.startswith("oss://"):
            raise Problem("临时上传模式缺少有效存储地址；未提交转写。")
        submission_headers["X-DashScope-OssResourceResolve"] = "enable"
    # Reserve before POST, so crashes and ambiguous responses cannot trigger a duplicate bill.
    state.update(status="SUBMISSION_UNKNOWN", submission_started=datetime.now(timezone.utc).isoformat())
    write_json(folder / "state.json", state)
    try:
        response = client.post(base_url(state["workspace"]) + "/api/v1/services/audio/asr/transcription",
                               headers=submission_headers,
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


def stage_for_bailian(source, direct, media_headers, model, workspace, auth, state, path, max_audio_mib):
    from temporary_audio import AudioUploadError, stage_audio
    def event(status, details):
        if status == "LOCAL_AUDIO_REMOVED":
            state["local_audio_removed"] = True
        else:
            state["upload_status"] = status
            state.update(details)
        write_json(path, state)
    try:
        uri, summary = stage_audio(direct, media_headers, source["duration_seconds"], model,
            base_url(workspace), auth, max_bytes=max_audio_mib * 1024 * 1024, event=event)
    except AudioUploadError as exc:
        state.update(upload_status="UPLOAD_UNKNOWN" if "result unknown" in exc.details["reason"] else "FAILED",
                     upload_error=exc.details)
        write_json(path, state)
        raise Problem("音频上传验证失败；未提交转写。" + str(exc)) from None
    state.update(summary)
    write_json(path, state)
    return uri


def caption_summary(result):
    return {k: v for k, v in result.items() if k != 'raw'}


def caption_raw(raw):
    # Provider metadata is redacted; preserve the actual cue text for exact recovery.
    return {**redact(raw), 'transcripts': raw['transcripts']}


def caption_check(args):
    ref = parse_video(args.url)
    folder = (args.output or ROOT / 'runs' / (ref.identity + '-caption-check')).resolve()
    with locked(folder):
        path = folder / 'caption-check.json'
        if path.exists() or (folder / 'state.json').exists():
            raise Problem('目录已有任务或字幕检查证据；不覆盖、不自动重试。')
        result = fetch_captions(ref, args.max_minutes, tuple(args.subtitle_langs.split(',')))
        summary = {**caption_summary(result), 'video_url': ref.url, 'media_downloaded': False,
                   'upload_submitted': False, 'transcription_submitted': False}
        write_json(path, summary)
        if result['status'] == 'available':
            write_json(folder / 'caption-raw.json', caption_raw(result['raw']))
        print(json.dumps({**summary, 'directory': str(folder)}, ensure_ascii=False))
        if result['status'] in {'blocked', 'restricted'}:
            raise Problem('字幕检查受访问限制或请求被阻断；未进入音频或收费转写：' + result['reason'])


def audio_check(args):
    """No credentials, upload policy, or ASR: local audio acquisition and validation only."""
    from temporary_audio import AudioUploadError, prepared_audio
    ref = parse_video(args.url)
    folder = (args.output or ROOT / 'runs' / (ref.identity + '-audio-check')).resolve()
    with locked(folder):
        path = folder / 'audio-check.json'
        if path.exists() or (folder / 'state.json').exists():
            raise Problem('目录已有检查证据；不覆盖、不自动重试。')
        state = {'status': 'RESOLVING', 'source': {'platform': ref.platform, 'video_id': ref.video_id,
                 'video_url': ref.url}, 'upload_submitted': False, 'transcription_submitted': False}
        write_json(path, state)
        def event(status, details):
            if status == 'LOCAL_AUDIO_REMOVED':
                state['local_audio_removed'] = True
            else:
                state['status'] = status
                state.update(details)
            write_json(path, state)
        try:
            headers = {}
            source, media = resolve(ref.url, args.max_minutes, media_headers=headers)
            state['source'] = source
            with prepared_audio(media, headers, source['duration_seconds'], max_bytes=args.max_audio_mib * 1024 * 1024,
                                event=event):
                pass
        except (Problem, AudioUploadError) as exc:
            state.update(status='FAILED', error=str(exc))
            write_json(path, state)
            raise Problem(str(exc)) from None
        print(json.dumps({**state, 'directory': str(folder)}, ensure_ascii=False))


def upload_check(args):
    """Deliberately separate from prepare: this command cannot submit ASR."""
    url, identity = canonical(args.url)
    folder = (args.output or ROOT / "runs" / (identity + "-upload-check")).resolve()
    with locked(folder):
        path = folder / "upload-state.json"
        if path.exists() or (folder / "state.json").exists():
            raise Problem("目录已有任务或上传记录；不覆盖、不自动重新上传。")
        model = os.environ.get("ASR_MODEL", "qwen-audio-3.1-asr-flash-filetrans")
        if model not in MODELS:
            raise Problem("ASR_MODEL 尚未适配。")
        auth = credentials()
        workspace = os.environ.get("BAILIAN_WORKSPACE_ID", "").strip()
        base_url(workspace)
        media_headers = {}
        source, direct = resolve(url, args.max_minutes, media_headers=media_headers)
        state = {"source": source, "model": model, "workspace": workspace,
                 "input_mode": "bailian-upload", "transcription_submitted": False}
        write_json(path, state)
        stage_for_bailian(source, direct, media_headers, model, workspace, auth, state, path, args.max_audio_mib)
        print(json.dumps({"upload_status": state["upload_status"], "audio": state["audio"],
            "upload": state["upload"], "local_audio_removed": state["local_audio_removed"],
            "transcription_submitted": False, "directory": str(folder)}, ensure_ascii=False))


def existing_tasks(url, reuse_from=None):
    paths = set((ROOT / 'runs').rglob('state.json'))
    if reuse_from is not None:
        supplied = reuse_from.resolve() / 'state.json'
        if not supplied.is_file():
            raise Problem('指定复用目录缺少 state.json；未提交转写。')
        paths.add(supplied)
    result = []
    for path in sorted(paths):
        if path.is_symlink():
            continue
        try:
            state = json.loads(path.read_text())
            if state.get('source', {}).get('video_url') == url:
                result.append((path.parent, state))
        except (OSError, ValueError, AttributeError):
            if reuse_from is not None and path.parent == reuse_from.resolve():
                raise Problem('指定复用目录的状态无效；未提交转写。') from None
    if reuse_from is not None and not any(p == reuse_from.resolve() and s.get('status') in {
            'TRANSCRIPT_READY', 'REPORT_VERIFIED'} for p, s in result):
        raise Problem('指定复用目录不属于该视频的成功任务；未提交转写。')
    return result


def intact_sources(folder, state):
    names = ('transcript.txt', 'transcript.md', 'transcript.srt', 'source-units.json', 'input.json',
             *evidence_files(state))
    try:
        if not all((folder / n).is_file() and not (folder / n).is_symlink() for n in names):
            return False
        units = json.loads((folder / 'source-units.json').read_text())
        digest = hashlib.sha256('\n'.join(u['text'] for u in units).encode()).hexdigest()
        return (digest == state['transcript_sha256'] and all(
            hashlib.sha256((folder / n).read_bytes()).hexdigest() == value
            for n, value in state.get('source_checksums', {}).items()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def reuse_success(tasks, folder, args):
    successes = [(p, s) for p, s in tasks if s.get('status') in {'TRANSCRIPT_READY', 'REPORT_VERIFIED'}]
    strategy = getattr(args, 'source_strategy', 'auto')
    if strategy == 'subtitles-only':
        successes = [(p, s) for p, s in successes if s.get('source_kind') == 'platform_subtitles']
    elif strategy == 'audio-only':
        successes = [(p, s) for p, s in successes if s.get('source_kind') != 'platform_subtitles']
    valid = [(p, s) for p, s in successes if intact_sources(p, s)]
    if successes and not valid:
        raise Problem('已有成功任务的来源文件缺失或校验失败；请恢复资料，不自动重新计费。')
    supplied = getattr(args, 'reuse_from', None)
    if supplied is not None:
        valid = [(p, s) for p, s in valid if p == supplied.resolve()]
        if not valid:
            raise Problem('指定复用任务的来源校验失败；不自动重新计费。')
    valid.sort(key=lambda item: item[1].get('report_mode') != args.mode)
    if not valid:
        return False
    source_folder, state = valid[0]
    if state.get('report_mode') == args.mode:
        print(json.dumps({'status': state['status'], 'directory': str(source_folder), 'reused': True}, ensure_ascii=False))
        return True
    # ASR is independent of reading mode. Create new report sources, never a new task.
    state = {**state, 'status': 'TRANSCRIPT_READY', 'report_mode': args.mode, 'reused_from': str(source_folder)}
    state.pop('report_files', None)
    state.pop('delivery_title', None)
    for name in (*evidence_files(state), 'transcript.txt', 'transcript.md',
                 'transcript.srt', 'source-units.json', 'input.json'):
        shutil.copyfile(source_folder / name, folder / name)
    input_data = json.loads((folder / 'input.json').read_text())
    input_data['report_mode'] = args.mode
    write_json(folder / 'input.json', input_data)
    state['source_checksums'] = {n: hashlib.sha256((folder / n).read_bytes()).hexdigest()
                                for n in state['source_checksums']}
    write_json(folder / 'state.json', state)
    print(json.dumps({'status': 'TRANSCRIPT_READY', 'directory': str(folder), 'reused': True}, ensure_ascii=False))
    return True


def prepare(args):
    url, identity = canonical(args.url)
    strategy = getattr(args, 'source_strategy', 'auto')
    suffix = '-subtitles' if strategy == 'subtitles-only' else ("-bailian-upload" if args.input_mode == "bailian-upload" else "")
    folder = (args.output or ROOT / "runs" / (identity + "-" + args.mode + suffix)).resolve()
    with locked(folder):
        path = folder / "state.json"
        if path.exists():
            state = json.loads(path.read_text())
            if state["source"]["video_url"] != url or state["report_mode"] != args.mode:
                raise Problem("输出目录属于其他视频或模式，请使用独立目录。")
            caption_source = state.get('source_kind') == 'platform_subtitles'
            if (strategy == 'subtitles-only' and not caption_source) or (strategy == 'audio-only' and caption_source):
                raise Problem('输出目录已有不同来源类型的任务；保留原任务，请使用独立 --output。')
            if state.get('status') in {'TRANSCRIPT_READY', 'REPORT_VERIFIED'} and not intact_sources(folder, state):
                raise Problem('已有成功任务的来源校验失败；不自动重新计费。')
            return resume_locked(folder, state, args.wait_seconds)
        tasks = existing_tasks(url, getattr(args, 'reuse_from', None))
        if reuse_success(tasks, folder, args):
            return
        for task_folder, old in tasks:
            if old.get('status') in {'TRANSCRIPT_READY', 'REPORT_VERIFIED'}:
                continue
            if old.get('status') == 'SUBMISSION_UNKNOWN':
                raise Problem(f'已有提交结果未知的任务：{task_folder}；先核实并绑定原任务，不得自动重提。')
            if old.get('task_id') and old.get('status') not in {'ASR_FAILED', 'SUBMIT_REJECTED'}:
                raise Problem(f'已有任务待查询：{task_folder}；请用 resume，不另建任务。')
            if (old.get('status') in {'ASR_FAILED', 'SUBMIT_REJECTED', 'AUDIO_STAGING'}
                    and not getattr(args, 'new_attempt', False) and strategy != 'subtitles-only'):
                raise Problem('已有失败或中断任务；须明确授权后用独立 --output 和 --new-attempt，不自动另建收费任务。')
        if strategy != 'audio-only':
            result = fetch_captions(parse_video(url), args.max_minutes,
                                    tuple(getattr(args, 'subtitle_langs', ','.join(DEFAULT_LANGUAGES)).split(',')))
            write_json(folder / 'caption-check.json', {**caption_summary(result), 'transcription_submitted': False})
            if result['status'] == 'available':
                state = {'source': result['source'], 'report_mode': args.mode,
                         'source_kind': 'platform_subtitles', 'source_strategy': strategy,
                         'status': 'SOURCE_EXPORT_PENDING'}
                write_json(folder / 'caption-raw.json', caption_raw(result['raw']))
                write_json(path, state)
                export_sources(folder, state, result['raw'])
                print(json.dumps({'status': 'TRANSCRIPT_READY', 'source_kind': 'platform_subtitles',
                                  'directory': str(folder), 'transcription_submitted': False}, ensure_ascii=False))
                return
            login_only = (result['status'] == 'restricted'
                          and result.get('reason') == 'subtitle_login_required')
            if result['status'] == 'blocked' or (result['status'] == 'restricted' and not login_only):
                raise Problem('字幕受访问限制或请求被阻断，未自动进入收费转写：' + result['reason'])
            if strategy == 'subtitles-only':
                raise Problem('当前无法取得可用平台字幕；subtitles-only 禁止音频下载和收费转写。')
        headers = credentials()
        model = os.environ.get("ASR_MODEL", "qwen-audio-3.1-asr-flash-filetrans")
        if model not in MODELS:
            raise Problem("ASR_MODEL 尚未适配。")
        workspace = os.environ.get("BAILIAN_WORKSPACE_ID", "").strip()
        base_url(workspace)
        media_headers = {}
        source, direct = resolve(url, args.max_minutes, media_headers=media_headers)
        if args.input_mode == 'direct-url':
            direct = direct_url(direct)
        state = {"source": source, "report_mode": args.mode, "workspace": workspace, "model": model,
                 "input_mode": args.input_mode, "source_kind": "asr", "source_strategy": strategy}
        if args.input_mode == "bailian-upload":
            state["status"] = "AUDIO_STAGING"
            write_json(path, state)
            direct = stage_for_bailian(source, direct, media_headers, model, workspace, headers,
                                      state, path, args.max_audio_mib)
        with httpx.Client(timeout=httpx.Timeout(90, connect=15)) as client:
            submit(client, folder, state, direct, headers)
            poll(client, folder, state, headers, args.wait_seconds)
    print(json.dumps({"status": "TRANSCRIPT_READY", "directory": str(folder)}, ensure_ascii=False))


def resume_locked(folder, state, wait_seconds):
    if state.get("status") in {"TRANSCRIPT_READY", "REPORT_VERIFIED"}:
        if not all((folder / name).is_file() for name in ("transcript.md", "transcript.txt", "transcript.srt", "source-units.json", "input.json")):
            raw_name = 'caption-raw.json' if state.get('source_kind') == 'platform_subtitles' else 'asr-raw.json'
            export_sources(folder, state, json.loads((folder / raw_name).read_text()))
        print(json.dumps({"status": state["status"], "directory": str(folder)}, ensure_ascii=False))
        return
    if state.get('source_kind') == 'platform_subtitles':
        raw_path = folder / 'caption-raw.json'
        if not raw_path.is_file():
            raise Problem('平台字幕原始资料缺失；请恢复资料，不自动转为收费 ASR。')
        export_sources(folder, state, json.loads(raw_path.read_text()))
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
    try:
        html_path, markdown_path = report_paths(folder, state)
    except DeliveryError as exc:
        raise Problem(str(exc)) from None
    html = html_path.read_text(encoding="utf-8")
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
    if not markdown_path.is_file():
        raise Problem('缺少完整 Markdown 阅读报告；运行 deliver 生成并打包。')
    markdown = markdown_path.read_text(encoding='utf-8')
    try:
        expected = render_markdown(html)
    except DeliveryError as exc:
        raise Problem(str(exc)) from None
    md_ids = markdown_source_ids(markdown)
    if markdown != expected or not md_ids or md_ids != parsed.source_ids or not md_ids <= known:
        raise Problem('Markdown 与 HTML 不对应或来源标记无效；重新运行 deliver。')
    check = {"static_check": "passed", "source_binding_ids": len(parsed.source_ids),
             "content_and_visual_review": "Agent must separately review source fidelity and layout",
             "report_sha256": hashlib.sha256(html.encode()).hexdigest(),
             "markdown_report_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
             "markdown_source_binding_ids": len(md_ids), "markdown_matches_html": True}
    write_json(folder / "report-check.json", check)
    state["status"] = "REPORT_VERIFIED"
    write_json(folder / "state.json", state)
    print(json.dumps(check, ensure_ascii=False))


def deliver(folder, title=None):
    try:
        state = json.loads((folder / 'state.json').read_text())
        old_html, old_markdown = report_paths(folder, state)
        html = old_html.read_text(encoding='utf-8')
        chosen_title = title if title is not None else state.get('delivery_title')
        stem = content_stem(html, state['source'], chosen_title, state['report_mode'])
        text_suffix = '_原始字幕.txt' if state.get('source_kind') == 'platform_subtitles' else '_原始转写.txt'
        files = {'html': stem + '.html', 'markdown': stem + '.md',
                 'subtitles': stem + '_字幕.srt', 'text': stem + text_suffix}
        if any((folder / n).is_symlink() for n in files.values()):
            raise DeliveryError('交付文件不能为符号链接。')
        new_html, new_markdown = folder / files['html'], folder / files['markdown']
        if new_html != old_html:
            if new_html.exists() and new_html.read_bytes() != old_html.read_bytes():
                raise DeliveryError('内容标题与已有不同报告重名，请指定其他 --title。')
            shutil.copyfile(old_html, new_html)
        new_markdown.write_text(render_markdown(html), encoding='utf-8')
        state['report_files'] = files
        if chosen_title is not None:
            state['delivery_title'] = chosen_title
        write_json(folder / 'state.json', state)
        verify(folder)
        for old, new in ((old_html, new_html), (old_markdown, new_markdown)):
            if old != new:
                old.unlink(missing_ok=True)
        shutil.copyfile(folder / 'transcript.srt', folder / files['subtitles'])
        shutil.copyfile(folder / 'transcript.txt', folder / files['text'])
        archive = write_archive(folder, stem + '_完整资料.zip', files)
    except DeliveryError as exc:
        raise Problem(str(exc)) from None
    print(json.dumps({'delivery': 'ready', 'archive': str(archive),
        'directory': str(folder), 'primary_files': [files['html'], files['markdown'], files['subtitles'], archive.name],
        'text_file': files['text']}, ensure_ascii=False))


def main():
    configure_ca()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="优先直接获取平台字幕；确认不可用时才准备音频并提交一次转写")
    p.add_argument("url")
    p.add_argument("--mode", choices=["standard", "brief"], default="standard")
    p.add_argument("--output", type=Path)
    p.add_argument("--max-minutes", type=int, default=DEFAULT_MAX_MINUTES)
    p.add_argument("--wait-seconds", type=int, default=900)
    p.add_argument("--input-mode", choices=["direct-url", "bailian-upload"], default="bailian-upload")
    p.add_argument("--max-audio-mib", type=int, default=DEFAULT_AUDIO_MIB)
    p.add_argument('--reuse-from', type=Path, help='优先复用已导入的成功任务目录')
    p.add_argument('--new-attempt', action='store_true', help='仅在明确授权重试后使用；未知提交仍禁止重提')
    p.add_argument('--source-strategy', choices=['auto', 'subtitles-only', 'audio-only'], default='auto',
                   help='默认字幕优先；字幕可用则不下载音频、不调用 ASR')
    p.add_argument('--subtitle-langs', default=','.join(DEFAULT_LANGUAGES), help='字幕语言优先级，逗号分隔；人工字幕优先')
    p = commands.add_parser('caption-check', help='仅检查并获取平台字幕，不下载媒体、不上传、不调用 ASR')
    p.add_argument('url')
    p.add_argument('--output', type=Path)
    p.add_argument('--max-minutes', type=int, default=DEFAULT_MAX_MINUTES)
    p.add_argument('--subtitle-langs', default=','.join(DEFAULT_LANGUAGES))
    p = commands.add_parser('audio-check', help='仅解析、下载独立音频并完整解码验证，不上传、不提交 ASR')
    p.add_argument('url')
    p.add_argument('--output', type=Path)
    p.add_argument('--max-minutes', type=int, default=DEFAULT_MAX_MINUTES)
    p.add_argument('--max-audio-mib', type=int, default=DEFAULT_AUDIO_MIB)
    p = commands.add_parser("upload-check", help="仅下载验证独立音频并上传官方临时存储，不提交转写")
    p.add_argument("url")
    p.add_argument("--output", type=Path)
    p.add_argument("--max-minutes", type=int, default=DEFAULT_MAX_MINUTES)
    p.add_argument("--max-audio-mib", type=int, default=DEFAULT_AUDIO_MIB)
    p = commands.add_parser("probe", help="仅解析元数据和音轨，不提交百炼或下载媒体")
    p.add_argument("url")
    p.add_argument("--max-minutes", type=int, default=DEFAULT_MAX_MINUTES)
    p.add_argument("--check-audio-range", action="store_true", help="用 Range 最多读取 1 KiB 验证音轨")
    for name in ("resume", "status", "verify", "deliver", "attach-task"):
        p = commands.add_parser(name)
        p.add_argument("folder", type=Path)
        if name == "resume":
            p.add_argument("--wait-seconds", type=int, default=900)
        if name == 'deliver':
            p.add_argument('--title', help='根据报告内容拟定的简洁主题标题，用作交付文件名')
        if name == "attach-task":
            p.add_argument("task_id", help="用户从百炼控制台核实后的原任务 ID")
    args = parser.parse_args()
    if getattr(args, "wait_seconds", 1) < 1 or not 1 <= getattr(args, "max_minutes", 1) <= 300:
        raise Problem("等待时长必须为正；单次视频上限支持 1–300 分钟。")
    if not 1 <= getattr(args, "max_audio_mib", 1) <= 1024:
        raise Problem("临时音频大小上限须为 1–1024 MiB。")
    if getattr(args, 'new_attempt', False) and args.output is None:
        raise Problem('新的收费尝试必须显式指定独立 --output，保留旧任务证据。')
    if args.command == "prepare":
        prepare(args)
    elif args.command == 'audio-check':
        audio_check(args)
    elif args.command == 'caption-check':
        caption_check(args)
    elif args.command == "upload-check":
        upload_check(args)
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
            elif args.command == 'deliver':
                deliver(args.folder, args.title)
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
