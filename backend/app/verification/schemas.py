from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractedClaim(BaseModel):
    claim: str = Field(min_length=8, max_length=500)
    search_query: str = Field(min_length=3, max_length=500)
    is_material: bool = True


class ClaimExtraction(BaseModel):
    claims: list[ExtractedClaim] = Field(min_length=1, max_length=20)


class EvidenceJudgment(BaseModel):
    stance: Literal["SUPPORTS", "CONTRADICTS", "CONTEXT"]
    is_materially_misleading: bool = False
    excerpt: str = Field(min_length=8, max_length=1_000)
    rationale: str = Field(min_length=1, max_length=500)


class EvidenceResponse(BaseModel):
    id: str
    url: str
    publisher: str
    published_at: datetime | None
    retrieved_at: datetime
    excerpt: str
    evidence_type: str
    stance: str
    is_materially_misleading: bool
    validated_cve_ids: list[str]
    independence_group: str
    source_tier: str

    model_config = ConfigDict(from_attributes=True)


class VerifiedClaimResponse(BaseModel):
    id: str
    claim_key: str
    text: str
    is_material: bool
    status: str
    verification_score: float
    score_factors: dict[str, float]
    rationale: str
    search_queries: list[str]
    evidence: list[EvidenceResponse]

    model_config = ConfigDict(from_attributes=True)


class VerificationResponse(BaseModel):
    id: str
    article_id: str
    version: int
    status: str
    verification_score: float
    importance_score: float
    importance_inputs: dict[str, float]
    importance_weights: dict[str, float]
    importance_policy_version: str
    score_factors: dict[str, float]
    policy_version: str
    provider: str
    model_name: str
    warnings: list[str]
    cve_ids: list[str]
    created_at: datetime
    claims: list[VerifiedClaimResponse]

    model_config = ConfigDict(from_attributes=True)


class CredibilityUpdateRequest(BaseModel):
    tier: Literal["UNASSESSED", "LOW", "MEDIUM", "HIGH", "AUTHORITATIVE"]
    rationale: str = Field(min_length=10, max_length=1_000)


class CredibilityResponse(BaseModel):
    domain: str
    tier: str
    rationale: str
    updated_by: str
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
