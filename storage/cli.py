#!/usr/bin/env python3
"""
storage/cli.py — uniform storage operations for the GTM pipeline.

Agents NEVER write raw SQL or file IO inline. They call this CLI with a small,
fixed op set and a single JSON object on --input; it returns canonical JSON on
stdout (logs go to stderr). Stage-handoff semantics are identical across backends.

    python3 storage/cli.py <op> --backend <local|postgres> [--dir DIR] \
        --input '<JSON>'

Ops (input JSON shape -> output JSON shape):
  create_list      {name, description?, search_criteria?}            -> {list_id}
  upsert_contacts  {list_id, contacts:[Contact,...]}                 -> {inserted, skipped_duplicates, total}
  advance_stage    {list_id, contact_ids:[int], stage, fields?:{}}   -> {updated, not_found:[int]}
  query_by_stage   {list_id, stage}                                  -> {contacts:[Contact,...]}
  list_summary     {list_id?}                                        -> {lists:[{...counts}]}
  export           {list_id, min_stage?, out_path?}                  -> {rows:[...], path, count}
  crossref_master  {linkedin_urls:[str]}                             -> {statuses:{url: "new"|...}}

Backends:
  local     -> ./.gtm-data (JSON/JSONL/CSV). Zero setup. (this file, no deps)
  postgres  -> shells out to `psql` against $DATABASE_URL. (Phase 2)

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
from datetime import datetime, timezone


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
EXPORT_COLUMNS = [
    "first_name", "last_name", "full_name", "title", "seniority",
    "company_name", "company_domain", "linkedin_url", "country", "location",
    "email", "phone", "phone_type", "source", "matched_persona",
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


def _now():
    return datetime.now(timezone.utc).isoformat()


def _log(msg):
    print(msg, file=sys.stderr)


# ===========================================================================
# Local backend
# ===========================================================================

class LocalBackend:
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

    def _index(self):
        return self._read_json(self.index_path, {"next_list_id": 1, "lists": []})

    def _list_dir(self, list_id):
        return os.path.join(self.lists_dir, str(list_id))

    def _list_meta_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "list.json")

    def _contacts_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "contacts.jsonl")

    def _read_contacts(self, list_id):
        path = self._contacts_path(list_id)
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def _write_contacts_atomic(self, list_id, contacts):
        path = self._contacts_path(list_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for c in contacts:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    # --- ops --------------------------------------------------------------
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

    def upsert_contacts(self, inp):
        list_id = inp["list_id"]
        incoming = inp.get("contacts", [])
        existing = self._read_contacts(list_id)
        seen_norm = {
            c.get("linkedin_url_normalized")
            for c in existing
            if c.get("linkedin_url_normalized")
        }
        next_id = (max([c.get("id", 0) for c in existing], default=0)) + 1
        inserted, skipped = 0, 0
        for raw in incoming:
            c = dict(raw)
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
            c.setdefault("sourced_at", _now())
            c["created_at"] = c.get("created_at") or _now()
            c["updated_at"] = _now()
            existing.append(c)
            inserted += 1
        self._write_contacts_atomic(list_id, existing)
        return {"inserted": inserted, "skipped_duplicates": skipped, "total": len(existing)}

    def advance_stage(self, inp):
        list_id = inp["list_id"]
        ids = set(inp.get("contact_ids", []))
        stage = inp["stage"]
        fields = inp.get("fields", {}) or {}
        contacts = self._read_contacts(list_id)
        present = {c.get("id") for c in contacts}
        updated = 0
        for c in contacts:
            if c.get("id") in ids:
                c["stage"] = stage
                for k, v in fields.items():
                    c[k] = v
                ts_field = STAGE_TIMESTAMP.get(stage)
                if ts_field and not c.get(ts_field):
                    c[ts_field] = _now()
                c["updated_at"] = _now()
                updated += 1
        self._write_contacts_atomic(list_id, contacts)
        return {"updated": updated, "not_found": sorted(ids - present)}

    def query_by_stage(self, inp):
        list_id = inp["list_id"]
        stage = inp["stage"]
        contacts = [c for c in self._read_contacts(list_id) if c.get("stage") == stage]
        return {"contacts": contacts}

    def list_summary(self, inp):
        idx = self._index()
        target = inp.get("list_id")
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

    def export(self, inp):
        list_id = inp["list_id"]
        min_stage = inp.get("min_stage", "sourced")
        include = MIN_STAGE_INCLUDES.get(min_stage, MIN_STAGE_INCLUDES["sourced"])
        contacts = [
            c for c in self._read_contacts(list_id)
            if c.get("stage") != "skipped" and c.get("stage") in include
        ]

        def sort_key(c):
            s = c.get("qualification_score")
            has = 0 if s is not None else 1                 # scored rows first (NULLS LAST)
            neg = -(s if s is not None else 0)              # DESC by score
            return (has, neg, (c.get("company_name") or ""))

        contacts.sort(key=sort_key)
        rows = [{col: c.get(col) for col in EXPORT_COLUMNS} for c in contacts]

        out_path = inp.get("out_path") or os.path.join(self._list_dir(list_id), "export.csv")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if v is None else v) for k, v in r.items()})
        return {"rows": rows, "path": out_path, "count": len(rows)}

    def crossref_master(self, inp):
        # Local backend has no cross-campaign memory — everything is "new".
        # (Enable the Postgres master-dedup backend for real suppression.)
        urls = inp.get("linkedin_urls", [])
        return {"statuses": {u: "new" for u in urls}, "master_enabled": False}

    # --- companies (company_enrich stage) --------------------------------
    def _companies_path(self, list_id):
        return os.path.join(self._list_dir(list_id), "companies.jsonl")

    def _read_companies(self, list_id):
        path = self._companies_path(list_id)
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def upsert_companies(self, inp):
        """Insert new companies or merge intel into existing ones (keyed by domain)."""
        list_id = inp["list_id"]
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
        path = self._companies_path(list_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for c in existing:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return {"inserted": inserted, "updated": updated, "total": len(existing)}

    def query_companies(self, inp):
        return {"companies": self._read_companies(inp["list_id"])}


# ===========================================================================
# Postgres backend (Phase 2 — shells out to psql against $DATABASE_URL)
# ===========================================================================

VALID_STAGES = {"sourced", "qualified", "email_enriched", "phone_enriched", "skipped"}
MIN_STAGES = {"sourced", "qualified", "email_enriched", "phone_enriched"}

# Columns an agent may set via advance_stage.fields (everything else is ignored —
# this is the injection guard for field KEYS; values go through literal helpers).
ADVANCE_TEXT_COLS = {
    "first_name", "last_name", "full_name", "title", "seniority", "department",
    "company_name", "company_domain", "linkedin_url", "country", "location",
    "source", "db_status", "qualification_status", "matched_persona",
    "company_segment", "qualification_notes", "enrich_recommended",
    "email", "email_source", "email_validation", "email_waterfall_log",
    "phone", "phone_type", "phone_source", "phone_validation", "phone_waterfall_log",
}
ADVANCE_NUM_COLS = {"lead_quality_score", "qualification_score"}
ADVANCE_JSON_COLS = {"provider_ids"}


class PostgresBackend:
    """Shells out to `psql` against $<url_env>. Postgres emits JSON; we parse it.
    Arbitrary text/JSON is embedded via dollar-quoting so no value needs escaping;
    identifiers and stage names are validated against fixed allow-lists."""

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
            raise RuntimeError(f"psql failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _json(self, sql):
        out = self._psql(sql)
        return json.loads(out) if out else None

    # --- literal helpers (dollar-quoted; no escaping needed) --------------
    @staticmethod
    def _tlit(s):
        if s is None:
            return "NULL"
        s = str(s)
        if "$GTMTXT$" in s:
            raise ValueError("text contains reserved delimiter")
        return f"$GTMTXT${s}$GTMTXT$"

    @staticmethod
    def _jlit(obj):
        if obj is None:
            return "NULL"
        js = json.dumps(obj)
        if "$GTMJSON$" in js:
            raise ValueError("json contains reserved delimiter")
        return f"$GTMJSON${js}$GTMJSON$"

    @staticmethod
    def _num(v):
        if v is None:
            return "NULL"
        float(v)  # validates numeric; raises ValueError otherwise
        return str(v)

    @staticmethod
    def _stage(s):
        if s not in VALID_STAGES:
            raise ValueError(f"invalid stage: {s}")
        return s

    # --- ops --------------------------------------------------------------
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

    def upsert_contacts(self, inp):
        lid = int(inp["list_id"])
        contacts = inp.get("contacts", [])
        arr = self._jlit(contacts)
        sql = f"""
