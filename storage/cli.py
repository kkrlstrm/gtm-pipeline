#!/usr/bin/env python3
"""
storage/cli.py — uniform storage operations for the GTM pipeline.

Agents NEVER write raw SQL or file IO inline. They call this CLI with a small,
fixed op set and a single JSON object on --input; it returns canonical JSON on
stdout (logs go to stderr). Stage-handoff semantics are identical across backends.

    python3 storage/cli.py <op> --input '<JSON>'

The backend is read from gtm.config.yaml (`storage.backend`), so agents do not pass it.
Explicit flags (--backend / --dir / --db-url-env) and the env vars GTM_BACKEND /
GTM_DATA_DIR override the config. The resolved backend is printed to stderr on every
call, so a run can never write to local files while you believed it was on Postgres.

Ops (input JSON shape -> output JSON shape):

  Lists and contacts
  create_list        {name, description?, search_criteria?}          -> {list_id}
  get_list           {list_id}                                       -> {list:{..., search_criteria, run_state}}
  update_list        {list_id, search_criteria?:{patch}, status?}    -> {list_id, updated}
  upsert_contacts    {list_id, contacts:[Contact,...]}               -> {inserted, skipped_duplicates, total}
  advance_stage      {list_id, contact_ids:[int], stage, fields?:{}} -> {updated, not_found:[int]}
  update_contacts    {list_id, contact_ids:[int], fields:{}}         -> {updated, not_found:[int]}   (stage unchanged)
  query_by_stage     {list_id, stage}                                -> {contacts:[Contact,...]}
  query_list         {list_id}                                       -> {contacts:[...]}              (every stage)
  list_summary       {list_id?}                                      -> {lists:[{...counts}]}
  export             {list_id, min_stage?, out_path?}                -> {rows, path, count, suppression_applied, qa_open_errors}
  crossref_master    {linkedin_urls:[str]}                           -> {statuses:{url: "new"|...}}
  upsert_companies   {list_id, companies:[Company,...]}              -> {inserted, updated, total}
  query_companies    {list_id}                                       -> {companies:[...]}

  Run integrity (see docs/run-integrity.md)
  log_event          {list_id, stage, status, provider?, counts?, warnings?, cost?, note?} -> {logged}
  run_report         {list_id}                                       -> {list, summary, events, open_warnings, gates, cost}
  check_suppression  {domains?, emails?, phones?, linkedin_urls?, file?} -> {applied, matches}   (read-only)
  suppress           {list_id, posture: research|send, file?}        -> {verdict}
  qa                 {list_id, default_country?}                     -> {findings, counts, open_errors}
  qa_resolve         {list_id, keys:[str], action: drop|keep|fix, fields? (one key), note?} -> {resolved, qa}
  preflight_activate {list_id, min_stage?}                           -> {ok, blockers, warnings, leads_ready}

Exit codes (branch on these, not on the prose):
  0 ok · 2 bad input · 3 not configured · 4 not found · 5 backend/upstream failure
  6 gate blocked — `suppress` with posture=send could not apply the list, or
    `preflight_activate` found a blocker. The JSON on stdout says why.

Backends:
  local     -> ./.gtm-data (JSON/JSONL/CSV). Zero setup.
  postgres  -> shells out to `psql` against $DATABASE_URL.

stdlib only — no pip installs. This is deliberate so the file can be fetched and
piped, and so dedup parity with Postgres is auditable in one place.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone


class GateBlocked(Exception):
    """A gate refused. Carries the JSON result to print before exiting 6."""

    def __init__(self, result):
        super().__init__("gate blocked")
        self.result = result


class NotFound(Exception):
    pass


class BackendError(Exception):
    pass


# ===========================================================================
# Canonical normalization — MUST stay byte-identical to the Postgres function
# normalize_linkedin_url() in storage/postgres/schema.sql, or the same contact
# will dedup differently across backends. The SQL is:
#   LOWER(regexp_replace(regexp_replace(url, '\?.*$', ''), '/$', ''))
# i.e. (1) drop the query string, (2) drop a single trailing slash, (3) lowercase.
# Postgres regexp_replace replaces only the FIRST match by default (no 'g' flag).
# ===========================================================================

def normalize_linkedin_url(url):
    if url is None or url == "":
        return None
    s = re.sub(r"\?.*$", "", url)   # strip query string (first match, greedy to end)
    s = re.sub(r"/$", "", s)         # strip one trailing slash
    return s.lower()


# Company dedup key. MUST match normalize_domain() in storage/postgres/schema.sql:
# lowercase, drop protocol, host only, drop leading www., drop trailing dot.
def normalize_domain(d):
    if not d:
        return None
    s = re.sub(r"^https?://", "", d.strip().lower())
    s = s.split("/")[0]
    s = re.sub(r"^www\.", "", s)
    s = s.rstrip(".")
    return s or None


# Canonical export column order — MUST match pipeline_export() in schema.sql.
# (selftest.sh checks the two lists against each other.)
EXPORT_COLUMNS = [
    "first_name", "last_name", "full_name", "title", "seniority",
    "company_name", "company_domain", "linkedin_url", "country", "location",
    "email", "phone", "phone_ext", "phone_type", "source", "matched_persona",
    "qualification_score",
]

# Stage ordering for the export min_stage gate — matches pipeline_export().
STAGE_TIMESTAMP = {
    "sourced": "sourced_at",
    "qualified": "qualified_at",
    "email_enriched": "email_enriched_at",
    "phone_enriched": "phone_enriched_at",
}

# Which stages satisfy a given min_stage gate (excludes 'skipped' everywhere).
MIN_STAGE_INCLUDES = {
    "sourced": {"sourced", "qualified", "email_enriched", "phone_enriched"},
    "qualified": {"qualified", "email_enriched", "phone_enriched"},
    "email_enriched": {"email_enriched", "phone_enriched"},
    "phone_enriched": {"phone_enriched"},
}

EVENT_STATUSES = {"ok", "warn", "error", "skipped"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _log(msg):
    print(msg, file=sys.stderr)


# ===========================================================================
# Contact-data normalization used by QA and suppression (backend-independent)
# ===========================================================================

# The marker must not be the tail of a word: "box 12" is not extension 12.
_EXT_RE = re.compile(r"\s*(?:(?<![A-Za-z])(?:ext\.?|extension|x)|#)\s*(\d{1,6})\s*$", re.I)


def _nanp_ok(d10):
    # NANP: area code and exchange both start 2-9.
    return len(d10) == 10 and d10[0] in "23456789" and d10[3] in "23456789"


def normalize_phone(raw, default_country="US"):
    """Return {e164, ext, valid, reason} or None for an empty value.

    Strips every non-digit (dots included), drops the "(0)" national trunk prefix that
    European numbers often carry after the country code (+49 (0)30 → +4930), splits a
    trailing extension out instead of gluing it onto the number, and refuses anything it
    cannot place in a country. Without a leading + it only assumes NANP when
    default_country is US/CA; any other national number needs its country code, because
    guessing one is inventing a digit."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    ext = None
    m = _EXT_RE.search(s)
    if m:
        ext = m.group(1)
        s = s[:m.start()]
    lead = s.lstrip()
    intl = lead.startswith("+") or lead.startswith("00")
    if intl:
        s = re.sub(r"\(\s*0\s*\)", "", s)
    digits = re.sub(r"\D", "", s)
    if lead.startswith("00"):
        digits = digits[2:]
    if not digits:
        return {"e164": None, "ext": ext, "valid": False, "reason": "no digits"}
    if intl:
        ok = 8 <= len(digits) <= 15 and digits[0] != "0"
        if ok and digits[0] == "1":
            ok = len(digits) == 11 and _nanp_ok(digits[1:])
        return {"e164": "+" + digits if ok else None, "ext": ext, "valid": ok,
                "reason": None if ok else "not a valid international number"}
    if (default_country or "").upper() in ("US", "CA", "NANP"):
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]
        if _nanp_ok(digits):
            return {"e164": "+1" + digits, "ext": ext, "valid": True, "reason": None}
        return {"e164": None, "ext": ext, "valid": False, "reason": "not a 10-digit NANP number"}
    return {"e164": None, "ext": ext, "valid": False,
            "reason": "no country code; store it as +<country><number> (only US/CA numbers may omit it)"}


def linkedin_key(url):
    """Suppression match key for a LinkedIn URL: the /in/<slug> path, whatever the scheme,
    www. or country subdomain. Deliberately more aggressive than normalize_linkedin_url,
    which must stay byte-identical to the SQL dedup key."""
    if not url:
        return None
    m = re.search(r"linkedin\.com/(in|pub|company)/([^/?#\s]+)", str(url), re.I)
    if m:
        from urllib.parse import unquote
        return f"{m.group(1).lower()}/{unquote(m.group(2)).lower()}"
    return normalize_linkedin_url(str(url))


FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
    "protonmail.com", "gmx.de", "gmx.net", "web.de", "t-online.de", "yandex.ru", "mail.ru",
    "qq.com", "163.com", "zoho.com",
}

ROLE_LOCALPARTS = {
    "info", "contact", "hello", "sales", "support", "help", "admin", "office", "team",
    "finance", "accounts", "billing", "hr", "jobs", "careers", "marketing", "press",
    "media", "records", "registrar", "reception", "enquiries", "inquiries", "noreply",
    "no-reply", "webmaster", "postmaster", "service", "operations",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NAME_DROP = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "dr", "mr", "mrs", "ms", "mx",
              "esq", "cpa", "mba", "edd", "prof"}


def normalize_email(e):
    if not e:
        return None
    s = str(e).strip().lower()
    return s or None


