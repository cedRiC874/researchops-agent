"""Deterministic review diagnostic of one pure budget helper, not full A/B."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from decimal import Decimal,localcontext
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"src"))

from researchops_external_closure.documents import _verify_envelope_commitments,_domain_hash,_RUNTIME_PLAN_DOMAIN
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes
from tests.test_external_closure_documents import SyntheticPreReceipt


def main() -> int:
    with patch("socket.socket",side_effect=AssertionError("network forbidden")):
        fixture=SyntheticPreReceipt()
        envelope=copy.deepcopy(fixture.documents["preregistration_envelope"])
        runtime=envelope["runtime_plan"]
        budget=runtime["budget_policy"]
        budget.update(input_token_limit_total=1,output_token_limit_total=1,
            input_price_per_million_cny="0.500001",output_price_per_million_cny="0.500001",local_observed_cost_stop_cny="0.000001")
        body=dict(budget); body.pop("budget_policy_commitment_sha256")
        budget["budget_policy_commitment_sha256"]=_domain_hash(_RUNTIME_PLAN_DOMAIN,b"budget_policy",canonical_json_bytes(body))
        body=dict(runtime); body.pop("external_plan_binding_sha256")
        runtime["external_plan_binding_sha256"]=_domain_hash(_RUNTIME_PLAN_DOMAIN,canonical_json_bytes(body))
        results={}
        for precision in (28,1):
            with localcontext() as context:
                context.prec=precision
                try:
                    _verify_envelope_commitments(envelope,fixture.documents["seen_case_exclusion_manifest"])
                except ExternalClosurePrimitiveError as error:
                    results[str(precision)]=error.code
                else:
                    results[str(precision)]="accepted"
        with localcontext() as context:
            context.prec=80
            exact=(Decimal("0.500001")+Decimal("0.500001"))/Decimal(1_000_000)
        receipt={"schema_version":"t6c-decimal-context-review/1.0",
            "status":"gap_reproduced" if len(set(results.values()))>1 else "gap_not_reproduced",
            "scope":"envelope_commitment_helper_only_not_full_A_B",
            "exact_reserved_cost_cny":format(exact,"f"),"cost_stop_cny":"0.000001",
            "decimal_precision_results":results,"same_input_documents":True,
            "full_signed_document_graph_revalidated":False,
            "documents_source_sha256":hashlib.sha256((ROOT/"src/researchops_external_closure/documents.py").read_bytes()).hexdigest(),
            "provider_calls":0,"real_key_loads":0}
        print(json.dumps(receipt,sort_keys=True,indent=2))
        return 0


if __name__=="__main__":
    raise SystemExit(main())
