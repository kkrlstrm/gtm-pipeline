-- GTM Pipeline — OPTIONAL cross-campaign suppression (master dedup)
-- Apply AFTER schema.sql (it reuses normalize_linkedin_url()):
--     psql "$DATABASE_URL" -f storage/postgres/master-optional.sql
-- Then set storage.postgres.enable_master_dedup: true in gtm.config.yaml.
--
-- WITHOUT this, the pipeline has NO cross-campaign memory: it cannot tell that a
-- contact already bounced or unsubscribed in a previous campaign, so an adopter
-- could re-contact a bounced/opted-out address. This table is the upgrade path.
--
-- master_contacts is the canonical record of everyone you have ever contacted.
-- How it gets populated is up to you (sync from your sending tool / CRM) — this
-- file only provides the table + the dedup lookup the sourcer calls.

CREATE TABLE IF NOT EXISTS master_contacts (
    contact_id SERIAL PRIMARY KEY,
    linkedin_url VARCHAR(500),
    linkedin_url_normalized VARCHAR(500),
    email VARCHAR(500),
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    full_name VARCHAR(500),
    company_name VARCHAR(500),

    overall_status VARCHAR(50) DEFAULT 'contacted',
        -- contacted | engaged | interested | replied | bounced | unsubscribed
    total_campaigns INTEGER DEFAULT 0,
    has_replied BOOLEAN DEFAULT FALSE,
    has_bounced BOOLEAN DEFAULT FALSE,
    has_unsubscribed BOOLEAN DEFAULT FALSE,

    first_contacted_at TIMESTAMP,
    last_contacted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),

    UNIQUE (linkedin_url_normalized)
);

CREATE INDEX IF NOT EXISTS idx_master_contacts_email ON master_contacts(email);

-- Keep linkedin_url_normalized in sync (reuses schema.sql's function).
CREATE OR REPLACE FUNCTION master_contacts_normalize_linkedin()
RETURNS TRIGGER AS $$
BEGIN
    NEW.linkedin_url_normalized := normalize_linkedin_url(NEW.linkedin_url);
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_master_contacts_normalize ON master_contacts;
CREATE TRIGGER trg_master_contacts_normalize
    BEFORE INSERT OR UPDATE ON master_contacts
    FOR EACH ROW EXECUTE FUNCTION master_contacts_normalize_linkedin();

-- Batch dedup lookup. Returns one row per input URL with a framework-canonical
-- status the contact-sourcer consumes:
--   new | previously_contacted | replied | interested | bounced | unsubscribed
-- Usage: SELECT * FROM check_linkedin_urls(ARRAY['https://linkedin.com/in/x', ...]);
CREATE OR REPLACE FUNCTION check_linkedin_urls(urls TEXT[])
RETURNS TABLE (
    input_url TEXT,
    found BOOLEAN,
    status TEXT,
    first_name VARCHAR,
    last_name VARCHAR,
    company_name VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        u.url AS input_url,
        (mc.contact_id IS NOT NULL) AS found,
        CASE
            WHEN mc.contact_id IS NULL                 THEN 'new'
            WHEN mc.has_unsubscribed                   THEN 'unsubscribed'
            WHEN mc.has_bounced AND NOT mc.has_replied THEN 'bounced'
            WHEN mc.overall_status = 'interested'      THEN 'interested'
            WHEN mc.has_replied                        THEN 'replied'
            ELSE 'previously_contacted'
        END AS status,
        mc.first_name,
        mc.last_name,
        mc.company_name
    FROM unnest(urls) AS u(url)
    LEFT JOIN master_contacts mc
        ON normalize_linkedin_url(u.url) = mc.linkedin_url_normalized;
END;
$$ LANGUAGE plpgsql;