def email_domain(e):
    e = normalize_email(e)
    if not e or "@" not in e:
        return None
    return normalize_domain(e.rsplit("@", 1)[1])


def person_key(c):
    """The full name with single-letter initials, honorifics and punctuation removed, so
    'Tom Walsh' and 'Tom R. Walsh' match. Hyphenated and middle names are kept: 'Ann
    Lee-Smith' / 'Ann Smith' and 'Mary Ann Smith' / 'Mary Smith' are different people until
    someone says otherwise, and a false match here would drop a real person."""
    first = c.get("first_name") or ""
    last = c.get("last_name") or ""
    raw = f"{first} {last}".strip() or (c.get("full_name") or "")
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    tokens = [t.strip("-'") for t in re.sub(r"[^a-z\s'-]", " ", raw).split()]
    tokens = [t for t in tokens if len(t) > 1 and t not in _NAME_DROP]
    if len(tokens) < 2:
        return None
    return " ".join(tokens)


def company_key(c):
    d = normalize_domain(c.get("company_domain"))
    if d:
        return d
    n = (c.get("company_name") or "").strip().lower()
    return re.sub(r"\s+", " ", n) or None


def _one_edit_apart(a, b):
    """True when a and b differ by exactly one insertion, deletion or substitution."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    edits = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            edits += 1
            if edits > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return edits + (len(b) - j) + (len(a) - i) == 1


def _related_domain(a, b):
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# ===========================================================================
# Config (stdlib reader for gtm.config.yaml, PyYAML when installed)
# ===========================================================================

class ConfigError(Exception):
    pass


def _split_flow(inner):
    parts, cur, q = [], [], None
    for ch in inner:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in _split_flow(v[1:-1])]
    if v.startswith("{") and v.endswith("}"):
        out = {}
        for part in _split_flow(v[1:-1]):
            if ":" not in part:
                raise ConfigError(f"cannot read inline map entry {part!r}")
            k, _, val = part.partition(":")
            out[k.strip()] = _scalar(val)
        return out
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _strip_comment(line):
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _mini_yaml(text):
    """The subset of YAML gtm.config.yaml uses: nested maps, scalars, inline [lists] and
    {maps}, and `- item` block lists of scalars. Anything else raises ConfigError instead
    of being skipped, because a skipped line can silently send storage to the wrong
    backend. Install PyYAML to use full YAML."""
    root = {}
    stack = [(-1, root, None, None)]          # (indent, container, parent, key)
    for n, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"line {n}: tabs in indentation")
        body = _strip_comment(raw)
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip())
        line = body.strip()
        if line == "-" or line.startswith("- "):
            while len(stack) > 1 and stack[-1][0] > indent:
                stack.pop()
            ind, cont, parent, pkey = stack[-1]
            if isinstance(cont, dict) and not cont and parent is not None:
                cont = []
                parent[pkey] = cont
                stack[-1] = (ind, cont, parent, pkey)
            item = line[1:].strip()
            if not isinstance(cont, list) or re.match(r"^[^\"'\[{]*:\s", item + " "):
                raise ConfigError(f"line {n}: unsupported list item {line!r}")
            cont.append(_scalar(item))
            continue
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        _, cont, _, _ = stack[-1]
        if ":" not in line or not isinstance(cont, dict):
            raise ConfigError(f"line {n}: cannot read {line!r}")
        key, _, val = line.partition(":")
        key, val = key.strip().strip("\"'"), val.strip()
        if key == "<<" or val.startswith(("&", "*", "|", ">", "!")):
            raise ConfigError(f"line {n}: anchors, aliases, tags and block scalars need PyYAML")
        if (val.startswith("[") and not val.endswith("]")) or (val.startswith("{") and not val.endswith("}")):
            raise ConfigError(f"line {n}: multi-line flow collections need PyYAML")
        if val == "":
            child = {}
            cont[key] = child
            stack.append((indent, child, cont, key))
        else:
            cont[key] = _scalar(val)
    return root


def load_config(path):
    """Returns {} when there is no config file. Records the file's directory under
    "__dir__" so config-relative paths (storage.local.dir, suppression.file) resolve
    against it rather than against wherever the CLI happens to be run from."""
    if not path or not os.path.exists(path):
        return {"__dir__": os.getcwd()}
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    try:
        import yaml  # optional
        try:
            cfg = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path}: {e}")
    except ImportError:
        try:
            cfg = _mini_yaml(text)
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}")
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    cfg["__dir__"] = os.path.dirname(os.path.abspath(path))
    return cfg


def _cfg(cfg, *keys, default=None):
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


# ===========================================================================
# Local backend
# ===========================================================================

class LocalBackend:
    name = "local"

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.lists_dir = os.path.join(self.root, "lists")
        self.index_path = os.path.join(self.root, "index.json")

    # --- low-level file helpers ------------------------------------------
    def _ensure_dirs(self):
        os.makedirs(self.lists_dir, exist_ok=True)

    def _read_json(self, path, default):
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json_atomic(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _read_jsonl(self, path):
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def _write_jsonl_atomic(self, path, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def _index(self):
        return self._read_json(self.index_path, {"next_list_id": 1, "lists": []})

    def _list_dir(self, list_id):
        return os.path.join(self.lists_dir, str(list_id))

    def _list_meta_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "list.json")

    def _contacts_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "contacts.jsonl")

    def _events_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "events.jsonl")

    def _read_contacts(self, list_id):
        return self._read_jsonl(self._contacts_path(list_id))

    def _write_contacts_atomic(self, list_id, contacts):
        self._write_jsonl_atomic(self._contacts_path(list_id), contacts)

    def _meta(self, list_id):
        meta = self._read_json(self._list_meta_path(list_id), None)
        if meta is None:
            raise NotFound(f"list {list_id} not found in {self.root}")
        return meta

    def default_export_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "export.csv")

    # --- lists ------------------------------------------------------------
    def create_list(self, inp):
        self._ensure_dirs()
        idx = self._index()
        list_id = idx["next_list_id"]
        meta = {
            "list_id": list_id,
            "name": inp.get("name") or f"list-{list_id}",
            "description": inp.get("description"),
            "search_criteria": inp.get("search_criteria"),
            "status": "active",
            "run_state": {},
            "created_at": _now(),
        }
        self._write_json_atomic(self._list_meta_path(list_id), meta)
        # touch an empty contacts file so the layout is visible immediately
        if not os.path.exists(self._contacts_path(list_id)):
            self._write_contacts_atomic(list_id, [])
        idx["next_list_id"] = list_id + 1
        idx["lists"].append({
            "id": list_id, "name": meta["name"],
            "status": "active", "created_at": meta["created_at"],
        })
        self._write_json_atomic(self.index_path, idx)
        return {"list_id": list_id}

    def get_list(self, inp):
        meta = self._meta(int(inp["list_id"]))
        meta.setdefault("run_state", {})
        return {"list": meta}

    def update_list(self, inp):
        list_id = int(inp["list_id"])
        meta = self._meta(list_id)
        patch = inp.get("search_criteria")
        if patch:
            sc = dict(meta.get("search_criteria") or {})
            sc.update(patch)
            meta["search_criteria"] = sc
        if inp.get("status"):
            meta["status"] = inp["status"]
            idx = self._index()
            for e in idx["lists"]:
                if e["id"] == list_id:
                    e["status"] = inp["status"]
            self._write_json_atomic(self.index_path, idx)
        meta["updated_at"] = _now()
        self._write_json_atomic(self._list_meta_path(list_id), meta)
        return {"list_id": list_id, "updated": True}

    def set_run_state(self, list_id, key, value):
        meta = self._meta(int(list_id))
        rs = dict(meta.get("run_state") or {})
        rs[key] = value
        meta["run_state"] = rs
        self._write_json_atomic(self._list_meta_path(int(list_id)), meta)

    # --- contacts ---------------------------------------------------------
    # Contacts carry exactly the schema's columns on both backends, coerced by the same
    # function (coerce_fields), so a value reads back the same whichever backend stored it.
    def upsert_contacts(self, inp):
        list_id = int(inp["list_id"])
        self._meta(list_id)
        incoming = inp.get("contacts", [])
        existing = self._read_contacts(list_id)
        seen_norm = {
            c.get("linkedin_url_normalized")
            for c in existing
            if c.get("linkedin_url_normalized")
        }
        next_id = (max([c.get("id", 0) for c in existing], default=0)) + 1
        inserted, skipped, ignored = 0, 0, set()
        for raw in incoming:
            c, dropped = coerce_fields(raw, allow_stage=True)
            ignored.update(dropped)
            norm = normalize_linkedin_url(c.get("linkedin_url"))
            c["linkedin_url_normalized"] = norm
            # Dedup ONLY on non-null normalized url (mirrors the partial unique index).
            if norm is not None and norm in seen_norm:
                skipped += 1
                continue
            if norm is not None:
                seen_norm.add(norm)
            c["id"] = next_id
            next_id += 1
            c.setdefault("stage", "sourced")
            c.setdefault("provider_ids", {})
            c["sourced_at"] = _now()
            c["created_at"] = _now()
            c["updated_at"] = _now()
            existing.append(c)
            inserted += 1
        self._write_contacts_atomic(list_id, existing)
        out = {"inserted": inserted, "skipped_duplicates": skipped, "total": len(existing)}
        if ignored:
            out["ignored_fields"] = sorted(ignored)
            _log(f"storage: ignored unknown contact field(s) {sorted(ignored)}")
        return out

    def _apply(self, inp, stage):
        list_id = int(inp["list_id"])
        self._meta(list_id)
        ids = {int(x) for x in inp.get("contact_ids", [])}
        fields, dropped = coerce_fields(inp.get("fields", {}) or {})
        if dropped:
            _log(f"storage: ignored unknown contact field(s) {sorted(dropped)}")
        contacts = self._read_contacts(list_id)
        present = {c.get("id") for c in contacts}
        updated = 0
        for c in contacts:
            if c.get("id") in ids:
                if stage is not None:
                    c["stage"] = stage
                for k, v in fields.items():
                    c[k] = v
                    if k == "linkedin_url":
                        c["linkedin_url_normalized"] = normalize_linkedin_url(v)
                ts_field = STAGE_TIMESTAMP.get(stage)
                if ts_field and not c.get(ts_field):
                    c[ts_field] = _now()
                c["updated_at"] = _now()
                updated += 1
        self._write_contacts_atomic(list_id, contacts)
        return {"updated": updated, "not_found": sorted(ids - present)}

    def advance_stage(self, inp):
        stage = inp["stage"]
        if stage not in VALID_STAGES:
            raise ValueError(f"invalid stage: {stage}")
        return self._apply(inp, stage)

    def update_contacts(self, inp):
        return self._apply(inp, None)

    def query_by_stage(self, inp):
        list_id = int(inp["list_id"])
        stage = inp["stage"]
        if stage not in VALID_STAGES:
            raise ValueError(f"invalid stage: {stage}")
        self._meta(list_id)
        contacts = [c for c in self._read_contacts(list_id) if c.get("stage") == stage]
        return {"contacts": contacts}

    def query_list(self, inp):
        list_id = int(inp["list_id"])
        self._meta(list_id)
        return {"contacts": self._read_contacts(list_id)}

    def list_summary(self, inp):
        idx = self._index()
        target = inp.get("list_id")
        target = int(target) if target is not None else None
        out = []
        for entry in idx["lists"]:
            lid = entry["id"]
            if target is not None and lid != target:
                continue
            contacts = self._read_contacts(lid)
            counts = {s: 0 for s in ["sourced", "qualified", "email_enriched", "phone_enriched", "skipped"]}
            for c in contacts:
                st = c.get("stage")
                if st in counts:
                    counts[st] += 1
            out.append({
                "list_id": lid,
                "list_name": entry.get("name"),
                "status": entry.get("status"),
                "created_at": entry.get("created_at"),
                **counts,
                "total_contacts": len(contacts),
            })
        return {"lists": out}

    def export_rows(self, list_id, min_stage):
        if min_stage not in MIN_STAGES:
            raise ValueError(f"invalid min_stage: {min_stage}")
        include = MIN_STAGE_INCLUDES[min_stage]
        contacts = [
            c for c in self._read_contacts(int(list_id))
            if c.get("stage") != "skipped" and c.get("stage") in include
        ]

        # Same order as pipeline_export(): score DESC NULLS LAST, company_name in codepoint
        # order (COLLATE "C") with NULLs last, then id.
        def sort_key(c):
            s = c.get("qualification_score")
            name = c.get("company_name")
            return (s is None, -(s or 0), name is None, name or "", c.get("id", 0))

        contacts.sort(key=sort_key)
        return [{col: c.get(col) for col in EXPORT_COLUMNS} for c in contacts]

    def crossref_master(self, inp):
        # Local backend has no cross-campaign memory — everything is "new".
        # (Enable the Postgres master-dedup backend for real suppression.)
        urls = inp.get("linkedin_urls", [])
        return {"statuses": {u: "new" for u in urls}, "master_enabled": False}

    # --- events (run ledger) ----------------------------------------------
    def log_event(self, list_id, event):
        list_id = int(list_id)
        self._meta(list_id)
        path = self._events_path(list_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def query_events(self, list_id):
        return self._read_jsonl(self._events_path(int(list_id)))

    # --- companies (company_enrich stage) --------------------------------
    def _companies_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "companies.jsonl")

    def _read_companies(self, list_id):
        return self._read_jsonl(self._companies_path(list_id))

    def upsert_companies(self, inp):
        """Insert new companies or merge intel into existing ones (keyed by domain)."""
        list_id = int(inp["list_id"])
        self._meta(list_id)
        incoming = inp.get("companies", [])
        existing = self._read_companies(list_id)
        by_domain = {c.get("company_domain_normalized"): c
                     for c in existing if c.get("company_domain_normalized")}
        next_id = (max([c.get("id", 0) for c in existing], default=0)) + 1
        inserted, updated = 0, 0
        for raw in incoming:
            c = dict(raw)
            norm = normalize_domain(c.get("company_domain"))
            c["company_domain_normalized"] = norm
            target = by_domain.get(norm) if norm else None
            if target is not None:
                for k, v in c.items():
                    if k == "intel" and isinstance(v, dict):
                        merged = dict(target.get("intel") or {})
                        merged.update(v)
                        target["intel"] = merged
                    elif k == "sources" and isinstance(v, list):
                        target["sources"] = list({*(target.get("sources") or []), *v})
                    elif k not in ("id", "created_at"):
                        if v is not None:
                            target[k] = v
                target["updated_at"] = _now()
                updated += 1
            else:
                c["id"] = next_id
                next_id += 1
                c.setdefault("intel", {})
                c["created_at"] = c.get("created_at") or _now()
                c["updated_at"] = _now()
                existing.append(c)
                if norm:
                    by_domain[norm] = c
                inserted += 1
        self._write_jsonl_atomic(self._companies_path(list_id), existing)
        return {"inserted": inserted, "updated": updated, "total": len(existing)}

    def query_companies(self, inp):
        self._meta(int(inp["list_id"]))
        return {"companies": self._read_companies(int(inp["list_id"]))}


# ===========================================================================
# Postgres backend (shells out to psql against $DATABASE_URL)
# ===========================================================================

VALID_STAGES = {"sourced", "qualified", "email_enriched", "phone_enriched", "skipped"}
MIN_STAGES = {"sourced", "qualified", "email_enriched", "phone_enriched"}

# Columns an agent may set via advance_stage / update_contacts fields (everything else
# is ignored — this is the injection guard for field KEYS; values go through literal
# helpers).
ADVANCE_TEXT_COLS = {
    "first_name", "last_name", "full_name", "title", "seniority", "department",
    "company_name", "company_domain", "linkedin_url", "country", "location",
    "source", "db_status", "qualification_status", "matched_persona",
    "company_segment", "qualification_notes", "enrich_recommended",
    "email", "email_source", "email_validation", "email_waterfall_log", "email_source_url",
    "phone", "phone_ext", "phone_type", "phone_source", "phone_validation",
    "phone_waterfall_log", "phone_source_url", "skip_reason",
}
ADVANCE_NUM_COLS = {"lead_quality_score", "qualification_score"}
ADVANCE_JSON_COLS = {"provider_ids"}
INT_COLS = {"qualification_score"}       # INTEGER in schema.sql; the rest of ADVANCE_NUM_COLS is NUMERIC


def coerce_fields(fields, allow_stage=False):
    """The one place a contact value gets its stored type, for both backends: text columns
    hold strings (lists/dicts JSON-encoded, booleans "true"/"false"), numeric columns hold
    finite numbers, provider_ids is an object. Returns (fields, dropped_keys). Unknown keys
    are dropped and reported, never stored on one backend and lost on the other."""
    import math
    out, dropped = {}, []
    for k, v in (fields or {}).items():
        if k in ADVANCE_TEXT_COLS:
            if v is None:
                out[k] = None
            elif isinstance(v, bool):
                out[k] = "true" if v else "false"
            elif isinstance(v, (list, dict)):
                out[k] = json.dumps(v, ensure_ascii=False)
            else:
                out[k] = str(v)
            if out[k] is not None and "$GTMTXT" in out[k]:
                # reserved by the Postgres quoting; rejected on both backends so they agree
                raise ValueError(f"{k}: contains the reserved sequence $GTMTXT")
        elif k in ADVANCE_NUM_COLS:
            if v is None or v == "":
                out[k] = None
                continue
            if isinstance(v, bool):
                raise ValueError(f"{k}: expected a number, got {v!r}")
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k}: expected a number, got {v!r}")
            if not math.isfinite(f):
                raise ValueError(f"{k}: expected a finite number, got {v!r}")
            out[k] = int(round(f)) if k in INT_COLS else (int(f) if f.is_integer() else f)
        elif k in ADVANCE_JSON_COLS:
            if v is None:
                out[k] = {}
            elif isinstance(v, dict):
                out[k] = v
            else:
                raise ValueError(f"{k}: expected an object, got {type(v).__name__}")
        elif k == "stage" and allow_stage:
            if v not in VALID_STAGES:
                raise ValueError(f"invalid stage: {v}")
            out[k] = v
        else:
            dropped.append(k)
    return out, dropped


class PostgresBackend:
    """Shells out to `psql` against $<url_env>. Postgres emits JSON; we parse it.
    Arbitrary text/JSON is embedded via dollar-quoting so no value needs escaping;
    identifiers and stage names are validated against fixed allow-lists."""

    name = "postgres"

    def __init__(self, url_env, master_enabled=False):
        self.url_env = url_env
        self.master_enabled = master_enabled

    # --- psql runner ------------------------------------------------------
    def _psql(self, sql):
        dsn = os.environ.get(self.url_env)
        if not dsn:
            raise RuntimeError(f"{self.url_env} not set in environment")
        env = dict(os.environ)
        # Clear libpq vars so psql can't silently fall back to a local socket.
        for v in ("PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD",
                  "PGSERVICE", "PGOPTIONS"):
            env.pop(v, None)
        proc = subprocess.run(
            ["psql", dsn, "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-f", "-"],
            input=sql, text=True, capture_output=True, env=env,
        )
        if proc.returncode != 0:
            raise BackendError(f"psql failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _json(self, sql):
        out = self._psql(sql)
        return json.loads(out) if out else None

    def default_export_path(self, list_id):
        return os.path.join(".", f"pipeline-export-list-{list_id}.csv")

    # --- literal helpers (dollar-quoted; no escaping needed) --------------
    @staticmethod
    def _tlit(s):
        if s is None:
            return "NULL"
        s = str(s)
        # "$GTMTXT" (without the closing $) also rejects a value ENDING in the tag, which
        # would otherwise merge with the closing delimiter.
        if "$GTMTXT" in s:
            raise ValueError("text contains the reserved sequence $GTMTXT")
        return f"$GTMTXT${s}$GTMTXT$"

    @staticmethod
    def _jlit(obj):
        if obj is None:
            return "NULL"
        js = json.dumps(obj)
        if "$GTMJSON" in js:
            raise ValueError("json contains the reserved sequence $GTMJSON")
        return f"$GTMJSON${js}$GTMJSON$"

    @staticmethod
    def _num(v):
        import math
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            raise ValueError(f"expected a number, got {v!r}")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"expected a finite number, got {v!r}")
        return repr(int(f)) if f.is_integer() else repr(f)

    @staticmethod
    def _stage(s):
        if s not in VALID_STAGES:
            raise ValueError(f"invalid stage: {s}")
        return s

    # --- lists ------------------------------------------------------------
    def create_list(self, inp):
        name = inp.get("name") or "list"
        desc = inp.get("description")
        sc = inp.get("search_criteria")
        sql = (
            "WITH ins AS ("
            "  INSERT INTO pipeline_lists (list_name, description, search_criteria)"
            f" VALUES ({self._tlit(name)}, {self._tlit(desc)}, {self._jlit(sc)}::jsonb)"
            "  RETURNING list_id)"
            " SELECT json_build_object('list_id', (SELECT list_id FROM ins))::text;"
        )
        return self._json(sql)

    def get_list(self, inp):
        lid = int(inp["list_id"])
        row = self._json(
            "SELECT row_to_json(x)::text FROM (SELECT list_id, list_name AS name, description,"
            " search_criteria, status, COALESCE(run_state, '{}'::jsonb) AS run_state,"
            f" created_at, updated_at FROM pipeline_lists WHERE list_id = {lid}) x;")
        if not row:
            raise NotFound(f"list {lid} not found")
        return {"list": row}

    def update_list(self, inp):
        lid = int(inp["list_id"])
        sets = ["updated_at = NOW()"]
        if inp.get("search_criteria"):
            sets.append("search_criteria = COALESCE(search_criteria, '{}'::jsonb) || "
                        f"{self._jlit(inp['search_criteria'])}::jsonb")
        if inp.get("status"):
            sets.append(f"status = {self._tlit(inp['status'])}")
        n = self._json(
            f"WITH u AS (UPDATE pipeline_lists SET {', '.join(sets)} WHERE list_id = {lid}"
            " RETURNING 1) SELECT json_build_object('n', (SELECT count(*) FROM u))::text;")
        if not n or not n["n"]:
            raise NotFound(f"list {lid} not found")
        return {"list_id": lid, "updated": True}

    def set_run_state(self, list_id, key, value):
        lid = int(list_id)
        self._psql(
            "UPDATE pipeline_lists SET run_state = COALESCE(run_state, '{}'::jsonb) || "
            f"jsonb_build_object({self._tlit(key)}, {self._jlit(value)}::jsonb)"
            f" WHERE list_id = {lid};")

    # --- contacts ---------------------------------------------------------
    def upsert_contacts(self, inp):
        lid = int(inp["list_id"])
        self.get_list({"list_id": lid})
        ignored = set()
        contacts = []
        for raw in inp.get("contacts", []):
            c, dropped = coerce_fields(raw, allow_stage=True)
            ignored.update(dropped)
            contacts.append(c)
        arr = self._jlit(contacts)
        cols = sorted(ADVANCE_TEXT_COLS | ADVANCE_NUM_COLS | ADVANCE_JSON_COLS)
        exprs = []
        for col in cols:
            if col in ADVANCE_NUM_COLS:
                exprs.append(f"NULLIF(e->>'{col}','')::numeric")
            elif col in ADVANCE_JSON_COLS:
                exprs.append(f"COALESCE(e->'{col}', '{{}}'::jsonb)")
            else:
                exprs.append(f"e->>'{col}'")
        sql = f"""
