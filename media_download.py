"""Isolated yt-dlp audio download worker; private media metadata arrives via stdin."""
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import DownloadError
from runtime_config import configure_ca


class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def download_selected(info, destination, max_bytes):
    if info.get('vcodec') != 'none' or info.get('acodec') in (None, 'none') or info.get('has_drm'):
        raise ValueError('independent_audio_required')
    if info.get('protocol', 'https') not in {'https', 'http_dash_segments', 'm3u8_native'}:
        raise ValueError('unsupported_audio_protocol')
    for address in [info.get('url', ''), info.get('fragment_base_url'), info.get('manifest_url')]:
        if address:
            url = urlsplit(address)
            if url.scheme != 'https' or url.username or url.password:
                raise ValueError('unsafe_media_address')
    if info.get('filesize') and info['filesize'] > max_bytes:
        raise ValueError('source_exceeds_size_limit')

    def limit(progress):
        if progress.get('downloaded_bytes', 0) > max_bytes:
            raise ValueError('source_exceeds_size_limit')
        if sum(p.stat().st_size for p in destination.parent.rglob('*') if p.is_file()) > max_bytes:
            raise ValueError('temporary_audio_exceeds_size_limit')

    options = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'logger': QuietLogger(),
        'compat_opts': {'no-certifi'}, 'nocheckcertificate': False, 'noplaylist': True,
        'retries': 0, 'fragment_retries': 0, 'extractor_retries': 0, 'file_access_retries': 0,
        'skip_unavailable_fragments': False, 'concurrent_fragment_downloads': 1,
        'socket_timeout': 30, 'max_filesize': max_bytes, 'progress_hooks': [limit],
        'outtmpl': str(destination), 'fixup': 'never', 'overwrites': False,
        'cachedir': False, 'hls_prefer_native': True}
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.process_info(info)
    if not destination.is_file() or not 0 < destination.stat().st_size <= max_bytes:
        raise ValueError('missing_or_excessive_audio_output')
    return destination.stat().st_size


def main():
    configure_ca()
    payload = json.load(sys.stdin)
    count = download_selected(payload['info'], Path(payload['destination']), payload['max_bytes'])
    print(json.dumps({'source_bytes': count, 'complete_download': True}))


if __name__ == '__main__':
    try:
        main()
    except DownloadError as exc:
        # Only a predefined category may cross the process boundary.
        from generic_media import safe_failure
        print(json.dumps({'stage': 'audio_download', 'reason': safe_failure(str(exc))}), file=sys.stderr)
        sys.exit(2)
    except Exception:
        print(json.dumps({'stage': 'audio_download', 'reason': '音频下载失败、缺片或超过大小限制'}), file=sys.stderr)
        sys.exit(2)
