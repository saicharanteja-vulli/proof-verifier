#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -c 'import fastapi,uvicorn,httpx' 2>/dev/null || .venv/bin/pip install -r requirements.txt
exec .venv/bin/python -m uvicorn proof.app:app --host 127.0.0.1 --port "${PORT:-8100}"
