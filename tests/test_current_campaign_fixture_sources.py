"""Pure fixture overlay coverage; not source admission or Provider evidence."""
import tempfile
import unittest
from pathlib import Path

from tests.timed_first_live_evidence_fixture import _with_current_project_python


class CurrentCampaignFixtureSourceTests(unittest.TestCase):
    def test_current_callers_and_callees_replace_archived_python_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = {'src/researchops/audit.py': b'current ledger\n',
                       'src/researchops_completion_timing/campaign_run.py': b'current caller\n',
                       'src/researchops/nested/helper.py': b'current nested dependency\n'}
            for name, value in current.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            old = {'src/researchops/audit.py': b'old ledger\n',
                   'src/retired_module.py': b'old-only module\n', 'evals/frozen.json': b'{"frozen":true}'}
            before = dict(old)
            result = _with_current_project_python(old, root)
            self.assertEqual(result, current | {'evals/frozen.json': old['evals/frozen.json']})
            self.assertEqual(old, before)
            self.assertEqual({name: (root / name).read_bytes() for name in current}, current)

    def test_empty_current_source_is_rejected_instead_of_using_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'current_runtime_python_source_missing'):
                _with_current_project_python({'src/old.py': b'old'}, Path(temporary))


if __name__ == '__main__': unittest.main()
