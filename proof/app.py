import json
import os
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime,timezone
from typing import Literal
from uuid import uuid4
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from .models import Review,SourceInput,Strict,digest
from . import generator,github
from .engine import runtime,run_version,compare,diff_files

BASE=Path(__file__).resolve().parent.parent
DATA=Path(os.environ.get('PROOF_DATA',BASE/'data'))
POOL=ThreadPoolExecutor(max_workers=1)
SLOTS=threading.BoundedSemaphore(3)

def now(): return datetime.now(timezone.utc).isoformat()
def connect():
    db=sqlite3.connect(DATA/'proof.db',timeout=15);db.row_factory=sqlite3.Row;return db

def initialize():
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700);DATA.chmod(0o700)
    fd=os.open(DATA/'proof.db',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);os.fchmod(fd,0o600);os.close(fd)
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,title TEXT,created TEXT,state TEXT,phase TEXT,progress INTEGER,payload TEXT,digest TEXT,report TEXT,events TEXT,decision TEXT,decision_note TEXT,decided TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS github_imports(id TEXT PRIMARY KEY,created TEXT,payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,created TEXT,input_digest TEXT,proposal TEXT)')
        for row in db.execute("SELECT id,events FROM reviews WHERE state IN ('queued','running')").fetchall():
            events=json.loads(row['events'])+[{'at':now(),'message':'Interrupted by server restart. Submit again to re-run; no pass was recorded.'}]
            db.execute("UPDATE reviews SET state='failed',phase='Interrupted',events=? WHERE id=?",(json.dumps(events),row['id']))

def get_review(rid, include_source=False):
    with connect() as db: row=db.execute('SELECT * FROM reviews WHERE id=?',(rid,)).fetchone()
    if not row: raise HTTPException(404,'Review not found.')
    obj=dict(row)
    for k in ['payload','report','events']: obj[k]=json.loads(obj[k])
    if not include_source:
        p=obj.pop('payload');obj.update(github_import_id=p.get('github_import_id'),proposal_id=p.get('proposal_id'),tests_reviewed=p.get('tests_reviewed',False),requirement=p['requirement'],entrypoint=p['entrypoint'],checks=p['checks'],files={'baseline':len(p['baseline']),'candidate':len(p['candidate'])})
    return obj

def update(rid,**fields):
    for k in ('report','events'):
        if k in fields: fields[k]=json.dumps(fields[k])
    with connect() as db: db.execute('UPDATE reviews SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',[*fields.values(),rid])

def work(rid):
    try:
        item=get_review(rid,True);p=item['payload'];events=item['events']
        def event(text):
            events.append({'at':now(),'message':text});update(rid,events=events)
        update(rid,state='running',phase='Checking isolation',progress=3)
        rt=runtime()
        if not rt['ready']: raise RuntimeError(rt['message'])
        event('Pinned runner image: '+rt['image'])
        report={'github':{k:v for k,v in get_import(p['github_import_id']).items() if k not in {'baseline','candidate'}} if p.get('github_import_id') else None,'image':rt['image'],'input_digest':item['digest'],'diff':diff_files(p['baseline'],p['candidate']),
            'limitations':['Only the supplied HTTP contracts were checked; the requirement text is not automatically proven.','No external network, database service, dependency installation, or full pytest suite is run.','Checks run sequentially in each fresh container. Cookies are not shared between requests.','This local tool is for your own reviewed source, not hostile multi-tenant code.','Response snippets are redacted best-effort. Review exports before sharing.']}
        for side,start in [('baseline',8),('candidate',48)]:
            update(rid,phase='Testing '+side,progress=start)
            event('Starting '+side+' in a fresh, network-disabled container.')
            report[side]=run_version(p[side],p['entrypoint'],p['checks'],rt['image'],lambda i,n:update(rid,progress=start+round(i/n*38)))
            event(side.capitalize()+' finished; temporary container removed.')
            update(rid,report=report)
        report.update(compare(report['baseline'],report['candidate'],p['checks']))
        if not report['diff']: report['limitations'].insert(0,'Baseline and candidate are identical. This run does not demonstrate a code change.')
        event(report['summary']);update(rid,state='completed',phase='Ready for review',progress=100,report=report,events=events)
    except Exception as exc:
        item=get_review(rid);events=item['events']+[{'at':now(),'message':str(exc)[:500]}]
        update(rid,state='failed',phase='Could not verify',events=events)
    finally: SLOTS.release()

