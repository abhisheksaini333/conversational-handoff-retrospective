"""Authenticated loopback demo service. No real human service is contacted."""
import hmac
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from handoff.core import Coordinator, canonical, identifier


class SimulatedDesk:
    """Durable acknowledgement keyed by ticket ID, with explicit fault injection."""
    def __init__(self, database):
        self.database = database
        self.available = True
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE IF NOT EXISTS desk_tickets (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def accept(self, ticket):
        if not self.available:
            raise ConnectionError('simulated desk unavailable')
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT OR IGNORE INTO desk_tickets VALUES (?,?)',(ticket['id'],canonical(ticket)))
        return {'accepted':True,'ticket_id':ticket['id']}


def resume_events(result):
    events=[{'event':'resume'},{'event':'slot','name':'handoff_ticket_id','value':None}]
    if result['resume_form']:
        events.append({'event':'active_loop','name':result['resume_form']})
    # Rasa 2 forms only validate the next user input after action_listen.
    events.append({'event':'action','name':'action_listen'})
    return events


def make_server(core, token, port=4340, host='127.0.0.1', rasa_url=None, rasa_token=None):
    if not token or len(token)<16:
        raise ValueError('HANDOFF_TOKEN must contain at least 16 characters')
    desk=SimulatedDesk(core.database)

    def resume(result):
        if not rasa_url:
            return
        url=rasa_url.rstrip('/')+'/conversations/'+urllib.parse.quote(result['conversation'],safe='')+'/tracker/events'
        headers={'Content-Type':'application/json'}
        if rasa_token:
            # Rasa 2 HTTP API accepts its configured authentication token.
            url += '?'+urllib.parse.urlencode({'token':rasa_token})
        request=urllib.request.Request(url,data=canonical(resume_events(result)).encode(),headers=headers)
        with urllib.request.urlopen(request,timeout=3) as response:
            if response.status!=200:
                raise ConnectionError('resume rejected')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Avoid logging tokens, transcript text or query strings.

        def reply(self,code,payload):
            raw=canonical(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(raw)

        def authorized(self):
            supplied=self.headers.get('Authorization','')
            if not hmac.compare_digest(supplied.encode(),('Bearer '+token).encode()):
                self.reply(401,{'error':'authentication required'})
                return False
            return True

        def do_GET(self):
            if self.path=='/health':
                self.reply(200,{'status':'ok','mode':'synthetic-demo'})
            elif self.authorized():
                if self.path=='/tickets': self.reply(200,{'tickets':core.tickets()})
                elif self.path.startswith('/conversations/'):
                    try:
                        conversation=urllib.parse.unquote(self.path[len('/conversations/'):])
                        identifier(conversation,'conversation')
                        state=core.conversation(conversation)
                        self.reply(200,{'state':state['state'] if state else 'bot'})
                    except ValueError:
                        self.reply(400,{'error':'invalid conversation'})
                    except sqlite3.Error:
                        self.reply(503,{'error':'storage unavailable'})
                else: self.reply(404,{'error':'not found'})

        def do_POST(self):
            if not self.authorized(): return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:
                    self.reply(413,{'error':'body must be between 1 and 65536 bytes'});return
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict): raise ValueError('body must be an object')
                if self.path=='/handoffs':
                    if set(body)-{'conversation','event_id','text','intent','confidence','active_form','context'}:
                        raise ValueError('unknown request field')
                    if not {'conversation','event_id','text','intent','confidence'}<=set(body):
                        raise ValueError('missing message fields')
                    result=core.message(**body)
                    if result['ticket_id']:
                        try:
                            state=core.dispatch(result['ticket_id'],desk)
                            # A duplicated old event must not pause a newer conversation.
                            if state['ticket_id']==result['ticket_id']:
                                result.update(state=state['state'])
                            else:
                                result.update(state=state['state'],ticket_id=state['ticket_id'])
                        except ConnectionError:
                            self.reply(202,result);return
                    self.reply(200,result)
                elif self.path=='/demo/desk':
                    if set(body)!={'available'} or not isinstance(body['available'],bool):
                        raise ValueError('available must be boolean')
                    desk.available=body['available']
                    self.reply(200,{'available':desk.available})
                elif self.path.startswith('/tickets/') and self.path.endswith('/complete'):
                    if set(body)!={'event_id'}: raise ValueError('event_id is required')
                    ticket_id=self.path[len('/tickets/'):-len('/complete')]
                    result=core.complete(ticket_id,body['event_id'],before_resume=resume)
                    self.reply(200,result)
                else:
                    self.reply(404,{'error':'not found'})
            except (ValueError,TypeError,UnicodeDecodeError):
                self.reply(400,{'error':'invalid request or state transition'})
            except KeyError:
                self.reply(404,{'error':'ticket not found'})
            except (urllib.error.URLError,ConnectionError,TimeoutError):
                self.reply(503,{'error':'resume unavailable; ownership remains with human; retry the same event'})
            except sqlite3.Error:
                self.reply(503,{'error':'storage unavailable'})

    return ThreadingHTTPServer((host,port),Handler)


if __name__=='__main__':
    path=Path(os.environ.get('HANDOFF_DB','var/handoff.sqlite3'))
    path.parent.mkdir(parents=True,exist_ok=True)
    server=make_server(Coordinator(str(path)),os.environ.get('HANDOFF_TOKEN',''),
                       host=os.environ.get('HANDOFF_HOST','127.0.0.1'),
                       port=int(os.environ.get('HANDOFF_PORT','4340')),
                       rasa_url=os.environ.get('RASA_URL'),rasa_token=os.environ.get('RASA_TOKEN'))
    print('Synthetic handoff service ready on port '+str(server.server_port),flush=True)
    server.serve_forever()
