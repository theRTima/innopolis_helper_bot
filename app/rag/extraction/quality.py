"""Offline review gates and coverage checks for known extracted sources."""

from copy import deepcopy


def _item_issues(item) -> list[str]:
    issues: list[str] = []
    if not isinstance(item, dict):
        return ["invalid_item"]
    transcription = item.get("transcription") or []
    if isinstance(transcription, list):
        if any(
            isinstance(entry, dict)
            and entry.get("readability") in {"uncertain", "unreadable"}
            for entry in transcription
        ):
            issues.append("uncertain_transcription")
    elif transcription:
        issues.append("invalid_transcription")
    diagram = item.get("diagram")
    if isinstance(diagram, dict):
        edges = diagram.get("edges") or []
        if isinstance(edges, list):
            if any(
                not isinstance(edge, dict)
                or edge.get("direction_status") != "clear"
                for edge in edges
            ):
                issues.append("uncertain_edge")
        else:
            issues.append("invalid_edges")
    return issues


def audit_analysis(analysis, raw, page, asset_id):
    """Accept clear items while retaining response-level review metadata.

    Response-level limitations, conflicts, unresolved notes and review-required
    proposals do not invalidate otherwise readable items. Uncertain item fields
    (transcription readability or diagram edge direction) still quarantine only
    that item.
    """
    analysis = analysis if isinstance(analysis, dict) else {}
    issues = []
    status = analysis.get("status")
    if status == "partial":
        issues.append("partial_or_invalid_status")
    elif status not in {"complete", "partial", "empty"}:
        issues.append("invalid_status")
    for key in ("conflicts", "unresolved", "limitations"):
        if analysis.get(key):
            issues.append(key)
    items = analysis.get("items")
    if not isinstance(items, list):
        items = []
        issues.append("invalid_items")
    if status == "complete" and not items:
        issues.append("status_items_mismatch")
    if status == "empty" and items:
        issues.append("status_items_mismatch")
    for proposal in analysis.get("proposals") or []:
        if (
            not isinstance(proposal, dict)
            or proposal.get("review_required")
            or proposal.get("operation") != "add"
            or proposal.get("target_element_ids")
        ):
            issues.append("proposal_requires_review")

    accepted = []
    rejected = []
    if status != "empty":
        for item in items:
            item_issues = _item_issues(item)
            if item_issues:
                rejected.append(
                    {
                        "local_id": (
                            item.get("local_id")
                            if isinstance(item, dict)
                            else None
                        ),
                        "issues": sorted(set(item_issues)),
                    }
                )
                issues.extend(item_issues)
                continue
            accepted.append(deepcopy(item))

    return accepted, {
        "page": page,
        "asset_id": asset_id,
        "quality_status": (
            "needs_review" if issues or rejected else "accepted"
        ),
        "issues": sorted(set(issues)),
        "analysis": deepcopy(analysis),
        "raw_response": raw,
        "accepted_item_ids": [item.get("local_id") for item in accepted],
        "rejected_items": rejected,
    }


def should_fallback(
    *,
    failed_visual,
    has_elements,
    body_chars,
    has_visual,
    min_page_text,
    picture_area_ratio=1.0,
    low_text_chars=400,
):
    if failed_visual:
        return True
    if not has_elements and body_chars < min_page_text and not has_visual:
        return True
    # A page dominated by a small picture plus a short/garbled text layer is
    # usually a visual page whose table or schedule was not captured by Docling.
    return (
        has_visual
        and picture_area_ratio < 0.20
        and body_chars < low_text_chars
    )


def coverage_report(nd, chunks, warnings):
    expected = {
        key
        for key, value in nd.sources.items()
        if value.text or value.data is not None
    }
    covered = {
        ref
        for chunk in chunks
        for ref in chunk.get("source_reference_ids", [])
    }
    missing = sorted(expected - covered)
    review = [
        audit
        for audit in nd.analysis_records
        if audit["quality_status"] == "needs_review"
    ]
    return {
        "status": (
            "needs_review"
            if missing or review or warnings or not chunks
            else "passed_checks"
        ),
        "scope": "Known extracted sources; not proof of PDF completeness",
        "source_count": len(expected),
        "covered_source_count": len(expected & covered),
        "missing_source_ids": missing,
        "review_region_count": len(review),
        "warning_count": len(warnings),
    }
