"""Offline data-readiness reports with durable inputs and retryable publication."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4, uuid5

from pydantic import model_serializer, model_validator

from stock_analyser.collection import EvidencePacket, encode
from stock_analyser.contracts import (
    Contract,
    DataAvailability,
    DataReadinessReport,
    Instrument,
    OperationalStatus,
    validate_request_coverage,
)
from stock_analyser.persistence import Database
from stock_analyser.persistence.records import (
    ArtifactRecord,
    RunRecord,
    SnapshotReference,
    StageRecord,
)
from stock_analyser.research import ResearchRecord, render_research
from stock_analyser.security import redact_values

ARTIFACT_NAMES = ("report.md", "report.json", "manifest.json")


class ReportInput(Contract):
    """Committed before touching the report destination; never recollected on retry."""

    run_id: UUID
    run: RunRecord
    report: DataReadinessReport
    instruments: tuple[Instrument, ...]
    collection_stages: tuple[StageRecord, ...]
    snapshots: tuple[SnapshotReference, ...]
    packets: tuple[EvidencePacket, ...]
    collection_manifest: dict[str, object] | None = None
    evidence_issues: tuple[str, ...] = ()
    research: tuple[ResearchRecord, ...] = ()
    renderer_version: Literal["data-readiness-1", "data-readiness-2"] = "data-readiness-2"

    @model_serializer(mode="wrap")
    def legacy_inputs(self, handler):
        data = handler(self)
        if self.renderer_version == "data-readiness-1":
            data.pop("research", None)
        return data

    @model_validator(mode="after")
    def consistent_run(self) -> Self:
        if self.run_id != self.run.run_id or self.run_id != self.report.run_id:
            raise ValueError("Report input run IDs differ")
        if self.report.request != self.run.request or self.report.status != self.run.status:
            raise ValueError("Report differs from frozen run")
        if self.report.evidence != tuple(s.metadata for s in self.snapshots):
            raise ValueError("Report snapshot metadata differs")
        scope = {i.instrument_id for i in self.instruments}
        if self.renderer_version == "data-readiness-1" and self.research:
            raise ValueError("Legacy renderer cannot contain research")
        if any(
            r.run_id != self.run_id
            or r.instrument.instrument_id not in scope
            or r.news.window_end != self.run.request.requested_as_of
            for r in self.research
        ):
            raise ValueError("Report research scope differs")
        return self


class ReportWriteError(OSError):
    pass


def atomic_write(path: Path, content: bytes) -> None:
    """Flush a sibling temporary file, then publish it by atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{uuid4()}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def verified_bytes(reference: str, sha256: str) -> bytes:
    content = Path(reference).read_bytes()
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError("Saved file hash mismatch")
    return content


