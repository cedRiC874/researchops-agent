"""过期运行可拒绝清理，但不能批准或执行。"""

from datetime import timedelta

from researchops.tool_runtime import ToolRuntimeError
from services.mcp_gateway_v1.tests.support import GatewayFixture


class ExpiredApprovalTests(GatewayFixture):
    def expire_run(self):
        self.now[0] += timedelta(seconds=self.gateway.run_ttl_seconds)

    def test_list_marks_expired_pending_calls(self):
        call_id = self.propose_publish()
        pending, = self.gateway.list_pending_approvals()
        self.assertEqual(pending["call_id"], call_id)
        self.assertFalse(pending["run_expired"])
        self.expire_run()
        pending, = self.gateway.list_pending_approvals()
        self.assertEqual(pending["call_id"], call_id)
        self.assertEqual(pending["status"], "awaiting_approval")
        self.assertTrue(pending["run_expired"])

    def test_reject_clears_expired_pending_call(self):
        call_id = self.propose_publish()
        self.expire_run()
        result = self.gateway.decide(call_id, decision="reject", approver="离线测试审批人")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.gateway.ledger.get_approval(call_id)["decision"], "reject")
        self.assertEqual(self.gateway.ledger.get_tool_call(call_id)["status"], "rejected")
        self.assertEqual(self.gateway.list_pending_approvals(), [])
        self.assertEqual(self.executions, [])

    def test_approve_still_rejects_expired_run(self):
        call_id = self.propose_publish()
        self.expire_run()
        with self.assertRaises(ToolRuntimeError) as error:
            self.approve(call_id)
        self.assertEqual(error.exception.code, "gateway_run_expired")
        self.assertIsNone(self.gateway.ledger.get_approval(call_id))
        self.assertEqual(self.gateway.ledger.get_tool_call(call_id)["status"], "awaiting_approval")
        self.assertEqual(self.executions, [])

    def test_execute_approved_still_rejects_expired_run(self):
        call_id = self.propose_publish()
        self.approve(call_id, ttl=7200)
        self.expire_run()
        self.assert_error(self.call("execute_approved", call_id=call_id), "gateway_run_expired")
        self.assertEqual(self.gateway.ledger.get_tool_call(call_id)["status"], "approved")
        self.assertEqual(self.gateway.ledger.list_attempts(call_id), [])
        self.assertEqual(self.executions, [])
        self.assertFalse((self.root / "artifacts/phase4/releases/reviewed-release").exists())
