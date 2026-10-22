"""Project-local yt-dlp adapter. Its private JSON stdout is captured by resolve()."""
from __future__ import annotations

import json
import math
import re
import sys
from urllib.parse import parse_qs, urlsplit

import yt_dlp
from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.utils import ExtractorError
from runtime_config import configure_ca


def media_headers(*sources):
    """Keep public media headers in global -> video -> format precedence."""
    allowed = {'user-agent', 'referer', 'origin', 'accept', 'accept-language', 'sec-fetch-mode'}
    result = {}
    for source in sources:
        if isinstance(source, dict):
            result.update({k.lower(): v for k, v in source.items()
                           if k.lower() in allowed and isinstance(v, str)})
    return result


def safe_message(value):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?:https?|oss)://[^\s\"<>]+", "[URL redacted]", value)
    value = re.sub(r"(?i)(cookie|authorization|token|signature)\s*[:=][^\n]*", "[redacted]", value)
    value = re.sub(r"[A-Za-z0-9_+/=-]{28,}", "[identifier redacted]", value)
    return " ".join(value.split())[:160]


class ExtractionFailure(ExtractorError):
    def __init__(self, stage, reason, **details):
        self.diagnostic = {"stage": stage, "reason": reason, **details}
        super().__init__(json.dumps(self.diagnostic, ensure_ascii=False), expected=True)