WITH input AS (SELECT {arr}::jsonb AS j),
elems AS (
  SELECT e, ord, normalize_linkedin_url(e->>'linkedin_url') AS norm,
         row_number() OVER (PARTITION BY normalize_linkedin_url(e->>'linkedin_url')
                            ORDER BY ord) AS rn
  FROM input, LATERAL jsonb_array_elements(input.j) WITH ORDINALITY AS t(e, ord)
),
deduped AS (SELECT e, ord FROM elems WHERE norm IS NULL OR rn = 1),
ins AS (
  INSERT INTO pipeline_contacts (list_id, stage, {', '.join(cols)})
  SELECT {lid}, COALESCE(e->>'stage','sourced'), {', '.join(exprs)}
  FROM deduped
  ORDER BY ord          -- ids follow input order, as on the local backend
  ON CONFLICT (list_id, linkedin_url_normalized) WHERE linkedin_url_normalized IS NOT NULL
  DO NOTHING
  RETURNING 1)
SELECT json_build_object(
  'inserted', (SELECT count(*) FROM ins),
  'input_count', (SELECT count(*) FROM elems),
  -- NOTE: a data-modifying CTE's rows are NOT visible to a sibling count() in the
  -- same statement (MVCC snapshot), so this is the PRE-insert count. Add inserted.
  'existing_before', (SELECT count(*) FROM pipeline_contacts WHERE list_id = {lid})
)::text;
"""
        r = self._json(sql)
        out = {
            "inserted": r["inserted"],
            "skipped_duplicates": r["input_count"] - r["inserted"],
            "total": r["existing_before"] + r["inserted"],
        }
        if ignored:
            out["ignored_fields"] = sorted(ignored)
            _log(f"storage: ignored unknown contact field(s) {sorted(ignored)}")
        return out

    def _update(self, inp, stage):
        lid = int(inp["list_id"])
        self.get_list({"list_id": lid})
        ids = [int(x) for x in inp.get("contact_ids", [])]
        fields, dropped = coerce_fields(inp.get("fields", {}) or {})
        if dropped:
            _log(f"storage: ignored unknown contact field(s) {sorted(dropped)}")

        sets = ["updated_at = NOW()"]
        if stage is not None:
            sets.append(f"stage = {self._tlit(stage)}")
        for k, v in fields.items():
            if k in ADVANCE_TEXT_COLS:
                sets.append(f"{k} = {self._tlit(v)}")
            elif k in ADVANCE_NUM_COLS:
                sets.append(f"{k} = {self._num(v)}")
            elif k in ADVANCE_JSON_COLS:
                sets.append(f"{k} = {self._jlit(v)}::jsonb")
        if stage is not None:
            for st, col in (("qualified", "qualified_at"),
                            ("email_enriched", "email_enriched_at"),
                            ("phone_enriched", "phone_enriched_at")):
                sets.append(
                    f"{col} = CASE WHEN {self._tlit(stage)} = '{st}' AND {col} IS NULL "
                    f"THEN NOW() ELSE {col} END"
                )
        ids_arr = "ARRAY[" + ",".join(str(i) for i in ids) + "]::int[]"
        set_sql = ", ".join(sets)
        sql = f"""
