from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

import httpx

from stationary_world_arm_alignment.candidate_review import (
    CandidateReviewService,
    ExternalReviewIdentityVerifier,
)
from stationary_world_arm_alignment.config import Settings
from stationary_world_arm_alignment.local_review_activation import (
    build_activation_request,
    build_candidate_review_request,
    issue_review_identity_assertion,
)
from stationary_world_arm_alignment.persistence import CalibrationStore


async def run(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    secret = os.environ.get("MIDBRAIN_REVIEW_AUTH_SECRET", "")
    store = CalibrationStore(settings.calibration_root)
    result = store.get(args.alignment_id)
    if result is None:
        raise RuntimeError(f"calibration {args.alignment_id!r} does not exist")
    candidate = result.get("candidate")
    if not isinstance(candidate, dict):
        raise RuntimeError("calibration result does not contain a candidate")
    if not args.approve:
        raise RuntimeError("explicit --approve is required")

    review_service = CandidateReviewService(
        store,
        settings.review_root,
    )
    existing_decision = review_service.decision_for(str(candidate["candidate_id"]))
    if existing_decision is not None:
        if (
            existing_decision.get("decision") != "APPROVE"
            or existing_decision.get("decision_state")
            != "APPROVED_FOR_ACTIVATION"
            or existing_decision.get("candidate_sha256")
            != build_candidate_review_request(
                candidate,
                idempotency_key=args.review_request_id,
                rationale=args.rationale,
            )["candidate_sha256"]
        ):
            raise RuntimeError(
                "the existing final review decision is not an exact approval "
                "for this candidate"
            )
        reviewer = existing_decision.get("reviewer")
        if not isinstance(reviewer, dict):
            raise RuntimeError("the existing review decision lacks reviewer identity")
        reviewer_id = str(reviewer.get("reviewer_id") or "").strip()
        issuer = str(reviewer.get("issuer") or "").strip()
        nonce = str(reviewer.get("assertion_nonce") or "").strip()
        if not reviewer_id or not issuer or not nonce:
            raise RuntimeError("the existing review identity is incomplete")
    else:
        reviewer_id = args.reviewer_id
        issuer = "midbrain.local.development"
        nonce = args.nonce

    assertion, identity_payload = issue_review_identity_assertion(
        secret=secret,
        candidate=candidate,
        reviewer_id=reviewer_id,
        issuer=issuer,
        nonce=nonce,
    )
    verifier = ExternalReviewIdentityVerifier(secret)
    verified_identity = verifier.verify(
        assertion,
        candidate_id=str(candidate["candidate_id"]),
        candidate_sha256=str(identity_payload["candidate_sha256"]),
        decision="APPROVE",
    )
    if existing_decision is not None:
        review_decision = existing_decision
        review_created = False
    else:
        review_request = build_candidate_review_request(
            candidate,
            idempotency_key=args.review_request_id,
            rationale=args.rationale,
        )
        review_decision, review_created = review_service.decide(
            args.alignment_id,
            review_request,
            verified_identity=verified_identity,
        )
    activation_request = build_activation_request(
        candidate,
        review_decision,
        assertion,
        request_id=args.activation_request_id,
        activated_by=reviewer_id,
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{settings.manager_url.rstrip('/')}/v1/workcell-calibrations/activate",
            json=activation_request,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            detail = response.text.strip() or str(error)
            raise RuntimeError(
                "Manager rejected workcell calibration activation: "
                f"HTTP {response.status_code}: {detail}"
            ) from error
        activation = response.json()
    return {
        "status": "ACTIVE",
        "alignment_id": args.alignment_id,
        "review_created": review_created,
        "review_decision_id": review_decision["decision_id"],
        "activation_id": activation["activation_id"],
        "candidate_sha256": activation["candidate_sha256"],
        "expires_at_us": activation["expires_at_us"],
        "validity_policy": activation["validity_policy"],
        "motion_usable": activation["motion_usable"],
        "review_identity_assertion_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Locally approve and Manager-activate one exact stationary "
            "calibration candidate for development."
        )
    )
    parser.add_argument("alignment_id")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument(
        "--reviewer-id",
        default="codex:gpt-5.6-sol",
    )
    parser.add_argument(
        "--rationale",
        required=True,
    )
    parser.add_argument(
        "--review-request-id",
        default=f"review-{uuid.uuid4()}",
    )
    parser.add_argument(
        "--activation-request-id",
        default=f"activate-{uuid.uuid4()}",
    )
    parser.add_argument(
        "--nonce",
        default=str(uuid.uuid4()),
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2))


if __name__ == "__main__":
    main()
