-- GOJEP Tenders Database Schema for Supabase
-- Two-table structure:
--   gojep_tenders_all     -> Historical archive (all tenders ever observed)
--   gojep_tenders_current -> Active tenders only (submission deadline has not passed)

CREATE TABLE gojep_tenders_all (
    -- Primary Keys & Identifiers
    id BIGSERIAL PRIMARY KEY,
    resource_id TEXT UNIQUE NOT NULL,
    competition_unique_id TEXT,
    
    -- Basic Info (from listing page)
    row_number INTEGER,
    title TEXT NOT NULL,
    detail_url TEXT,
    procuring_entity TEXT,
    procuring_entity_url TEXT, -- Link to entity details
    
    -- Procurement Details
    procurement_type TEXT,
    services_subtype TEXT,
    procurement_method TEXT,
    procedure TEXT, -- Same as procurement_method, kept for compatibility
    evaluation_mechanism TEXT,
    procurement_technique TEXT,
    
    -- Descriptions
    description TEXT, -- Short description from tooltip
    detailed_description TEXT, -- Full description from detail page
    combined_description TEXT,
    
    -- Categories & Classifications
    ppc_ncc_categories TEXT[], -- Array of category codes
    cpv_codes TEXT[], -- Common Procurement Vocabulary codes
    
    -- Tender Status & Flags
    retender_flag TEXT,
    framework_agreement_establishment BOOLEAN,
    contract_awarded_in_lots BOOLEAN,
    pe_audit_correspondence BOOLEAN,
    special_differential_treatment TEXT,
    
    -- Project Information
    project_reference_number TEXT,
    country_contract_performance TEXT DEFAULT 'Jamaica',
    funding_source TEXT,
    non_petroleum_indicator TEXT,
    number_of_stages INTEGER,
    
    -- Critical Dates (UTC-5 timezone)
    submission_deadline TIMESTAMPTZ,
    original_submission_deadline TIMESTAMPTZ,
    clarification_period_end TIMESTAMPTZ,
    bid_opening_date TIMESTAMPTZ,
    site_visit_date TIMESTAMPTZ,
    publication_date TIMESTAMPTZ,
    submission_deadline_parsed TIMESTAMPTZ,
    publication_date_parsed TIMESTAMPTZ,
    
    -- Time Calculations
    bid_deadline_days_remaining INTEGER,
    bid_deadline_hours_remaining INTEGER,
    
    -- Document URLs
    pdf_url TEXT,
    
    -- Extraction Metadata
    extraction_timestamp TIMESTAMPTZ DEFAULT NOW(),
    source_url TEXT,
    detail_page_extracted BOOLEAN DEFAULT FALSE,
    extraction_errors TEXT[],
    
    -- Search & Indexing
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(description, '')), 'B') ||
        setweight(to_tsvector('english', COALESCE(procuring_entity, '')), 'C')
    ) STORED
);

-- Indexes for performance
CREATE INDEX idx_gojep_tenders_all_resource_id ON gojep_tenders_all(resource_id);
CREATE INDEX idx_gojep_tenders_all_submission_deadline ON gojep_tenders_all(submission_deadline);
CREATE INDEX idx_gojep_tenders_all_submission_deadline_parsed ON gojep_tenders_all(submission_deadline_parsed);
CREATE INDEX idx_gojep_tenders_all_procuring_entity ON gojep_tenders_all(procuring_entity);
CREATE INDEX idx_gojep_tenders_all_procurement_type ON gojep_tenders_all(procurement_type);
CREATE INDEX idx_gojep_tenders_all_extraction_timestamp ON gojep_tenders_all(extraction_timestamp);
CREATE INDEX idx_gojep_tenders_all_search ON gojep_tenders_all USING GIN(search_vector);

-- Enable Row Level Security
ALTER TABLE gojep_tenders_all ENABLE ROW LEVEL SECURITY;

-- Create policy for public read access (adjust as needed)
CREATE POLICY "Public read access (archive)" ON gojep_tenders_all
    FOR SELECT USING (true);

-- Comments for documentation
COMMENT ON TABLE gojep_tenders_all IS 'GOJEP tender history with comprehensive data from both listing and detail pages.';
COMMENT ON COLUMN gojep_tenders_all.resource_id IS 'Unique identifier from GOJEP system.';
COMMENT ON COLUMN gojep_tenders_all.detail_page_extracted IS 'Flag indicating if detail page data has been scraped.';
COMMENT ON COLUMN gojep_tenders_all.search_vector IS 'Full-text search index combining title, description, and entity.';


-- -------------------------------------------------------------------
-- Active tenders table (current opportunities)
-- -------------------------------------------------------------------