WITH upd AS (
  UPDATE pipeline_contacts SET {set_sql}
  WHERE list_id = {lid} AND id = ANY({ids_arr})
  RETURNING id)
SELECT json_build_object(
  'updated', (SELECT count(*) FROM upd),
  'not_found', (SELECT COALESCE(json_agg(x), '[]'::json)
                FROM (SELECT unnest({ids_arr}) EXCEPT
                      SELECT id FROM pipeline_contacts WHERE list_id = {lid}) q(x))
)::text;
"""
        return self._json(sql)

    def advance_stage(self, inp):
        return self._update(inp, self._stage(inp["stage"]))

    def update_contacts(self, inp):
        return self._update(inp, None)

    def query_by_stage(self, inp):
        lid = int(inp["list_id"])
        stage = self._stage(inp["stage"])
        sql = (
            "SELECT COALESCE(json_agg(row_to_json(pc) ORDER BY pc.id), '[]'::json)::text "
            f"FROM pipeline_contacts pc WHERE list_id = {lid} AND stage = {self._tlit(stage)};"
        )
        return {"contacts": self._json(sql) or []}

    def query_list(self, inp):
        lid = int(inp["list_id"])
        self.get_list({"list_id": lid})
        sql = ("SELECT COALESCE(json_agg(row_to_json(pc) ORDER BY pc.id), '[]'::json)::text "
               f"FROM pipeline_contacts pc WHERE list_id = {lid};")
        return {"contacts": self._json(sql) or []}

    def list_summary(self, inp):
        target = inp.get("list_id")
        where = f"WHERE list_id = {int(target)}" if target is not None else ""
        sql = (
            "SELECT COALESCE(json_agg(row_to_json(s)), '[]'::json)::text "
            f"FROM pipeline_list_summary s {where};"
        )
        return {"lists": self._json(sql) or []}

    def export_rows(self, list_id, min_stage):
        if min_stage not in MIN_STAGES:
            raise ValueError(f"invalid min_stage: {min_stage}")
        sql = (
            "SELECT COALESCE(json_agg(row_to_json(x)), '[]'::json)::text "
            f"FROM pipeline_export({int(list_id)}, {self._tlit(min_stage)}) x;"
        )
        return self._json(sql) or []

    def crossref_master(self, inp):
        urls = inp.get("linkedin_urls", [])
        if not self.master_enabled:
            return {"statuses": {u: "new" for u in urls}, "master_enabled": False}
        arr = self._jlit(urls)
        sql = (
            "SELECT COALESCE(json_object_agg(input_url, status), '{}'::json)::text "
            f"FROM check_linkedin_urls(ARRAY(SELECT jsonb_array_elements_text({arr}::jsonb)));"
        )
        return {"statuses": self._json(sql) or {}, "master_enabled": True}

    # --- events (run ledger) ----------------------------------------------
    def log_event(self, list_id, event):
        lid = int(list_id)
        self._psql(
            "INSERT INTO pipeline_run_events (list_id, at, stage, status, provider, event)"
            f" VALUES ({lid}, {self._tlit(event['at'])}::timestamptz, {self._tlit(event['stage'])},"
            f" {self._tlit(event['status'])}, {self._tlit(event.get('provider'))},"
            f" {self._jlit(event)}::jsonb);")

    def query_events(self, list_id):
        lid = int(list_id)
        return self._json(
            "SELECT COALESCE(json_agg(event ORDER BY id), '[]'::json)::text"
            f" FROM pipeline_run_events WHERE list_id = {lid};") or []

    # --- companies --------------------------------------------------------
    def upsert_companies(self, inp):
        lid = int(inp["list_id"])
        arr = self._jlit(inp.get("companies", []))
        sql = f"""
