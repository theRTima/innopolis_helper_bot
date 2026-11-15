"""Offline review gates and coverage checks for known extracted sources."""

from copy import deepcopy


def audit_analysis(analysis, raw, page, asset_id):
    analysis = analysis if isinstance(analysis, dict) else {}
    issues = []
    if analysis.get("status") not in {"complete", "empty"}:
        issues.append("partial_or_invalid_status")
    for key in ("conflicts", "unresolved", "limitations"):
        if analysis.get(key):
            issues.append(key)
    items = analysis.get("items")
    if not isinstance(items, list):
        items = []
        issues.append("invalid_items")
    if bool(items) != (analysis.get("status") == "complete"):
        issues.append("status_items_mismatch")
    for proposal in analysis.get("proposals") or []:
        if (
            not isinstance(proposal, dict)
            or proposal.get("review_required")
            or proposal.get("operation") != "add"
            or proposal.get("target_element_ids")
        ):
            issues.append("proposal_requires_review")
    for item in items:
        if not isinstance(item, dict):
            issues.append("invalid_item")
            continue
        if any(
            isinstance(entry, dict)
            and entry.get("readability") in {"uncertain", "unreadable"}
            for entry in item.get("transcription") or []
        ):
            issues.append("uncertain_transcription")
        diagram = item.get("diagram")
        if isinstance(diagram, dict) and any(
            not isinstance(edge, dict)
            or edge.get("direction_status") != "clear"
            for edge in diagram.get("edges") or []
        ):
            issues.append("uncertain_edge")
    accepted = [] if issues else deepcopy(items)
    return accepted, {
        "page": page,
        "asset_id": asset_id,
        "quality_status": "needs_review" if issues else "accepted",
        "issues": sorted(set(issues)),
        "analysis": deepcopy(analysis),
        "raw_response": raw,
        "accepted_item_ids": [item.get("local_id") for item in accepted],
    }


def should_fallback(
    *, failed_visual, has_elements, body_chars, has_visual, min_page_text
):
    return failed_visual or (
        not has_elements and body_chars < min_page_text and not has_visual
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
