import copy
import json
import unittest

from research.elc_blend import PLAN, choose, later_metrics


class ELCBlendTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(PLAN.read_text())
        self.rows = [{'date': f'2024-01-{i+1:02d}', 'actual': 0,
                      'v1': [.4, .3, .3], 'shots': [.7, .2, .1], 'base': [.4, .3, .3]} for i in range(10)]

    def test_later_labels_change_metrics_but_cannot_change_the_earlier_selected_weight(self):
        choice = choose(self.rows, self.plan)
        periods = {'validation': copy.deepcopy(self.rows), 'audit': copy.deepcopy(self.rows),
                   'monitoring': copy.deepcopy(self.rows)}
        before = later_metrics(periods, choice, self.plan)[0]
        for row in periods['audit']:row['actual'] = 2
        after = later_metrics(periods, choice, self.plan)[0]
        self.assertEqual(choice, choose(self.rows, self.plan))
        self.assertEqual(choice['shotWeight'], 1)
        self.assertNotEqual(before['audit']['metrics']['blend'], after['audit']['metrics']['blend'])
        self.assertEqual(before['validation'], after['validation'])

    def test_zero_weight_is_an_allowed_tie_result_and_the_search_is_bounded(self):
        rows = [{**row, 'v1': [.5, .25, .25], 'shots': [.5, .25, .25]} for row in self.rows]
        choice = choose(rows, self.plan)
        self.assertEqual(choice['shotWeight'], 0)
        self.assertEqual(len(choice['trials']), 5)
        with self.assertRaises(ValueError):choose(rows, {**self.plan, 'maximumCandidates': 10})


if __name__ == '__main__':unittest.main()
