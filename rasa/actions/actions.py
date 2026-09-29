import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from rasa_sdk import Action
from rasa_sdk.events import ActionExecuted, ActiveLoop, ConversationPaused, FollowupAction, Restarted, SlotSet


class ActionGuardedRestart(Action):
    def name(self):
        return 'action_restart'

    def run(self, dispatcher, tracker, domain):
        # Rasa allows /restart even when paused. Only completed handoffs may reset.
        if tracker.get_slot('handoff_ticket_id'):
            return [FollowupAction('action_listen')]
        url=os.environ['HANDOFF_URL'].rstrip('/')+'/conversations/'+urllib.parse.quote(tracker.sender_id,safe='')
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+os.environ['HANDOFF_TOKEN']})
        try:
            with urllib.request.urlopen(request,timeout=5) as response:
                ownership=json.load(response)
        except (OSError,TimeoutError,ValueError):
            return [FollowupAction('action_listen')]
        return [Restarted()] if ownership.get('state')=='bot' else [FollowupAction('action_listen')]


class ActionRequestHandoff(Action):
    def name(self):
        return 'action_request_handoff'

    def run(self, dispatcher, tracker, domain):
        message=tracker.latest_message
        intent=message.get('intent') or {}
        # Rasa supplies message_id; event timestamp fallback keeps retries stable.
        user_events=[e for e in tracker.events if e.get('event')=='user']
        last=user_events[-1] if user_events else {}
        event_id=message.get('message_id') or last.get('message_id') or hashlib.sha256(json.dumps([tracker.sender_id,last.get('timestamp'),message.get('text')],sort_keys=True).encode()).hexdigest()
        context=[]
        for event in tracker.events:
            if event is last: break
            if event.get('event') in ('user','bot') and event.get('text'):
                context.append({'role':'user' if event['event']=='user' else 'assistant','text':event['text'][:2000]})
        payload={'conversation':tracker.sender_id,'event_id':event_id,'text':message.get('text') or 'Please connect me to support',
                 'intent':intent.get('name') or 'nlu_fallback','confidence':intent.get('confidence',0.0),
                 'active_form':(tracker.active_loop or {}).get('name'),'context':context[-20:]}
        request=urllib.request.Request(os.environ['HANDOFF_URL'].rstrip('/')+'/handoffs',data=json.dumps(payload).encode(),
            headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['HANDOFF_TOKEN']})
        try:
            with urllib.request.urlopen(request,timeout=5) as response:
                result=json.load(response)
        except (urllib.error.URLError,TimeoutError,ValueError):
            dispatcher.utter_message(text='The support connection is unavailable. Your conversation is still with the assistant; please retry.')
            return []
        if result['state']=='human':
            dispatcher.utter_message(text='The simulated human desk accepted your request. The assistant is paused until the ticket is completed.')
            # RulePolicy recognizes /restart only after action_listen. Record it
            # before another message can trigger fallback and rewind the pause.
            return [SlotSet('handoff_ticket_id',result['ticket_id']),ActiveLoop(None),ConversationPaused(),
                    ActionExecuted('action_listen')]
        if result['state']=='pending':
            dispatcher.utter_message(text='Your request is queued, but no person has accepted it yet. Please retry the human request.')
        return []
