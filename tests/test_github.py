import io,json,zipfile,pytest
from fastapi.testclient import TestClient
import proof.app as app
from proof import github
from proof.models import digest
HEAD={'X-Proof-Client':'dashboard'}

def archive(entries):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for path,text in entries.items(): z.writestr('repo-sha/'+path,text)
    return b.getvalue()

def test_archive_filters_hidden_and_keeps_source():
    files=github.decode_archive(archive({'app.py':'app=1','.env':'secret','.github/workflows/run.yml':'unsafe','data/users.json':'secret'}))
    assert files==[{'path':'app.py','content':'app=1'}]

def test_archive_does_not_extract_traversal():
    assert github.decode_archive(archive({'../../escape.py':'bad','app.py':'ok'}))==[{'path':'app.py','content':'ok'}]

def test_archive_rejects_large_file():
    with pytest.raises(ValueError): github.decode_archive(archive({'app.py':'a'*150001}))

@pytest.mark.parametrize('url',['http://github.com/a/b/pull/1','https://evil.com/a/b/pull/1','https://github.com/a/b/pull/1?token=abc','https://github.com/a/b/pull/0'])
def test_only_public_github_pr_url(url):
    with pytest.raises(ValueError): github.pr(url)

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(app,'DATA',tmp_path/'data')
    with TestClient(app.app) as c: yield c

def fixture():
    p=json.loads((app.BASE/'examples/relay-change.json').read_text())
    meta={'url':'https://github.com/a/b/pull/1','base_sha':'a'*40,'head_sha':'b'*40,'baseline':p['baseline'],'candidate':p['candidate'],'source_digest':digest([p['baseline'],p['candidate']])}
    return p,meta

def test_import_never_executes_and_requires_review(client,monkeypatch):
    p,meta=fixture();monkeypatch.setattr(github,'import_pr',lambda _:meta)
    monkeypatch.setattr(app,'runtime',lambda:pytest.fail('Import attempted execution'))
    r=client.post('/api/github/import',json={'url':meta['url']},headers=HEAD)
    assert r.status_code==201 and client.get('/api/reviews').json()==[]
    p['github_import_id']=r.json()['github_import_id']
    assert client.post('/api/reviews',json=p,headers=HEAD).status_code==422

def test_stale_head_and_edited_snapshot_block_execution(client,monkeypatch):
    p,meta=fixture();monkeypatch.setattr(github,'import_pr',lambda _:meta)
    iid=client.post('/api/github/import',json={'url':meta['url']},headers=HEAD).json()['github_import_id']
    p.update(github_import_id=iid,tests_reviewed=True)
    monkeypatch.setattr(github,'pr',lambda _:('a/b',1,{'base':{'sha':'a'*40},'head':{'sha':'c'*40}}))
    assert client.post('/api/reviews',json=p,headers=HEAD).status_code==409
    p['candidate'][0]['content']='changed'
    assert client.post('/api/reviews',json=p,headers=HEAD).status_code==409
    assert client.get('/api/reviews').json()==[]
