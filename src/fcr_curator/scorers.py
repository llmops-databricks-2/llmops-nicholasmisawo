"""How the FCR agent's answers are scored (lecture 4.3): code checks, built-in judges and a custom judge.

| Scorer | Kind | Checks | Skips |
|---|---|---|---|
| answered | code | The turn ended with an answer, not the no-answer fallback | |
| figures_stated | code | The share of expected figures the answer states (245% counts as 2.45) | cases with no figures |
| cites_page_urls | code | The answer links an FCR page (a URL with #page=) | cases without page evidence |
| no_tool_markup | code | No tool-style citations (【search_fcr†…】) instead of links | |
| correctness | built-in judge | Each expected fact is in the answer | cases without expected facts |
| expectations_guidelines | built-in judge | The case's own guidelines are followed | cases without guidelines |
| evidence_support | custom judge | Every claim is backed by the case's evidence | |

The code checks are cheap and exact but shallow: stating a figure isn't citing it correctly. The judges read
meaning but can be wrong themselves, so evidence_support is checked against human verdicts
(judge_agreement). The judges run on a different model family from the agent's.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

from mlflow.entities import Feedback
from mlflow.genai.judges import make_judge
from mlflow.genai.scorers import Correctness, ExpectationsGuidelines, Scorer, scorer

from fcr_curator.agent import NO_ANSWER

# A figure as FCRs print them: 1,757.3 or 142% or $1,156; plain years (2024) are dates, not figures
FIGURE = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])\d+(?:\.\d+)?%?")
YEARS = range(1990, 2101)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\b|\b(?:{MONTHS})\s+\d{{1,2}}\b", re.IGNORECASE)
PAGE_LINK = re.compile(r"https?://\S+#page=\d+")
TOOL_MARKUP = ("【", "†")
EVIDENCE_VERDICTS = ("supported", "unsupported", "insufficient_evidence", "no_claims")
EvidenceVerdict = Literal["supported", "unsupported", "insufficient_evidence", "no_claims"]


def figure_value(text: str) -> float:
    """Return a printed figure's value, a percentage as a multiple: 245% is 2.45, so either form matches."""
    number = float(text.rstrip("%").replace(",", ""))
    return round(number / 100 if text.endswith("%") else number, 4)


def figures_in(text: str) -> set[float]:
    """Return the figures text states, leaving out dates, years and single digits (class numbers, list markers)."""
    found = set()
    for match in FIGURE.findall(DATE.sub(" ", text)):
        is_plain_integer = match.isdigit()
        if is_plain_integer and (int(match) in YEARS or len(match) == 1):
            continue
        found.add(figure_value(match))
    return found


@scorer
def answered(outputs: str) -> bool:
    """Return whether the turn ended with an answer: not empty, and not the no-answer fallback."""
    return bool(outputs and outputs.strip()) and outputs.strip() != NO_ANSWER


@scorer
def figures_stated(outputs: str, expectations: dict[str, Any]) -> Feedback | None:
    """Return the share of the expected figures the answer states; skip a case expecting none."""
    expected = set().union(*(figures_in(fact) for fact in expectations.get("expected_facts", [])))
    if not expected:
        return None
    missing = sorted(expected - figures_in(outputs or ""))
    rationale = f"Missing: {', '.join(map(str, missing))}" if missing else "Every expected figure is stated"
    return Feedback(value=round(1 - len(missing) / len(expected), 2), rationale=rationale)


@scorer
def cites_page_urls(outputs: str, expectations: dict[str, Any]) -> Feedback | None:
    """Return whether the answer links an FCR page; skip a case whose evidence has no pages."""
    if not any("citation_url" in item for item in expectations.get("evidence", [])):
        return None
    links = PAGE_LINK.findall(outputs or "")
    return Feedback(value=bool(links), rationale=f"{len(links)} page links" if links else "No FCR page linked")


