import unittest
from handoff.server import resume_events

class EventTests(unittest.TestCase):
    def test_resume_clears_handoff_and_restores_interrupted_form(self):
        events=resume_events({'resume_form':'order_form'})
        self.assertEqual(events[0],{'event':'resume'})
        self.assertEqual(events[1],{'event':'slot','name':'handoff_ticket_id','value':None})
        self.assertEqual(events[-1],{'event':'action','name':'action_listen'})

    def test_no_form_does_not_schedule_one(self):
        events=resume_events({'resume_form':None})
        self.assertEqual(len(events),3)
        self.assertEqual(events[-1],{'event':'action','name':'action_listen'})
