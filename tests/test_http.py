import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from handoff.core import Coordinator
from handoff.server import make_server


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Coordinator(str(Path(self.tmp.name)/'db.sqlite3'))
        self.token = 'test-only-credential-not-a-secret'
        self.server = make_server(self.core, self.token, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, body=None, token=True):
        headers = {'Content-Type':'application/json'}
        if token: headers['Authorization']='Bearer '+self.token
        request = urllib.request.Request(self.url+path, headers=headers, data=json.dumps(body).encode() if body is not None else None)
        try: response=urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error: response=error
        return response.status,json.load(response)

    def payload(self):
        return dict(conversation='c1',event_id='m1',text='please help',intent='request_human',confidence=.9,active_form='order_form')

    def test_no_token_denied(self):
        self.assertEqual(self.request('/tickets',token=False)[0],401)

    def test_conversation_ownership_requires_auth_and_tracks_completion(self):
        self.assertEqual(self.request('/conversations/c1',token=False)[0],401)
        self.assertEqual(self.request('/conversations/c1')[1]['state'],'bot')
        _, result=self.request('/handoffs',self.payload())
        self.assertEqual(self.request('/conversations/c1')[1]['state'],'human')
        self.request('/tickets/'+result['ticket_id']+'/complete',{'event_id':'complete1'})
        self.assertEqual(self.request('/conversations/c1')[1]['state'],'bot')

    def test_handoff_and_resume(self):
        code, result=self.request('/handoffs',self.payload())
        self.assertEqual(code,200)
        self.assertEqual(result['state'],'human')
        code, result=self.request('/tickets/'+result['ticket_id']+'/complete',{'event_id':'complete1'})
        self.assertEqual(code,200)
        self.assertEqual(result['resume_form'],'order_form')

    def test_unavailable_desk_does_not_claim_human_ownership(self):
        self.request('/demo/desk',{'available':False})
        code,result=self.request('/handoffs',self.payload())
        self.assertEqual(code,202)
        self.assertEqual(result['state'],'pending')
        self.request('/demo/desk',{'available':True})
        code,result=self.request('/handoffs',self.payload())
        self.assertEqual(result['state'],'human')

    def test_invalid_confidence_is_400(self):
        body=self.payload();body['confidence']=-10
        self.assertEqual(self.request('/handoffs',body)[0],400)

    def test_unknown_ticket_does_not_expose_traceback(self):
        code,body=self.request('/tickets/missing/complete',{'event_id':'x'})
        self.assertEqual(code,404)
        self.assertNotIn('Traceback',json.dumps(body))

    def test_unknown_fields_rejected(self):
        body=self.payload();body['database']='other'
        self.assertEqual(self.request('/handoffs',body)[0],400)