def render_markdown(data: ReportInput) -> bytes:
    run, report = data.run, data.report
    lines = [
        "# Data-readiness report",
        "",
        f"Run ID: {data.run_id}",
        f"Schema version: {data.schema_version}; renderer: {data.renderer_version}",
        "Snapshot IDs: "
        + (", ".join(str(s.metadata.snapshot_id) for s in data.snapshots) or "none"),
        f"Operational status: {report.status}; data availability: {report.availability}",
        f"Reason: {report.reason or 'None'}",
        f"Mode: {run.request.mode}; horizon: {run.request.horizon}",
        f"Market: {run.market.market_id}; currency: {run.market.currency}",
        f"Market configuration: {run.market.configuration_version}; "
        f"timezone: {run.market.timezone}",
        f"Requested as-of cutoff: {run.request.requested_as_of.isoformat()}",
        f"Sector: {run.request.sector or 'not requested'}; "
        f"portfolio revision: {run.request.portfolio_revision or 'not requested'}",
        "",
        "## Analysis stages",
        "",
        "Indicators: NOT_RUN; setups: NOT_RUN; debate: NOT_RUN; verdict: NOT_RUN.",
        "No recommendation has been generated.",
        "",
        "## Instruments and collection outcomes",
        "",
    ]
    for instrument in data.instruments:
        lines.append(
            f"- {instrument.symbol} ({instrument.instrument_id}): "
            f"{instrument.venue}, {instrument.quote_currency}"
        )
    for stage in data.collection_stages:
        lines.append(
            f"- Collection {stage.instrument_id or 'run'}: {stage.status}; "
            f"{stage.reason or 'no failure reason'}"
        )
    sell = run.configuration.get("sell_collection")
    if sell:
        lines.extend(
            [
                "",
                "## Frozen open holdings",
                "",
                f"Selection: {sell['selection_outcome']}; "
                f"selected revision: {sell['selected_revision']}",
                f"Observed revision: {sell['observed_revision']}; "
                f"portfolio changed: {sell['portfolio_changed']}",
                str(sell["revision_check"]),
                "Scans never mutate holdings or trades. SELL/HOLD verdicts: NOT_RUN.",
            ]
        )
        for holding in sell["holdings"]:
            identity, position = holding["instrument"], holding["position"]
            lines.append(
                f"- {identity['symbol']} ({identity['instrument_id']}), {identity['venue']}, "
                f"{position['currency']}: quantity {position['quantity']}; "
                f"cost basis {position['cost_basis']}; "
                "market value unavailable at selection."
            )
            if position["reconciliation_required"]:
                lines.append(
                    "  Reconciliation required: " + ", ".join(position["reconciliation_required"])
                )
            for lot in position["lots"]:
                lines.append(
                    f"  Lot {lot['execution_id']}: remaining quantity {lot['quantity']}; "
                    f"cost basis {lot['cost_basis']} {position['currency']}"
                )
            for entry in holding["entries"]:
                trade = entry["confirmation"]
                lines.append(
                    f"  Entry {entry['execution_id']}: {trade['executed_at']}; "
                    f"price {trade['price']} {trade['currency']}; "
                    f"recommendation {trade['recommendation_id'] or 'unavailable'}; "
                    f"run {trade['run_id'] or 'unavailable'}. {entry['context_status']}"
                )
    buy = run.configuration.get("buy_collection")
    if buy:
        lines.extend(
            [
                "## Buy collection scope",
                "",
                f"Sector: {buy['sector']}; selection policy: {buy['policy_version']}",
                f"Selection outcome: {buy['selection_outcome']}; shortfall: {buy['shortfall']}",
                "PROTOTYPE: incomplete sector coverage; full-sector top-ten scan not completed.",
                f"Enumeration complete: {buy['enumeration_complete']}; unresolved classifications: "
                + ", ".join(buy["unresolved_classifications"]),
                f"Configured stock concurrency: {buy['stock_concurrency']}",
                "Holdings unchanged. Indicators, strategy and agent verdicts: NOT_RUN.",
            ]
        )
        for context in buy["shared_context"]:
            lines.append(
                f"Shared context {context['key']}: {context['status']}; "
                f"run {context['run_id']}; collected once for reuse"
            )
        lines.append("")
    lines.extend(["", "## History and source coverage", ""])
    if run.price_routing is not None:
        selection = run.price_routing
        lines.append(
            f"- Selected provider: {selection.route.provider if selection.route else 'none'}; "
            f"routing policy {selection.policy_version}; reason: {selection.reason}"
        )
        if selection.massive_coverage is not None:
            coverage = selection.massive_coverage
            lines.append(
                f"- Verified Massive coverage: {coverage.start} to {coverage.end}; "
                f"reference {coverage.reference}"
            )
        if selection.route is not None and selection.route.provider == "yfinance":
            lines.append(
                "- yfinance technical OHLC is split-adjusted; dividends are retained separately. "
                "Auto-adjust, back-adjust, repair, and rounding are disabled. "
                "Adjusted Close is retained for inspection; "
                "it is not used as technical or total-return data. "
                "Source labels require an aware exchange timezone; normalization uses "
                f"{run.market.timezone}."
            )
    for route in run.market.routes:
        if route.role in ("prices", "long_history_prices"):
            lines.append(
                f"- Configured {route.role} route: {route.provider}; "
                f"feed {route.feed.feed_id} ({route.feed.kind}); "
                f"venues {', '.join(route.feed.venues)}. Actual sources are listed per snapshot."
            )
    if not data.snapshots:
        lines.append("- No trusted price snapshots were collected; data is unavailable.")
    for requirement in run.request.history:
        lines.append(
            f"- Requested {requirement.timeframe}: {requirement.minimum_bars} minimum + "
            f"{requirement.warmup_bars} warmup bars; adjustment {requirement.adjustment}; "
            f"mandatory {requirement.mandatory}"
        )
    for plan in run.history_resolutions:
        lines.append(
            f"- {plan.venue} {plan.requirement.timeframe}: local sessions "
            f"{plan.history_start} to {plan.session_date}; actual close cutoff "
            f"{plan.session_cutoff}; calendar {plan.calendar.reference}/{plan.calendar.version}; "
            f"policy {plan.policy.policy_version}; "
            f"publication timing {plan.policy.publication_timing}; "
            f"context {', '.join(plan.reasons) or 'none'}"
        )
    for snapshot in data.snapshots:
        meta = snapshot.metadata
        lines.append(
            f"- Snapshot {meta.snapshot_id}, {meta.instrument.symbol} {meta.timeframe}: "
            f"{meta.availability}; returned {meta.returned_bars}/{meta.requested_bars}; "
            f"price basis {meta.adjustment}; feed {meta.feed.feed_id} ({meta.feed.kind}); "
            f"venues {', '.join(meta.feed.venues)}; cutoff {meta.session_cutoff.isoformat()}"
        )
        lines.append(f"  Quality findings: {', '.join(meta.quality_flags) or 'none'}")
        if meta.reason:
            lines.append(f"  Reason: {meta.reason}")
        for source in meta.sources:
            lines.append(
                f"  Source: {source.provider}/{source.provider_symbol}; "
                f"sessions {source.session_start} to {source.session_end}; "
                f"retrieved {source.retrieved_at.isoformat()}; SHA-256 {source.sha256}; "
                f"reference {source.reference}"
            )
    for packet in data.packets:
        if packet.cache_refresh:
            lines.append(
                f"- Refresh of snapshot {packet.cache_refresh['parent_snapshot_id']}; "
                f"corrected sessions: "
                f"{', '.join(packet.cache_refresh['corrected_sessions']) or 'none'}; "
                f"queried {packet.cache_refresh['refresh_start']} to "
                f"{packet.cache_refresh['refresh_end']}"
            )
        for finding in packet.findings:
            lines.append(
                f"- {packet.metadata.snapshot_id}: {finding.severity} {finding.code}; "
                f"row {finding.row}; session {finding.session}"
            )
    for rejection in (data.collection_manifest or {}).get("rejected_responses", []):
        diagnostic = rejection["diagnostic"]
        lines.append(
            f"- Rejected response {diagnostic['snapshot_id']}: "
            + ", ".join(f["code"] for f in diagnostic["findings"])
        )
    if data.renderer_version == "data-readiness-2":
        lines.extend(render_research(data.research))
    lines.extend(["", "## Coverage limits", ""])
    lines.extend(f"- {issue}" for issue in data.evidence_issues)
    lines.extend(
        [
            "- Coverage is limited to requested instruments, histories and declared feed venues.",
            "- Source session intervals describe query windows, not proof of returned coverage.",
            "- Partial or failed evidence is for inspection and cannot support a strategy.",
            "- Synthetic fixtures do not establish live provider coverage or publication timing.",
            "- Indicators, strategy evaluation and agent debate are unimplemented.",
            "",
            "Configured provider capabilities: "
            + json.dumps(
                [c.model_dump(mode="json") for c in run.market.capabilities], sort_keys=True
            ),
            "Benchmark mapping: "
            + json.dumps(
                [b.model_dump(mode="json") for b in run.market.benchmarks], sort_keys=True
            ),
            "Source version: " + json.dumps(run.source_version, sort_keys=True),
            "Component versions: " + json.dumps(run.component_versions, sort_keys=True),
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def render(data: ReportInput) -> dict[str, bytes]:
    snapshot_ids = [str(s.metadata.snapshot_id) for s in data.snapshots]
    common = {
        "schema_version": data.schema_version,
        "renderer_version": data.renderer_version,
        "run_id": str(data.run_id),
        "snapshot_ids": snapshot_ids,
    }
    files = {
        "report.md": render_markdown(data),
        "report.json": encode({**common, **data.model_dump(mode="json")}),
    }
    files["manifest.json"] = encode(
        {
            **common,
            "status": data.report.status.value,
            "availability": data.report.availability.value,
            "report_input_sha256": hashlib.sha256(encode(data.model_dump(mode="json"))).hexdigest(),
            "configuration": data.run.configuration,
            "market": data.run.market.model_dump(mode="json"),
            "source_version": data.run.source_version,
            "component_versions": data.run.component_versions,
            "snapshots": [s.model_dump(mode="json") for s in data.snapshots],
            "collection_manifest": data.collection_manifest,
            **(
                {"research": [r.model_dump(mode="json") for r in data.research]}
                if data.renderer_version == "data-readiness-2"
                else {}
            ),
            "rejected_responses": (data.collection_manifest or {}).get("rejected_responses", []),
            "files": {
                name: {"reference": name, "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in files.items()
            },
        }
    )
    return files


class ReportService:
    def __init__(self, database: Database, reports_dir: Path):
        self.database = database
        self.reports_dir = reports_dir.resolve()

    def freeze(self, run_id: UUID) -> ReportInput:
        with self.database.read() as repo:
            try:
                return repo.get_report_input(run_id)
            except LookupError:
                pass
            run = repo.get_run(run_id)
            if run.status in (OperationalStatus.NOT_RUN, OperationalStatus.RUNNING):
                raise ValueError("Reporting requires a finished collection run")
            snapshots = repo.list_snapshots(run_id)
            stages = tuple(s for s in repo.list_stages(run_id) if s.name == "collection")
            instruments = {
                s.metadata.instrument.instrument_id: s.metadata.instrument for s in snapshots
            }
            for instrument_id in run.request.instrument_ids:
                if instrument_id not in instruments:
                    instruments[instrument_id] = repo.get_instrument(instrument_id)
            sell = run.configuration.get("sell_collection")
            if sell:
                # Failed collections have no snapshot identity; preserve the identity
                # frozen with the selected portfolio instead of relabeling it later.
                instruments = {
                    i.instrument_id: i
                    for i in (Instrument.model_validate(h["instrument"]) for h in sell["holdings"])
                }
            resume_plan = run.configuration.get("resume_plan")
            if resume_plan:
                instruments = {
                    i.instrument_id: i
                    for i in (
                        Instrument.model_validate(job["instrument"]) for job in resume_plan["jobs"]
                    )
                    if i.instrument_id in run.request.instrument_ids
                }
            artifacts = repo.list_artifacts(run_id)
            research = repo.research_for_report(run)
        packets, issues = [], []
        completed = {s.instrument_id for s in stages if s.status == OperationalStatus.SUCCEEDED}
        unfinished = set(run.request.instrument_ids) - completed
        if unfinished and "resume_plan" in run.configuration:
            issues.append(
                f"Unfinished collection: {len(unfinished)} instruments; no strategy verdict"
            )
        for snapshot in snapshots:
            try:
                from stock_analyser.cache import load_snapshot

                packet = load_snapshot(snapshot)
                if packet.metadata != snapshot.metadata:
                    raise ValueError("Evidence metadata mismatch")
                packets.append(packet)
            except (OSError, ValueError):
                issues.append(
                    f"Evidence packet unavailable or invalid: {snapshot.metadata.snapshot_id}"
                )
        collection_manifest = None
        for artifact in artifacts:
            if artifact.name == "manifest.json" and artifact.status == "SAVED":
                try:
                    collection_manifest = json.loads(
                        verified_bytes(artifact.reference, artifact.sha256)
                    )
                except (OSError, ValueError):
                    issues.append(
                        "Collection manifest unavailable or invalid; retained metadata shown"
                    )
        evidence = tuple(s.metadata for s in snapshots)
        availability = DataAvailability.UNAVAILABLE
        if any(e.returned_bars for e in evidence):
            availability = DataAvailability.PARTIAL
        if evidence and all(e.availability == DataAvailability.AVAILABLE for e in evidence):
            try:
                validate_request_coverage(run.request, evidence, ())
                availability = DataAvailability.AVAILABLE
            except ValueError:
                pass
        if issues and availability == DataAvailability.AVAILABLE:
            availability = DataAvailability.PARTIAL
        data = ReportInput(
            run_id=run_id,
            run=run,
            report=DataReadinessReport(
                run_id=run_id,
                request=run.request,
                status=run.status,
                availability=availability,
                evidence=evidence,
                reason=run.reason,
            ),
            instruments=tuple(instruments.values()),
            collection_stages=stages,
            snapshots=snapshots,
            packets=tuple(packets),
            collection_manifest=collection_manifest,
            evidence_issues=tuple(issues),
            research=research,
        )
        # Freeze redaction once, so later environment changes cannot change report bytes.
        data = ReportInput.model_validate(redact_values(data.model_dump(mode="json")))
        with self.database.write() as repo:
            repo.put_report_input(data)
        return data

    def generate(self, run_id: UUID) -> tuple[ArtifactRecord, ...]:
        data = self.freeze(run_id)
        files = render(data)
        with self.database.write() as repo:
            existing = {a.name: a for a in repo.list_artifacts(run_id)}
            directory = self.reports_dir / data.run.created_at.date().isoformat() / str(run_id)
            if "resume_plan" in data.run.configuration:
                directory /= data.run.updated_at.strftime("%Y%m%dT%H%M%S%f")
            if "report.md" in existing and Path(existing["report.md"].reference).is_absolute():
                directory = Path(existing["report.md"].reference).parent
            records = [
                ArtifactRecord(
                    artifact_id=existing[name].artifact_id
                    if name in existing
                    else uuid5(run_id, name),
                    run_id=run_id,
                    name=name,
                    reference=str(directory / name),
                )
                for name in ARTIFACT_NAMES
            ]
            for record in records:
                repo.put_artifact(record)
        try:
            for record in records:
                path, content = Path(record.reference), files[record.name]
                if not path.is_file() or path.read_bytes() != content:
                    atomic_write(path, content)
            # A saved batch means all three files exist and match, including on retries.
            for record in records:
                verified_bytes(record.reference, hashlib.sha256(files[record.name]).hexdigest())
        except Exception:
            reason = (
                "Report write failed; retained analysis can be regenerated without recollection"
            )
            with self.database.write() as repo:
                for record in records:
                    repo.put_artifact(
                        record.model_copy(update={"status": "FAILED", "reason": reason})
                    )
            raise ReportWriteError(
                f"Report write failed for run {run_id}. "
                f"Retry: stock-analyser report regenerate {run_id}"
            ) from None
        saved = []
        with self.database.write() as repo:
            for record in records:
                digest = hashlib.sha256(files[record.name]).hexdigest()
                previous = existing.get(record.name)
                saved_at = (
                    previous.saved_at
                    if previous and previous.sha256 == digest and previous.saved_at
                    else datetime.now(UTC)
                )
                artifact = ArtifactRecord.model_validate(
                    record.model_dump()
                    | {"status": "SAVED", "sha256": digest, "saved_at": saved_at}
                )
                repo.put_artifact(artifact)
                saved.append(artifact)
        return tuple(saved)

    def show(self, run_id: UUID, name: Literal["report.md", "report.json"] = "report.md") -> str:
        with self.database.read() as repo:
            repo.get_run(run_id)
            artifacts = {a.name: a for a in repo.list_artifacts(run_id)}
        try:
            if set(artifacts) != set(ARTIFACT_NAMES) or any(
                a.status != "SAVED" for a in artifacts.values()
            ):
                raise ValueError("Incomplete report")
            contents = {n: verified_bytes(a.reference, a.sha256) for n, a in artifacts.items()}
            return contents[name].decode("utf-8")
        except (OSError, ValueError):
            raise ValueError(
                "Report unavailable or incomplete. "
                f"Retry: stock-analyser report regenerate {run_id}"
            ) from None
