"""Credential-free media preflight and bounded, non-secret rejection evidence."""
import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx

from runtime_config import configure_ca


ERROR_BODY_LIMIT = 8192
MEDIA_TYPES = {'audio/mp4', 'video/mp4', 'application/octet-stream', 'audio/mpeg',
               'audio/aac', 'audio/x-m4a', 'audio/flac', 'audio/ogg', 'audio/opus',
               'audio/wav', 'audio/x-wav', 'audio/webm', 'video/webm'}


class MediaAccessError(Exception):
    def __init__(self, details):
        self.details = details
        super().__init__(json.dumps(details, ensure_ascii=False))


def response_evidence(response):
    """No URLs, raw header values, raw HTML, cookies, or signatures cross this boundary."""
    mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
    result = {'http_status': response.status_code,
              'content_type': mime if mime in MEDIA_TYPES | {'text/html', 'text/plain', 'application/json'} else 'other',
              'classification': 'non_media_response' if response.status_code == 200 else 'http_rejected',
              'error_body_bytes': 0}
    length = response.headers.get('content-length', '')
    if re.fullmatch(r'[0-9]{1,15}', length):
        result['content_length'] = int(length)
    # A request ID is useful to the CDN operator; accept only its bounded trace-ID syntax.
    trace = response.headers.get('eo-log-uuid', '')
    if re.fullmatch(r'[A-Za-z0-9_-]{1,96}', trace):
        result['edgeone_request_id'] = trace
        if response.status_code == 567:
            result['classification'] = 'edgeone_security_block'
    server = response.headers.get('server', '').lower()
    if server in {'envoy', 'nginx', 'tengine', 'edgeone'}:
        result['server'] = server
    # Read only textual, identity-encoded errors. A 206 can contain media: never read it here.
    if mime in {'text/html', 'text/plain', 'application/json'} and response.headers.get('content-encoding', 'identity') == 'identity':
        try:
            body = (response.content[:ERROR_BODY_LIMIT] if response.is_stream_consumed
                    else next(response.iter_raw(chunk_size=ERROR_BODY_LIMIT), b''))
            result['error_body_bytes'] = len(body)
            result['error_body_sha256'] = hashlib.sha256(body).hexdigest()
            text = body.decode('utf-8', errors='replace').lower()
            phrases = {'restricted_access': 'restricted access', 'security_policy': 'security policy of this website',
                       'access_blocked': 'blocked you from further access', 'bilibili_security': 'bilibili security control policy',
                       'captcha': 'captcha', 'proxy_denied': 'proxy access denied'}
            result['body_markers'] = [key for key, phrase in phrases.items() if phrase in text]
        except httpx.HTTPError:
            result['error_body_unavailable'] = True
    return result


def check_public_audio(url, max_bytes):
    """One full GET without site credentials/headers; close a valid response before reading audio.

    This checks the current worker's access, never promises the provider can fetch the URL.
    No Range/HEAD substitution, redirects, alternate hosts, or retries.
    """
    configure_ca()
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise MediaAccessError({'classification': 'unsafe_media_url'})
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=15), follow_redirects=False) as client:
            with client.stream('GET', url, headers={'accept-encoding': 'identity'}) as response:
                mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if response.status_code != 200 or mime not in MEDIA_TYPES:
                    raise MediaAccessError(response_evidence(response))
                length = response.headers.get('content-length', '')
                if not re.fullmatch(r'[0-9]{1,15}', length) or not 0 < int(length) <= max_bytes:
                    raise MediaAccessError({'http_status': 200, 'classification': 'missing_or_excessive_content_length'})
                if response.headers.get('content-encoding', 'identity') != 'identity':
                    raise MediaAccessError({'http_status': 200, 'classification': 'unexpected_content_encoding'})
                return {'status': 'accessible_from_worker', 'http_status': 200, 'content_type': mime,
                        'content_length': int(length), 'media_body_bytes_read': 0,
                        'provider_access_verified': False}
    except httpx.HTTPError:
        raise MediaAccessError({'classification': 'network_or_tls_failure'}) from None