class ProjectBiliBiliIE(BiliBiliIE):
    """Supply missing initial state; keep upstream playback and format parsing."""

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        if not match or match['prefix'].upper() != 'BV':
            raise ExtractionFailure('input', 'BV URL required')
        self._requested_bvid = 'BV' + match['id']
        parts = parse_qs(urlsplit(url).query, keep_blank_values=True).get('p', ['1'])
        if len(parts) != 1 or not re.fullmatch(r'[1-9][0-9]{0,4}', parts[0]):
            raise ExtractionFailure('input', 'invalid part parameter')
        self._requested_part = int(parts[0])
        self._detail_pages = None
        result = super()._real_extract(url)
        if self._detail_pages is not None and isinstance(result, dict):
            # Metadata duration must describe the selected part, never the whole anthology.
            if result.get('duration') is None:
                result['duration'] = self._detail_pages[self._requested_part - 1]['duration']
        return result

    def _search_json(self, start_pattern, string, name, *args, **kwargs):
        value = super()._search_json(start_pattern, string, name, *args, **kwargs)
        if name != 'initial state' or value:
            return value
        risk = super()._search_json(r'\bwindow\._riskdata_\s*=', string, 'risk',
                                    self._requested_bvid, default={})
        if isinstance(risk, dict) and risk.get('v_voucher'):
            raise ExtractionFailure('page', 'verification required; stopped')
        return self._detail_state()

    def _detail_state(self):
        """Validated selected-part metadata, also usable without playback for captions."""
        detail = self._download_json(
            'https://api.bilibili.com/x/web-interface/wbi/view/detail', self._requested_bvid,
            query=self._sign_wbi({'bvid': self._requested_bvid, 'platform': 'web'}, self._requested_bvid),
            headers={'Referer': 'https://www.bilibili.com/', 'Origin': 'https://www.bilibili.com'},
            note='Downloading detail fallback')
        data = detail.get('data') if isinstance(detail, dict) else None
        view = data.get('View') if isinstance(data, dict) else None
        if not isinstance(view, dict) or view.get('bvid') != self._requested_bvid:
            raise ExtractionFailure('detail', 'missing View or BV mismatch')
        if not isinstance(view.get('title'), str) or not view['title'].strip():
            raise ExtractionFailure('detail', 'missing video title')
        pages = view.get('pages')
        if not isinstance(pages, list) or not pages or any(not isinstance(p, dict) for p in pages):
            raise ExtractionFailure('detail', 'missing part metadata')
        numbers = [p.get('page') for p in pages]
        if any(type(n) is not int or n < 1 for n in numbers) or sorted(numbers) != list(range(1, len(pages) + 1)):
            raise ExtractionFailure('detail', 'invalid or duplicate part numbers')
        pages = sorted(pages, key=lambda p: p['page'])
        if self._requested_part > len(pages):
            raise ExtractionFailure('detail', 'requested part does not exist')
        part = pages[self._requested_part - 1]
        if type(part.get('cid')) is not int or part['cid'] <= 0:
            raise ExtractionFailure('detail', 'invalid selected cid')
        duration = part.get('duration')
        if (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
            raise ExtractionFailure('detail', 'invalid selected duration')
        if view.get('redirect_url') or (view.get('rights') or {}).get('is_stein_gate'):
            raise ExtractionFailure('detail', 'redirected or interactive video unsupported')
        self._detail_pages = pages
        return {'videoData': {**view, 'pages': pages, 'cid': part['cid'], 'duration': duration},
                'upData': view.get('owner') if isinstance(view.get('owner'), dict) else {},
                'tags': data.get('Tags') if isinstance(data.get('Tags'), list) else []}

    def _download_json(self, url_or_request, *args, **kwargs):
        url = getattr(url_or_request, 'url', url_or_request)
        path = urlsplit(url).path
        stages = {'/x/web-interface/wbi/view/detail': 'detail',
                  '/x/player/wbi/playurl': 'playback', '/x/web-interface/nav': 'signature'}
        stage = stages.get(path)
        if path == '/x/player/pagelist' and self._detail_pages is not None:
            return {'code': 0, 'data': self._detail_pages}
        try:
            data = super()._download_json(url_or_request, *args, **kwargs)
        except ExtractorError as exc:
            if stage:
                status = getattr(exc.cause, 'status', None)
                reason = 'TLS certificate verification failed' if 'CERTIFICATE_VERIFY_FAILED' in str(exc) else 'request failed'
                raise ExtractionFailure(stage, reason, endpoint=path, http_status=status) from None
            raise
        if stage in {'detail', 'playback'}:
            code = data.get('code') if isinstance(data, dict) else None
            if type(code) is not int or code != 0:
                raise ExtractionFailure(stage, 'API rejected request', endpoint=path,
                    provider_code=code if type(code) is int else None,
                    provider_message=safe_message(data.get('message')) if isinstance(data, dict) else '')
            if stage == 'playback' and not isinstance(data.get('data'), dict):
                raise ExtractionFailure(stage, 'missing playback data', endpoint=path)
        return data

    def _download_playinfo(self, bvid, cid, headers=None, query=None, fatal=True):
        # Do not let upstream's fatal=False continue with an empty/failed response.
        return super()._download_playinfo(bvid, cid, headers=headers, query=query, fatal=True)

    def _get_chapters(self, aid, cid):
        if self._detail_pages is not None:
            return None  # Avoid unrelated chapter requests during the narrow fallback.
        return super()._get_chapters(aid, cid)


class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def main():
    configure_ca()
    parsed = yt_dlp.parse_options(sys.argv[1:])
    if len(parsed.urls) != 1:
        raise ExtractionFailure('input', 'one video URL required')
    options = {**parsed.ydl_opts, 'logger': QuietLogger(), 'ignoreerrors': False,
               'dump_single_json': False, 'noplaylist': True}
    with yt_dlp.YoutubeDL(options) as ydl:
        # extract() returns upstream formats without downloading or processing playlists.
        info = ProjectBiliBiliIE(ydl).extract(parsed.urls[0])
        headers = media_headers(dict(ydl.params['http_headers']), info.get('http_headers'))
    output = {key: info.get(key) for key in ('_type', 'title', 'uploader', 'duration')}
    output['formats'] = [{**{key: f.get(key) for key in ('url', 'format_id', 'acodec', 'vcodec')},
                          'http_headers': media_headers(headers, f.get('http_headers'))}
                         for f in info.get('formats', [])]
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except ExtractionFailure as exc:
        print('BILI_DIAGNOSTIC ' + json.dumps(exc.diagnostic, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        reason = 'TLS certificate verification failed' if 'CERTIFICATE_VERIFY_FAILED' in str(exc) else 'extraction failed'
        print('BILI_DIAGNOSTIC ' + json.dumps({'stage': 'extractor', 'reason': reason}), file=sys.stderr)
        sys.exit(2)
