import copy
from datetime import timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from research.hundred import DIRECTORY,REGISTERED_SHA,verify_files,instant,settle,reproduce,can_finalize
from scripts.publish_hundred import compact


class HundredTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.packet=verify_files()

    def game(self,**changes):
        f=self.packet['fixtures'][0]
        return {'id':f['fixtureId'],'utcDate':f['kickoff'],'status':'FINISHED',
            'homeTeam':{'id':f['homeId'],'name':f['home']},'awayTeam':{'id':f['awayId'],'name':f['away']},
            'score':{'duration':'REGULAR','fullTime':{'home':0,'away':1}},**changes}

    def response(self,game):
        code=self.packet['fixtures'][0]['league']
        return {code:{'competition':{'code':code},'filters':{'season':2026},'matches':[game]}}

    def test_all_hundred_are_independently_reproduced_and_information_precedes_registration(self):
        p=self.packet
        self.assertEqual(len(p['fixtures']),100)
        self.assertEqual(len({f['fixtureId'] for f in p['fixtures']}),100)
        self.assertEqual({code:sum(f['league']==code for f in p['fixtures']) for code in p['leagues']},
            {'BSA':26,'ELC':36,'PD':18,'PL':20})
        self.assertTrue(all(instant(f['kickoff'])-instant(p['frozenAt'])>=timedelta(hours=24) for f in p['fixtures']))

    def test_changed_forecast_or_input_bytes_cannot_be_accepted_as_the_original(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'cohort';shutil.copytree(DIRECTORY,path)
            body=(path/'packet.json').read_text();(path/'packet.json').write_text(body+' ')
            with self.assertRaisesRegex(ValueError,'packet changed'):verify_files(path)
            (path/'packet.json').write_text(body)
            (path/'inputs/PL.json').write_text('[]')
            with self.assertRaisesRegex(ValueError,'input bytes'):verify_files(path)
        f=self.packet['fixtures'][0];inputs=json.loads((DIRECTORY/'inputs'/(f['league']+'.json')).read_text())
        inputs[0]['utcDate']=f['kickoff']
        with self.assertRaisesRegex(ValueError,'fence'):reproduce(f,inputs,self.packet)

    def test_actual_result_waits_for_four_hour_fence_and_uses_the_same_paired_denominator(self):
        p=copy.deepcopy(self.packet);before=copy.deepcopy(p);f=p['fixtures'][0];game=self.game()
        early=settle(p,self.response(game),instant(f['kickoff'])+timedelta(hours=3))
        self.assertEqual(early['counts']['settled'],0);self.assertIsNone(early['model']['accuracy'])
        report=settle(p,self.response(game),instant(f['kickoff'])+timedelta(hours=5))
        self.assertEqual(report['model']['n'],1);self.assertEqual(report['baseline']['n'],1)
        self.assertEqual(report['fixtures'][0]['outcome'],2);self.assertEqual(p,before)
        self.assertFalse(report['forecastsChanged'])
        self.assertNotIn('probabilities',compact(report)['fixtures'][0])

    def test_moved_identity_postponed_and_unverified_scores_stay_out_of_metrics(self):
        f=self.packet['fixtures'][0];now=instant(f['kickoff'])+timedelta(hours=5)
        for game in [self.game(utcDate=(now+timedelta(days=1)).isoformat()),self.game(status='POSTPONED'),
            self.game(homeTeam={'id':999,'name':'Other'}),self.game(score={'duration':'EXTRA_TIME','fullTime':{'home':2,'away':1}})]:
            report=settle(self.packet,self.response(game),now)
            self.assertEqual(report['fixtures'][0]['status'],'review');self.assertEqual(report['model']['n'],0)
            restored=settle(self.packet,self.response(self.game()),now,compact(report))
            self.assertEqual(restored['fixtures'][0]['status'],'review')

    def test_missing_future_or_invalid_provider_data_cannot_look_like_a_correct_prediction(self):
        f=self.packet['fixtures'][0]
        future=settle(self.packet,self.response(self.game()),instant(self.packet['frozenAt']))
        self.assertEqual(future['counts']['settled'],0);self.assertIsNone(future['model']['brier'])
        response=self.response(self.game());response[f['league']]['filters']['season']=2025
        with self.assertRaisesRegex(ValueError,'season'):settle(self.packet,response,instant(f['kickoff']))

    def test_provider_checks_stop_only_after_the_complete_cohort_and_correction_window(self):
        end=max(instant(f['kickoff']) for f in self.packet['fixtures'])
        previous={'status':'complete','packetSha256':REGISTERED_SHA}
        self.assertFalse(can_finalize(self.packet,previous,end+timedelta(days=13)))
        self.assertTrue(can_finalize(self.packet,previous,end+timedelta(days=15)))
        self.assertFalse(can_finalize(self.packet,{**previous,'status':'active'},end+timedelta(days=15)))


if __name__=='__main__':unittest.main()
