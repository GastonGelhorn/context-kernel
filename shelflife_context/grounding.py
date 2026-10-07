"""Bounded review signals, never a general answer correctness certificate."""

import re


CURRENCIES = {
    "EUR": r"€|\bEUR\b|\beuros?\b",
    "USD": r"\$|\bUSD\b|\bUS dollars?\b|\bdolares?\b",
    "GBP": r"£|\bGBP\b|\bpounds?\b|\blibras?\b",
    "JPY": r"¥|\bJPY\b|\byen\b",
}


def currency_review(answer, context):
    """Flag currency references absent from projected numeric metadata.

    Dollar and yen symbols are ambiguous. Flags ask for review, not automatic
    rejection or rewriting. This does not bind each sentence to a particular fact.
    """
    claims = (context or {}).get("claims", [])
    numeric = [c for c in claims if "quantity_metadata" in c]
    known = {c["quantity_metadata"].get("currency") for c in numeric}
    mentioned = {code for code, pattern in CURRENCIES.items()
                 if re.search(pattern, answer or "", re.IGNORECASE)}
    findings = []
    if numeric:
        unknown = sorted(mentioned - known)
        if unknown:
            findings.append({"code": "currency_not_in_projection", "currencies": unknown})
    return {"review_required": bool(findings), "findings": findings,
            "scope": "projected_currency_references_only", "certifies_answer": False}
