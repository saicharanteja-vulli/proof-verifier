"""Run reviewed source in Docker only. Never import submitted code on the host."""
import difflib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
from .models import digest

IMAGE='proof-fastapi-runner:1'

def command(args, timeout=8, input=None):
    return subprocess.run(['docker',*args],input=input,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False)

def runtime():
    try:
        info=command(['info','--format','{{.ServerVersion}}'],4)
        if info.returncode: return {'ready':False,'message':'Start Docker Desktop to enable isolated verification.'}
        image=command(['image','inspect',IMAGE,'--format','{{.Id}}'],4)
        if image.returncode: return {'ready':False,'message':'Build the runner once with sh setup-runner.sh.'}
        return {'ready':True,'message':'Docker runner ready','image':image.stdout.strip(),'version':info.stdout.strip()}
    except (OSError,subprocess.TimeoutExpired):
        return {'ready':False,'message':'Docker is unavailable. Proof will not run code directly on your computer.'}

def redact(text):
    text=re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~-]+',r'\1[redacted]',text)
    return re.sub(r'(?i)((?:password|secret|token|api_key)[\"\s]*[:=]\s*[\"\x27]?)[^\s\"\x27,}]+',r'\1[redacted]',text)[:16000]

def subset(expected, actual):
    if isinstance(expected,dict):
        return isinstance(actual,dict) and all(k in actual and subset(v,actual[k]) for k,v in expected.items())
    return type(expected) is type(actual) and expected==actual

def assess(check, observed):
    if observed.get('error'): return {'passed':False,'reason':observed['error'],'status':observed.get('status'),'body':redact(observed.get('body',''))}
    status=observed.get('status')
    ok=status==check['expected_status']
    reason='Expected status and JSON assertions matched.' if ok else f"Expected HTTP {check['expected_status']}, received {status}."
    if ok and check.get('expected_json') is not None:
        try: ok=subset(check['expected_json'],json.loads(observed.get('body','')))
        except (ValueError,TypeError): ok=False
        if not ok: reason='Response JSON did not contain the expected values.'
    return {'passed':ok,'reason':reason,'status':status,'body':redact(observed.get('body',''))}

def diff_files(before, after):
    left={f['path']:f['content'] for f in before};right={f['path']:f['content'] for f in after};result=[]
    for name in sorted(left.keys()|right.keys()):
        if left.get(name)==right.get(name): continue
        patch=''.join(difflib.unified_diff(left.get(name,'').splitlines(True),right.get(name,'').splitlines(True),fromfile='baseline/'+name,tofile='candidate/'+name))
        result.append({'path':name,'kind':'added' if name not in left else 'removed' if name not in right else 'modified','patch':redact(patch),'truncated':len(patch)>16000})
    return result

def container_args(name,folder,image_id,entrypoint):
    return ['run','--detach','--pull=never','--name',name,'--label','proof.managed=true','--network=none','--read-only',
        '--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--memory-swap=256m','--cpus=1',
        '--log-driver=local','--log-opt=max-size=1m','--log-opt=max-file=1','--log-opt=compress=false',
        '--tmpfs','/tmp:rw,noexec,nosuid,size=33554432,mode=1777','--user','65534:65534',
        '--mount',f'type=bind,source={folder},target=/workspace,readonly','--workdir','/workspace',
        '--env','RELAY_DATA=/tmp/relay','--env','CONTROL_DB=/tmp/orbit/control.db','--env','AEGIS_DATA=/tmp/aegis',
        '--env','HOME=/tmp','--entrypoint','python',image_id,'-m','uvicorn',entrypoint,'--host','127.0.0.1','--port','8000','--no-access-log']

def probe(name,check):
    reply=command(['exec','--user','1001:1001','-i',name,'python','-I','/opt/proof/probe.py'],6,input=json.dumps(check))
    if reply.returncode: return {'error':'Probe could not run; application container may have stopped.'}
    try: return json.loads(reply.stdout)
    except ValueError: return {'error':'Probe returned invalid evidence.'}

def run_version(files,entrypoint,checks,image_id,progress):
    name='proof-'+uuid.uuid4().hex
    started=time.monotonic();results=[];cleanup=True
    with tempfile.TemporaryDirectory(prefix='proof-source-') as folder:
        # Temporary snapshots contain only explicitly submitted source, never live app data.
        os.chmod(folder,0o755)
        for f in files:
            p=Path(folder)/f['path'];p.parent.mkdir(parents=True,exist_ok=True);p.write_text(f['content']);p.chmod(0o444)
        try:
            launch=command(container_args(name,str(Path(folder).resolve()),image_id,entrypoint),15)
            if launch.returncode: return {'error':'Container startup failed. Check Docker and available resources.','diagnostic':redact(launch.stderr),'checks':[],'duration':round(time.monotonic()-started,2)}
            ready=False
            for _ in range(20):
                response=probe(name,{'method':'GET','path':'/','headers':{}})
                if 'status' in response: ready=True;break
                if time.monotonic()-started>20: break
                time.sleep(.35)
            if not ready:
                logs=command(['logs','--tail','20',name],4)
                return {'error':'Application did not become ready within 20 seconds. Confirm entrypoint and supported dependencies.','diagnostic':redact(logs.stderr+logs.stdout),'checks':[],'duration':round(time.monotonic()-started,2)}
            for i,check in enumerate(checks):
                if time.monotonic()-started>65: raise TimeoutError('Version exceeded the 65-second time budget.')
                observed=probe(name,check)
                results.append({'name':check['name'],'expected_status':check['expected_status'],**assess(check,observed)})
                progress(i+1,len(checks))
            return {'checks':results,'duration':round(time.monotonic()-started,2)}
        except (OSError,subprocess.TimeoutExpired,TimeoutError) as exc:
            return {'error':'Verification interrupted: '+type(exc).__name__,'checks':results,'duration':round(time.monotonic()-started,2)}
        finally:
            try:
                removed=command(['rm','--force',name],10)
                cleanup=removed.returncode==0 or 'No such container' in removed.stderr
            except Exception: cleanup=False
            if not cleanup: raise RuntimeError('Container cleanup failed. Stop Docker and inspect proof-managed containers before retrying.')

def compare(baseline,candidate,checks):
    if baseline.get('error') or candidate.get('error') or len(baseline['checks'])!=len(checks) or len(candidate['checks'])!=len(checks):
        return {'verdict':'inconclusive','summary':'Execution was incomplete. No approval recommendation is available.','rows':[]}
    rows=[]
    for b,c,check in zip(baseline['checks'],candidate['checks'],checks):
        state='unchanged_pass' if b['passed'] and c['passed'] else 'improved' if c['passed'] else 'regression' if b['passed'] else 'persistent_failure'
        rows.append({'name':check['name'],'method':check['method'],'path':check['path'],'transition':state,'baseline':b,'candidate':c})
    failures=sum(not r['candidate']['passed'] for r in rows);regressions=sum(r['transition']=='regression' for r in rows)
    return {'verdict':'blocked' if failures else 'checks_passed','summary':f'{regressions} regression(s); {failures} candidate check(s) failed.' if failures else 'All supplied checks passed on the candidate. Human review is still required.','rows':rows}
