"""Subtitle-first sources; missing subtitles and blocked requests are distinct outcomes."""
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

from platforms import extractor_env

ROOT = Path(__file__).resolve().parent
DEFAULT_LANGUAGES = ('zh-Hans', 'zh-CN', 'zh-Hant', 'zh', 'en')


class CaptionBlocked(Exception):
    def __init__(self, reason, **details):
        self.details = {'status': 'blocked', 'reason': reason, **details}
        super().__init__(reason)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_data(self, value): self.parts.append(value)


def plain(value):
    parser = PlainText()
    parser.feed(re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', value))
    return unescape(''.join(parser.parts)).strip()


def parse_captions(body, ext, duration):
    sentences = []
    data = json.loads(body) if ext in {'json3', 'bilibili-json'} else body
    if ext == 'bilibili-json':
        rows = data.get('body', [])
        for row in rows:
            sentences.append({'text': str(row.get('content') or '').strip(),
                              'begin_time': float(row['from']) * 1000, 'end_time': float(row['to']) * 1000})
    elif ext == 'json3':
        for event in data.get('events', []):
            text = ''.join(str(s.get('utf8', '')) for s in event.get('segs', [])).strip()
            if not text:
                continue
            start, length = event.get('tStartMs'), event.get('dDurationMs')
            if type(start) not in (int, float) or type(length) not in (int, float):
                raise ValueError('caption timing missing')
            sentences.append({'text': text, 'begin_time': start, 'end_time': start + length})
    elif ext in {'vtt', 'srt'}:
        def millis(value):
            parts = value.replace(',', '.').split(':')
            if len(parts) == 2: parts.insert(0, '0')
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])) * 1000
        pattern = r'((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})[^\n]*\n([^\n]+(?:\n(?!\s*\n)[^\n]+)*)'
        for match in re.finditer(pattern, body.replace('\r\n', '\n')):
            sentences.append({'text': plain(match[3]), 'begin_time': millis(match[1]), 'end_time': millis(match[2])})
    else:
        raise ValueError('unsupported caption format')
    sentences = [s for s in sentences if s['text']]
    if not sentences:
        raise ValueError('empty caption body')
    previous = -1
    for sentence in sentences:
        a, b = sentence['begin_time'], sentence['end_time']
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (a, b))
                or not 0 <= a < b <= (duration + 5) * 1000 or a < previous):
            raise ValueError('invalid caption timing')
        sentence['begin_time'], sentence['end_time'] = round(a), max(round(a) + 1, round(b))
        previous = a
    return {'transcripts': [{'sentences': sentences}], 'original_format': ext,
            'original_payload': data, 'payload_sha256': hashlib.sha256(body.encode()).hexdigest()}


def choose_track(manual, automatic, languages=DEFAULT_LANGUAGES):
    candidates = []
    formats = {'json3': 0, 'bilibili-json': 0, 'vtt': 1, 'srt': 2}
    for automatic_flag, catalog in ((False, manual), (True, automatic)):
        for language, tracks in (catalog or {}).items():
            if language == 'danmaku': continue
            for track in tracks:
                if track.get('ext') not in formats or not track.get('url'): continue
                # Prefer original captions, never silently select machine-translated tracks.
                if 'tlang' in parse_qs(urlsplit(track['url']).query): continue
                normalized = language.removeprefix('ai-').casefold()
                rank = next((i for i, pref in enumerate(languages)
                             if normalized == pref.casefold() or normalized.startswith(pref.casefold()+'-')), len(languages))
                candidates.append(((automatic_flag, rank, formats[track['ext']]),
                    {**track, 'language': language, 'kind': 'automatic' if automatic_flag else 'manual',
                     'language_preference_matched': rank < len(languages)}))
    return min(candidates, key=lambda v: v[0])[1] if candidates else None


def caption_quality(raw, duration):
    """Timing diagnostics, not an accuracy score or automatic ASR decision."""
    sentences = raw['transcripts'][0]['sentences']
    end_limit = duration * 1000
    covered, cursor, largest_gap, overlaps = 0, 0, 0, 0
    seen = set()
    duplicates = 0
    for sentence in sentences:
        a = min(end_limit, sentence['begin_time'])
        b = min(end_limit, sentence['end_time'])
        key = (sentence['begin_time'], sentence['end_time'], sentence['text'])
        duplicates += key in seen
        seen.add(key)
        if a < cursor:
            overlaps += 1
        largest_gap = max(largest_gap, a - cursor)
        covered += max(0, b - max(cursor, a))
        cursor = max(cursor, b)
    largest_gap = max(largest_gap, end_limit - cursor)
    return {'first_start_seconds': sentences[0]['begin_time'] / 1000,
            'last_end_seconds': max(s['end_time'] for s in sentences) / 1000,
            'covered_seconds': round(covered / 1000, 3),
            'timing_coverage_ratio': round(covered / end_limit, 4),
            'largest_gap_seconds': round(largest_gap / 1000, 3),
            'overlapping_cues': overlaps, 'exact_duplicate_cues': duplicates,
            'content_review_required': True,
            'note': 'Timing coverage cannot establish speech completeness; silence and music may have no captions.'}


def fetch_captions(ref, max_minutes=300, languages=DEFAULT_LANGUAGES):
    if ref.platform not in {'youtube', 'bilibili'}:
        return {'status': 'unavailable', 'reason': 'caption_adapter_not_enabled'}
    try:
        result = subprocess.run([sys.executable, str(ROOT/'caption_extractor.py')],
            input=json.dumps({'url': ref.url, 'max_minutes': max_minutes, 'languages': languages}),
            env=extractor_env(), capture_output=True, text=True, timeout=150, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {'status': 'blocked', 'reason': 'caption_worker_failed_or_timed_out'}
    try:
        data = json.loads(result.stdout)
        if data.get('status') not in {'available', 'unavailable', 'restricted', 'blocked'}: raise ValueError
        return data
    except (ValueError, AttributeError):
        # Never print stderr or private platform responses.
        return {'status': 'blocked', 'reason': 'invalid_caption_worker_result'}
