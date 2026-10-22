"""YouTube/Vimeo extraction using their upstream yt-dlp extractors, never Bili rules."""
import json
import math
import re
import subprocess
import sys
from urllib.parse import urlsplit

from platforms import Problem, extractor_env, public_headers
from runtime_config import javascript_runtime

PROTOCOLS = {'https', 'http_dash_segments', 'm3u8_native'}


def safe_failure(error):
    """Classify only known signals; never return the original extractor message."""
    lower = error.lower()
    if 'certificate_verify_failed' in lower:
        return 'TLS 证书校验失败'
    for code in ('401', '403', '404', '410', '412', '429'):
        if re.search(r'\b(?:http error |http status |status code |http |tunnel connection failed: )' + code + r'\b', lower):
            return f'HTTP {code}'
    if any(v in lower for v in ('confirm you’re not a bot', "confirm you're not a bot", 'sign in', 'login required', 'log in', 'logged-in', 'password-protected')):
        return '需要登录、验证或访问授权'
    if any(v in lower for v in ('not available', 'unavailable', 'private video', 'removed')):
        return '视频不可用或受访问限制'
    if 'javascript runtime' in lower or 'challenge solving failed' in lower:
        return 'JavaScript 提取依赖或平台挑战失败'
    return '提取器失败（原始消息已隐藏）'


def bounded_diagnostic(error):
    error = re.sub(r'(?im)^.*\b(?:authorization|cookie)\s*[:=].*$', '[credential hidden]', error)
    sanitized = re.sub(r'https?://[^\s<>"\']+', '[URL hidden]', error)
    sanitized = re.sub(r'(?i)\b(?:cookie|authorization|token|signature|sig|policy|api_key)\s*[:=][^\s,;]+',
                       '[credential hidden]', sanitized)
    sanitized = re.split(r'; please report|Use --cookies|See \[URL hidden\]', sanitized)[0]
    return ' '.join(sanitized.split())[:500]


def select_audio(info):
    candidates = [f for f in info.get('formats', []) if f.get('vcodec') == 'none'
                  and f.get('acodec') not in (None, 'none') and not f.get('has_drm')
                  and f.get('protocol', 'https') in PROTOCOLS and f.get('url')]
    if not candidates:
        raise Problem('没有可用的独立音轨；不下载含画面的视频，不提交百炼。')
    # Prefer a stable single-file AAC source; accept Opus and segmented audio too.
    candidates.sort(key=lambda f: (f.get('protocol', 'https') == 'https',
                                  str(f.get('acodec', '')).startswith(('mp4a', 'aac')),
                                  f.get('abr') or 0), reverse=True)
    return candidates[0]


def normalize_info(ref, info, max_minutes, media_headers=None):
    if (not isinstance(info, dict) or info.get('_type') in {'playlist', 'multi_video', 'url', 'url_transparent'}
            or info.get('entries') is not None):
        raise Problem('仅支持单个视频，不展开合集或频道。')
    if str(info.get('id')) != ref.video_id:
        raise Problem('提取器返回的视频 ID 与输入不符，停止处理。')
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
        raise Problem('直播或尚未就绪的视频不处理。')
    duration = info.get('duration')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= max_minutes * 60:
        raise Problem(f'时长缺失或超过本次 {max_minutes} 分钟上限；未提交百炼。')
    audio = select_audio(info)
    url = urlsplit(audio['url'])
    if url.scheme != 'https' or url.username or url.password:
        raise Problem('音轨不是无登录凭据的 HTTPS 地址，停止处理。')
    headers = public_headers(info.get('http_headers'), audio.get('http_headers'))
    if media_headers is not None:
        media_headers.update(headers)
    metadata = {'platform': ref.platform, 'video_id': ref.video_id, 'canonical_url': ref.url,
                'video_url': ref.url, 'title': str(info.get('title') or ''), 'video_title': str(info.get('title') or ''),
                'author': str(info.get('uploader') or info.get('channel') or ''),
                'uploader': str(info.get('uploader') or info.get('channel') or ''), 'duration_seconds': duration,
                'audio_format_id': audio.get('format_id'), 'audio_codec': audio.get('acodec'),
                'audio_protocol': audio.get('protocol', 'https'),
                'audio': {'format_id': audio.get('format_id'), 'codec': audio.get('acodec'),
                          'protocol': audio.get('protocol', 'https'), 'container': audio.get('ext'),
                          'segmented': bool(audio.get('fragments')) or audio.get('protocol') != 'https'}}
    # This object is private: pass it in memory/stdin only, never write to a task file.
    private = {k: audio[k] for k in ('format_id', 'url', 'ext', 'protocol', 'fragments', 'fragment_base_url',
               'manifest_url', 'acodec', 'vcodec', 'abr', 'asr', 'audio_channels', 'filesize') if k in audio}
    private.update(id=ref.video_id, title='audio', extractor_key=ref.platform.title(), http_headers=headers)
    return metadata, {'kind': 'yt-dlp-audio', 'info': private}


def resolve_generic(ref, max_minutes, *, media_headers=None):
    command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--use-extractors', ref.platform,
               '--compat-options', 'no-certifi', '--no-playlist', '--skip-download', '--dump-single-json',
               '--no-warnings', '--no-cache-dir', '--socket-timeout', '15', '--retries', '0', '--extractor-retries', '0',
               '--fragment-retries', '0', '--js-runtimes', 'deno:' + javascript_runtime(),
               '--no-remote-components', ref.url]
    try:
        result = subprocess.run(command, env=extractor_env(), capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Problem(f'{ref.platform} 解析超时或运行依赖缺失；未重试、未提交百炼。') from None
    if result.returncode:
        raise Problem(f'{ref.platform} 解析失败：{safe_failure(result.stderr)}；停止，不重试。'
                      + bounded_diagnostic(result.stderr))
    try:
        info = json.loads(result.stdout)
    except ValueError:
        raise Problem(f'{ref.platform} 提取器未返回有效单视频数据，停止处理。') from None
    return normalize_info(ref, info, max_minutes, media_headers)
