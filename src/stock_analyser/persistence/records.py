"""Validated persistence records; operational and artifact states are independent."""

from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_analyser.contracts import (
    AnalysisRequest,
    Contract,
    EvidenceMetadata,
    MarketContext,
    OperationalStatus,
    Text,
)


class RunRecord(Contract):
    run_id: UUID
    request: AnalysisRequest
    market: MarketContext
    configuration: dict[str, object]
    source_version: dict[str, object]
    component_versions: dict[str, str]
    status: OperationalStatus = OperationalStatus.NOT_RUN
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: Text | None = None

    @model_validator(mode="after")
    def consistent_market(self) -> Self:
        if self.request.market_id != self.market.market_id:
            raise ValueError("Request and configuration markets differ")
        return self


class StageRecord(Contract):
    run_id: UUID
    name: Text
    instrument_id: UUID | None = None
    status: OperationalStatus = OperationalStatus.NOT_RUN
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: Text | None = None


class SnapshotReference(Contract):
    metadata: EvidenceMetadata
    reference: Text
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactRecord(Contract):
    artifact_id: UUID
    run_id: UUID
    name: Literal["report.md", "report.json", "manifest.json"]
    reference: Text
    status: Literal["PENDING", "SAVED", "FAILED"] = "PENDING"
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    saved_at: AwareDatetime | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def saved_metadata(self) -> Self:
        if self.status == "SAVED" and (self.sha256 is None or self.saved_at is None):
            raise ValueError("Saved artifacts require a hash and timestamp")
        if self.status != "SAVED" and (self.sha256 is not None or self.saved_at is not None):
            raise ValueError("Only saved artifacts may have saved metadata")
        if self.status == "FAILED" and self.reason is None:
            raise ValueError("Failed artifacts require a reason")
        return self
