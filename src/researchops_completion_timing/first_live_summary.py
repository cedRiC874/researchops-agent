"""Strict local invocation summary, never an admission or archive verifier."""
from .contract import _decode, digest, fail
from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object

SCHEMA_PATH = 'evals/provider_completion_first_live_run_v4/summary_v4.schema.json'
SCHEMA_SHA256 = '5e8b1f1090e992101c53922429b54b56004395b6d2fdce39aa750cfcebbf8936'


def load_summary_schema(root):
    payload = read_regular_file_no_follow(root / SCHEMA_PATH, max_bytes=8192)
    if digest(payload) != SCHEMA_SHA256:
        fail('first_live_summary_schema_invalid')
    return decode_strict_json_object(payload, max_bytes=8192)


def validate_invocation_summary(root, payload):
    value = _decode(payload, load_summary_schema(root), 4096, ())
    base = 'output/first-live-v4/' + value['authorization_id_sha256']
    if raw(value) != payload or value['artifact_directory'] != base or value['bundle_path'] != base + '.bundle.json':
        fail('first_live_summary_binding_invalid')
    return value
