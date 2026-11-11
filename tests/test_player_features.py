import copy
import unittest

from research.features import feature_row
from tests.test_features import snapshot


def player_packet():
    p=snapshot();f=p['fixture'];time=p['collectedAt']
    def player(i,status,minutes,old=False):
        return {'playerId':i,'player':f'Player {i}','position':3,'status':status,'seasonMinutes':minutes,
            'newsPublishedAt':'2026-09-01T12:00:00Z' if old else '2026-10-27T12:00:00Z',
            'newsFreshness':'stale' if old else 'recent'}
    home=[player(1,'available',600),player(2,'injured',300),player(3,'doubtful',100,True)]
    away=[player(4,'available',1000)]
    p['playerAvailability']={'status':'available','sourceType':'official_fpl_metadata','fetchedAt':time,
        'coverage':{'fixtureMatched':True},'fixtureMatch':{'home':f['home'],'away':f['away'],'kickoff':f['kickoff'],
            'validation':'exact_clubs_and_utc_kickoff'},'teams':[
        {'team':f['home'],'registeredPlayerCount':3,'roster':home,'reports':[
            {'playerId':2,'team':f['home'],'fetchedAt':time,'publishedAt':'2026-10-27T12:00:00Z','stale':False},
            {'playerId':3,'team':f['home'],'fetchedAt':time,'publishedAt':'2026-09-01T12:00:00Z','stale':True}]},
        {'team':f['away'],'registeredPlayerCount':1,'roster':away,'reports':[]}]}
    return p


class PlayerFeatureTests(unittest.TestCase):
    def test_dated_minutes_weight_reports_and_keep_stale_information_separate(self):
        p=player_packet();before=copy.deepcopy(p);f=feature_row(p)['features']
        self.assertAlmostEqual(f['home_fpl_reported_loss_minutes_share'],.3)
        self.assertAlmostEqual(f['home_fpl_uncertain_minutes_share'],.1)
        self.assertEqual(f['home_fpl_doubtful_minutes_share'],0)
        self.assertTrue(f['home_fpl_minutes_complete'])
        self.assertEqual(p,before)
        # Old news is unresolved information, not proof of an absence or health.
        p['playerAvailability']['teams'][0]['roster'][1]['newsFreshness']='stale'
        self.assertAlmostEqual(feature_row(p)['features']['home_fpl_uncertain_minutes_share'],.4)

    def test_missing_minutes_and_legacy_snapshots_do_not_receive_zero_or_newer_values(self):
        p=player_packet();p['playerAvailability']['teams'][0]['roster'][0]['seasonMinutes']=None
        f=feature_row(p)['features']
        self.assertFalse(f['home_fpl_minutes_complete']);self.assertIsNone(f['home_fpl_reported_loss_minutes_share'])
        p=player_packet();p['playerAvailability']['teams'][0].pop('roster')
        f=feature_row(p)['features'];self.assertTrue(f['home_fpl_source_available'])
        self.assertIsNone(f['home_fpl_reported_loss_minutes_share'])
        self.assertIsNone(feature_row(snapshot())['features']['home_fpl_reports'])

    def test_wrong_fixture_future_source_publication_and_duplicate_roster_are_rejected(self):
        for change in ('fixture','source','publication','duplicate'):
            p=player_packet();source=p['playerAvailability']
            if change=='fixture':source['fixtureMatch']['home']='Other FC'
            if change=='source':source['fetchedAt']='2026-10-28T12:00:00Z'
            if change=='publication':source['teams'][0]['reports'][0]['publishedAt']='2026-10-28T12:00:00Z'
            if change=='duplicate':source['teams'][0]['roster'][1]['playerId']=1
            with self.subTest(change=change),self.assertRaises(ValueError):feature_row(p)

    def test_provider_xi_is_available_only_for_a_separate_late_window(self):
        p=snapshot();f=p['fixture'];time='2026-10-31T11:30:00Z'
        p.update(collectedAt=time,featureCutoffAt=time)
        p['candidate']['generatedAt']=time
        p['reportedLineups']={'status':'reported_by_provider','fixture':f.copy(),'fetchedAt':time,'teams':[
            {'team':f[side],'status':'reported_by_provider','players':[{'id':str(i),'name':str(i)} for i in range(11)]}
            for side in ('home','away')]}
        row=feature_row(p)
        self.assertTrue(row['features']['home_provider_xi_received'])
        self.assertFalse(row['player_context']['lineups']['home']['official_confirmation'])
        p['collectedAt']=p['featureCutoffAt']='2026-10-30T11:30:00Z'
        p['candidate']['generatedAt']=p['reportedLineups']['fetchedAt']=p['collectedAt']
        with self.assertRaisesRegex(ValueError,'window'):feature_row(p)

    def test_future_model_training_cannot_hide_behind_a_pre_kickoff_date(self):
        p=snapshot();p['candidate']['asOf']='2026-10-28'
        with self.assertRaisesRegex(ValueError,'postdates'):feature_row(p)


if __name__=='__main__':unittest.main()
