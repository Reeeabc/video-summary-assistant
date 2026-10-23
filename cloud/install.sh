#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Reuse the platform checkout. Never fetch/reset/stash or change network credentials here.
bash setup.sh
# Cloud tasks may start in /workspace, above the repository's AGENTS.md.
.venv/bin/python cloud/install_instructions.py --workspace /workspace
git rev-parse HEAD