@asynccontextmanager
async def lifespan(app):
    initialize();yield

app=FastAPI(title='Proof — Code verification',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
@app.middleware('http')
async def boundary(request:Request,call_next):
    if request.url.hostname not in {'localhost','127.0.0.1','testserver'}: return JSONResponse({'detail':'Proof is a local single-user application.'},403)
    origin=request.headers.get('origin')
    if (origin and origin!=str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site')=='cross-site':
        return JSONResponse({'detail':'Cross-origin request blocked.'},403)
    if request.method not in {'GET','HEAD'}:
        if request.headers.get('x-proof-client')!='dashboard': return JSONResponse({'detail':'Dashboard header required.'},403)
        if request.headers.get('content-type','').split(';')[0]!='application/json': return JSONResponse({'detail':'JSON required.'},415)
        size=0;chunks=[]
        async for chunk in request.stream():
            size+=len(chunk)
            if size>5_000_000: return JSONResponse({'detail':'Request exceeds 5 MB.'},413)
            chunks.append(chunk)
        request._body=b''.join(chunks)
    response=await call_next(request)
    response.headers.update({'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer','Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response

@app.get('/api/runtime')
def get_runtime(): return runtime()

@app.get('/api/reviews')
def list_reviews():
    with connect() as db:
        rows=db.execute('SELECT id,title,created,state,phase,progress,report,decision FROM reviews ORDER BY created DESC LIMIT 100').fetchall()
    items=[]
    for row in rows:
        item=dict(row);r=json.loads(item.pop('report'));item['verdict']=r.get('verdict');item['summary']=r.get('summary','');items.append(item)
    return items

@app.post('/api/reviews',status_code=202)
def create_review(body:Review):
    if body.github_import_id:
        imported=get_import(body.github_import_id)
        if digest([body.model_dump()['baseline'],body.model_dump()['candidate']])!=imported['source_digest']:
            raise HTTPException(409,'Imported source changed. Re-import the PR.')
        try: _,_,live=github.pr(imported['url'])
        except Exception as exc: raise HTTPException(503,'Could not confirm the current GitHub commits. Nothing was run.') from exc
        if live['head']['sha']!=imported['head_sha'] or live['base']['sha']!=imported['base_sha']:
            raise HTTPException(409,'PR commits changed. Re-import and review the tests again.')
    if body.proposal_id:
        with connect() as db: proposal=db.execute('SELECT * FROM proposals WHERE id=?',(body.proposal_id,)).fetchone()
        if not proposal: raise HTTPException(404,'Test proposal not found.')
        source={k:body.model_dump()[k] for k in SourceInput.model_fields}
        if digest(source)!=proposal['input_digest']: raise HTTPException(409,'Source or requirement changed. Generate a new proposal before running.')
    rt=runtime()
    if not rt['ready']: raise HTTPException(503,rt['message'])
    if not SLOTS.acquire(blocking=False): raise HTTPException(429,'Three reviews are already queued or running. Try again shortly.')
    rid=uuid4().hex;p=body.model_dump();events=[{'at':now(),'message':'Execution consent recorded. Source snapshots and shared checks sealed.'}]
    if body.proposal_id:
        events.append({'at':now(),'message':'Reviewed tests confirmed for proposal '+body.proposal_id+'. Final edited checks are sealed in this run digest.'})
    try:
        with connect() as db:
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(rid,body.title,now(),'queued','Waiting for runner',0,json.dumps(p),digest(p),'{}',json.dumps(events),'pending','',''))
        POOL.submit(work,rid)
    except Exception:
        SLOTS.release();raise
    return {'id':rid}

@app.post('/api/proposals',status_code=201)
def propose(body:SourceInput):
    if not generator.LOCK.acquire(blocking=False): raise HTTPException(429,'A test proposal is being generated. Try again shortly.')
    try:
        payload=body.model_dump()
        try: result=generator.generate(payload)
        except Exception as exc:
            raise HTTPException(503,'Local test generation failed or returned invalid checks. Ensure Ollama is running with '+generator.MODEL+'. Nothing was executed.') from exc
        pid=uuid4().hex
        with connect() as db: db.execute('INSERT INTO proposals VALUES(?,?,?,?)',(pid,now(),digest(payload),json.dumps(result)))
        return {'id':pid,'input_digest':digest(payload),**result}
    finally: generator.LOCK.release()

@app.get('/api/proposals/{pid}')
def proposal_detail(pid:str):
    with connect() as db: row=db.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone()
    if not row: raise HTTPException(404,'Test proposal not found.')
    return {'id':row['id'],'created':row['created'],'input_digest':row['input_digest'],**json.loads(row['proposal'])}

@app.get('/api/reviews/{rid}')
def detail(rid:str): return get_review(rid)

class Decision(Strict):
    action: Literal['approve','reject']
    reviewed: Literal[True]
    note: str = Field(default='',max_length=1500)

@app.post('/api/reviews/{rid}/decision')
def decide(rid:str,body:Decision):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM reviews WHERE id=?',(rid,)).fetchone()
        if not row: raise HTTPException(404,'Review not found.')
        report=json.loads(row['report'])
        if row['state']!='completed': raise HTTPException(409,'Wait for complete evidence before deciding.')
        if row['decision']!='pending': raise HTTPException(409,'A decision has already been recorded. Create a new review for further changes.')
        if body.action=='approve' and report.get('verdict')!='checks_passed': raise HTTPException(409,'Failed or inconclusive checks cannot be approved.')
        events=json.loads(row['events'])+[{'at':now(),'message':'Local reviewer '+('approved' if body.action=='approve' else 'rejected')+' this exact input digest. No merge or deployment was performed.'}]
        db.execute('UPDATE reviews SET decision=?,decision_note=?,decided=?,events=? WHERE id=?',('approved' if body.action=='approve' else 'rejected',body.note,now(),json.dumps(events),rid))
    return get_review(rid)

@app.get('/api/reviews/{rid}/report')
def export(rid:str,format:Literal['json','md']='md'):
    r=get_review(rid)
    if r['state']!='completed': raise HTTPException(409,'Report is not ready.')
    if format=='json': return Response(json.dumps(r,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="proof-{rid}.json"'})
    report=r['report'];lines=['# Proof verification report','',r['title'],'','Requirement: '+r['requirement'],'','Verdict: '+report['verdict'],'',report['summary'],'','Decision: '+r['decision'],'','Input SHA-256: '+r['digest'],'','Runner image: '+report['image'],'','## Checks','']
    if report.get('github'):
        gh=report['github'];lines+=['GitHub PR: '+gh['url'],'','Base commit: '+gh['base_sha'],'','Head commit: '+gh['head_sha'],'','Local results do not publish or override GitHub required checks.','']
    if r.get('proposal_id'):
        lines+=['AI test proposal: '+r['proposal_id'],'','Tests reviewed before execution: '+str(r.get('tests_reviewed',False)),'']
    for row in report['rows']:
        lines+=['- '+row['name']+': '+row['transition']+' (baseline '+str(row['baseline']['status'])+', candidate '+str(row['candidate']['status'])+')']
    lines+=['','## Exact executed checks','','```json',json.dumps(r['checks'],indent=2),'```','']
    lines+=['','## Scope and limitations','']+['- '+s for s in report['limitations']]
    lines+=['','No merge or deployment was performed.']
    return Response('\n'.join(lines),media_type='text/markdown',headers={'Content-Disposition':f'attachment; filename="proof-{rid}.md"'})

class ImportPR(Strict):
    url: str = Field(max_length=400)

@app.post('/api/github/import',status_code=201)
def import_github(body:ImportPR):
    try: data=github.import_pr(body.url)
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    except Exception as exc: raise HTTPException(503,'GitHub import unavailable. Check the public URL or API rate limit.') from exc
    iid=uuid4().hex
    with connect() as db: db.execute('INSERT INTO github_imports VALUES(?,?,?)',(iid,now(),json.dumps(data)))
    return {'github_import_id':iid,**data}

def get_import(iid):
    with connect() as db: row=db.execute('SELECT payload FROM github_imports WHERE id=?',(iid,)).fetchone()
    if not row: raise HTTPException(404,'PR import not found.')
    return json.loads(row['payload'])

@app.get('/api/github/imports/{iid}/status')
def github_status(iid:str):
    data=get_import(iid)
    try: return github.status(data)
    except Exception as exc: raise HTTPException(503,'Could not read GitHub check status.') from exc

@app.get('/api/example')
def example(): return json.loads((BASE/'examples/relay-change.json').read_text())

@app.get('/')
def home(): return FileResponse(BASE/'static/index.html')
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
