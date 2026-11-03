import math
from dataclasses import dataclass

WEIGHTS = {
    "relevance": 0.25,
    "novelty": 0.20,
    "impact": 0.20,
    "credibility": 0.20,
    "technical_significance": 0.15,
}


@dataclass(frozen=True)
class ScoreBreakdown:
    relevance: float
    novelty: float
    impact: float
    credibility: float
    technical_significance: float
    importance: float


def calculate_importance(scores: dict[str, float]) -> float:
    missing = WEIGHTS.keys() - scores.keys()
    if missing:
        raise ValueError(f"Missing score dimensions: {', '.join(sorted(missing))}")
    invalid = [
        name
        for name in WEIGHTS
        if (
            isinstance(scores[name], bool)
            or not isinstance(scores[name], (int, float))
            or not math.isfinite(scores[name])
            or not 0 <= scores[name] <= 10
        )
    ]
    if invalid:
        raise ValueError(f"Scores must be numeric values from 0 to 10: {', '.join(invalid)}")
    return round(sum(scores[name] * weight for name, weight in WEIGHTS.items()), 2)


def build_score_breakdown(scores: dict[str, float]) -> ScoreBreakdown:
    importance = calculate_importance(scores)
    return ScoreBreakdown(
        relevance=float(scores["relevance"]),
        novelty=float(scores["novelty"]),
        impact=float(scores["impact"]),
        credibility=float(scores["credibility"]),
        technical_significance=float(scores["technical_significance"]),
        importance=importance,
    )
