"""Public read-only entrypoint for T6-C pre-receipt document verification."""

from __future__ import annotations

from pathlib import Path

from .documents import VerifiedPreReceiptDocuments, verify_pre_receipt_documents
from .errors import ExternalClosurePrimitiveError
from .types import PreReceiptDocumentBytes


def verify_preregistration_envelope(
    project_root: Path,
    documents: PreReceiptDocumentBytes,
    *,
    external_observation_bundle: bytes,
) -> VerifiedPreReceiptDocuments:
    """Verify the frozen contract graph and all pre-receipt signed documents.

    The function is read-only and returns only immutable hashes/timestamps.  It
    cannot create a runtime binding, release task content, load a Provider key,
    or perform a network request.
    """

    if not isinstance(project_root, Path):
        raise ExternalClosurePrimitiveError("external_closure_project_root_invalid")
    try:
        from researchops.provider_completion_external_contract import (
            ExternalPreregistrationContractError,
            load_frozen_external_preregistration_contract,
        )

        try:
            contract = load_frozen_external_preregistration_contract(project_root)
        except ExternalPreregistrationContractError:
            raise ExternalClosurePrimitiveError(
                "external_closure_frozen_contract_invalid"
            ) from None
        schemas = contract["schemas"]
        return verify_pre_receipt_documents(
            documents,
            schemas=schemas,
            external_observation_bundle=external_observation_bundle,
        )
    except ExternalClosurePrimitiveError:
        raise


__all__ = ["verify_preregistration_envelope"]
