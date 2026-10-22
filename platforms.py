"""Explicit single-video adapters; reserved platforms stay disabled until verified."""
from dataclasses import dataclass
import os
import re
from urllib.parse import parse_qs, urlsplit


class Problem(Exception):
    """Bounded, sanitized error suitable for user output."""


@dataclass(frozen=True)
class VideoRef:
    platform: str
    video_id: str
    url: str
    part: int = 1

    @property
    def identity(self):
        return f'{self.platform}-{self.video_id}' + (f'-p{self.part}' if self.platform == 'bilibili' else '')


# New adapters must define URL validation, extraction, audio selection and evidence.
ADAPTERS = {'bilibili': 'bilibili_media', 'youtube': 'generic_media', 'vimeo': 'generic_media',
            'douyin': None, 'tiktok': None, 'ixigua': None}
RESERVED_HOSTS = {'douyin.com': 'douyin', 'tiktok.com': 'tiktok', 'ixigua.com': 'ixigua'}


def parse_video(value):
    if re.fullmatch(r'BV[0-9A-Za-z]{10}', value):
        value = 'https://www.bilibili.com/video/' + value
    p = urlsplit(value)
    if p.scheme != 'https' or p.username or p.password or p.fragment or p.netloc != p.hostname:
        raise Problem('请提供无登录凭据的 HTTPS 单视频链接。')
    host = p.hostname or ''
    q = parse_qs(p.query, keep_blank_values=True)
    if host in {'bilibili.com', 'www.bilibili.com'}:
        match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10})/?', p.path)
        pages = q.get('p', ['1'])
        if not match or len(pages) != 1 or not re.fullmatch(r'[1-9][0-9]{0,4}', pages[0]):
            raise Problem('B站链接或分 P 参数无效；不展开合集。')
        part = int(pages[0])
        return VideoRef('bilibili', match[1], f'https://www.bilibili.com/video/{match[1]}?p={part}', part)
    if host in {'www.youtube.com', 'youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}:
        ids = q.get('v', []) if p.path == '/watch' else []
        match = re.fullmatch(r'/(?:shorts/|embed/)?([A-Za-z0-9_-]{11})/?', p.path)
        if match and (host == 'youtu.be' or p.path.startswith(('/shorts/', '/embed/'))):
            ids = [match[1]]
        if len(ids) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{11}', ids[0]):
            raise Problem('需要 YouTube 单视频链接；不处理频道、合集或直播入口。')
        return VideoRef('youtube', ids[0], 'https://www.youtube.com/watch?v=' + ids[0])
    if host in {'vimeo.com', 'www.vimeo.com', 'player.vimeo.com'}:
        match = re.fullmatch(r'/(?:video/)?([1-9][0-9]{0,14})/?', p.path)
        if not match or (host == 'player.vimeo.com' and not p.path.startswith('/video/')):
            raise Problem('需要 Vimeo 公开视频数字 ID 链接；不处理频道、合集或需鉴权链接。')
        if 'h' in q:
            raise Problem('本轮不处理需额外访问令牌的 Vimeo 链接。')
        return VideoRef('vimeo', match[1], 'https://vimeo.com/' + match[1])
    for domain, platform in RESERVED_HOSTS.items():
        if host == domain or host.endswith('.' + domain):
            raise Problem(f'{platform} 仅预留适配入口，尚未实测开放。')
    raise Problem('当前仅接入 B站、YouTube 和 Vimeo 单视频链接。')


def extractor_env():
    allowed = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
               'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
               'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
    return {k: v for k, v in os.environ.items() if k in allowed}


def public_headers(*sources):
    allowed = {'user-agent', 'referer', 'origin', 'accept', 'accept-language', 'sec-fetch-mode'}
    return {k.lower(): v for source in sources if isinstance(source, dict) for k, v in source.items()
            if k.lower() in allowed and isinstance(v, str) and '\r' not in v and '\n' not in v}
