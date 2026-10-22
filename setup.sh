#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"'
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  if [[ "$(id -u)" == 0 ]]; then
    video_apt=(apt-get)
  elif command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
    video_apt=(sudo -n apt-get)
  else
    echo 'FFmpeg/FFprobe missing. Install the ffmpeg package in environment setup with administrator rights.' >&2
    exit 1
  fi
  "${video_apt[@]}" update
  "${video_apt[@]}" install -y --no-install-recommends ffmpeg ca-certificates
fi
video_ca_bundle="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
if [[ ! -s "$video_ca_bundle" ]]; then
  echo 'System CA bundle missing. Configure the platform CA trust before setup.' >&2
  exit 1
fi
export SSL_CERT_FILE="$video_ca_bundle"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$video_ca_bundle}"
# Keep inherited proxy and explicit CA settings; never disable TLS verification.
python3 -m venv .venv
.venv/bin/python -m pip install --quiet -r requirements.txt
# requirements pin Deno and yt-dlp-ejs for YouTube challenges, plus HLS AES support.
.venv/bin/python runtime_config.py
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
