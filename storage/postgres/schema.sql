-- GTM Pipeline — Postgres storage schema (self-contained)
-- Apply this first:  psql "$DATABASE_URL" -f storage/postgres/schema.sql
-- Optional cross-campaign suppression lives in master-optional.sql (apply after).
--
-- Stages: sourced -> qualified -> email_enriched -> phone_enriched (plus 'skipped').
-- One change from the original private schema: apollo_id VARCHAR -> provider_ids JSONB
-- (so a swapped-in enricher can stash its own native id). Everything else is the
-- proven schema, bundled so the trigger's helper functions exist BEFORE the trigger.

-- =============================================================================
-- HELPER FUNCTIONS  (must be defined BEFORE the trigger that calls them)
-- The local backend (storage/cli.py: normalize_linkedin_url) re-implements this
-- byte-identically for dedup parity. Do not change one without the other.
-- =============================================================================

CREATE OR REPLACE FUNCTION normalize_linkedin_url(url TEXT)
RETURNS TEXT AS $$
BEGIN
    IF url IS NULL OR url = '' THEN
        RETURN NULL;
    END IF;
    RETURN LOWER(
        regexp_replace(
            regexp_replace(url, '\?.*$', ''),
            '/$', ''
        )
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION normalize_company_name(name TEXT)
RETURNS TEXT AS $$
BEGIN
    IF name IS NULL OR name = '' THEN
        RETURN NULL;
    END IF;
    RETURN LOWER(TRIM(regexp_replace(name, '\s+', ' ', 'g')));
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Company dedup key. MUST match normalize_domain() in storage/cli.py:
-- lowercase, drop protocol, host only, drop leading www., drop trailing dot.
CREATE OR REPLACE FUNCTION normalize_domain(d TEXT)
RETURNS TEXT AS $$
BEGIN
    IF d IS NULL OR d = '' THEN
        RETURN NULL;
    END IF;
    RETURN NULLIF(
        rtrim(
            regexp_replace(
                split_part(regexp_replace(lower(trim(d)), '^https?://', ''), '/', 1),
                '^www\.', ''
            ),
            '.'
        ),
        ''
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- =============================================================================
-- PIPELINE TABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS pipeline_lists (
    list_id SERIAL PRIMARY KEY,
    list_name VARCHAR(500) NOT NULL,
    description TEXT,
    created_by VARCHAR(255) DEFAULT 'unknown',
    search_criteria JSONB,  -- {titles, seniority, geography, persona, companies, expansion}
    status VARCHAR(50) DEFAULT 'active',  -- active, completed, archived
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_lists_status ON pipeline_lists(status);
CREATE INDEX IF NOT EXISTS idx_pipeline_lists_created ON pipeline_lists(created_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_contacts (
    id SERIAL PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES pipeline_lists(list_id) ON DELETE CASCADE,

    -- Stage tracking
    stage VARCHAR(50) NOT NULL DEFAULT 'sourced',
        -- sourced, qualified, email_enriched, phone_enriched, skipped

    -- Identity (populated at sourced stage)
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    full_name VARCHAR(500),
    title VARCHAR(500),
    seniority VARCHAR(100),
    department VARCHAR(255),
    company_name VARCHAR(500),
    company_domain VARCHAR(255),
    linkedin_url VARCHAR(500),
    linkedin_url_normalized VARCHAR(500),
    country VARCHAR(255),
    location VARCHAR(500),

    -- Sourcing metadata
    source VARCHAR(50),       -- provider name (apollo, lemlist, dropleads, apify, ...)
    db_status VARCHAR(50),    -- new, previously_contacted, replied, interested, unknown
    lead_quality_score NUMERIC,
    provider_ids JSONB DEFAULT '{}'::jsonb,   -- {"apollo":"...", "dropleads":123} (was apollo_id)

    -- Qualification fields (populated at qualified stage)
    qualification_status VARCHAR(50),  -- QUALIFY, MAYBE, SKIP
    qualification_score INTEGER,
    matched_persona VARCHAR(255),
    company_segment VARCHAR(10),       -- A, B, C
    qualification_notes TEXT,
    enrich_recommended VARCHAR(10),

    -- Email fields (populated at email_enriched stage)
    email VARCHAR(500),
    email_source VARCHAR(50),       -- provider | not_found
    email_validation VARCHAR(100),
    email_waterfall_log TEXT,

    -- Phone fields (populated at phone_enriched stage)
    phone VARCHAR(100),
    phone_type VARCHAR(50),         -- mobile, direct_dial, switchboard
    phone_source VARCHAR(50),       -- provider | not_found
    phone_validation VARCHAR(100),
    phone_waterfall_log TEXT,

    -- Timestamps
    sourced_at TIMESTAMP DEFAULT NOW(),
    qualified_at TIMESTAMP,
    email_enriched_at TIMESTAMP,
    phone_enriched_at TIMESTAMP,

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Dedup within a list: one entry per normalized LinkedIn URL per list.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_contacts_list_linkedin
    ON pipeline_contacts(list_id, linkedin_url_normalized)
    WHERE linkedin_url_normalized IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pipeline_contacts_list_stage
    ON pipeline_contacts(list_id, stage);
CREATE INDEX IF NOT EXISTS idx_pipeline_contacts_stage
    ON pipeline_contacts(stage);
CREATE INDEX IF NOT EXISTS idx_pipeline_contacts_company
    ON pipeline_contacts(list_id, company_name);
-- Lookup by any provider id (generalizes the old apollo_id index).
CREATE INDEX IF NOT EXISTS idx_pipeline_contacts_provider_ids
    ON pipeline_contacts USING GIN (provider_ids);

-- Auto-normalize LinkedIn URL + bump updated_at on insert/update.
CREATE OR REPLACE FUNCTION pipeline_contacts_normalize_linkedin()
RETURNS TRIGGER AS $$
BEGIN
    NEW.linkedin_url_normalized := normalize_linkedin_url(NEW.linkedin_url);
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pipeline_contacts_normalize ON pipeline_contacts;
CREATE TRIGGER trg_pipeline_contacts_normalize
    BEFORE INSERT OR UPDATE ON pipeline_contacts
    FOR EACH ROW EXECUTE FUNCTION pipeline_contacts_normalize_linkedin();

-- =============================================================================
-- COMPANIES (company_enrich stage) — account-level intel, keyed by list_id + domain
-- =============================================================================

CREATE TABLE IF NOT EXISTS pipeline_companies (
    id SERIAL PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES pipeline_lists(list_id) ON DELETE CASCADE,
    company_name VARCHAR(500),
    company_domain VARCHAR(255),
    company_domain_normalized VARCHAR(255),
    linkedin_url VARCHAR(500),
    industry VARCHAR(255),
    country VARCHAR(255),
    source VARCHAR(50),                 -- provider that discovered it
    intel JSONB DEFAULT '{}'::jsonb,    -- {founded_year, hq_location, description,
                                        --  estimated_employees, funding_stage, total_raised,
                                        --  investors[], tech_stack[], leadership[], signals[], custom{}}
    sources JSONB DEFAULT '[]'::jsonb,  -- evidence URLs
    verified BOOLEAN DEFAULT FALSE,
    enriched BOOLEAN DEFAULT FALSE,
    enriched_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_companies_list_domain
    ON pipeline_companies(list_id, company_domain_normalized)
    WHERE company_domain_normalized IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pipeline_companies_list ON pipeline_companies(list_id);

CREATE OR REPLACE FUNCTION pipeline_companies_normalize_domain()
RETURNS TRIGGER AS $$
BEGIN
    NEW.company_domain_normalized := normalize_domain(NEW.company_domain);
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pipeline_companies_normalize ON pipeline_companies;
CREATE TRIGGER trg_pipeline_companies_normalize
    BEFORE INSERT OR UPDATE ON pipeline_companies
    FOR EACH ROW EXECUTE FUNCTION pipeline_companies_normalize_domain();

-- =============================================================================
-- HELPER VIEWS
-- =============================================================================

CREATE OR REPLACE VIEW pipeline_list_summary AS
SELECT
    pl.list_id,
    pl.list_name,
    pl.description,
    pl.created_by,
    pl.status,
    pl.created_at,
    COUNT(*) FILTER (WHERE pc.stage = 'sourced') AS sourced,
    COUNT(*) FILTER (WHERE pc.stage = 'qualified') AS qualified,
    COUNT(*) FILTER (WHERE pc.stage = 'email_enriched') AS email_enriched,
    COUNT(*) FILTER (WHERE pc.stage = 'phone_enriched') AS phone_enriched,
    COUNT(*) FILTER (WHERE pc.stage = 'skipped') AS skipped,
    COUNT(pc.id) AS total_contacts
FROM pipeline_lists pl
LEFT JOIN pipeline_contacts pc ON pl.list_id = pc.list_id
GROUP BY pl.list_id
ORDER BY pl.created_at DESC;

CREATE OR REPLACE VIEW pipeline_contacts_detail AS
SELECT
    pc.*,
    pl.list_name,
    pl.description AS list_description
FROM pipeline_contacts pc
JOIN pipeline_lists pl ON pc.list_id = pl.list_id;

-- =============================================================================
-- EXPORT FUNCTION  — column order MUST match storage/cli.py EXPORT_COLUMNS
-- =============================================================================

CREATE OR REPLACE FUNCTION pipeline_export(p_list_id INTEGER, p_min_stage VARCHAR DEFAULT 'sourced')
RETURNS TABLE (
    first_name VARCHAR,
    last_name VARCHAR,
    full_name VARCHAR,
    title VARCHAR,
    seniority VARCHAR,
    company_name VARCHAR,
    company_domain VARCHAR,
    linkedin_url VARCHAR,
    country VARCHAR,
    location VARCHAR,
    email VARCHAR,
    phone VARCHAR,
    phone_type VARCHAR,
    source VARCHAR,
    matched_persona VARCHAR,
    qualification_score INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        pc.first_name, pc.last_name, pc.full_name,
        pc.title, pc.seniority,
        pc.company_name, pc.company_domain,
        pc.linkedin_url, pc.country, pc.location,
        pc.email, pc.phone, pc.phone_type,
        pc.source, pc.matched_persona, pc.qualification_score
    FROM pipeline_contacts pc
    WHERE pc.list_id = p_list_id
      AND pc.stage != 'skipped'
      AND CASE p_min_stage
          WHEN 'sourced' THEN TRUE
          WHEN 'qualified' THEN pc.stage IN ('qualified', 'email_enriched', 'phone_enriched')
          WHEN 'email_enriched' THEN pc.stage IN ('email_enriched', 'phone_enriched')
          WHEN 'phone_enriched' THEN pc.stage = 'phone_enriched'
          ELSE TRUE
      END
    ORDER BY pc.qualification_score DESC NULLS LAST, pc.company_name;
END;
$$ LANGUAGE plpgsql;
