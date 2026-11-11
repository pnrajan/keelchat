"""AnalysisResult: what the Analyst returns for one question. Also the `analytics_query` tool's output schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from keelchat.datasets.models import Dataset
from keelchat.reasoning import Verification

Scalar = float | int | str | bool | None
Status = Literal["ok", "partial", "failed"]
#: repair stages of the analyst; each is the number of fix attempts after a failure (verify: re-dos after the verifier
#: finds problems)
STAGES = ("sql", "code", "interpret", "verify")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryItem(_Out):
    """An earlier question in the conversation, so follow-ups ("now only EU") resolve."""

    question: str = Field(max_length=2_000)
    sql: str = Field(default="", max_length=20_000)


class AnalyzeIn(_Out):
    question: str = Field(min_length=1, max_length=2_000)
    dataset: Dataset
    history: list[HistoryItem] = Field(default_factory=list, max_length=10)
    #: repair attempts per stage, already capped by the node's maximum; the service applies its own cap too
    repairs: dict[str, int] = Field(default_factory=dict)


class AnalysisInput(_Out):
    question: str
    #: the question made standalone using the conversation history
    resolved_question: str
    dataset: str
    dataset_version: str


class Column(_Out):
    name: str
    type: str


class AnalysisData(_Out):
    tables: list[str] = Field(default_factory=list)
    sql: str = ""
    columns: list[Column] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    #: summary statistics of the full query result (per numeric column: count, mean, min, max, sum, ...)
    summary: dict[str, dict[str, Scalar]] = Field(default_factory=dict)
    code: str | None = None
    code_output: dict[str, Any] | None = None
    sandbox: str | None = None


class Assumption(_Out):
    text: str
    #: system = added by code (truncation, small sample, nulls); analyst = stated by the model
    source: Literal["system", "analyst"]


class Finding(_Out):
    id: str
    statement: str
    values: dict[str, Scalar] = Field(default_factory=dict)
    evidence: str = ""


class Outcome(_Out):
    method: Literal["sql", "sql+python"] = "sql"
    findings: list[Finding] = Field(default_factory=list)
    statistics: dict[str, Scalar] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    #: attempts made per stage (1 = first try worked); counted in code
    attempts: dict[str, int] = Field(default_factory=dict)


class Recommendation(_Out):
    action: str
    rationale: str
    #: Finding ids; at least one
    based_on: list[str]
    confidence: Literal["low", "medium", "high"]


class AnalysisResult(_Out):
    schema_version: Literal[1] = 1
    status: Status
    input: AnalysisInput
    data: AnalysisData = Field(default_factory=AnalysisData)
    assumptions: list[Assumption] = Field(default_factory=list)
    outcome: Outcome = Field(default_factory=Outcome)
    summary: str = ""
    recommendations: list[Recommendation] = Field(default_factory=list)
    #: the plan and checks written before the work, and the verifier's verdict; None when there was nothing to verify
    verification: Verification | None = None
    #: set when status is not ok; a category, never vendor or database error text
    error_kind: str | None = None
