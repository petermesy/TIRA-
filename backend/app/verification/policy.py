import re
from dataclasses import dataclass
from datetime import UTC, datetime

POLICY_VERSION = "v2.1"
SOURCE_QUALITY = {
    "UNASSESSED": 0.25,
    "LOW": 0.3,
    "MEDIUM": 0.6,
    "HIGH": 0.85,
    "AUTHORITATIVE": 1.0,
}
RELIABLE_TIERS = {"MEDIUM", "HIGH", "AUTHORITATIVE"}
STRONG_TIERS = {"HIGH", "AUTHORITATIVE"}
VERIFICATION_WEIGHTS = {
    "primary_source_evidence": 0.30,
    "independent_corroboration": 0.25,
    "source_quality": 0.20,
    "technical_evidence": 0.15,
    "recency_and_context": 0.10,
}


@dataclass(frozen=True)
class EvidenceAssessment:
    evidence_type: str
    stance: str
    source_tier: str
    independence_group: str
    content_hash: str
    published_at: datetime | None
    excerpt: str = ""
    is_materially_misleading: bool = False


def _independent_groups(evidence: list[EvidenceAssessment]) -> list[list[EvidenceAssessment]]:
    parents = list(range(len(evidence)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parents[root_second] = root_first

    seen_groups: dict[str, int] = {}
    seen_hashes: dict[str, int] = {}
    token_sets = [set(re.findall(r"[a-z0-9]{4,}", item.excerpt.casefold())) for item in evidence]
    for index, item in enumerate(evidence):
        group = item.independence_group.casefold().strip()
        digest = item.content_hash.strip()
        if group and group in seen_groups:
            union(index, seen_groups[group])
        elif group:
            seen_groups[group] = index
        if digest and digest in seen_hashes:
            union(index, seen_hashes[digest])
        elif digest:
            seen_hashes[digest] = index
        for previous in range(index):
            first, second = token_sets[index], token_sets[previous]
            if first and second and len(first & second) / len(first | second) >= 0.8:
                union(index, previous)

    clusters: dict[int, list[EvidenceAssessment]] = {}
    for index, item in enumerate(evidence):
        clusters.setdefault(find(index), []).append(item)
    return list(clusters.values())


def _representatives(evidence: list[EvidenceAssessment]) -> list[EvidenceAssessment]:
    representatives: list[EvidenceAssessment] = []
    for group in _independent_groups(evidence):
        representatives.append(
            max(
                group,
                key=lambda item: (
                    SOURCE_QUALITY.get(item.source_tier, 0.0),
                    item.evidence_type == "PRIMARY",
                    item.stance in {"SUPPORTS", "CONTRADICTS"},
                ),
            )
        )
    return representatives


def classify_claim_evidence(evidence: list[EvidenceAssessment]) -> str:
    independent = _representatives(evidence)
    reliable = [item for item in independent if item.source_tier in RELIABLE_TIERS]
    supporting = [item for item in reliable if item.stance == "SUPPORTS"]
    contradicting = [item for item in reliable if item.stance == "CONTRADICTS"]

    if supporting and contradicting:
        return "CONFLICTING"
    if (
        any(
            item.stance == "CONTRADICTS"
            and item.evidence_type == "PRIMARY"
            and item.source_tier in STRONG_TIERS
            for item in reliable
        )
        or len({item.independence_group for item in contradicting}) >= 2
    ):
        return "FALSE"
    if supporting:
        if any(
            item.evidence_type == "PRIMARY" and item.source_tier in STRONG_TIERS
            for item in supporting
        ) and any(
            item.evidence_type == "INDEPENDENT_SECONDARY"
            and item.source_tier in RELIABLE_TIERS
            and item.stance == "SUPPORTS"
            for item in independent
        ):
            return "CONFIRMED"
        return "SUPPORTED"
    if any(
        item.is_materially_misleading
        and item.source_tier in STRONG_TIERS
        and item.evidence_type == "PRIMARY"
        for item in independent
    ):
        return "MISLEADING"
    return "UNCONFIRMED"


def score_claim_evidence(
    evidence: list[EvidenceAssessment],
    *,
    now: datetime | None = None,
) -> tuple[float, dict[str, float]]:
    distinct = _representatives(evidence)
    primary = [item for item in distinct if item.evidence_type == "PRIMARY"]
    primary_groups = {item.independence_group for item in primary}
    independent_support = [
        item
        for item in distinct
        if item.evidence_type == "INDEPENDENT_SECONDARY"
        and item.stance == "SUPPORTS"
        and item.source_tier in RELIABLE_TIERS
        and item.independence_group not in primary_groups
    ]
    corroboration_groups = {item.independence_group for item in independent_support}
    source_quality = (
        sum(SOURCE_QUALITY.get(item.source_tier, 0.0) for item in distinct) / len(distinct)
        if distinct
        else 0.0
    )
    technical_re = re.compile(
        r"\b(?:CVE-\d{4}-\d{4,}|benchmark|dataset|methodology|version|patch|"
        r"source code|reproducible|experiment|affected product)\b",
        re.IGNORECASE,
    )
    technical = 1.0 if any(technical_re.search(item.excerpt) for item in distinct) else 0.0
    current_time = now or datetime.now(UTC)
    recency_values: list[float] = []
    for item in distinct:
        if item.published_at is None:
            recency_values.append(0.5)
        else:
            age_days = max(0.0, (current_time - item.published_at).total_seconds() / 86_400)
            recency_values.append(1.0 if age_days <= 90 else 0.7 if age_days <= 365 else 0.4)
    factors = {
        "primary_source_evidence": 1.0 if primary else 0.0,
        "independent_corroboration": min(1.0, len(corroboration_groups) / 2),
        "source_quality": round(source_quality, 4),
        "technical_evidence": technical,
        "recency_and_context": round(sum(recency_values) / len(recency_values), 4)
        if recency_values
        else 0.0,
    }
    score = round(
        sum(factors[name] * weight for name, weight in VERIFICATION_WEIGHTS.items()),
        4,
    )
    return score, factors


def aggregate_status(statuses: list[str]) -> str:
    if not statuses:
        return "UNCONFIRMED"
    if "FALSE" in statuses:
        return "FALSE"
    if "CONFLICTING" in statuses:
        return "CONFLICTING"
    if all(status == "CONFIRMED" for status in statuses):
        return "CONFIRMED"
    if "SUPPORTED" in statuses or "CONFIRMED" in statuses:
        return "SUPPORTED"
    if all(status == "MISLEADING" for status in statuses):
        return "MISLEADING"
    return "UNCONFIRMED"
