"""Preserved Bilibili-specific format selection and detail fallback subprocess."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from platforms import Problem, parse_video

ROOT = Path(__file__).resolve().parent


def audio_address(audio):
    """Prefer the exact API-provided Akamai alternative to the range-dependent cosov URL."""
    primary = urlsplit(audio['url'])
    if primary.hostname == 'upos-sz-mirrorcosov.bilivideo.com':
        backups = audio.get('backup_urls')
        for address in backups if isinstance(backups, list) else []:
            if not isinstance(address, str):
                continue
            try:
                candidate = urlsplit(address)
                if (candidate.scheme == 'https' and not candidate.username and not candidate.password
                        and candidate.port in (None, 443) and not candidate.fragment
                        and (candidate.hostname or '').endswith('.akamaized.net')
                        and candidate.path == primary.path):
                    return address, 'provided_backup'
            except ValueError:
                continue
    return audio['url'], 'primary'


def resolve_bilibili(url, max_minutes=300, *, media_headers=None, audio_format=None):
    if audio_format not in (None, '30216', '30232'):
        raise Problem('B站音轨选项无效；未请求媒体或提交百炼。')
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
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
        raise Problem('直播或尚未就绪的视频不处理。')
    duration = info.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not 0 < duration <= max_minutes * 60):
        raise Problem(f"时长缺失或超过本次 {max_minutes} 分钟上限；未提交百炼。")
    audios = [f for f in info.get("formats", []) if f.get("vcodec") == "none"
              and f.get("acodec") == "mp4a.40.2" and f.get("url")]
    if not audios:
        raise Problem("没有找到可用的独立 AAC 音频直链；未下载媒体或提交百炼。")
    if audio_format is not None:
        audio = next((f for f in audios if f.get('format_id') == audio_format), None)
        if audio is None:
            raise Problem('指定的 B站独立 AAC 音轨不可用；不替换音轨、不提交百炼。')
    else:
        audio = next((f for preferred in ('30216', '30232') for f in audios
                      if f.get('format_id') == preferred), audios[0])
    address, selection = audio_address(audio)
    media = urlsplit(address)
    if media.scheme != "https" or media.username or media.password:
        raise Problem("媒体直链不符合 HTTPS 要求；未提交百炼。")
    ref = parse_video(url)
    metadata = {"platform": "bilibili", "video_id": ref.video_id, "canonical_url": ref.url,
                "title": info.get("title", ""), "author": info.get("uploader", ""), "part": ref.part,
                "audio_protocol": audio.get("protocol", "https"), "video_url": url, "video_title": info.get("title", ""),
                "uploader": info.get("uploader", ""), "duration_seconds": duration,
                "audio_format_id": audio.get("format_id"), "audio_codec": audio.get("acodec"),
                "audio_cdn_host": media.hostname, "audio_url_selection": selection,
                "audio": {"format_id": audio.get('format_id'), "codec": audio.get('acodec'),
                          "protocol": audio.get('protocol', 'https'), "container": audio.get('ext', 'm4a'),
                          "segmented": False}}
    if media_headers is not None and isinstance(audio.get("http_headers"), dict):
        # Headers travel in memory, never in source metadata or task checkpoints.
        media_headers.update({k.lower(): v for k, v in audio["http_headers"].items()
            if k.lower() in {"user-agent", "referer", "origin", "accept", "accept-language", "sec-fetch-mode"}
            and isinstance(v, str)})
    return metadata, address
