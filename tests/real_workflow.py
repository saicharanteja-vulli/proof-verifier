"""Opt-in real Docker smoke test. Run with Proof serving locally on port 8100."""
import copy
import json
from pathlib import Path
import time
import httpx

root=Path(__file__).resolve().parents[1]
base=json.loads((root/'examples/relay-change.json').read_text())
client=httpx.Client(base_url='http://127.0.0.1:8100',headers={'X-Proof-Client':'dashboard'},timeout=20)

def run(p):
    response=client.post('/api/reviews',json=p);response.raise_for_status();rid=response.json()['id']
    deadline=time.monotonic()+160
    while time.monotonic()<deadline:
        r=client.get('/api/reviews/'+rid).json()
        if r['state'] in {'completed','failed'}: return r
        time.sleep(.5)
    raise AssertionError('Review did not finish in time')

broken=copy.deepcopy(base)
broken['title']='Relay · authentication regression control'
broken['baseline']=copy.deepcopy(base['candidate'])
broken['requirement']='Control experiment: deliberately remove authentication from /api/me in a source copy. Proof must flag the resulting unauthorized access regression.'
for f in broken['candidate']:
    if f['path']=='relay/app.py':
        old="def me(u=Depends(human)): return {'username':u['username'],'csrf':u['csrf']}"
        assert old in f['content']
        f['content']=f['content'].replace(old,"def me(): return {'username':'unprotected-test-account'}")
r=run(broken)
assert r['state']=='completed',r
assert r['report']['verdict']=='blocked',r['report']
regressions=[x for x in r['report']['rows'] if x['transition']=='regression']
assert len(regressions)==1 and regressions[0]['path']=='/api/me'
assert regressions[0]['baseline']['status']==401 and regressions[0]['candidate']['status']==200
assert client.post('/api/reviews/'+r['id']+'/decision',json={'action':'approve','reviewed':True}).status_code==409
report=client.get('/api/reviews/'+r['id']+'/report?format=json');report.raise_for_status()
(root/'data/real-regression-report.json').write_text(report.text)
print(json.dumps({'review':r['id'],'verdict':r['report']['verdict'],'regressions':len(regressions),'unauthorized_before':401,'unauthorized_after':200,'approval_blocked':True}))