WITH input AS (SELECT {arr}::jsonb AS j),
elems AS (
  SELECT e, normalize_linkedin_url(e->>'linkedin_url') AS norm,
         row_number() OVER (PARTITION BY normalize_linkedin_url(e->>'linkedin_url')
                            ORDER BY ord) AS rn
  FROM input, LATERAL jsonb_array_elements(input.j) WITH ORDINALITY AS t(e, ord)
),
deduped AS (SELECT e FROM elems WHERE norm IS NULL OR rn = 1),
ins AS (
  INSERT INTO pipeline_contacts (
    list_id, stage, first_name, last_name, full_name, title, seniority, department,
    company_name, company_domain, linkedin_url, country, location, source, db_status,
    lead_quality_score, provider_ids)
  SELECT {lid},
    COALESCE(e->>'stage','sourced'),
    e->>'first_name', e->>'last_name', e->>'full_name', e->>'title', e->>'seniority',
    e->>'department', e->>'company_name', e->>'company_domain', e->>'linkedin_url',
    e->>'country', e->>'location', e->>'source', e->>'db_status',
    NULLIF(e->>'lead_quality_score','')::numeric,
    COALESCE(e->'provider_ids', '{{}}'::jsonb)
  FROM deduped
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
        return {
            "inserted": r["inserted"],
            "skipped_duplicates": r["input_count"] - r["inserted"],
            "total": r["existing_before"] + r["inserted"],
        }

    def advance_stage(self, inp):
        lid = int(inp["list_id"])
        ids = [int(x) for x in inp.get("contact_ids", [])]
        stage = self._stage(inp["stage"])
        fields = inp.get("fields", {}) or {}

        sets = [f"stage = {self._tlit(stage)}"]
        for k, v in fields.items():
            if k in ADVANCE_TEXT_COLS:
                sets.append(f"{k} = {self._tlit(v)}")
            elif k in ADVANCE_NUM_COLS:
                sets.append(f"{k} = {self._num(v)}")
            elif k in ADVANCE_JSON_COLS:
                sets.append(f"{k} = {self._jlit(v)}::jsonb")
            # unknown keys ignored
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

    def query_by_stage(self, inp):
        lid = int(inp["list_id"])
        stage = self._stage(inp["stage"])
        sql = (
            "SELECT COALESCE(json_agg(row_to_json(pc)), '[]'::json)::text "
            f"FROM pipeline_contacts pc WHERE list_id = {lid} AND stage = {self._tlit(stage)};"
        )
        return {"contacts": self._json(sql) or []}

    def list_summary(self, inp):
        target = inp.get("list_id")
        where = f"WHERE list_id = {int(target)}" if target is not None else ""
        sql = (
            "SELECT COALESCE(json_agg(row_to_json(s)), '[]'::json)::text "
            f"FROM pipeline_list_summary s {where};"
        )
        return {"lists": self._json(sql) or []}

    def export(self, inp):
        lid = int(inp["list_id"])
        min_stage = inp.get("min_stage", "sourced")
        if min_stage not in MIN_STAGES:
            raise ValueError(f"invalid min_stage: {min_stage}")
        sql = (
            "SELECT COALESCE(json_agg(row_to_json(x)), '[]'::json)::text "
            f"FROM pipeline_export({lid}, {self._tlit(min_stage)}) x;"
        )
        rows = self._json(sql) or []
        out_path = inp.get("out_path") or os.path.join(".", f"pipeline-export-list-{lid}.csv")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in EXPORT_COLUMNS})
        return {"rows": rows, "path": out_path, "count": len(rows)}

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
# Dispatch
# ===========================================================================

