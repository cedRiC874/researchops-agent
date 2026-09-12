"""Real temporary v5/source-v4 API and CLI processes; fake Key/MockTransport only."""
import os
import unittest

from tests import test_completion_campaign_public_process as process_fixture


@unittest.skipUnless(os.name == 'nt', 'designated Windows environment')
class CampaignV5PublicProcessTests(unittest.TestCase):
    def run_process(self, mode):
        helper = process_fixture.PublicCampaignProcessTests(); self.addCleanup(helper.doCleanups)
        return helper.run_process(mode, implementation_version=5)

    def test_api_v5_with_real_source_claim_opening_runtime_and_archive(self):
        value = self.run_process('api')
        self.assertEqual(value['schema_version'], 'provider-completion-campaign-invocation/1.0')
        self.assertFalse(value['closure_claim_allowed'])

    def test_cli_v5_with_real_source_claim_opening_runtime_and_archive(self):
        self.assertEqual(self.run_process('cli')['fixture_cli_exit_code'], 0)


if __name__ == '__main__': unittest.main()
