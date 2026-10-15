import pytest
from pydantic import ValidationError
from proof.models import Source,Check,Review,digest
from proof.engine import subset,assess,compare,diff_files,container_args

@pytest.mark.parametrize('path',['../app.py','/app.py','a//b.py','a/./b.py','.env','data/account.json','.git/config.py','a\\b.py','file.db','venv/a.py'])
def test_rejects_private_and_unsafe_paths(path):
    with pytest.raises(ValidationError): Source(path=path,content='x')

def test_valid_source(): assert Source(path='api/main.py',content='x').path=='api/main.py'
@pytest.mark.parametrize('path',['https://example.com','//example.com','/a\r\nx: y'])
def test_probe_cannot_change_destination(path):
    with pytest.raises(ValidationError): Check(name='x',path=path,expected_status=200)
@pytest.mark.parametrize('headers',[{'Host':'evil.com'},{'X-Test':'a\r\nb'},{'Content-Length':'999'}])
def test_rejects_transport_injection(headers):
    with pytest.raises(ValidationError): Check(name='x',path='/',expected_status=200,headers=headers)

def test_json_assertions_do_not_confuse_bool_and_number():
    assert not subset({'ok':True},{'ok':1})
    assert subset({'nested':{'a':2}},{'nested':{'a':2,'extra':3}})

def test_status_pass_does_not_hide_json_failure():
    assert not assess({'expected_status':200,'expected_json':{'ok':True}},{'status':200,'body':'{"ok":false}'})['passed']

def test_invalid_json_fails_assertion():
    assert not assess({'expected_status':200,'expected_json':{}},{'status':200,'body':'bad'})['passed']

def test_network_error_is_not_success():
    assert not assess({'expected_status':200},{'error':'timeout'})['passed']

def test_change_diff_contains_actual_lines():
    result=diff_files([{'path':'app.py','content':'old\n'}],[{'path':'app.py','content':'new\n'}]);assert '-old' in result[0]['patch'] and '+new' in result[0]['patch']

def test_comparison_classifies_all_transitions():
    b={'checks':[{'passed':v} for v in [True,False,True,False]]};c={'checks':[{'passed':v} for v in [True,True,False,False]]};checks=[{'name':str(i),'method':'GET','path':'/'} for i in range(4)]
    r=compare(b,c,checks);assert r['verdict']=='blocked';assert [x['transition'] for x in r['rows']]==['unchanged_pass','improved','regression','persistent_failure']

def test_missing_checks_are_inconclusive():
    assert compare({'checks':[]},{'checks':[]},[{'name':'required'}])['verdict']=='inconclusive'

def test_startup_error_is_inconclusive():
    assert compare({'checks':[],'error':'startup failed'},{'checks':[]},[])['verdict']=='inconclusive'

def test_all_candidate_checks_pass():
    checks=[{'name':'x','method':'GET','path':'/'}];assert compare({'checks':[{'passed':False}]},{'checks':[{'passed':True}]},checks)['verdict']=='checks_passed'

def test_runner_boundary_has_no_host_socket_or_network():
    args=container_args('proof-test','/tmp/source','sha256:abc','app:app')
    for item in ['--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--memory=256m','--pids-limit=64','65534:65534']: assert item in args
    assert not any('docker.sock' in x for x in args)

def test_input_hash_covers_checks(): assert digest({'checks':[200]})!=digest({'checks':[401]})

def test_duplicate_paths_and_missing_entrypoint_rejected():
    p={'title':'x','requirement':'A required behavior','entrypoint':'app:app','baseline':[{'path':'app.py','content':''}],'candidate':[{'path':'app.py','content':''}],'checks':[{'name':'x','path':'/','expected_status':200}],'execution_consent':True}
    Review(**p)
    p['baseline']*=2
    with pytest.raises(ValidationError): Review(**p)
    p['baseline']=[{'path':'other.py','content':''}]
    with pytest.raises(ValidationError): Review(**p)