OPS = {
    "create_list", "upsert_contacts", "advance_stage", "query_by_stage",
    "list_summary", "export", "crossref_master",
    "upsert_companies", "query_companies",
}


def make_backend(args):
    if args.backend == "local":
        return LocalBackend(args.dir)
    if args.backend == "postgres":
        return PostgresBackend(args.db_url_env, master_enabled=args.master)
    raise SystemExit(f"unknown backend: {args.backend}")


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


def main():
    p = argparse.ArgumentParser(description="GTM pipeline storage CLI")
    p.add_argument("op", choices=sorted(OPS))
    p.add_argument("--backend", default="local", choices=["local", "postgres"])
    p.add_argument("--dir", default="./.gtm-data", help="local backend data dir")
    p.add_argument("--db-url-env", default="DATABASE_URL", help="env var with the postgres URL")
    p.add_argument("--master", action="store_true",
                   help="postgres: enable master cross-campaign dedup (crossref_master)")
    p.add_argument("--input", help="JSON input object")
    p.add_argument("--input-file", help="path to a JSON input file")
    args = p.parse_args()

    backend = make_backend(args)
    try:
        inp = load_input(args)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": {"type": "bad_input_json", "message": str(e)}}))
        sys.exit(2)

    try:
        result = getattr(backend, args.op)(inp)
    except NotImplementedError as e:
        print(json.dumps({"error": {"type": "not_implemented", "message": str(e)}}))
        sys.exit(3)
    except KeyError as e:
        print(json.dumps({"error": {"type": "missing_field", "message": f"required field {e}"}}))
        sys.exit(2)

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
