import json
import pytest
from fastapi.testclient import TestClient
import proof.app as module
from proof.models import Review

HEAD={'X-Proof-Client':'dashboard'}
@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'DATA',tmp_path/'data')
    monkeypatch.setattr(module,'runtime',lambda:{'ready':True,'image':'sha256:test'})
    with TestClient(module.app) as client: yield client

def insert(client,verdict='checks_passed',state='completed'):
    p={'requirement':'Check behavior','entrypoint':'app:app','checks':[],'baseline':[],'candidate':[]}
    with module.connect() as db:
        db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('test','review',module.now(),state,'Done',100,json.dumps(p),'fixed-digest',json.dumps({'verdict':verdict,'summary':'test','rows':[],'image':'sha256:test','limitations':[]}),'[]','pending','',''))

@pytest.mark.parametrize('headers',[{}, {'Origin':'https://evil.example','X-Proof-Client':'dashboard'},{'Sec-Fetch-Site':'cross-site','X-Proof-Client':'dashboard'}])
def test_write_boundary(client,headers): assert client.post('/api/reviews/test/decision',json={},headers=headers).status_code==403

def test_untrusted_host_blocked(client): assert client.get('/api/reviews',headers={'Host':'evil.example'}).status_code==403

def test_approval_requires_review_confirmation(client):
    insert(client);assert client.post('/api/reviews/test/decision',json={'action':'approve','reviewed':False},headers=HEAD).status_code==422

@pytest.mark.parametrize('verdict',['blocked','inconclusive'])
def test_failed_checks_cannot_be_approved(client,verdict):
    insert(client,verdict);assert client.post('/api/reviews/test/decision',json={'action':'approve','reviewed':True},headers=HEAD).status_code==409

def test_running_review_cannot_be_approved(client):
    insert(client,state='running');assert client.post('/api/reviews/test/decision',json={'action':'approve','reviewed':True},headers=HEAD).status_code==409

def test_decision_is_immutable_and_bound_to_hash(client):
    insert(client);r=client.post('/api/reviews/test/decision',json={'action':'approve','reviewed':True,'note':'Reviewed'},headers=HEAD)
    assert r.status_code==200 and r.json()['decision']=='approved' and r.json()['digest']=='fixed-digest'
    assert client.post('/api/reviews/test/decision',json={'action':'reject','reviewed':True},headers=HEAD).status_code==409

def test_report_is_real_download(client):
    insert(client);r=client.get('/api/reviews/test/report');assert r.status_code==200 and 'fixed-digest' in r.text and 'No merge' in r.text

def test_no_partial_report(client):
    insert(client,state='running');assert client.get('/api/reviews/test/report').status_code==409

def test_unknown_review(client): assert client.get('/api/reviews/unknown').status_code==404

def test_database_permissions(client):
    assert module.DATA.stat().st_mode&0o777==0o700
    assert (module.DATA/'proof.db').stat().st_mode&0o777==0o600

def test_restart_marks_interrupted_review_failed(client):
    insert(client,state='running');module.initialize();assert client.get('/api/reviews/test').json()['state']=='failed'

def test_image_missing_fails_closed(client,monkeypatch):
    p=json.loads((module.BASE/'examples/relay-change.json').read_text());monkeypatch.setattr(module,'runtime',lambda:{'ready':False,'message':'No image'})
    assert client.post('/api/reviews',json=p,headers=HEAD).status_code==503

def test_html_boundary_headers(client):
    r=client.get('/');assert r.status_code==200;assert "frame-ancestors 'none'" in r.headers['content-security-policy'];assert r.headers['x-frame-options']=='DENY'

def test_rejection_is_recorded_correctly(client):
    insert(client,'blocked');r=client.post('/api/reviews/test/decision',json={'action':'reject','reviewed':True,'note':'A real regression'},headers=HEAD)
    assert r.status_code==200 and r.json()['decision']=='rejected'

def test_duplicate_check_names_rejected(client):
    p=json.loads((module.BASE/'examples/relay-change.json').read_text());p['checks'].append(p['checks'][0]);assert client.post('/api/reviews',json=p,headers=HEAD).status_code==422

def test_execution_consent_required(client):
    p=json.loads((module.BASE/'examples/relay-change.json').read_text());p['execution_consent']=False;assert client.post('/api/reviews',json=p,headers=HEAD).status_code==422
