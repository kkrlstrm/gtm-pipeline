#!/usr/bin/env python3
"""Offline fixture test for providers/alphaosint contact mapping. No Apify calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `python3 providers/alphaosint/test_mapping.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapter import (  # noqa: E402
    build_actor_input,
    chunked,
    company_strings,
    title_keywords,
    to_contacts,
)

FIXTURE_OK = {
    "result_type": "person",
    "companyName": "Acme GmbH",
    "companySlug": "acme-gmbh",
    "companyUrl": "https://www.linkedin.com/company/acme-gmbh",
    "profileUrl": "https://www.linkedin.com/in/jane-doe",
    "slug": "jane-doe",
    "fullName": "Jane Doe",
    "headline": "VP Engineering at Acme GmbH",
    "location": "Berlin, Germany",
    "currentTitle": "VP Engineering",
    "currentCompany": "Acme GmbH",
    "verified": True,
    "matchReason": "title+company",
}

FIXTURE_ERROR = {
    "result_type": "error",
    "error": "CompanyNotFound",
    "message": "no public employees matched",
}


def main() -> None:
    """Assert mapping, batching, and title-keyword rules against fixtures."""
    contacts, failed = to_contacts([FIXTURE_OK, FIXTURE_ERROR])
    assert len(contacts) == 1, contacts
    assert len(failed) == 1, failed
    c = contacts[0]
    assert c["first_name"] == "Jane"
    assert c["last_name"] == "Doe"
    assert c["full_name"] == "Jane Doe"
    assert c["title"] == "VP Engineering"
    assert c["company_name"] == "Acme GmbH"
    assert c["linkedin_url"] == "https://www.linkedin.com/in/jane-doe"
    assert c["location"] == "Berlin, Germany"
    assert c["source"] == "alphaosint"
    assert c["meta"]["verified"] is True
    assert c["meta"]["matchReason"] == "title+company"

    companies = company_strings(
        {"companies": [{"name": "Acme", "domain": "acme.de"}, "Globex", "https://linkedin.com/company/x"]}
    )
    assert companies == ["acme.de", "Globex", "https://linkedin.com/company/x"]
    assert len(chunked(list(range(12)), 5)) == 3  # type: ignore[arg-type]
    assert chunked(["a", "b", "c", "d", "e", "f"], 5) == [["a", "b", "c", "d", "e"], ["f"]]

    keywords = title_keywords({"titles": ["CFO"], "seniorities": ["Director", "VP"]})
    assert "CFO" in keywords
    assert "director" in keywords
    assert "vp" in keywords or "vice president" in keywords

    payload = build_actor_input(["Acme", "Globex"], keywords, ["Germany"], 20)
    assert payload["companies"] == ["Acme", "Globex"]
    assert payload["maxResultsPerCompany"] == 20
    assert payload["verifyEmployment"] is True
    assert payload["locations"] == ["Germany"]

    print(json.dumps({"ok": True, "contacts": 1, "failed": 1}))


if __name__ == "__main__":
    main()