WITH input AS (SELECT {arr}::jsonb AS j),
elems AS (SELECT e FROM input, jsonb_array_elements(input.j) e),
ins AS (
  INSERT INTO pipeline_companies (
    list_id, company_name, company_domain, linkedin_url, industry, country, source,
    intel, sources, verified, enriched, enriched_at)
  SELECT {lid},
    e->>'company_name', e->>'company_domain', e->>'linkedin_url',
    e->>'industry', e->>'country', e->>'source',
    COALESCE(e->'intel', '{{}}'::jsonb), COALESCE(e->'sources', '[]'::jsonb),
    COALESCE((e->>'verified')::boolean, false),
    COALESCE((e->>'enriched')::boolean, false),
    CASE WHEN e ? 'enriched_at' THEN (e->>'enriched_at')::timestamptz ELSE NULL END
  FROM elems
  ON CONFLICT (list_id, company_domain_normalized) WHERE company_domain_normalized IS NOT NULL
  DO UPDATE SET
    company_name = COALESCE(EXCLUDED.company_name, pipeline_companies.company_name),
    linkedin_url = COALESCE(EXCLUDED.linkedin_url, pipeline_companies.linkedin_url),
    industry     = COALESCE(EXCLUDED.industry, pipeline_companies.industry),
    country      = COALESCE(EXCLUDED.country, pipeline_companies.country),
    source       = COALESCE(EXCLUDED.source, pipeline_companies.source),
    intel        = pipeline_companies.intel || EXCLUDED.intel,
    sources      = pipeline_companies.sources || EXCLUDED.sources,
    verified     = EXCLUDED.verified OR pipeline_companies.verified,
    enriched     = EXCLUDED.enriched OR pipeline_companies.enriched,
    enriched_at  = COALESCE(EXCLUDED.enriched_at, pipeline_companies.enriched_at)
  RETURNING (xmax = 0) AS inserted)
SELECT json_build_object(
  'inserted', (SELECT count(*) FROM ins WHERE inserted),
  'updated', (SELECT count(*) FROM ins WHERE NOT inserted),
  'existing_before', (SELECT count(*) FROM pipeline_companies WHERE list_id = {lid})
)::text;
"""
        r = self._json(sql)
        return {"inserted": r["inserted"], "updated": r["updated"],
                "total": r["existing_before"] + r["inserted"]}

    def query_companies(self, inp):
        lid = int(inp["list_id"])
        sql = ("SELECT COALESCE(json_agg(row_to_json(c)), '[]'::json)::text "
               f"FROM pipeline_companies c WHERE list_id = {lid};")
        return {"companies": self._json(sql) or []}


# ===========================================================================
# Backend-independent ops — written once on top of the primitives above, so the
# local and Postgres backends cannot drift apart on the gates.
# ===========================================================================

def _check_min_stage(min_stage):
    if min_stage not in MIN_STAGES:
        raise ValueError(f"invalid min_stage {min_stage!r}; use one of {sorted(MIN_STAGES)}")
    return min_stage


def op_export(b, inp, cfg):
    list_id = int(inp["list_id"])
    min_stage = _check_min_stage(inp.get("min_stage", "sourced"))
    rows = b.export_rows(list_id, min_stage)
    out_path = inp.get("out_path") or b.default_export_path(list_id)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in EXPORT_COLUMNS})
    _, qa_state = _run_qa(b, list_id, cfg)          # current state, not the last stored one
    rs = b.get_list({"list_id": list_id})["list"].get("run_state") or {}
    sup = rs.get("suppression")
    applied = bool(sup and sup.get("applied"))
    verdict = {
        "list_id": list_id, "csv": out_path, "rows": len(rows), "min_stage": min_stage,
        "suppression": sup, "qa": qa_state, "generated_at": _now(),
    }
    with open(out_path + ".verdict.json", "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2)
    if not applied:
        _log(f"UNSCRUBBED: list {list_id} has no applied do-not-contact pass. {out_path} has "
             "NOT been checked against your suppression list — run `suppress` before anyone "
             "contacts these rows.")
    return {"rows": rows, "path": out_path, "count": len(rows),
            "verdict_path": out_path + ".verdict.json",
            "suppression_applied": applied,
            "qa_open_errors": qa_state.get("open_errors")}


def op_log_event(b, inp, cfg):
    status = inp.get("status", "ok")
    if status not in EVENT_STATUSES:
        raise ValueError(f"invalid status {status!r}; use one of {sorted(EVENT_STATUSES)}")
    if not inp.get("stage"):
        raise KeyError("stage")
    event = {
        "at": _now(), "stage": inp["stage"], "status": status,
        "provider": inp.get("provider"), "counts": inp.get("counts") or {},
        "warnings": inp.get("warnings") or [], "cost": inp.get("cost") or {},
        "note": inp.get("note"),
    }
    b.log_event(int(inp["list_id"]), event)
    return {"logged": True, "event": event}


def _record(b, list_id, stage, status, **kw):
    ev = {"at": _now(), "stage": stage, "status": status, "provider": kw.get("provider"),
          "counts": kw.get("counts") or {}, "warnings": kw.get("warnings") or [],
          "cost": kw.get("cost") or {}, "note": kw.get("note")}
    b.log_event(list_id, ev)


def op_run_report(b, inp, cfg):
    list_id = int(inp["list_id"])
    meta = b.get_list({"list_id": list_id})["list"]
    summary = (b.list_summary({"list_id": list_id})["lists"] or [{}])[0]
    events = b.query_events(list_id)
    rs = meta.get("run_state") or {}

    # The ledger keeps history; "open" means the LATEST event of each stage. Re-running a
    # stage (or resolving QA) supersedes the warnings its earlier events carried.
    latest = {}
    for e in events:
        latest[e.get("stage")] = e
    open_warnings = []
    for e in latest.values():
        for w in e.get("warnings") or []:
            open_warnings.append({"stage": e.get("stage"), "at": e.get("at"), "warning": w})
        if e.get("status") == "error":
            open_warnings.append({"stage": e.get("stage"), "at": e.get("at"),
                                  "warning": f"stage error: {e.get('note') or 'see event'}"})
    sup = rs.get("suppression")
    if not sup or not sup.get("applied"):
        open_warnings.append({"stage": "suppression", "warning":
                              "no applied do-not-contact pass on this list"})
    if (rs.get("qa") or {}).get("open_errors"):
        open_warnings.append({"stage": "qa", "warning":
                              f"{rs['qa']['open_errors']} unresolved QA error(s)"})

    cost = {}
    for e in events:
        c = e.get("cost") or {}
        unit = c.get("unit") or "credits"
        slot = cost.setdefault(unit, {"estimate": 0, "actual": 0})
        for k in ("estimate", "actual"):
            if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool):
                slot[k] += c[k]
    done = sorted({e["stage"] for e in latest.values() if e.get("status") in ("ok", "warn")})
    skipped = sorted({e["stage"] for e in latest.values() if e.get("status") == "skipped"})
    return {
        "list": {k: meta.get(k) for k in ("list_id", "name", "description", "status", "created_at")},
        "plan": (meta.get("search_criteria") or {}).get("plan"),
        "summary": summary, "stages_logged": done, "stages_skipped": skipped, "events": events,
        "open_warnings": open_warnings,
        "gates": {"suppression": sup, "qa": rs.get("qa"), "preflight": rs.get("preflight")},
        "cost": cost,
    }


# --- suppression ------------------------------------------------------------

_SUP_COLS = {
    "email": {"email", "e_mail", "email_address", "work_email", "emailaddress"},
    "domain": {"domain", "company_domain", "website", "company_website", "web", "site"},
    "phone": {"phone", "phone_number", "direct_dial", "mobile", "mobile_phone", "telephone", "tel"},
    "linkedin": {"linkedin_url", "linkedin", "linkedin_profile", "linkedin_profile_url", "profile_url"},
}
_SUP_META = {"reason", "note", "notes", "comment", "added", "added_at", "date", "source", "name",
             "first_name", "last_name", "full_name", "company", "company_name"}


def _hdr(h):
    return re.sub(r"[\s\-]+", "_", (h or "").strip().lower())


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_suppression(path, default_country):
    """Read a do-not-contact CSV. Columns are matched loosely (`Email Address`, `Company
    Domain`, `Phone Number`, `LinkedIn` all work), or a two-column type,value file.

    Returns (entries, stats). stats counts what could NOT be used — unrecognised columns and
    values that do not parse — because an entry the loader silently drops is a person the
    list silently stops protecting."""
    entries = {"email": {}, "domain": {}, "phone": {}, "linkedin": {}}
    stats = {"data_rows": 0, "recognised": 0, "unrecognised_columns": [], "unparseable": []}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        raw_headers = rdr.fieldnames or []
        headers = [_hdr(h) for h in raw_headers]
        typed = "type" in headers and "value" in headers
        col_kind = {}
        for h in headers:
            for kind, names in _SUP_COLS.items():
                if h in names:
                    col_kind[h] = kind
        if not typed:
            stats["unrecognised_columns"] = [raw for raw, h in zip(raw_headers, headers)
                                             if h not in col_kind and h not in _SUP_META and h]
        for raw in rdr:
            row = {_hdr(k): (v or "").strip() for k, v in raw.items() if k is not None}
            if not any(row.values()):
                continue
            stats["data_rows"] += 1
            reason = row.get("reason") or row.get("note") or "on do-not-contact list"
            pairs = []
            if typed:
                kind = _hdr(row.get("type"))
                for k, names in _SUP_COLS.items():
                    if kind == k or kind in names:
                        pairs.append((k, row.get("value", "")))
                        break
                else:
                    if row.get("value"):
                        stats["unparseable"].append(f"type {row.get('type')!r}: {row.get('value')}")
            else:
                pairs = [(col_kind[h], row[h]) for h in col_kind if row.get(h)]
            for kind, val in pairs:
                if not val:
                    continue
                if kind == "email":
                    e = normalize_email(val)
                    if e and _EMAIL_RE.match(e):
                        entries["email"][e] = reason
                    else:
                        stats["unparseable"].append(f"email {val!r}")
                        continue
                elif kind == "domain":
                    d = normalize_domain(val)
                    if d and "." in d:
                        entries["domain"][d] = reason
                    else:
                        stats["unparseable"].append(f"domain {val!r}")
                        continue
                elif kind == "phone":
                    n = normalize_phone(val, default_country)
                    if n and n["e164"]:
                        entries["phone"][n["e164"]] = reason
                    else:
                        stats["unparseable"].append(f"phone {val!r} ({(n or {}).get('reason')})")
                        continue
                elif kind == "linkedin":
                    k = linkedin_key(val)
                    if k:
                        entries["linkedin"][k] = reason
                    else:
                        stats["unparseable"].append(f"linkedin {val!r}")
                        continue
                stats["recognised"] += 1
    return entries, stats


def _suppression_problems(stats):
    """Reasons the file cannot be called 'applied' in full."""
    probs = []
    if stats["data_rows"] and not stats["recognised"]:
        probs.append("the file has rows but no recognised values; expected columns email, domain, "
                     f"phone or linkedin_url (unrecognised: {stats['unrecognised_columns'] or 'none'})")
    if stats["unparseable"]:
        probs.append(f"{len(stats['unparseable'])} entr{'y' if len(stats['unparseable']) == 1 else 'ies'} "
                     f"could not be read, e.g. {stats['unparseable'][:3]} — fix them (phones need "
                     "+<country> unless qa.default_country is US/CA)")
    return probs


def _domain_hit(domain, blocked):
    """A blocked domain suppresses itself and its subdomains, never its parent — so a
    shared system domain can be listed per campus without suppressing every campus."""
    if not domain:
        return None
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in blocked:
            return cand
    return None


def _resolve(path, cfg):
    """Config-relative paths resolve against the config file's directory."""
    if not path or os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(cfg.get("__dir__") or os.getcwd(), path))


