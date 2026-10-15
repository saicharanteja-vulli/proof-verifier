import json
import pytest
from fastapi.testclient import TestClient
import proof.app as module
from proof.models import SourceInput, Review
from proof.generator import context, Proposal
HEAD={'X-Proof-Client':'dashboard'}
@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'DATA',tmp_path/'data')
    with TestClient(module.app) as c: yield c

def source():
    p=json.loads((module.BASE/'examples/relay-change.json').read_text())
    return {k:p.get(k) for k in SourceInput.model_fields}

def fake(p):
    return {'checks':[{'name':'Private account','method':'GET','path':'/api/me','expected_status':401}], 'rationale':'Preserve authentication','limitations':['No session checks'],'model':'test','context_note':'AST'}

def test_generation_never_runs_source(client,monkeypatch):
    monkeypatch.setattr(module.generator,'generate',fake)
    monkeypatch.setattr(module,'runtime',lambda:pytest.fail('Generation called Docker'))
    r=client.post('/api/proposals',json=source(),headers=HEAD)
    assert r.status_code==201
    assert client.get('/api/reviews').json()==[]
    assert client.get('/api/proposals/'+r.json()['id']).json()['checks']==fake(None)['checks']

def test_review_gate_and_stale_inputs(client,monkeypatch):
    monkeypatch.setattr(module.generator,'generate',fake)
    d=client.post('/api/proposals',json=source(),headers=HEAD).json()
    p={**source(),'checks':d['checks'],'proposal_id':d['id'],'execution_consent':True}
    assert client.post('/api/reviews',json=p,headers=HEAD).status_code==422
    p['tests_reviewed']=True;p['requirement']+=' Changed.'
    assert client.post('/api/reviews',json=p,headers=HEAD).status_code==409
    assert client.get('/api/reviews').json()==[]

def test_reviewed_edits_are_sealed(client,monkeypatch):
    monkeypatch.setattr(module.generator,'generate',fake)
    monkeypatch.setattr(module,'runtime',lambda:{'ready':True})
    # Avoid real execution, release the reserved slot as the worker would.
    monkeypatch.setattr(module.POOL,'submit',lambda *a:module.SLOTS.release())
    d=client.post('/api/proposals',json=source(),headers=HEAD).json()
    checks=[{**d['checks'][0],'name':'Reviewer edited name'}]
    p={**source(),'checks':checks,'proposal_id':d['id'],'execution_consent':True,'tests_reviewed':True}
    r=client.post('/api/reviews',json=p,headers=HEAD)
    assert r.status_code==202
    saved=client.get('/api/reviews/'+r.json()['id']).json()
    assert saved['checks'][0]['name']=='Reviewer edited name'
    assert saved['proposal_id']==d['id'] and saved['tests_reviewed']
    assert any(d['id'] in e['message'] for e in saved['events'])

def test_model_failure_fails_closed(client,monkeypatch):
    def fail(p): raise ValueError('Invalid model output')
    monkeypatch.setattr(module.generator,'generate',fail)
    assert client.post('/api/proposals',json=source(),headers=HEAD).status_code==503
    assert client.get('/api/reviews').json()==[]

def test_busy_generation_releases_lock(client,monkeypatch):
    module.generator.LOCK.acquire()
    try: assert client.post('/api/proposals',json=source(),headers=HEAD).status_code==429
    finally: module.generator.LOCK.release()

def test_context_does_not_execute_source(tmp_path):
    marker=tmp_path/'bad'
    code=f"open({str(marker)!r},'w').write('executed')\n@app.get('/health')\ndef health(): return {{'ok':True}}"
    assert '/health' in context([{'path':'app.py','content':code}])
    assert not marker.exists()

def test_invalid_generated_request_rejected():
    with pytest.raises(ValueError): Proposal.model_validate({'checks':[{'name':'Bad','path':'https://evil.com','expected_status':200}],'rationale':'test','limitations':['test']})

def test_route_inventory_preserves_http_methods():
    from proof.generator import route_inventory
    routes=route_inventory([{'path':'app.py','content':"@app.post('/login')\ndef login(): pass\n@app.get('/health')\ndef health(): pass"}])
    assert routes==[{'method':'POST','path':'/login','function':'login'},{'method':'GET','path':'/health','function':'health'}]

def test_missing_proposal_rejected(client):
    p={**source(),'checks':fake(None)['checks'],'proposal_id':'0'*32,'tests_reviewed':True,'execution_consent':True}
    assert client.post('/api/reviews',json=p,headers=HEAD).status_code==404
