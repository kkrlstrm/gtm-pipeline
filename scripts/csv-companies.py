#!/usr/bin/env python3
"""
scripts/csv-companies.py — load a CSV of companies into canonical company JSON so a run
can START from your own list instead of discovery. company-discovery then FILTERS this
list against your ICP/exclusions and proceeds through the normal stages.

    python3 scripts/csv-companies.py --file accounts.csv   # -> {"companies":[...], "count":N}

Tolerant column mapping (case-insensitive): name/company/company_name/account/organization
-> name; domain/company_domain/website/url/site -> domain; linkedin/linkedin_url -> linkedin_url.
Any other columns pass through onto the company object (handy as filter signals). stdlib only.
"""

import argparse
import csv
import json
import sys

NAME_COLS = ("company_name", "company", "name", "account", "organization", "account_name")
DOMAIN_COLS = ("company_domain", "domain", "website", "url", "site", "web")
LINKEDIN_COLS = ("company_linkedin_url", "linkedin_url", "linkedin", "company_linkedin")


def pick(row_lc, cols):
    for c in cols:
        if c in row_lc and (row_lc[c] or "").strip():
            return row_lc[c].strip()
    return None


def main():
    p = argparse.ArgumentParser(description="CSV of companies -> canonical company JSON")
    p.add_argument("--file", required=True)
    args = p.parse_args()

    companies, skipped = [], 0
    with open(args.file, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)              # DictReader handles multiline fields safely
        for raw in reader:
            row_lc = { (k or "").strip().lower(): (v or "") for k, v in raw.items() }
            name = pick(row_lc, NAME_COLS)
            domain = pick(row_lc, DOMAIN_COLS)
            linkedin = pick(row_lc, LINKEDIN_COLS)
            if not name and not domain:
                skipped += 1
                continue
            known = set(NAME_COLS) | set(DOMAIN_COLS) | set(LINKEDIN_COLS)
            extra = {k: v.strip() for k, v in row_lc.items()
                     if k and k not in known and v and v.strip()}
            company = {"name": name, "domain": domain}
            if linkedin:
                company["linkedin_url"] = linkedin
            company.update(extra)               # pass-through columns as filter signals
            companies.append(company)

    print(json.dumps({"companies": companies, "count": len(companies), "skipped": skipped},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
