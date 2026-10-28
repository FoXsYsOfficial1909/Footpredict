from datetime import datetime, timezone
import tempfile
from pathlib import Path
import unittest

from research.prospective import immutable_json, select_cohort


class ProspectiveProtocolTests(unittest.TestCase):
    def test_selection_is_chronological_and_ignores_outcomes(self):
        now = datetime(2026, 10, 27, 19, tzinfo=timezone.utc)
        def game(fixture_id, kickoff, status='TIMED'):
            return {'id': fixture_id, 'utcDate': kickoff, 'status': status,
                    'homeTeam': {'name': 'Home'}, 'awayTeam': {'name': 'Away'}, 'score': {'fullTime': {'home': 9}}}
        games = [game(3, '2026-10-31T15:00:00Z'), game(2, '2026-10-31T12:30:00Z'),
                 game(1, '2026-10-27T20:00:00Z'), game(4, '2026-10-31T12:30:00Z'),
                 game(5, '2026-11-15T12:30:00Z'), game(6, '2026-10-31T15:00:00Z', 'FINISHED')]
        selected = select_cohort(games, now, 2)
        self.assertEqual([game['id'] for game in selected], [2, 4])
        self.assertNotIn('score', selected[0])
        self.assertEqual(select_cohort(list(reversed(games)), now, 2), selected)

    def test_evidence_cannot_be_overwritten_with_different_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'evidence.json'
            immutable_json(path, {'probabilities': [.4, .3, .3]})
            immutable_json(path, {'probabilities': [.4, .3, .3]})
            with self.assertRaisesRegex(RuntimeError, 'immutable'):
                immutable_json(path, {'probabilities': [.9, .05, .05]})


if __name__ == '__main__': unittest.main()
