"""Read platform caption catalogs/body without media or ASR. Private URLs never leave here."""
import json
import math
import re
import sys
from urllib.parse import urlsplit

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from bilibili_extractor import ProjectBiliBiliIE, ExtractionFailure, QuietLogger
from captions import CaptionBlocked, caption_quality, choose_track, parse_captions
from generic_media import safe_failure
from platforms import parse_video, public_headers
from runtime_config import configure_ca, javascript_runtime

MAX_BODY = 8 * 1024 * 1024


def public_source(ref, title, author, duration):
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise CaptionBlocked('invalid_video_duration')
    return {'platform': ref.platform, 'video_id': ref.video_id, 'part': ref.part,
            'canonical_url': ref.url, 'video_url': ref.url, 'title': title, 'video_title': title,
            'author': author, 'uploader': author, 'duration_seconds': duration, 'audio': None}


def bili_catalog(ref, options):
    with yt_dlp.YoutubeDL(options) as ydl:
        ie = ProjectBiliBiliIE(ydl)
        ie._requested_bvid, ie._requested_part = ref.video_id, ref.part
        view = ie._detail_state()['videoData']
        part = ie._detail_pages[ref.part - 1]
        data = ie._download_json('https://api.bilibili.com/x/player/wbi/v2', ref.video_id,
            query={'bvid': ref.video_id, 'cid': part['cid']}, headers={'Referer': ref.url})
    if not isinstance(data, dict) or data.get('code') != 0 or not isinstance(data.get('data'), dict):
        raise CaptionBlocked('subtitle_api_rejected', endpoint='/x/player/wbi/v2',
                             provider_code=data.get('code') if isinstance(data, dict) and type(data.get('code')) is int else None)
    source = public_source(ref, view['title'], (view.get('owner') or {}).get('name', ''), part['duration'])
    source['cid'] = part['cid']
    manual, automatic = {}, {}
    payload = data['data']
    subtitles = (payload.get('subtitle') or {}).get('subtitles')
    if not isinstance(subtitles, list):
        raise CaptionBlocked('subtitle_catalog_missing', endpoint='/x/player/wbi/v2')
    for track in subtitles:
        if not track.get('subtitle_url') or not track.get('lan'): continue
        language = track['lan']
        auto = bool(track.get('ai_type')) or language.startswith('ai-') or '自动' in track.get('lan_doc', '') or 'AI' in track.get('lan_doc', '')
        catalog = automatic if auto else manual
        catalog.setdefault(language, []).append({'ext': 'bilibili-json', 'url': track['subtitle_url']})
    return source, manual, automatic, {'subtitle_login_required': bool(payload.get('need_login_subtitle'))}


def youtube_catalog(ref, options):
    options.update(allowed_extractors=['youtube'], skip_download=True, ignore_no_formats_error=True,
                   js_runtimes={'deno': {'path': javascript_runtime()}}, remote_components=set())
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(ref.url, download=False)
    if (not isinstance(info, dict) or str(info.get('id')) != ref.video_id
            or info.get('_type') in {'playlist', 'multi_video'} or info.get('entries') is not None):
        raise CaptionBlocked('video_identity_or_single_video_check_failed')
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
        raise CaptionBlocked('live_video_not_supported')
    source = public_source(ref, info.get('title', ''), info.get('uploader') or info.get('channel', ''), info.get('duration'))
    return source, info.get('subtitles') or {}, info.get('automatic_captions') or {}, {}


def caption_body(track, ref):
    address = track['url']
    if address.startswith('//'): address = 'https:' + address
    parsed = urlsplit(address)
    domains = ('bilibili.com', 'hdslb.com') if ref.platform == 'bilibili' else ('youtube.com',)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or not any(parsed.hostname == host or (parsed.hostname or '').endswith('.'+host) for host in domains)):
        raise CaptionBlocked('unexpected_caption_host')
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            with client.stream('GET', address, headers=public_headers(track.get('http_headers'), {'referer': ref.url})) as response:
                if response.status_code != 200:
                    raise CaptionBlocked('caption_request_rejected', http_status=response.status_code)
                if 'text/html' in response.headers.get('content-type', '').lower():
                    raise CaptionBlocked('caption_response_is_html_or_verification_page')
                raw = bytearray()
                for chunk in response.iter_bytes(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY: raise CaptionBlocked('caption_body_too_large')
                return raw.decode('utf-8-sig')
    except httpx.HTTPError:
        raise CaptionBlocked('caption_network_or_tls_failure') from None


def collect(ref, max_minutes, languages):
    options = {'compat_opts': {'no-certifi'}, 'nocheckcertificate': False, 'noplaylist': True,
               'quiet': True, 'no_warnings': True, 'logger': QuietLogger(), 'cachedir': False,
               'retries': 0, 'extractor_retries': 0, 'socket_timeout': 15}
    source, manual, automatic, flags = (bili_catalog if ref.platform == 'bilibili' else youtube_catalog)(ref, options)
    if source['duration_seconds'] > max_minutes * 60: raise CaptionBlocked('video_exceeds_duration_limit')
    track = choose_track(manual, automatic, languages)
    common = {'source': source, 'manual_languages': sorted(manual), 'automatic_languages': sorted(automatic), **flags}
    if track is None:
        return {**common, 'status': 'restricted' if flags.get('subtitle_login_required') else 'unavailable',
                'reason': 'subtitle_login_required' if flags.get('subtitle_login_required') else 'no_supported_original_captions'}
    body = caption_body(track, ref)
    if re.match(r'\s*(?:<!doctype\s+html|<html\b)', body, re.I):
        raise CaptionBlocked('caption_response_is_html_or_verification_page')
    try:
        raw = parse_captions(body, track['ext'], source['duration_seconds'])
    except (ValueError, KeyError, TypeError, AttributeError):
        return {**common, 'status': 'unavailable', 'reason': 'empty_or_invalid_caption_body'}
    selected = {'language': track['language'], 'kind': track['kind'], 'format': track['ext'],
                'cue_count': len(raw['transcripts'][0]['sentences']),
                'language_preference_matched': track['language_preference_matched'],
                'quality': caption_quality(raw, source['duration_seconds'])}
    source['subtitles'] = selected
    return {**common, 'status': 'available', 'selected': selected, 'raw': raw}


def main():
    configure_ca()
    args = json.load(sys.stdin)
    ref = parse_video(args['url'])
    try:
        result = collect(ref, args['max_minutes'], args['languages'])
    except CaptionBlocked as exc:
        result = exc.details
    except ExtractionFailure as exc:
        result = {'status': 'blocked', 'reason': 'bilibili_caption_metadata_failed', 'diagnostic': exc.diagnostic}
    except (DownloadError, ExtractorError) as exc:
        result = {'status': 'blocked', 'reason': safe_failure(str(exc)),
                  'network_layer': 'proxy_tunnel' if 'Tunnel connection failed' in str(exc) else 'unknown'}
    except Exception:
        result = {'status': 'blocked', 'reason': 'caption_extraction_failed'}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
