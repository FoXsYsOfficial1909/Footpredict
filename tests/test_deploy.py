import unittest
from unittest.mock import Mock,patch
from scripts.deploy_worker import binding_metadata,multipart,parse_modules,verify_settings,confirmed_modules


class DeploySafetyTests(unittest.TestCase):
    def setUp(self):
        self.settings={'compatibility_date':'2026-10-15','bindings':[{'name':'ADMIN_TOKEN','type':'secret_text'},
                       {'name':'ALLOWED_ORIGIN','type':'plain_text','text':'https://example.org'},
                       {'name':'DB','type':'d1','id':'public-placeholder-id'}]}

    def test_existing_secrets_use_inherit_without_reading_values(self):
        metadata=binding_metadata(self.settings,True)
        self.assertEqual(metadata['bindings'][0],{'name':'ADMIN_TOKEN','type':'inherit'})
        self.assertEqual(metadata['bindings'][-1],{'name':'AI','type':'ai'})
        self.assertNotIn('text',metadata['bindings'][1])

    def test_multipart_round_trip_and_unsafe_names(self):
        original={'worker.js':b'export default {};','research.mjs':b'export const a=1;'}
        body,kind=multipart(original,{'main_module':'worker.js'})
        parsed=parse_modules(body,kind)
        self.assertEqual({name:content for name,content in parsed.items() if name!='metadata'},original)
        with self.assertRaises(ValueError):multipart({'../secret':b'no'}, {})

    def test_binding_drift_blocks_success_claim(self):
        after={**self.settings,'bindings':self.settings['bindings']+[{'name':'AI','type':'ai'}]}
        verify_settings(self.settings,after,True)
        after={**after,'bindings':[item for item in after['bindings'] if item['name']!='ADMIN_TOKEN']}
        with self.assertRaises(RuntimeError):verify_settings(self.settings,after,True)

    def test_eventual_source_visibility_does_not_cause_a_false_failed_deployment(self):
        expected={'worker.js':b'export default {};'}
        client=Mock();client.modules.side_effect=[{'worker.js':b'previous source'},expected]
        with patch('scripts.deploy_worker.time.sleep'):
            self.assertEqual(confirmed_modules(client,expected),expected)

    def test_a_persistent_source_mismatch_is_never_accepted(self):
        client=Mock();client.modules.return_value={'worker.js':b'different source'}
        with patch('scripts.deploy_worker.time.sleep'),self.assertRaisesRegex(RuntimeError,'do not match'):
            confirmed_modules(client,{'worker.js':b'expected source'})


if __name__=='__main__':unittest.main()
