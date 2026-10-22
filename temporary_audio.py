"""Bounded audio-only download, local validation, and official temporary upload.

This module never submits transcription. URLs and upload credentials stay in memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid

import httpx


class AudioUploadError(Exception):
    def __init__(self, stage, reason, **details):
        self.details = {"stage": stage, "reason": reason, **details}
        super().__init__(json.dumps(self.details, ensure_ascii=False))


def download_audio(client, url, headers, path, max_bytes):
    allowed = {"user-agent", "referer", "origin", "accept", "accept-language", "sec-fetch-mode"}
    request_headers = {k.lower(): v for k, v in headers.items() if k.lower() in allowed and isinstance(v, str)}
    request_headers['accept-encoding'] = 'identity'
    digest, md5, size = hashlib.sha256(), hashlib.md5(), 0
    try:
        with client.stream('GET', url, headers=request_headers) as response:
            if response.status_code != 200:
                raise AudioUploadError('download', 'full audio request rejected', http_status=response.status_code)
            length = response.headers.get('content-length', '')
            if not length.isdecimal() or not 0 < int(length) <= max_bytes:
                raise AudioUploadError('download', 'missing or excessive Content-Length')
            if response.headers.get('content-encoding', 'identity') != 'identity':
                raise AudioUploadError('download', 'unexpected transfer encoding')
            if response.headers.get('content-type', '').split(';')[0].lower() not in {
                    'audio/mp4', 'video/mp4', 'application/octet-stream'}:
                raise AudioUploadError('download', 'unexpected media content type')
            with path.open('xb') as file:
                for chunk in response.iter_raw(chunk_size=65536):
                    size += len(chunk)
                    if size > int(length) or size > max_bytes:
                        raise AudioUploadError('download', 'audio exceeds declared or configured size')
                    file.write(chunk)
                    digest.update(chunk)
                    md5.update(chunk)
            if size != int(length):
                raise AudioUploadError('download', 'truncated audio response')
    except httpx.HTTPError:
        raise AudioUploadError('download', 'network or TLS failure; not retried') from None
    return {'bytes': size, 'sha256': digest.hexdigest(), 'md5': md5.hexdigest(), 'content_length_verified': True}


def validate_audio(path, expected_duration):
    if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):
        raise AudioUploadError('validation', 'ffprobe and ffmpeg are required')
    # Local paths only. These processes never need credentials or network access.
    env = {'PATH': str(Path(shutil.which('ffprobe')).parent) + ':' + str(Path(shutil.which('ffmpeg')).parent)}
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-show_entries', 'stream=codec_type,codec_name,duration,sample_rate,channels:format=duration',
            '-of', 'json', str(path)], capture_output=True, text=True, timeout=60, env=env, check=False)
        data = json.loads(probe.stdout) if probe.returncode == 0 else {}
        streams = data.get('streams', [])
        if len(streams) != 1 or streams[0].get('codec_type') != 'audio' or streams[0].get('codec_name') != 'aac':
            raise AudioUploadError('validation', 'expected exactly one AAC audio stream and no video')
        stream = streams[0]
        duration = float(stream.get('duration') or data.get('format', {}).get('duration', 0))
        if not math.isfinite(duration) or duration <= 0 or abs(duration - expected_duration) > max(2, expected_duration * .01):
            raise AudioUploadError('validation', 'audio duration does not match selected part')
        decode = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode',
            '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:a:0', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=min(600, max(120, duration * 2)), env=env, check=False)
        if decode.returncode or decode.stderr.strip():
            raise AudioUploadError('validation', 'full audio decode failed')
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
        raise AudioUploadError('validation', 'local inspection failed') from None
    return {'codec': 'aac', 'audio_streams': 1, 'video_streams': 0, 'duration_seconds': duration,
            'sample_rate': stream.get('sample_rate'), 'channels': stream.get('channels'), 'full_decode_verified': True}


def get_policy(client, api_base, auth, model):
    try:
        response = client.get(api_base + '/api/v1/uploads',
            headers={**auth, 'Content-Type': 'application/json'}, params={'action': 'getPolicy', 'model': model})
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise AudioUploadError('policy', 'network, TLS, or JSON response failure') from None
    if response.status_code != 200:
        code = data.get('code') if isinstance(data, dict) else None
        safe_code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'unknown'
        raise AudioUploadError('policy', 'upload policy rejected', http_status=response.status_code, provider_code=safe_code)
    policy = data.get('data') if isinstance(data, dict) else None
    required = ('upload_host', 'upload_dir', 'oss_access_key_id', 'signature', 'policy',
                'x_oss_object_acl', 'x_oss_forbid_overwrite')
    if not isinstance(policy, dict) or any(not isinstance(policy.get(k), str) or not policy[k] for k in required):
        raise AudioUploadError('policy', 'missing upload policy fields')
    host = urlsplit(policy['upload_host'])
    if (host.scheme != 'https' or not (host.hostname or '').endswith('.aliyuncs.com')
            or host.username or host.password or host.port not in (None, 443)
            or host.path not in ('', '/') or host.query or host.fragment):
        raise AudioUploadError('policy', 'unexpected official storage host')
    if not re.fullmatch(r'[A-Za-z0-9_/-]+', policy['upload_dir']) or '..' in policy['upload_dir']:
        raise AudioUploadError('policy', 'invalid upload directory')
    return policy


def upload_audio(client, policy, path, md5):
    key = policy['upload_dir'].rstrip('/') + '/' + path.name
    fields = {'OSSAccessKeyId': policy['oss_access_key_id'], 'Signature': policy['signature'],
        'policy': policy['policy'], 'x-oss-object-acl': policy['x_oss_object_acl'],
        'x-oss-forbid-overwrite': policy['x_oss_forbid_overwrite'], 'key': key, 'success_action_status': '200'}
    try:
        with path.open('rb') as file:
            response = client.post(policy['upload_host'], files=[
                *((k, (None, v)) for k, v in fields.items()), ('file', (path.name, file, 'audio/mp4'))])
    except httpx.HTTPError:
        raise AudioUploadError('upload', 'upload result unknown; no automatic retry') from None
    if response.status_code != 200:
        raise AudioUploadError('upload', 'storage rejected upload', http_status=response.status_code)
    etag = response.headers.get('etag', '').strip('"').lower()
    if etag and etag != md5:
        raise AudioUploadError('upload', 'storage checksum mismatch; not retried')
    return 'oss://' + key, {'http_status': 200, 'server_checksum_verified': bool(etag),
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()}


def stage_audio(direct, headers, expected_duration, model, api_base, auth,
                *, max_bytes=256 * 1024 * 1024, event=lambda status, details: None):
    """Upload validation only; return a private URI, delete local audio on all exits."""
    try:
        with tempfile.TemporaryDirectory(prefix='bailian-audio-') as directory:
            path = Path(directory) / (uuid.uuid4().hex + '.m4a')
            event('DOWNLOADING', {})
            with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False) as media:
                summary = download_audio(media, direct, headers, path, max_bytes)
            summary.update(validate_audio(path, expected_duration))
            event('AUDIO_VERIFIED', {'audio': summary})
            with httpx.Client(timeout=30, follow_redirects=False) as api:
                policy = get_policy(api, api_base, auth, model)
            event('UPLOAD_PENDING', {})
            # Fresh storage client: never send the model API Authorization header here.
            with httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as storage:
                uri, receipt = upload_audio(storage, policy, path, summary['md5'])
            result = {'audio': summary, 'upload': receipt}
            event('UPLOADED', result)
        return uri, result
    finally:
        event('LOCAL_AUDIO_REMOVED', {})
