"""审批演示只使用合成材料；批准必须由测试显式调用本地 CLI。"""
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from researchops_mcp_gateway.cli import main as cli_main
from researchops_mcp_gateway.gateway import Gateway
from services.mcp_gateway_v1.scripts.approval_demo import RELEASE_NAME, main


class ApprovalDemoTests(unittest.TestCase):
    def test_prepare_requires_cli_approval_and_finish_rejects_parameter_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            demo = Path(temporary) / "demo"
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["prepare", "--demo-dir", str(demo)]), 0)
            pending = json.loads(output.getvalue())
            self.assertEqual(pending["status"], "awaiting_approval")
            self.assertEqual(pending["row_count"], 240)
            self.assertEqual(pending["release_name"], RELEASE_NAME)
            self.assertNotIn(str(demo), output.getvalue())
            call_id = pending["call_id"]
            gateway = Gateway(demo, demo / "state")
            self.assertIsNone(gateway.ledger.get_approval(call_id))
            release = demo / "artifacts/phase4/releases" / RELEASE_NAME
            self.assertFalse(release.exists())
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["finish", "--demo-dir", str(demo)]), 1)
            self.assertEqual(json.loads(output.getvalue())["error_code"], "tool_approval_required")
            self.assertFalse(release.exists())
            with redirect_stdout(StringIO()):
                approved = cli_main(["--project-root", str(demo), "--state-dir", str(demo / "state"),
                                     "approvals", "approve", call_id, "--approver", "演示复核员"])
            self.assertEqual(approved, 0)
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["finish", "--demo-dir", str(demo)]), 0)
            finished = json.loads(output.getvalue())
            self.assertEqual(finished["status"], "succeeded")
            self.assertFalse(finished["execute_is_error"])
            self.assertTrue(finished["tamper_is_error"])
            self.assertEqual(finished["tamper_error_code"], "tool_arguments_invalid")
            self.assertTrue((release / "release_manifest.json").is_file())
            self.assertFalse((release.parent / "tampered-release").exists())
            self.assertEqual(len(gateway.ledger.list_attempts(call_id)), 1)
            self.assertNotIn(str(demo), output.getvalue())
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["prepare", "--demo-dir", str(demo)]), 1)
            self.assertEqual(json.loads(output.getvalue())["error_code"], "demo_directory_exists")
            self.assertTrue((release / "release_manifest.json").is_file())
