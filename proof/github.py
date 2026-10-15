"""Public GitHub PR import. Never clones or executes PR code on the host."""
import base64
import json
import hashlib
import io
import re
import stat
import zipfile
import httpx
from pydantic import ValidationError
from .models import Source, Review, digest

REPO=r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+'
PR_URL=re.compile(r'https://github\.com/('+REPO+r')/pull/([1-9][0-9]*)/?$')
SHA=re.compile(r'^[a-f0-9]{40}$')

def request_json(path):
    with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as c:
        r=c.get('https://api.github.com'+path,headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
        r.raise_for_status();return r.json()

def pr(url):
    m=PR_URL.fullmatch(url)
    if not m: raise ValueError('Use a public https://github.com/owner/repository/pull/123 URL.')
    repo,number=m.groups();p=request_json(f'/repos/{repo}/pulls/{number}')
    if p['state']!='open': raise ValueError('Import an open pull request.')
    for side in ('base','head'):
        if not p[side].get('repo') or p[side]['repo'].get('private'): raise ValueError('This version supports public repositories only.')
        if not SHA.fullmatch(p[side]['sha']): raise ValueError('Invalid commit ID.')
    return repo,int(number),p

def decode_archive(content):
    files=[];total=0
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        if len(z.infolist())>3000: raise ValueError('Repository archive has too many entries.')
        for item in z.infolist():
            if item.is_dir(): continue
            parts=item.filename.split('/');path='/'.join(parts[1:])
            if not path: continue
            if stat.S_ISLNK(item.external_attr>>16): raise ValueError('Repository symlinks are not supported.')
            try: Source(path=path,content='')
            except ValidationError: continue
            if item.file_size>150000: raise ValueError('A source file exceeds 150 KB.')
            total+=item.file_size
            if total>2000000 or len(files)>=80: raise ValueError('Source exceeds Proof limits (80 files / 2 MB).')
            try: text=z.read(item).decode('utf-8')
            except UnicodeDecodeError: raise ValueError('Source files must be UTF-8.')
            files.append({'path':path,'content':text})
    if not files: raise ValueError('No supported source found.')
    return sorted(files,key=lambda f:f['path'])

def snapshot(repo,sha):
    if not re.fullmatch(REPO,repo) or not SHA.fullmatch(sha): raise ValueError('Invalid repository or commit.')
    data=bytearray()
    with httpx.Client(timeout=30,trust_env=False,follow_redirects=False) as c:
        with c.stream('GET',f'https://codeload.github.com/{repo}/zip/{sha}') as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                data.extend(chunk)
                if len(data)>5000000: raise ValueError('Repository download exceeds 5 MB.')
    return decode_archive(data)

def import_pr(url):
    repo,number,p=pr(url)
    baseline=snapshot(p['base']['repo']['full_name'],p['base']['sha'])
    candidate=snapshot(p['head']['repo']['full_name'],p['head']['sha'])
    policy={}
    try:
        raw=request_json(f"/repos/{repo}/contents/.proof/policy.json?ref={p['base']['sha']}")
        if raw.get('size',0)>50000: raise ValueError('Policy is too large.')
        value=json.loads(base64.b64decode(raw['content']).decode())
        validated=Review.model_validate({**value,'baseline':baseline,'candidate':candidate,'execution_consent':True})
        policy={'entrypoint':validated.entrypoint,'checks':[c.model_dump() for c in validated.checks]}
    except (httpx.HTTPError,ValueError,KeyError):
        pass
    return {'policy':policy,'url':p['html_url'],'repository':repo,'number':number,'base_sha':p['base']['sha'],'head_sha':p['head']['sha'],
            'base_ref':p['base']['ref'],'head_repository':p['head']['repo']['full_name'],
            'title':p['title'][:100],'requirement':(p.get('body') or 'Review the proposed pull request behavior.')[:2000],
            'baseline':baseline,'candidate':candidate,'source_digest':digest([baseline,candidate])}

def status(metadata):
    _,_,p=pr(metadata['url']);sha=p['head']['sha'];repo=metadata['repository']
    runs=request_json(f'/repos/{repo}/commits/{sha}/check-runs?per_page=100')
    combined=request_json(f'/repos/{repo}/commits/{sha}/status')
    return {'head_sha':sha,'stale':sha!=metadata['head_sha'] or p['base']['sha']!=metadata['base_sha'],
            'mergeable_state':p.get('mergeable_state'),'checks':[{'name':r['name'],'status':r['status'],'conclusion':r.get('conclusion'),'url':r['html_url']} for r in runs['check_runs']],
            'statuses':[{'name':r['context'],'state':r['state']} for r in combined['statuses']],
            'note':'GitHub enforces repository branch rules. A local pass alone does not unlock merging.'}
