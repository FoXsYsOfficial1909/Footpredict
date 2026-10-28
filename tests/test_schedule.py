import unittest
from datetime import datetime,timezone

from scripts.overnight import render_report


class ScheduleTests(unittest.TestCase):
    def test_report_uses_moscow_timezone_and_actual_outcomes(self):
        state={'started_at':'2026-10-27T18:40:05Z','report_at':'2026-10-28T04:50:00Z','stop_at':'2026-10-28T05:00:00Z',
               'updates':[{'message':'Dataset validated'}],'tests':[{'name':'Temporal split','status':'passed','evidence':'Future results ignored'}],
               'jobs':[{'id':'training','status':'running','successes':0}], 'blockers':['AI access not confirmed']}
        report=render_report(state,'Срез на 07:50',datetime(2026,10,28,4,50,tzinfo=timezone.utc))
        self.assertIn('28.10.2026 07:50',report)
        self.assertIn('Temporal split: **passed**',report)
        self.assertIn('AI access not confirmed',report)
        self.assertIn('| training | running | 0 |',report)


if __name__=='__main__':unittest.main()
