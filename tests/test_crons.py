import unittest

from scripts.configure_automation import cron_values, merged_schedules


class ScheduleConfigTests(unittest.TestCase):
    def test_existing_triggers_are_retained_and_new_triggers_are_idempotent(self):
        original = {'schedules': [{'cron': '17 */2 * * *'}, {'cron': '3 1 * * *'}]}
        merged = merged_schedules(original)
        self.assertIn('3 1 * * *', merged)
        self.assertIn('50 4 * * *', merged)
        self.assertIn('7,37 * * * *', merged)
        self.assertEqual(merged_schedules([{'cron': value} for value in merged]), merged)
        self.assertEqual(cron_values(original), ['17 */2 * * *', '3 1 * * *'])


if __name__ == '__main__': unittest.main()