def _configured_suppression(cfg):
    return _resolve(_cfg(cfg, "suppression", "file", default="context/do-not-contact.csv"), cfg)


def _suppression_path(inp, cfg):
    if inp.get("file"):
        return os.path.abspath(inp["file"])
    return _configured_suppression(cfg)


def _match_contact(c, entries, default_country):
    e = normalize_email(c.get("email"))
    if e and e in entries["email"]:
        return f"email {e}", entries["email"][e]
    for d in (email_domain(c.get("email")), normalize_domain(c.get("company_domain"))):
        hit = _domain_hit(d, entries["domain"])
        if hit:
            return f"domain {hit}", entries["domain"][hit]
    p = normalize_phone(c.get("phone"), default_country)
    if p and p["e164"] and p["e164"] in entries["phone"]:
        return f"phone {p['e164']}", entries["phone"][p["e164"]]
    li = linkedin_key(c.get("linkedin_url"))
    if li and li in entries["linkedin"]:
        return "linkedin_url", entries["linkedin"][li]
    return None


def op_check_suppression(b, inp, cfg):
    path = _suppression_path(inp, cfg)
    country = _cfg(cfg, "qa", "default_country", default="US")
    if not os.path.exists(path):
        return {"applied": False, "file": path,
                "reason": f"suppression file not found: {path}", "matches": {}}
    entries, stats = _load_suppression(path, country)
    probs = _suppression_problems(stats)
    matches = {}
    for d in inp.get("domains") or []:
        hit = _domain_hit(normalize_domain(d), entries["domain"])
        if hit:
            matches[d] = f"domain {hit}: {entries['domain'][hit]}"
    for key, field in (("emails", "email"), ("phones", "phone"), ("linkedin_urls", "linkedin_url")):
        for v in inp.get(key) or []:
            m = _match_contact({field: v}, entries, country)
            if m:
                matches[v] = f"{m[0]}: {m[1]}"
    out = {"applied": not probs, "file": path, "matches": matches}
    if probs:
        out["reason"] = "; ".join(probs)
    return out


def op_suppress(b, inp, cfg):
    list_id = int(inp["list_id"])
    posture = inp.get("posture", "research")
    if posture not in ("research", "send"):
        raise ValueError("posture must be 'research' or 'send'")
    path = _suppression_path(inp, cfg)
    country = _cfg(cfg, "qa", "default_country", default="US")
    contacts = [c for c in b.query_list({"list_id": list_id})["contacts"]
                if c.get("stage") != "skipped"]
    max_id = max([c.get("id", 0) for c in contacts], default=0)

    def refuse(reason, checked=0, suppressed=None, extra=None):
        verdict = {"applied": False, "posture": posture, "file": path, "at": _now(),
                   "checked": checked, "suppressed": len(suppressed or []),
                   "max_checked_id": None, "reason": reason, **(extra or {})}
        b.set_run_state(list_id, "suppression", verdict)
        _record(b, list_id, "suppress", "error" if posture == "send" else "warn",
                warnings=[reason], note=f"posture={posture}")
        _log(f"DO-NOT-CONTACT NOT APPLIED on list {list_id}: {reason}")
        if posture == "send":
            raise GateBlocked({"verdict": verdict, "suppressed": suppressed or []})
        return {"verdict": verdict, "suppressed": suppressed or []}

    if not os.path.exists(path):
        return refuse(f"suppression file not found: {path}. Create it (a header-only file means "
                      "'my list is empty') or set suppression.file.")

    entries, stats = _load_suppression(path, country)
    suppressed = []
    for c in contacts:
        m = _match_contact(c, entries, country)
        if m:
            b.advance_stage({"list_id": list_id, "contact_ids": [c["id"]], "stage": "skipped",
                             "fields": {"skip_reason": f"do-not-contact ({m[0]}): {m[1]}"}})
            suppressed.append({"id": c["id"], "matched": m[0]})
    probs = _suppression_problems(stats)
    if probs:
        # Whatever could be read has been applied; the verdict still says the list was not
        # applied in full, so the send boundary refuses until the file is fixed.
        return refuse("; ".join(probs), checked=len(contacts), suppressed=suppressed,
                      extra={"entries": {k: len(v) for k, v in entries.items()}})
    warnings = []
    if not stats["data_rows"]:
        warnings.append(f"{path} has no entries — the list was checked against an empty "
                        "do-not-contact list")
    if stats["unrecognised_columns"]:
        warnings.append(f"ignored columns in {os.path.basename(path)}: {stats['unrecognised_columns']}")
    verdict = {"applied": True, "posture": posture, "file": path, "file_sha256": _sha256(path),
               "at": _now(), "entries": {k: len(v) for k, v in entries.items()},
               "checked": len(contacts), "suppressed": len(suppressed),
               "max_checked_id": max_id, "warnings": warnings}
    b.set_run_state(list_id, "suppression", verdict)
    _record(b, list_id, "suppress", "warn" if warnings else "ok", warnings=warnings,
            counts={"checked": len(contacts), "suppressed": len(suppressed)},
            note=f"posture={posture}")
    return {"verdict": verdict, "suppressed": suppressed}


# --- QA ---------------------------------------------------------------------

_QA_FIELDS = ("first_name", "last_name", "full_name", "email", "phone", "phone_ext", "phone_type",
              "company_domain", "company_name", "linkedin_url", "stage")


