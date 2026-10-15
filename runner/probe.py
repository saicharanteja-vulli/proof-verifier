"""Trusted HTTP probe; executed with a different UID from the app under test."""
import http.client
import json
import sys

check = json.load(sys.stdin)
try:
    body = json.dumps(check['body']).encode() if check.get('body') is not None else None
    headers = {'Host': 'localhost', **check.get('headers', {})}
    if body is not None:
        headers.setdefault('Content-Type', 'application/json')
    conn = http.client.HTTPConnection('127.0.0.1', 8000, timeout=3)
    conn.request(check['method'], check['path'], body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read(65537)
    if len(raw) > 65536:
        print(json.dumps({'error': 'Response exceeds 64 KB evidence limit.'}))
    else:
        print(json.dumps({'status': response.status, 'body': raw.decode('utf-8', 'replace')}))
except Exception as exc:
    print(json.dumps({'error': type(exc).__name__ + ': ' + str(exc)[:300]}))
