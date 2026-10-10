"""用临时合成字节验证来源核对，不依赖冻结产物或其具体哈希。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from researchops_mcp_gateway.gateway import Gateway


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def make_gateway(self, current, evidence_source, *, source_root=None):
        source = source_root or self.root
        (source / "data").mkdir(parents=True, exist_ok=True)
        (source / "data/synthetic_trial.csv").write_bytes(current)
        bundle = source / "artifacts/phase3"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "analysis_bundle.json").write_text(json.dumps({
            "run_id": "synthetic-analysis",
            "dataset": {"sha256": hashlib.sha256(evidence_source).hexdigest(), "raw_data_embedded": False},
            "evidence": [],
        }), encoding="utf-8")
        # 读取摘要只核对图表文件的存在及哈希，不解析图像。
        (bundle / "effect_estimates.png").write_bytes(b"synthetic-chart")
        gateway = Gateway(self.root, self.root / "state", source_snapshot_root=source)
        run_id = gateway.call_tool("begin_run", {})["structuredContent"]["run_id"]
        return gateway, run_id

    def read(self, gateway, run_id):
        result = gateway.call_tool("read_aggregate_evidence", {"run_id": run_id, "bundle_id": "phase3"})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        saved = gateway.ledger.get_tool_call(payload["call_id"])["safe_result"]
        self.assertNotIn("warnings", saved, "网关告警不得改写核心保存结果")
        self.assertEqual(saved["dataset_sha256"], payload["dataset_sha256"])
        self.assertTrue(gateway.ledger.verify_chain(run_id).valid)
        return payload

    def test_identical_bytes_do_not_add_warning(self):
        current = b"value,group\n1,A\n2,B\n"
        gateway, run_id = self.make_gateway(current, current)
        payload = self.read(gateway, run_id)
        self.assertNotIn("warnings", payload)
        self.assertEqual(payload["dataset_sha256"], hashlib.sha256(current).hexdigest())

    def test_line_ending_conversion_is_computed_in_both_directions(self):
        lf = b"value,group\n1,A\n2,B\n"
        crlf = lf.replace(b"\n", b"\r\n")
        for current, recorded, ending in ((lf, crlf, "CRLF"), (crlf, lf, "LF"),
                                           (b"value,group\r\n1,A\n2,B\r", lf, "LF")):
            with self.subTest(ending=ending, current=current):
                gateway, run_id = self.make_gateway(current, recorded)
                warning, = self.read(gateway, run_id)["warnings"]
                self.assertEqual(warning, {
                    "code": "gateway_dataset_line_endings_only",
                    "current_dataset_sha256": hashlib.sha256(current).hexdigest(),
                    "evidence_dataset_sha256": hashlib.sha256(recorded).hexdigest(),
                    "matching_line_ending": ending,
                    "relationship": "line_endings_only",
                    "message": f"证据包数据集 SHA-256 与当前已登记文件不同；将当前文件的换行统一为 {ending} 后，SHA-256 与证据包记录一致。",
                })

    def test_content_change_is_mismatch_and_current_file_is_rechecked(self):
        recorded = b"value,group\n1,A\n2,B\n"
        gateway, run_id = self.make_gateway(recorded, recorded)
        self.assertNotIn("warnings", self.read(gateway, run_id))
        current = b"value,group\r\n9,A\r\n2,B\r\n"
        (self.root / "data/synthetic_trial.csv").write_bytes(current)
        warning, = self.read(gateway, run_id)["warnings"]
        self.assertEqual(warning, {
            "code": "gateway_dataset_sha256_mismatch",
            "current_dataset_sha256": hashlib.sha256(current).hexdigest(),
            "evidence_dataset_sha256": hashlib.sha256(recorded).hexdigest(),
            "relationship": "mismatch",
            "message": "当前文件原始字节、统一为 LF 或 CRLF 后的 SHA-256 均与证据包记录不同。",
        })

    def test_uses_registered_source_snapshot(self):
        (self.root / "data").mkdir()
        (self.root / "data/synthetic_trial.csv").write_bytes(b"unrelated\n9\n")
        current = b"value,group\n1,A\n"
        gateway, run_id = self.make_gateway(current, current, source_root=self.root / "snapshot")
        self.assertNotIn("warnings", self.read(gateway, run_id))