def _finding(code, severity, cs, detail, **extra):
    """A finding's key names the rows AND fingerprints the values it judged, so a `keep`
    stops applying the moment those values change."""
    import hashlib
    cs = sorted(cs, key=lambda c: c["id"])
    ids = [c["id"] for c in cs]
    fp = hashlib.sha1(json.dumps([[c.get(k) for k in _QA_FIELDS] for c in cs],
                                 default=str).encode()).hexdigest()[:8]
    f = {"key": f"{code}:{','.join(str(i) for i in ids)}:{fp}", "code": code,
         "severity": severity, "contact_ids": ids, "detail": detail}
    f.update(extra)
    return f


def _richness(c):
    return sum(1 for k in ("email", "phone", "linkedin_url", "title", "company_domain")
               if c.get(k))


def _domain_typo(edom, cdom):
    """One label differs by exactly one edit, that label is not the TLD, and it is long
    enough that a one-letter difference is more likely a slip than a different name
    (`cityoriverton.org` vs `cityofriverton.org`; not `bank.be` vs `bank.de`)."""
    a, b = edom.split("."), cdom.split(".")
    if len(a) != len(b):
        return False
    diffs = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diffs) != 1 or diffs[0] == len(a) - 1:
        return False
    i = diffs[0]
    return min(len(a[i]), len(b[i])) >= 5 and _one_edit_apart(a[i], b[i])


def qa_findings(contacts, default_country="US", other_org_patterns=None):
    """Deterministic checks for the defects that survive a name+org dedupe and a human
    read-through. Every finding names the rows and, where the fix is mechanical, carries
    a suggestion that `qa_resolve action=fix` can apply."""
    live = [c for c in contacts if c.get("stage") != "skipped"]
    findings = []

    def dup_finding(code, cs, detail):
        keep = sorted(cs, key=lambda c: (-_richness(c), c["id"]))[0]
        return _finding(code, "error", cs, detail, keep_id=keep["id"],
                        drop_ids=sorted(c["id"] for c in cs if c["id"] != keep["id"]))

    groups = {}
    for c in live:
        pk, ck = person_key(c), company_key(c)
        if pk and ck:
            groups.setdefault((pk, ck), []).append(c)
    for (pk, ck), cs in groups.items():
        if len(cs) > 1:
            names = sorted({(c.get("full_name") or f"{c.get('first_name') or ''} {c.get('last_name') or ''}").strip()
                            for c in cs})
            findings.append(dup_finding("duplicate_person", cs,
                                        f"{' / '.join(names)} at {ck} look like one person"))

    by_email = {}
    for c in live:
        e = normalize_email(c.get("email"))
        if e:
            by_email.setdefault(e, []).append(c)
    for e, cs in by_email.items():
        if len(cs) > 1:
            findings.append(dup_finding("duplicate_email", cs, f"{e} appears on {len(cs)} rows"))

    by_phone = {}
    for c in live:
        n = normalize_phone(c.get("phone"), default_country)
        if n and n["e164"] and (c.get("phone_type") or "") in ("mobile", "direct_dial"):
            by_phone.setdefault(n["e164"], []).append(c)
    for p, cs in by_phone.items():
        if len(cs) > 1:
            findings.append(_finding(
                "shared_phone_labelled_direct", "error", cs,
                f"{p} is on {len(cs)} people but labelled as their own line; a shared number "
                "is a switchboard or department line", suggestion={"phone_type": "switchboard"}))

    patterns = [p.lower() for p in (other_org_patterns or []) if p]
    for c in live:
        email = normalize_email(c.get("email"))
        cdom = normalize_domain(c.get("company_domain"))
        if not cdom:
            findings.append(_finding("missing_domain", "warn", [c],
                                     "no company_domain; enrichers will skip this row"))
        if email:
            if not _EMAIL_RE.match(email):
                findings.append(_finding("email_invalid", "error", [c], f"{email!r} is not an email"))
            else:
                local, _, edom = email.rpartition("@")
                edom = normalize_domain(edom)
                if local.split("+")[0] in ROLE_LOCALPARTS:
                    findings.append(_finding("role_mailbox", "warn", [c],
                                             f"{email} is a shared mailbox, not a person"))
                if edom in FREEMAIL:
                    findings.append(_finding("freemail", "warn", [c], f"{email} is a personal address"))
                elif cdom and not _related_domain(edom, cdom):
                    if _domain_typo(edom, cdom):
                        findings.append(_finding(
                            "email_domain_typo", "error", [c],
                            f"{email}: domain {edom} is one character off the company domain {cdom}",
                            suggestion={"email": f"{local}@{cdom}"}))
                    elif any(p in edom and p not in cdom for p in patterns):
                        findings.append(_finding(
                            "email_other_org", "error", [c],
                            f"{email} belongs to a different kind of organisation than {cdom} "
                            "(matched qa.other_org_patterns)"))
                    else:
                        findings.append(_finding(
                            "email_domain_mismatch", "warn", [c],
                            f"{email} is not on the company domain {cdom}; confirm it is the same org"))
        if c.get("phone"):
            n = normalize_phone(c.get("phone"), default_country)
            if not n["valid"]:
                findings.append(_finding("phone_invalid", "error", [c],
                                         f"{c['phone']!r}: {n['reason']}"))
            elif c.get("phone") != n["e164"] or (n["ext"] and c.get("phone_ext") != n["ext"]):
                sug = {"phone": n["e164"]}
                if n["ext"]:
                    sug["phone_ext"] = n["ext"]
                findings.append(_finding("phone_not_e164", "error", [c],
                                         f"{c['phone']!r} is not dialable as stored", suggestion=sug))
        if not email and not c.get("linkedin_url") and c.get("stage") in ("email_enriched", "phone_enriched"):
            findings.append(_finding("no_reachable_identity", "warn", [c],
                                     "no email and no LinkedIn URL; a sequencer cannot reach this row"))
    return findings


def _run_qa(b, list_id, cfg, default_country=None):
    rs = b.get_list({"list_id": list_id})["list"].get("run_state") or {}
    prev = rs.get("qa") or {}
    # A country passed to `qa` is remembered, so qa_resolve / preflight / export judge the
    # same findings (and the same keys) that the user was shown.
    country = default_country or prev.get("default_country") or _cfg(cfg, "qa", "default_country", default="US")
    patterns = _cfg(cfg, "qa", "other_org_patterns", default=[]) or []
    if not isinstance(patterns, list):
        raise ValueError("qa.other_org_patterns must be a list")
    contacts = b.query_list({"list_id": list_id})["contacts"]
    acked = set(prev.get("acknowledged") or [])
    findings = qa_findings(contacts, country, patterns)
    for f in findings:
        f["acknowledged"] = f["key"] in acked
    open_errors = sum(1 for f in findings if f["severity"] == "error" and not f["acknowledged"])
    counts = {}
    for f in findings:
        counts[f["code"]] = counts.get(f["code"], 0) + 1
    state = {"at": _now(), "open_errors": open_errors,
             "warnings": sum(1 for f in findings if f["severity"] == "warn"),
             "counts": counts, "default_country": country,
             # keep only acknowledgements that still match a current finding
             "acknowledged": sorted(acked & {f["key"] for f in findings})}
    b.set_run_state(list_id, "qa", state)
    return findings, state


def op_qa(b, inp, cfg):
    list_id = int(inp["list_id"])
    findings, state = _run_qa(b, list_id, cfg, inp.get("default_country"))
    _record(b, list_id, "qa", "warn" if state["open_errors"] else "ok",
            counts=state["counts"],
            warnings=[f"{state['open_errors']} unresolved QA error(s)"] if state["open_errors"] else [])
    return {"findings": findings, "counts": state["counts"], "open_errors": state["open_errors"]}


_MERGE_FIELDS = ("email", "email_source", "email_validation", "email_source_url", "phone", "phone_ext",
                 "phone_type", "phone_source", "phone_validation", "phone_source_url", "linkedin_url",
                 "title", "seniority", "country", "location")


def op_qa_resolve(b, inp, cfg):
    list_id = int(inp["list_id"])
    action = inp.get("action")
    if action not in ("drop", "keep", "fix"):
        raise ValueError("action must be drop | keep | fix")
    keys = inp.get("keys") or ([inp["key"]] if inp.get("key") else [])
    if not keys:
        raise KeyError("keys")
    if inp.get("fields") and len(keys) > 1:
        raise ValueError("`fields` applies to one finding; pass a single key, or omit fields to "
                         "apply each finding's own suggestion")
    findings, state = _run_qa(b, list_id, cfg)
    by_key = {f["key"]: f for f in findings}
    missing = [k for k in keys if k not in by_key]
    if missing:
        raise NotFound(f"no current QA finding with key(s) {missing}; the data may have changed — "
                       "re-run qa for current keys")
    if action == "fix":
        nofix = [k for k in keys if not (inp.get("fields") or by_key[k].get("suggestion"))]
        if nofix:
            raise ValueError(f"{nofix} have no mechanical fix; pass fields (one key), or use drop/keep")
    # Everything validated before the first write, so a bad key cannot leave a half-applied batch.
    note = inp.get("note") or ""
    resolved = []
    acked = set(state.get("acknowledged") or [])
    rows = {c["id"]: c for c in b.query_list({"list_id": list_id})["contacts"]}
    for k in keys:
        f = by_key[k]
        if action == "drop":
            ids = f.get("drop_ids") or f["contact_ids"]
            merged = {}
            if f.get("keep_id"):
                # Keep the person, lose nothing: fill the kept row's blanks from the dropped rows.
                keep = rows[f["keep_id"]]
                for i in ids:
                    for fld in _MERGE_FIELDS:
                        if not keep.get(fld) and not merged.get(fld) and rows[i].get(fld):
                            merged[fld] = rows[i][fld]
                if merged:
                    b.update_contacts({"list_id": list_id, "contact_ids": [f["keep_id"]], "fields": merged})
            b.advance_stage({"list_id": list_id, "contact_ids": ids, "stage": "skipped",
                             "fields": {"skip_reason": f"qa {f['code']}: {note or f['detail']}"}})
            resolved.append({"key": k, "action": "drop", "contact_ids": ids, "merged_into_kept": merged})
        elif action == "keep":
            acked.add(k)
            resolved.append({"key": k, "action": "keep"})
        else:
            fields = inp.get("fields") or f.get("suggestion")
            b.update_contacts({"list_id": list_id, "contact_ids": f["contact_ids"], "fields": fields})
            resolved.append({"key": k, "action": "fix", "fields": fields})
    if action == "keep":
        rs_qa = dict(state)
        rs_qa["acknowledged"] = sorted(acked)
        b.set_run_state(list_id, "qa", rs_qa)
    _record(b, list_id, "qa_resolve", "ok", counts={action: len(resolved)}, note=note or None)
    _, new_state = _run_qa(b, list_id, cfg)
    _record(b, list_id, "qa", "warn" if new_state["open_errors"] else "ok", counts=new_state["counts"],
            warnings=[f"{new_state['open_errors']} unresolved QA error(s)"] if new_state["open_errors"] else [])
    return {"resolved": resolved, "qa": new_state}


