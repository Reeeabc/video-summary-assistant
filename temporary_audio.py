"""Bounded audio-only download, local validation, and official temporary upload.

This module never submits transcription. URLs and upload credentials stay in memory.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
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
from runtime_config import configure_ca
from media_http import response_evidence


class AudioUploadError(Exception):
    def __init__(self, stage, reason, **details):
        self.details = {"stage": stage, "reason": reason, **details}
        super().__init__(json.dumps(self.details, ensure_ascii=False))


DOWNLOAD_CHUNK_BYTES = 1024 * 1024


def download_audio(client, url, headers, path, max_bytes):
    allowed = {"user-agent", "referer", "origin", "accept", "accept-language", "sec-fetch-mode"}
    request_headers = {k.lower(): v for k, v in headers.items() if k.lower() in allowed and isinstance(v, str)}
    request_headers['accept-encoding'] = 'identity'
    request_headers['cache-control'] = 'no-cache'
    request_headers['pragma'] = 'no-cache'
    # Reusing a proxy/CDN connection can stall later range bodies despite valid 206 headers.
    request_headers['connection'] = 'close'
    digest, md5, size = hashlib.sha256(), hashlib.md5(), 0
    chunk_bytes = DOWNLOAD_CHUNK_BYTES
    network_retries = 0
    total, validator, file, requests = None, None, None, 0
    try:
        while total is None or size < total:
            # Learn size with a small request; use small tail ranges because some CDN/proxy
            # combinations terminate a large EOF response early while the same bytes are readable.
            if total is None:
                span = min(chunk_bytes, 32768)
            elif total - size <= chunk_bytes + 1024 * 1024:
                span = min(chunk_bytes, 16384)
            else:
                span = chunk_bytes
            end = min(size + span, total or max_bytes) - 1
            part_headers = dict(request_headers)
            part_headers['range'] = f'bytes={size}-{end}'
            chunk_retries = 0
            while True:
                checkpoint, saved_digest, saved_md5 = size, digest.copy(), md5.copy()
                response_status = None
                try:
                    with client.stream('GET', url, headers=part_headers) as response:
                        requests += 1
                        response_status = response.status_code
                        if response.status_code not in {200, 206}:
                            raise AudioUploadError('download', 'full audio request rejected', **response_evidence(response))
                        length = response.headers.get('content-length', '')
                        if not re.fullmatch(r'[0-9]{1,15}', length) or not 0 < int(length) <= max_bytes:
                            raise AudioUploadError('download', 'missing or excessive Content-Length')
                        length = int(length)
                        if response.status_code == 206:
                            match = re.fullmatch(r'bytes ([0-9]{1,15})-([0-9]{1,15})/([0-9]{1,15})',
                                                 response.headers.get('content-range', ''))
                            if not match:
                                raise AudioUploadError('download', 'invalid Content-Range')
                            start, last, resource_size = map(int, match.groups())
                            if (not 0 < resource_size <= max_bytes or start != size
                                    or last != min(end, resource_size - 1) or last < start
                                    or length != last - start + 1 or total not in (None, resource_size)):
                                raise AudioUploadError('download', 'inconsistent or incomplete audio range')
                        else:
                            # Servers may ignore the first Range and return the whole resource.
                            # Never append a full response to a partially downloaded file.
                            if size or response.headers.get('content-range'):
                                raise AudioUploadError('download', 'unexpected full response during range download')
                            resource_size = length
                        if response.headers.get('content-encoding', 'identity') != 'identity':
                            raise AudioUploadError('download', 'unexpected transfer encoding')
                        if response.headers.get('content-type', '').split(';')[0].lower() not in {
                                'audio/mp4', 'video/mp4', 'application/octet-stream'}:
                            raise AudioUploadError('download', 'unexpected media content type', **response_evidence(response))
                        etag = response.headers.get('etag', '')
                        if validator and etag != validator:
                            raise AudioUploadError('download', 'audio changed between ranges')
                        if total is None and re.fullmatch(r'"[!-~]{1,200}"', etag):
                            validator = etag  # In memory only; never record arbitrary response header values.
                        total = resource_size
                        if file is None:
                            file = path.open('xb')
                        part_size = 0
                        for chunk in response.iter_raw(chunk_size=65536):
                            part_size += len(chunk)
                            size += len(chunk)
                            if part_size > length or size > total or size > max_bytes:
                                raise AudioUploadError('download', 'audio exceeds declared or configured size')
                            file.write(chunk)
                            digest.update(chunk)
                            md5.update(chunk)
                        if part_size != length:
                            raise AudioUploadError('download', 'truncated audio response')
                    break
                except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError):
                    # Only transport failures may retry the identical range once (three total).
                    # Refusals and logical/integrity failures are AudioUploadError and never retry.
                    if response_status == 200 or chunk_retries >= 1 or network_retries >= 3:
                        raise
                    chunk_retries += 1
                    network_retries += 1
                    size, digest, md5 = checkpoint, saved_digest, saved_md5
                    if file is not None:
                        file.seek(checkpoint)
                        file.truncate(checkpoint)
        if size != total:
            raise AudioUploadError('download', 'incomplete audio resource')
    except httpx.HTTPError as exc:
        known = {'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout', 'ConnectError',
                 'ReadError', 'WriteError', 'RemoteProtocolError', 'LocalProtocolError', 'ProxyError'}
        category = type(exc).__name__ if type(exc).__name__ in known else 'HTTPError'
        raise AudioUploadError('download', 'network or TLS failure; stopped', network_error=category,
                               bytes_received=size, responses_received=requests, network_retries=network_retries) from None
    finally:
        if file is not None:
            file.close()
    return {'bytes': size, 'sha256': digest.hexdigest(), 'md5': md5.hexdigest(), 'content_length_verified': True,
            'complete_file_verified': True, 'download_http_status': response.status_code,
            'download_requests': requests, 'download_network_retries': network_retries, 'range_validator_verified': bool(validator)}


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


def policy_size_bounds(policy):
    try:
        decoded = json.loads(base64.b64decode(policy['policy'], validate=True))
        conditions = decoded['conditions']
        if not isinstance(conditions, list):
            raise ValueError
        bounds = [c for c in conditions if isinstance(c, list) and c and c[0] == 'content-length-range']
        if any(len(c) != 3 or type(c[1]) is not int or type(c[2]) is not int or not 0 <= c[1] <= c[2] for c in bounds):
            raise ValueError
        return (max(c[1] for c in bounds), min(c[2] for c in bounds)) if bounds else (0, None)
    except (ValueError, KeyError, TypeError):
        raise AudioUploadError('policy', 'invalid server size policy; not uploading') from None


def upload_audio(client, policy, path, md5):
    lower, upper = policy_size_bounds(policy)
    size = path.stat().st_size
    if size < lower or (upper is not None and size > upper):
        raise AudioUploadError('upload', 'audio violates server policy size limit; not uploading',
            file_bytes=size, server_min_bytes=lower, server_max_bytes=upper)
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
        code = re.search(r'<Code>([A-Za-z0-9_.-]{1,80})</Code>', response.text[:4096])
        raise AudioUploadError('upload', 'storage rejected upload; not retried', http_status=response.status_code,
                               provider_code=code[1] if code else 'unknown', server_max_bytes=upper)
    etag = response.headers.get('etag', '').strip('"').lower()
    if etag and etag != md5:
        raise AudioUploadError('upload', 'storage checksum mismatch; not retried')
    return 'oss://' + key, {'http_status': 200, 'server_checksum_verified': bool(etag), 'server_max_bytes': upper,
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()}


@contextmanager
def prepared_audio(direct, headers, expected_duration, *, max_bytes=1024 * 1024 * 1024,
                   event=lambda status, details: None):
    """Prepare and validate local audio only. No policy upload or ASR calls."""
    configure_ca()
    try:
        with tempfile.TemporaryDirectory(prefix='bailian-audio-') as directory:
            path = Path(directory) / (uuid.uuid4().hex + '.m4a')
            event('DOWNLOADING', {})
            if isinstance(direct, dict):
                from audio_acquisition import acquire_audio
                summary = acquire_audio(direct, path, expected_duration, max_bytes)
            else:
                with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False) as media:
                    summary = download_audio(media, direct, headers, path, max_bytes)
                summary.update(validate_audio(path, expected_duration))
            event('AUDIO_VERIFIED', {'audio': summary})
            yield path, summary
    finally:
        event('LOCAL_AUDIO_REMOVED', {})


def stage_audio(direct, headers, expected_duration, model, api_base, auth,
                *, max_bytes=1024 * 1024 * 1024, event=lambda status, details: None):
    """Official upload after complete local validation; no transcription here."""
    with prepared_audio(direct, headers, expected_duration, max_bytes=max_bytes, event=event) as (path, summary):
        with httpx.Client(timeout=30, follow_redirects=False) as api:
            policy = get_policy(api, api_base, auth, model)
        event('UPLOAD_PENDING', {})
        with httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as storage:
            uri, receipt = upload_audio(storage, policy, path, summary['md5'])
        result = {'audio': summary, 'upload': receipt}
        event('UPLOADED', result)
    return uri, result