CREATE TABLE gojep_tenders_current (
    id BIGSERIAL PRIMARY KEY,
    resource_id TEXT UNIQUE NOT NULL REFERENCES gojep_tenders_all(resource_id) ON DELETE CASCADE,
    competition_unique_id TEXT,
    
    row_number INTEGER,
    title TEXT NOT NULL,
    detail_url TEXT,
    procuring_entity TEXT,
    procuring_entity_url TEXT,
    
    procurement_type TEXT,
    services_subtype TEXT,
    procurement_method TEXT,
    procedure TEXT,
    evaluation_mechanism TEXT,
    procurement_technique TEXT,
    
    description TEXT,
    detailed_description TEXT,
    combined_description TEXT,
    
    ppc_ncc_categories TEXT[],
    cpv_codes TEXT[],
    
    retender_flag TEXT,
    framework_agreement_establishment BOOLEAN,
    contract_awarded_in_lots BOOLEAN,
    pe_audit_correspondence BOOLEAN,
    special_differential_treatment TEXT,
    
    project_reference_number TEXT,
    country_contract_performance TEXT DEFAULT 'Jamaica',
    funding_source TEXT,
    non_petroleum_indicator TEXT,
    number_of_stages INTEGER,
    
    submission_deadline TIMESTAMPTZ,
    original_submission_deadline TIMESTAMPTZ,
    clarification_period_end TIMESTAMPTZ,
    bid_opening_date TIMESTAMPTZ,
    site_visit_date TIMESTAMPTZ,
    publication_date TIMESTAMPTZ,
    submission_deadline_parsed TIMESTAMPTZ,
    publication_date_parsed TIMESTAMPTZ,
    
    bid_deadline_days_remaining INTEGER,
    bid_deadline_hours_remaining INTEGER,
    
    pdf_url TEXT,
    
    extraction_timestamp TIMESTAMPTZ DEFAULT NOW(),
    source_url TEXT,
    detail_page_extracted BOOLEAN DEFAULT FALSE,
    extraction_errors TEXT[],
    
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(description, '')), 'B') ||
        setweight(to_tsvector('english', COALESCE(procuring_entity, '')), 'C')
    ) STORED
);

CREATE INDEX idx_gojep_tenders_current_resource_id ON gojep_tenders_current(resource_id);
CREATE INDEX idx_gojep_tenders_current_submission_deadline ON gojep_tenders_current(submission_deadline);
CREATE INDEX idx_gojep_tenders_current_submission_deadline_parsed ON gojep_tenders_current(submission_deadline_parsed);
CREATE INDEX idx_gojep_tenders_current_search ON gojep_tenders_current USING GIN(search_vector);

ALTER TABLE gojep_tenders_current ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read access (current)" ON gojep_tenders_current
    FOR SELECT USING (true);

COMMENT ON TABLE gojep_tenders_current IS 'Active GOJEP tenders with submission deadlines in the future.';

-- -------------------------------------------------------------------
-- Contract awards tables
-- -------------------------------------------------------------------

-- Stores rows from modules/awards/get_awards.py snapshots (awards_*.json)
-- Keyed by `resource_id` from the GOJEP system.

CREATE TABLE IF NOT EXISTS gojep_awards_all (
    id BIGSERIAL PRIMARY KEY,
    resource_id TEXT UNIQUE NOT NULL,

    row_number INTEGER,

    procurement_method TEXT,
    procuring_entity TEXT,
    title TEXT NOT NULL,

    contract_amount NUMERIC,
    contract_amount_raw TEXT,

    award_date TIMESTAMPTZ,
    award_date_raw TEXT,

    contract_url TEXT,

    pdf_url TEXT,
    pdf_resource_id TEXT,

    extraction_timestamp TIMESTAMPTZ DEFAULT NOW(),
    source_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_gojep_awards_all_resource_id ON gojep_awards_all(resource_id);
CREATE INDEX IF NOT EXISTS idx_gojep_awards_all_award_date ON gojep_awards_all(award_date);

ALTER TABLE gojep_awards_all ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read access (awards_all)" ON gojep_awards_all
    FOR SELECT USING (true);

COMMENT ON TABLE gojep_awards_all IS 'GOJEP contract awards archive extracted from viewCaNotices.do.';

-- -------------------------------------------------------------------
-- Contract award details (parsed PDF fields)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gojep_award_details_all (
    id BIGSERIAL PRIMARY KEY,
    resource_id TEXT UNIQUE NOT NULL REFERENCES gojep_awards_all(resource_id) ON DELETE CASCADE,

    -- Applicant/contracting authority side
    official_name TEXT,
    postal_address TEXT,
    tender_reference_number TEXT,

    -- Contractor side
    name_of_contractor TEXT,

    -- PPC + CPV
    ppc_category_code_and_title TEXT[],
    cpv_codes TEXT[],

    -- Contract price
    contract_price_raw TEXT,
    contract_price_amount NUMERIC,
    contract_price_currency TEXT,

    -- Dates
    contract_award_date_raw TEXT,
    contract_award_date TIMESTAMPTZ,
    date_of_dispatch_of_notice_raw TEXT,
    date_of_dispatch_of_notice TIMESTAMPTZ,
    commencement_date_raw TEXT,
    commencement_date TIMESTAMPTZ,

    -- Award text fields
    justification TEXT,
    procurement_type TEXT,
    procurement_method_selected TEXT,
    contract_award_criteria TEXT,
    level_of_competition TEXT,
    principal_site_of_performance TEXT,
    duration TEXT,

    -- Funding
    funding_source TEXT,
    funding_providers TEXT,

    extraction_timestamp TIMESTAMPTZ DEFAULT NOW(),
    source_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_gojep_award_details_all_resource_id ON gojep_award_details_all(resource_id);
CREATE INDEX IF NOT EXISTS idx_gojep_award_details_all_contract_award_date ON gojep_award_details_all(contract_award_date);

ALTER TABLE gojep_award_details_all ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read access (award_details_all)" ON gojep_award_details_all
    FOR SELECT USING (true);