@scorer
def no_tool_markup(outputs: str) -> bool:
    """Return whether the answer is free of the tool-style citations the model copies (【…†…】)."""
    return not any(marker in (outputs or "") for marker in TOOL_MARKUP)


EVIDENCE_SUPPORT_INSTRUCTIONS = """\
You check answers about Bermuda insurers' Financial Condition Reports (FCRs) against their evidence.

The question is in {{ inputs }}. The answer is in {{ outputs }}. The evidence is in {{ expectations }}: the
expected facts, and quotes from the FCR pages or register rows that hold them.

Judge only whether the answer's factual claims (figures, ratios, dates, names, compliance) are backed by
that evidence, not its tone or completeness. A ratio the answer works out is backed if the figures it uses
are, and the arithmetic is right to two significant figures. A unit (such as $ millions) the evidence
doesn't give is a claim beyond it.

Return:
- supported: every factual claim is backed by the evidence
- unsupported: a claim contradicts the evidence, or goes beyond it
- insufficient_evidence: the answer says the information isn't available, and the evidence agrees
- no_claims: the answer makes no factual claim about insurers (a refusal, or a redirection)"""


def evidence_support_judge(model: str) -> Any:  # noqa: ANN401 - MLflow's Judge
    """Return the custom judge of whether an answer's claims are backed by the case's evidence."""
    return make_judge(
        name="evidence_support",
        instructions=EVIDENCE_SUPPORT_INSTRUCTIONS,
        feedback_value_type=EvidenceVerdict,
        model=model,
    )


def skip_without(expectation: str, judge: Scorer) -> Scorer:
    """Return judge, skipping cases with no `expectation` (MLflow's built-in judges fail on those instead).

    Correctness needs expected_facts, and ExpectationsGuidelines needs guidelines; an out-of-scope case has
    no facts, and some cases have no guidelines. A skip leaves them out of the judge's mean.
    """

    @scorer(name=judge.name)
    def judged_when_expected(
        inputs: dict[str, Any], outputs: str, expectations: dict[str, Any]
    ) -> Feedback | list[Feedback] | None:
        if not expectations.get(expectation):
            return None
        return judge(inputs=inputs, outputs=outputs, expectations=expectations)

    return judged_when_expected


def judge_uri(model_service: str) -> str:
    """Return the MLflow judge URI of a gateway model service."""
    return f"databricks:/{model_service}"


def fcr_scorers(judge_model: str) -> list[Any]:
    """Return every scorer, the judges running on judge_model (a gateway model service)."""
    model = judge_uri(judge_model)
    return [
        answered,
        figures_stated,
        cites_page_urls,
        no_tool_markup,
        skip_without("expected_facts", Correctness(model=model)),
        skip_without("guidelines", ExpectationsGuidelines(model=model)),
        evidence_support_judge(model),
    ]


@dataclass(frozen=True)
class JudgeAgreement:
    """How often the judge's verdicts matched a person's, on the cases the person reviewed."""

    compared: int
    agreed: int
    disagreements: tuple[str, ...]  # "<case>: judge <verdict>, human <verdict>"

    @property
    def rate(self) -> float | None:
        """Return the share of reviewed cases the judge agreed on; None before any review."""
        return round(self.agreed / self.compared, 2) if self.compared else None


def judge_agreement(judge: dict[str, str], human: dict[str, str | None]) -> JudgeAgreement:
    """Compare the judge's verdicts with a person's, case by case, on the cases both gave a verdict for."""
    unknown = sorted({verdict for verdict in human.values() if verdict} - set(EVIDENCE_VERDICTS))
    if unknown:
        raise ValueError(f"Unknown human verdicts {unknown}; use one of {EVIDENCE_VERDICTS}")
    reviewed = sorted(case for case, verdict in human.items() if verdict and case in judge)
    disagreements = tuple(
        f"{case}: judge {judge[case]}, human {human[case]}" for case in reviewed if judge[case] != human[case]
    )
    return JudgeAgreement(len(reviewed), len(reviewed) - len(disagreements), disagreements)
