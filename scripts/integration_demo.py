"""Exercise a real local Rasa 2 server and synthetic human desk; no real messages."""
import json
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def credentials():
    return dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if '=' in line)


def request(path, body=None, service='coordinator'):
    env=credentials()
    port=4340 if service=='coordinator' else 4341
    headers={'Content-Type':'application/json'}
    if service=='coordinator':
        headers['Authorization']='Bearer '+env['HANDOFF_TOKEN']
    else:
        path+=('&' if '?' in path else '?')+urllib.parse.urlencode({'token':env['RASA_TOKEN']})
    req=urllib.request.Request('http://127.0.0.1:'+str(port)+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    with urllib.request.urlopen(req,timeout=30) as response:
        return json.load(response)


def main():
    sender='demo-'+uuid.uuid4().hex[:10]
    events=[]
    def say(text):
        result=request('/webhooks/rest/webhook',{'sender':sender,'message':text},'rasa')
        events.append({'sent':text,'responses':result})
        return result
    say('/order_status')
    tracker=request('/conversations/'+sender+'/tracker',service='rasa')
    assert tracker['active_loop']['name']=='order_form',tracker
    say('/request_human')
    tracker=request('/conversations/'+sender+'/tracker',service='rasa')
    assert tracker['paused'] is True,tracker
    ticket=tracker['slots']['handoff_ticket_id']
    subprocess.run(['docker','compose','restart','rasa'],cwd=ROOT,check=True,capture_output=True)
    for attempt in range(60):
        try:
            status=request('/status',service='rasa')
            if status.get('model_file'): break
        except (OSError,TimeoutError):
            pass
        if attempt==59: raise RuntimeError('Rasa did not recover after restart')
        time.sleep(2)
    tracker=request('/conversations/'+sender+'/tracker',service='rasa')
    assert tracker['paused'] is True,'Rasa restart lost human ownership'
    assert tracker['slots']['handoff_ticket_id']==ticket,'Rasa restart lost ticket slot'
    assert say('Can you help me while I wait?')==[], 'Bot replied during human ownership'
    for _ in range(2):
        assert say('/restart')==[], 'Restart must stay silent during human ownership'
        tracker=request('/conversations/'+sender+'/tracker',service='rasa')
        assert tracker['paused'] is True,'Restart bypassed human ownership'
        assert tracker['slots']['handoff_ticket_id']==ticket,'Restart cleared active ticket'
    result=request('/tickets/'+ticket+'/complete',{'event_id':'complete-'+sender})
    assert result['resume_form']=='order_form',result
    tracker=request('/conversations/'+sender+'/tracker',service='rasa')
    assert tracker['paused'] is False,tracker
    assert tracker['active_loop']['name']=='order_form',tracker
    responses=say('ORDER-1001')
    assert any('being prepared' in r.get('text','') for r in responses),responses
    # The same completion event is a replay, not another resume.
    assert request('/tickets/'+ticket+'/complete',{'event_id':'complete-'+sender})==result
    say('/restart')
    tracker=request('/conversations/'+sender+'/tracker',service='rasa')
    assert tracker['slots']['order_id'] is None,'Completed bot-owned conversation cannot restart'
    assert tracker['paused'] is False,tracker
    # An unavailable desk must never pause Rasa or claim that a human accepted.
    request('/demo/desk',{'available':False})
    try:
        sender='failed-'+uuid.uuid4().hex[:10]
        say('/request_human')
        tracker=request('/conversations/'+sender+'/tracker',service='rasa')
        assert not tracker['paused'],tracker
        request('/demo/desk',{'available':True})
        say('/request_human')
        tracker=request('/conversations/'+sender+'/tracker',service='rasa')
        assert tracker['paused'],tracker
    finally:
        request('/demo/desk',{'available':True})
    report={'executed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'rasa_version':'2.8.14',
            'checks':['form interrupted','human acknowledgement before pause','paused state survives Rasa restart','bot stays silent while human owns conversation','restart cannot bypass human ownership','authenticated completion','form resumed','order completed','completion idempotent','restart works after completion','desk failure stays unpaused','retry succeeds'],
            'events':events,'status':'passed'}
    (ROOT/'evidence/integration-demo.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':'passed','checks':report['checks']},indent=2))

if __name__=='__main__':main()
