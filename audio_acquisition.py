"""Download a selected audio-only format and normalize it locally to validated AAC."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

from platforms import extractor_env

ROOT = Path(__file__).resolve().parent


def normalize_audio(source, output, expected_duration, max_bytes):
    from temporary_audio import AudioUploadError, validate_audio
    env = {'PATH': extractor_env().get('PATH', '')}
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-show_entries', 'stream=codec_type,codec_name,duration:format=duration', '-of', 'json', str(source)],
            capture_output=True, text=True, timeout=60, env=env, check=False)
        data = json.loads(probe.stdout) if probe.returncode == 0 else {}
        streams = data.get('streams', [])
        if len(streams) != 1 or streams[0].get('codec_type') != 'audio':
            raise AudioUploadError('normalization', 'expected one independent audio stream; no video accepted')
        duration = float(streams[0].get('duration') or data.get('format', {}).get('duration', 0))
        if not math.isfinite(duration) or abs(duration - expected_duration) > max(2, expected_duration * .01):
            raise AudioUploadError('normalization', 'source audio duration mismatch; possible missing fragments')
        codec = ['-c:a', 'copy'] if streams[0].get('codec_name') == 'aac' else ['-c:a', 'aac', '-b:a', '128k']
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode',
            '-protocol_whitelist', 'file,pipe', '-i', str(source), '-map', '0:a:0', '-vn', *codec,
            '-movflags', '+faststart', '-fs', str(max_bytes + 1), str(output)],
            capture_output=True, text=True, timeout=min(3600, max(180, duration * 2)), env=env, check=False)
        if result.returncode or result.stderr.strip() or not output.is_file() or not 0 < output.stat().st_size <= max_bytes:
            raise AudioUploadError('normalization', 'M4A conversion failed or exceeds size limit')
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        raise AudioUploadError('normalization', 'local audio normalization failed') from None
    summary = validate_audio(output, expected_duration)
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with output.open('rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            sha.update(chunk)
            md5.update(chunk)
    return {**summary, 'bytes': output.stat().st_size, 'sha256': sha.hexdigest(), 'md5': md5.hexdigest(),
            'normalized_to_m4a': True, 'converted_from_codec': streams[0].get('codec_name')}


def acquire_audio(media, path, expected_duration, max_bytes):
    from temporary_audio import AudioUploadError
    source = path.with_name('source.media')
    payload = {'info': media['info'], 'destination': str(source), 'max_bytes': max_bytes}
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'media_download.py')], input=json.dumps(payload),
            capture_output=True, text=True, env=extractor_env(), timeout=min(3600, max(180, expected_duration * 2)), check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise AudioUploadError('download', 'audio worker failed or timed out; not retried') from None
    if result.returncode:
        # Worker diagnostics contain categories only, never raw yt-dlp output.
        try:
            details = json.loads(result.stderr)
            reason = details['reason']
            if reason not in {'HTTP 401', 'HTTP 403', 'HTTP 404', 'HTTP 410', 'HTTP 412', 'HTTP 429',
                              '需要登录、验证或访问授权', '视频不可用或受访问限制', 'TLS 证书校验失败'}:
                reason = 'independent audio download failed, fragment missing or size exceeded'
        except (ValueError, KeyError, TypeError):
            reason = 'independent audio download failed'
        raise AudioUploadError('download', reason + '; not retried')
    if not source.is_file() or not 0 < source.stat().st_size <= max_bytes:
        raise AudioUploadError('download', 'missing or excessive audio output')
    summary = normalize_audio(source, path, expected_duration, max_bytes)
    summary.update(source_bytes=source.stat().st_size, complete_download=True)
    source.unlink()
    return summary
