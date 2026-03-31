-- GOJEP Tender Analysis Results Table
-- Stores AI-powered analysis results for tender ease of fulfillment scoring

CREATE TABLE gojep_analysis_results (
    -- Primary Key & Identifiers
    id BIGSERIAL PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES gojep_tenders_all(resource_id) ON DELETE CASCADE,
    
    -- Analysis Metadata
    analysis_timestamp TIMESTAMPTZ DEFAULT NOW(),
    model_used TEXT NOT NULL,
    prompt_version TEXT DEFAULT 'primary_classification_v1',
    analysis_type TEXT DEFAULT 'ease_of_fulfillment',
    
    -- Core Analysis Results
    ease_of_fulfillment_score INTEGER CHECK (ease_of_fulfillment_score >= 1 AND ease_of_fulfillment_score <= 10),
    confidence_level TEXT CHECK (confidence_level IN ('High', 'Medium', 'Low')),
    primary_classification TEXT, -- 'PROCUREMENT' or 'PROJECT_EXECUTION'
    
    -- Detailed Reasoning (JSON format for flexibility)
    reasoning JSONB,
    key_factors TEXT[],
    
    -- Expertise Boost Information
    opportunity_score INTEGER CHECK (opportunity_score >= 1 AND opportunity_score <= 10),
    expertise_match BOOLEAN DEFAULT FALSE,
    matched_expertise TEXT[],
    base_market_score INTEGER CHECK (base_market_score >= 1 AND base_market_score <= 10),
    expertise_boost INTEGER DEFAULT 0,
    user_expertise TEXT[], -- The expertise areas that were considered
    
    -- Processing Information
    parsing_success BOOLEAN DEFAULT TRUE,
    parsing_error TEXT,
    analysis_duration_seconds DECIMAL,
    
    -- Raw Data for debugging/audit
    raw_response TEXT,
    tender_data_snapshot JSONB, -- The tender data that was analyzed
    
    -- Status & Flags
    is_active BOOLEAN DEFAULT TRUE, -- For soft deletes or versioning
    analysis_notes TEXT,
    
    -- Constraints
    CONSTRAINT valid_scores CHECK (
        (ease_of_fulfillment_score IS NULL OR ease_of_fulfillment_score BETWEEN 1 AND 10) AND
        (opportunity_score IS NULL OR opportunity_score BETWEEN 1 AND 10) AND
        (base_market_score IS NULL OR base_market_score BETWEEN 1 AND 10)
    )
);

-- Indexes for performance
CREATE INDEX idx_analysis_results_resource_id ON gojep_analysis_results(resource_id);
CREATE INDEX idx_analysis_results_timestamp ON gojep_analysis_results(analysis_timestamp);
CREATE INDEX idx_analysis_results_score ON gojep_analysis_results(ease_of_fulfillment_score);
CREATE INDEX idx_analysis_results_opportunity_score ON gojep_analysis_results(opportunity_score);
CREATE INDEX idx_analysis_results_model ON gojep_analysis_results(model_used);
CREATE INDEX idx_analysis_results_expertise_match ON gojep_analysis_results(expertise_match);
CREATE INDEX idx_analysis_results_classification ON gojep_analysis_results(primary_classification);

-- GIN index for JSONB columns
CREATE INDEX idx_analysis_results_reasoning ON gojep_analysis_results USING GIN(reasoning);
CREATE INDEX idx_analysis_results_tender_snapshot ON gojep_analysis_results USING GIN(tender_data_snapshot);

-- Index for array searches
CREATE INDEX idx_analysis_results_matched_expertise ON gojep_analysis_results USING GIN(matched_expertise);
CREATE INDEX idx_analysis_results_user_expertise ON gojep_analysis_results USING GIN(user_expertise);
CREATE INDEX idx_analysis_results_key_factors ON gojep_analysis_results USING GIN(key_factors);

-- Enable Row Level Security
ALTER TABLE gojep_analysis_results ENABLE ROW LEVEL SECURITY;

-- Create policy for public read access (adjust as needed)
CREATE POLICY "Public read access" ON gojep_analysis_results
    FOR SELECT USING (true);

-- Create policy for insert access (adjust as needed)
CREATE POLICY "Public insert access" ON gojep_analysis_results
    FOR INSERT WITH CHECK (true);

-- Comments for documentation
COMMENT ON TABLE gojep_analysis_results IS 'AI-powered analysis results for GOJEP tenders including ease of fulfillment scoring and expertise matching';
COMMENT ON COLUMN gojep_analysis_results.resource_id IS 'Foreign key reference to gojep_tenders_all table';
COMMENT ON COLUMN gojep_analysis_results.ease_of_fulfillment_score IS 'Primary analysis score from 1-10 (10 = very easy, 1 = very difficult)';
COMMENT ON COLUMN gojep_analysis_results.opportunity_score IS 'Personalized opportunity score including expertise boost';
COMMENT ON COLUMN gojep_analysis_results.primary_classification IS 'High-level classification: PROCUREMENT vs PROJECT_EXECUTION';
COMMENT ON COLUMN gojep_analysis_results.reasoning IS 'Structured reasoning from the AI analysis stored as JSON';
COMMENT ON COLUMN gojep_analysis_results.expertise_boost IS 'Points added to base score due to expertise match (+0 to +2)';
COMMENT ON COLUMN gojep_analysis_results.tender_data_snapshot IS 'Snapshot of tender data that was analyzed for audit purposes';

-- View for easy querying with tender details
CREATE VIEW analysis_results_with_tenders AS
SELECT 
    ar.*,
    t.title,
    t.procurement_type,
    t.procuring_entity,
    t.submission_deadline,
    t.submission_deadline_parsed,
    t.publication_date,
    t.publication_date_parsed,
    t.combined_description,
    t.description,
    t.services_subtype
FROM gojep_analysis_results ar
JOIN gojep_tenders_all t ON ar.resource_id = t.resource_id
WHERE ar.is_active = true;

COMMENT ON VIEW analysis_results_with_tenders IS 'Combined view of analysis results with tender details for easy querying'; 