# --- activation preflight ---------------------------------------------------

def op_preflight_activate(b, inp, cfg):
    """The send boundary. Fails closed. Takes no override for the suppression requirement
    (that is config, not a per-call choice), and does not trust the stored verdict alone:
    it re-reads the configured file and re-matches every row about to be sent, so a row
    edited, un-skipped or added after the last pass cannot slip through."""
    list_id = int(inp["list_id"])
    min_stage = _check_min_stage(inp.get("min_stage", "email_enriched"))
    required = _cfg(cfg, "suppression", "required_for_activation", default=True) is not False
    path = _configured_suppression(cfg)
    country = _cfg(cfg, "qa", "default_country", default="US")
    contacts = b.query_list({"list_id": list_id})["contacts"]
    include = MIN_STAGE_INCLUDES[min_stage]
    ready = [c for c in contacts if c.get("stage") in include]
    blockers, warnings = [], []
    gate = blockers if required else warnings

    if not ready:
        blockers.append(f"no contacts at stage {min_stage} or later")

    rs = b.get_list({"list_id": list_id})["list"].get("run_state") or {}
    sup = rs.get("suppression")
    if not sup or not sup.get("applied"):
        gate.append("no applied do-not-contact pass: run `suppress` with posture=send"
                    + (f" ({sup.get('reason')})" if sup and sup.get("reason") else ""))
    else:
        if sup.get("posture") != "send":
            gate.append("the last do-not-contact pass was research posture; run `suppress` with "
                        "posture=send before activating")
        if os.path.abspath(sup.get("file") or "") != os.path.abspath(path):
            gate.append(f"the last pass used {sup.get('file')}, but suppression.file is {path}; "
                        "run `suppress` posture=send against the configured file")
        elif os.path.exists(path) and sup.get("file_sha256") and _sha256(path) != sup["file_sha256"]:
            gate.append(f"{path} changed after the last pass; run `suppress` posture=send again")
        newer = [c["id"] for c in ready if c["id"] > (sup.get("max_checked_id") or 0)]
        if newer:
            gate.append(f"{len(newer)} contact(s) were added after the last do-not-contact pass; "
                        "run `suppress` again")

    # Live re-match against the configured file. A match here always blocks, whatever the
    # config says: it is a person on the list you are about to send to.
    if os.path.exists(path):
        entries, stats = _load_suppression(path, country)
        probs = _suppression_problems(stats)
        if probs:
            gate.append(f"{path}: " + "; ".join(probs))
        hits = []
        for c in ready:
            m = _match_contact(c, entries, country)
            if m:
                hits.append(f"{c['id']} ({m[0]})")
        if hits:
            blockers.append(f"{len(hits)} row(s) about to be sent match the do-not-contact list: "
                            f"{', '.join(hits[:10])} — run `suppress` posture=send")
    else:
        gate.append(f"do-not-contact file not found: {path}")

    findings, state = _run_qa(b, list_id, cfg)
    ready_ids = {c["id"] for c in ready}
    open_err = [f for f in findings if f["severity"] == "error" and not f["acknowledged"]
                and ready_ids.intersection(f["contact_ids"])]
    if open_err:
        by_code = {}
        for f in open_err:
            by_code[f["code"]] = by_code.get(f["code"], 0) + 1
        blockers.append("unresolved QA errors: " + ", ".join(f"{k} x{v}" for k, v in sorted(by_code.items()))
                        + " — resolve with `qa_resolve` (drop | keep | fix)")
    no_identity = [c["id"] for c in ready if not c.get("email") and not c.get("linkedin_url")]
    if no_identity:
        warnings.append(f"{len(no_identity)} contact(s) have no email and no LinkedIn URL and "
                        "will be skipped by the sequencer")
    result = {"ok": not blockers, "blockers": blockers, "warnings": warnings,
              "leads_ready": len(ready) - len(no_identity),
              "leads_without_identity": len(no_identity), "min_stage": min_stage}
    b.set_run_state(list_id, "preflight", {"at": _now(), **result})
    _record(b, list_id, "preflight_activate", "ok" if result["ok"] else "error",
            warnings=blockers + warnings, counts={"leads_ready": result["leads_ready"]})
    if blockers:
        raise GateBlocked(result)
    return result


# ===========================================================================
# Dispatch
# ===========================================================================

BACKEND_OPS = {
    "create_list", "get_list", "update_list", "upsert_contacts", "advance_stage",
    "update_contacts", "query_by_stage", "query_list", "list_summary", "crossref_master",
    "upsert_companies", "query_companies",
}
SHARED_OPS = {
    "export": op_export,
    "log_event": op_log_event,
    "run_report": op_run_report,
    "check_suppression": op_check_suppression,
    "suppress": op_suppress,
    "qa": op_qa,
    "qa_resolve": op_qa_resolve,
    "preflight_activate": op_preflight_activate,
}
OPS = BACKEND_OPS | set(SHARED_OPS)


def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "on", "1"))


def make_backend(args, cfg):
    backend = args.backend or os.environ.get("GTM_BACKEND") or _cfg(cfg, "storage", "backend") or "local"
    if backend == "local":
        if args.dir or os.environ.get("GTM_DATA_DIR"):
            d = args.dir or os.environ.get("GTM_DATA_DIR")
        else:
            d = _resolve(_cfg(cfg, "storage", "local", "dir", default="./.gtm-data"), cfg)
        _log(f"storage: local ({d})")
        return LocalBackend(d)
    if backend == "postgres":
        url_env = args.db_url_env or _cfg(cfg, "storage", "postgres", "url_env") or "DATABASE_URL"
        master = args.master or _truthy(_cfg(cfg, "storage", "postgres", "enable_master_dedup"))
        _log(f"storage: postgres (${url_env}{', master dedup on' if master else ''})")
        return PostgresBackend(url_env, master_enabled=master)
    raise SystemExit(f"unknown backend: {backend}")


def load_input(args):
    if args.input is not None:
        raw = args.input
    elif args.input_file is not None:
        with open(args.input_file, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    raw = raw.strip()
    if not raw:
        return {}
    return json.loads(raw)


def _fail(code, etype, message):
    print(json.dumps({"error": {"type": etype, "message": message}}))
    sys.exit(code)


def _lock(backend):
    """Serialize local-backend ops: stage agents may call the CLI in parallel, and the
    local files are read-modify-write. (Postgres handles its own concurrency.)"""
    if not isinstance(backend, LocalBackend):
        return None
    try:
        import fcntl
    except ImportError:          # Windows: no advisory locks; run stages sequentially there
        return None
    os.makedirs(backend.root, exist_ok=True)
    fh = open(os.path.join(backend.root, ".lock"), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def main():
    p = argparse.ArgumentParser(description="GTM pipeline storage CLI")
    p.add_argument("op", choices=sorted(OPS))
    p.add_argument("--config", default="gtm.config.yaml",
                   help="config file to read storage/suppression/qa settings from")
    p.add_argument("--backend", choices=["local", "postgres"], help="override storage.backend")
    p.add_argument("--dir", help="override storage.local.dir")
    p.add_argument("--db-url-env", help="override storage.postgres.url_env")
    p.add_argument("--master", action="store_true",
                   help="postgres: enable master cross-campaign dedup (crossref_master)")
    p.add_argument("--input", help="JSON input object")
    p.add_argument("--input-file", help="path to a JSON input file")
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        _fail(3, "bad_config", str(e))
    backend = make_backend(args, cfg)
    try:
        inp = load_input(args)
    except json.JSONDecodeError as e:
        _fail(2, "bad_input_json", str(e))

    lock = _lock(backend)
    try:
        if args.op in SHARED_OPS:
            result = SHARED_OPS[args.op](backend, inp, cfg)
        else:
            result = getattr(backend, args.op)(inp)
    except GateBlocked as g:
        print(json.dumps(g.result, ensure_ascii=False, default=str))
        sys.exit(6)
    except KeyError as e:
        _fail(2, "missing_field", f"required field {e}")
    except (ValueError, TypeError) as e:
        _fail(2, "bad_value", str(e))
    except NotFound as e:
        _fail(4, "not_found", str(e))
    except BackendError as e:
        _fail(5, "backend_error", str(e))
    except ConfigError as e:
        _fail(3, "bad_config", str(e))
    except RuntimeError as e:
        _fail(3, "not_configured", str(e))
    finally:
        if lock:
            lock.close()

    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
