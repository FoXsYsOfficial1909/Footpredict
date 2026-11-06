import json
from pathlib import Path
import tempfile
import unittest

from scripts.build_site import ROOT, collect_assets, render_module


class PublicSiteBundleTests(unittest.TestCase):
    def test_only_referenced_public_assets_and_transitive_imports_are_bundled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<link rel="stylesheet" href="./view.css?v=2"><script type="module" src="./view.js"></script><a href="private.json">not an asset</a>')
            (root / 'view.css').write_text('body { color: red; }')
            (root / 'view.js').write_text('import {value} from "./shared.js"; export {value};')
            (root / 'shared.js').write_text('export const value="Привет";')
            (root / 'unused.js').write_text('PRIVATE_UNREFERENCED')
            (root / '.env').write_text('PRIVATE_CREDENTIAL')
            (root / 'private.json').write_text('PRIVATE_DATA')
            assets = collect_assets(root)
            self.assertEqual(set(assets), {'/app/', '/app/index.html', '/app/view.css', '/app/view.js', '/app/shared.js'})
            module = render_module(root)
            self.assertEqual(module, render_module(root))
            self.assertNotIn('PRIVATE_', module)
            self.assertEqual(assets['/app/'], assets['/app/index.html'])
            self.assertEqual(assets['/app/shared.js']['contentType'], 'text/javascript; charset=utf-8')

    def test_traversal_private_and_missing_references_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for reference in ('../secret.js', './.env', './tests/private.js', './missing.js', './%2e%2e/secret.js'):
                (root / 'index.html').write_text('<script src="' + reference + '"></script>')
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    collect_assets(root)

    def test_real_bundle_matches_site_sources_and_has_no_extra_project_files(self):
        assets = collect_assets()
        self.assertEqual(assets['/app/']['body'], (ROOT / 'index.html').read_text())
        self.assertIn('/app/research-ui.js', assets)
        self.assertIn('/app/navigation.css', assets)
        self.assertTrue(all(path == '/app/' or Path(path).suffix in ('.html', '.css', '.js') for path in assets))
        self.assertFalse(any('worker/' in path or 'research/' in path or 'tests/' in path for path in assets))
        module = render_module()
        payload = module.split('Object.freeze(', 1)[1].split(');\n', 1)[0]
        self.assertEqual(json.loads(payload), assets)


if __name__ == '__main__':
    unittest.main()
