"""
Pydantic response models for the GOJEP Tender API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Milestone(BaseModel):
    event: Optional[str] = None
    date: Optional[str] = None


class TenderSummary(BaseModel):
    """Compact representation used in list responses."""
    competition_unique_id: str
    resource_id: Optional[str] = None
    title: str
    procuring_entity: Optional[str] = None
    procurement_type: Optional[str] = None        # from tenders_current (Goods/Works/Services)
    procedure: Optional[str] = None               # procurement method / procedure type
    description: Optional[str] = None
    submission_deadline: Optional[str] = None
    publication_date: Optional[str] = None
    days_remaining: Optional[int] = None
    contract_type: Optional[str] = None          # from LLM analysis
    contract_title: Optional[str] = None
    scope_of_work: Optional[str] = None
    contract_value: Optional[str] = None
    contract_duration: Optional[str] = None
    eligibility_requirements: List[str] = Field(default_factory=list)
    experience_requirements: List[str] = Field(default_factory=list)
    financial_requirements: List[str] = Field(default_factory=list)
    mandatory_documents: List[str] = Field(default_factory=list)
    evaluation_criteria: List[str] = Field(default_factory=list)
    special_conditions: List[str] = Field(default_factory=list)
    suitability_summary: Optional[str] = None
    procurement_method: Optional[str] = None
    funding_source: Optional[str] = None
    cpv_codes: List[str] = Field(default_factory=list)
    ppc_ncc_categories: List[str] = Field(default_factory=list)
    retender_flag: Optional[Any] = None
    pdf_url: Optional[str] = None
    detail_url: Optional[str] = None
    has_analysis: bool = False


class TenderDetail(BaseModel):
    """Full representation returned for a single tender."""
    # Identity
    competition_unique_id: str
    resource_id: Optional[str] = None
    title: str
    procuring_entity: Optional[str] = None
    procuring_entity_url: Optional[str] = None

    # Classification
    contract_type: Optional[str] = None
    procurement_type: Optional[str] = None
    procedure: Optional[str] = None               # procurement method / procedure type
    services_subtype: Optional[str] = None
    procurement_method: Optional[str] = None
    evaluation_mechanism: Optional[str] = None
    procurement_technique: Optional[str] = None
    funding_source: Optional[str] = None
    cpv_codes: List[str] = Field(default_factory=list)
    ppc_ncc_categories: List[str] = Field(default_factory=list)
    retender_flag: Optional[Any] = None
    non_petroleum_indicator: Optional[Any] = None
    special_differential_treatment: Optional[Any] = None
    framework_agreement_establishment: Optional[Any] = None
    contract_awarded_in_lots: Optional[Any] = None
    number_of_stages: Optional[int] = None

    # Descriptions
    description: Optional[str] = None
    detailed_description: Optional[str] = None
    contract_title: Optional[str] = None          # from LLM analysis
    scope_of_work: Optional[str] = None

    # Contract value & duration (from LLM)
    contract_value: Optional[str] = None
    contract_duration: Optional[str] = None

    # Timeline
    submission_deadline: Optional[str] = None
    original_submission_deadline: Optional[str] = None
    clarification_period_end: Optional[str] = None
    bid_opening_date: Optional[str] = None
    site_visit_date: Optional[str] = None
    publication_date: Optional[str] = None
    days_remaining: Optional[int] = None
    key_milestones: List[Milestone] = Field(default_factory=list)

    # Requirements (from LLM)
    eligibility_requirements: List[str] = Field(default_factory=list)
    experience_requirements: List[str] = Field(default_factory=list)
    financial_requirements: List[str] = Field(default_factory=list)
    mandatory_documents: List[str] = Field(default_factory=list)
    evaluation_criteria: List[str] = Field(default_factory=list)
    special_conditions: List[str] = Field(default_factory=list)
    lots: List[Any] = Field(default_factory=list)

    # Location
    country_contract_performance: Optional[str] = None
    project_reference_number: Optional[str] = None

    # Documents & links
    pdf_url: Optional[str] = None
    detail_url: Optional[str] = None
    source_files: List[str] = Field(default_factory=list)

    # Analysis metadata
    suitability_summary: Optional[str] = None
    analysis_timestamp: Optional[str] = None
    has_analysis: bool = False


class TenderListResponse(BaseModel):
    total: int
    page: int
    per_page: int
    has_next: bool
    data: List[TenderSummary]


class StatsResponse(BaseModel):
    total_open: int
    closing_within_7_days: int
    closing_within_30_days: int
    by_contract_type: Dict[str, int]
    by_procurement_type: Dict[str, int]
    by_funding_source: Dict[str, int]
    analysed: int
    not_yet_analysed: int
