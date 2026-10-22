"""Shared system-CA setup for fresh workers; never disable certificate checks."""
import json
import os
from pathlib import Path
import shutil
import ssl
import sys


def configure_ca():
    ca = os.environ.get('SSL_CERT_FILE') or '/etc/ssl/certs/ca-certificates.crt'
    if not Path(ca).is_file():
        raise RuntimeError('System CA bundle is missing; run the environment setup.')
    context = ssl.create_default_context(cafile=ca)
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise RuntimeError('TLS certificate verification is required.')
    if not os.environ.get('SSL_CERT_FILE'):
        os.environ['SSL_CERT_FILE'] = ca
    if not os.environ.get('REQUESTS_CA_BUNDLE'):
        os.environ['REQUESTS_CA_BUNDLE'] = ca
    return ca


if __name__ == '__main__':
    ca = configure_ca()
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12+ is required.')
    tools = {name: bool(shutil.which(name)) for name in ('ffmpeg', 'ffprobe')}
    if not all(tools.values()):
        raise SystemExit('FFmpeg and FFprobe are required; run setup.sh.')
    print(json.dumps({'python': sys.version.split()[0], **tools, 'system_ca': ca, 'tls_verification': True}))
