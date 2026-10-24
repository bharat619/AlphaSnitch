"""Durable batch collection plans, independent of the current universe and ledger."""

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from uuid import UUID

from pydantic import Field

from stock_analyser.collection import CollectionService, encode
from stock_analyser.contracts import Contract, Instrument, OperationalStatus
from stock_analyser.persistence.records import StageRecord
from stock_analyser.reports import ReportService


class CollectionJob(Contract):
    run_id: UUID
    instrument: Instrument


class ResumePlan(Contract):
    version: str = "collection-resume-1"
    jobs: tuple[CollectionJob, ...]
    provider: dict[str, object] = Field(default_factory=lambda: {"kind": "external"})
    cache_mode: str = "reuse"
    max_request_attempts: int = 3
    stock_concurrency: int = Field(default=2, ge=1, le=16)


def provider_recipe(provider):
    # Explicit frozen references include hashes; never reload a mutable input filename.
    from stock_analyser.buy import FrozenPriceProvider
    from stock_analyser.fixtures import RawFixtureProvider

    if type(provider) is FrozenPriceProvider:
        return {
            "kind": "frozen",
            "histories": [r.model_dump(mode="json") for r in provider.histories],
        }
    if type(provider) is RawFixtureProvider:
        return {"kind": "fixture", "scenario": provider.scenario}
    identity = getattr(provider, "collection_identity", None)
    configuration = identity() if callable(identity) else None
    if not isinstance(configuration, dict) or not configuration:
        raise ValueError(
            "External collection adapters require a stable collection_identity() configuration"
        )
    return {
        "kind": "external",
        "adapter": f"{type(provider).__module__}.{type(provider).__qualname__}",
        "configuration": json.loads(encode(configuration)),
    }


def restore_provider(recipe):
    from stock_analyser.buy import FrozenPriceProvider
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.persistence.records import SnapshotReference

    if recipe["kind"] == "frozen":
        return FrozenPriceProvider(
            tuple(SnapshotReference.model_validate(r) for r in recipe["histories"])
        )
    if recipe["kind"] == "fixture":
        scenario = recipe.get("scenario", "valid")
        failures = recipe.get("failure_symbols", [])

        class FixtureProvider(RawFixtureProvider):
            def fetch(self, instrument, market, resolution):
                selected = scenario if instrument.symbol in failures or not failures else "valid"
                selected = {
                    "one-stock-failure": "provider-failure",
                    "one-holding-failure": "provider-failure",
                    "unassessable": "missing-bar",
                    "zero": "valid",
                    "shortfall": "valid",
                }.get(selected, selected)
                return RawFixtureProvider(selected).fetch(instrument, market, resolution)

        return FixtureProvider()
    raise ValueError(
        "This run requires its original provider adapter; resume through the Python API"
    )


def execute_plan(database, runtime, run_id, provider=None, *, progress=lambda message: None):
    """Resume only persisted jobs. A fresh scan always creates a fresh run."""
    with database.collection_lock(run_id):
        return _execute_plan(database, runtime, run_id, provider, progress)


def _execute_plan(database, runtime, run_id, provider, progress):
    from stock_analyser.buy import SharedProvider
    from stock_analyser.ledger import portfolio_revision

    with database.read() as repo:
        run = repo.get_run(run_id)
    plan = ResumePlan.model_validate(run.configuration.get("resume_plan"))
    if plan.version != "collection-resume-1" or plan.max_request_attempts != 3:
        raise ValueError("Unsupported frozen collection configuration; start a new run")
    # Validate adapters before report invalidation, state writes, or collection I/O.
    # Built-ins always execute the saved recipe, never the caller's implementation.
    if provider is not None and provider_recipe(provider) != plan.provider:
        raise ValueError("Changed provider identity or input configuration requires a new run")
    if run.status == OperationalStatus.SUCCEEDED:
        artifacts = ReportService(database, runtime.reports_dir).generate(run_id)
        return run_id, run.status, artifacts
    if runtime.market != run.market.market_id:
        raise ValueError("Resume market differs from original run")
    if plan.provider["kind"] == "external":
        if provider is None or not plan.provider.get("configuration"):
            raise ValueError("Resume requires the original identified external adapter")
    else:
        provider = restore_provider(plan.provider)
    provider = SharedProvider(provider)
    with database.write() as repo:
        repo.reset_unfinished_report(run_id)
        repo.set_run_status(run_id, OperationalStatus.RUNNING)
    progress(
        f"Run: {run_id}; frozen cutoff: {run.request.requested_as_of.isoformat()}; resume supported"
    )

    def collect(job):
        with database.read() as repo:
            child = repo.get_run(job.run_id)
        if child.history_resolutions:
            try:
                CollectionService(database, runtime.snapshots_dir).collect(
                    job.run_id,
                    job.instrument,
                    provider,
                    cache_mode=plan.cache_mode,
                    resume=child.status != OperationalStatus.NOT_RUN,
                )
            except Exception:
                # A recorded provider failure is a batch outcome. Integrity or scope
                # rejection on an otherwise completed child must stop the batch.
                with database.read() as repo:
                    if repo.get_run(job.run_id).status != OperationalStatus.FAILED:
                        raise
        return job

    def synchronize():
        with database.write() as repo:
            outcomes = []
            shared = [
                dict(c)
                for c in run.configuration.get("buy_collection", {}).get("shared_context", [])
            ]
            for job in plan.jobs:
                child = repo.get_run(job.run_id)
                refs = repo.list_snapshots(job.run_id)
                outcomes.append(child.status)
                if job.instrument.instrument_id in run.request.instrument_ids:
                    for reference in refs:
                        repo.attach_snapshot(run_id, reference)
                    repo.put_stage(
                        StageRecord(
                            run_id=run_id,
                            name="collection",
                            instrument_id=job.instrument.instrument_id,
                            status=child.status,
                            reason=child.reason
                            or (
                                "Unfinished collection; resume original inputs"
                                if child.status
                                in (
                                    OperationalStatus.NOT_RUN,
                                    OperationalStatus.RUNNING,
                                    OperationalStatus.CANCELLED,
                                )
                                else None
                            ),
                        )
                    )
                for context in shared:
                    if context.get("run_id") == str(job.run_id):
                        context.update(
                            status=child.status.value,
                            snapshots=[r.model_dump(mode="json") for r in refs],
                        )
            if shared:
                repo.set_collection_outcome(run_id, {"buy_collection": {"shared_context": shared}})
            return outcomes, sum(c["status"] != "SUCCEEDED" for c in shared)

    interrupted = None
    executor = ThreadPoolExecutor(max_workers=plan.stock_concurrency)
    pending = {}
    jobs = iter(plan.jobs)
    completed = 0
    try:
        for _ in range(plan.stock_concurrency):
            job = next(jobs, None)
            if job is not None:
                pending[executor.submit(collect, job)] = job
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                future.result()
                completed += 1
                synchronize()
                with database.read() as repo:
                    status = repo.get_run(job.run_id).status
                progress(
                    f"Collection {completed}/{len(plan.jobs)}: "
                    f"{job.instrument.symbol} {status.value}"
                )
                job = next(jobs, None)
                if job is not None:
                    pending[executor.submit(collect, job)] = job
    except BaseException as exc:
        interrupted = exc
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
    outcomes, context_failures = synchronize()
    failures = sum(status != OperationalStatus.SUCCEEDED for status in outcomes)
    if interrupted is not None:
        status = (
            OperationalStatus.CANCELLED
            if isinstance(interrupted, (KeyboardInterrupt, SystemExit))
            else OperationalStatus.FAILED
        )
    elif failures or context_failures:
        with database.read() as repo:
            stock_outcomes = [s.status for s in repo.list_stages(run_id)]
        status = (
            OperationalStatus.FAILED
            if stock_outcomes and all(s != OperationalStatus.SUCCEEDED for s in stock_outcomes)
            else OperationalStatus.PARTIAL
        )
    else:
        status = OperationalStatus.SUCCEEDED
    reason = (
        f"{len(outcomes) - failures}/{len(outcomes)} collection jobs completed; "
        f"{failures} unfinished or failed; strategy verdict NOT_RUN"
    )
    with database.write() as repo:
        if "sell_collection" in run.configuration:
            revision = portfolio_revision(database)
            changed = revision != run.request.portfolio_revision
            repo.set_collection_outcome(
                run_id,
                {"sell_collection": {"observed_revision": revision, "portfolio_changed": changed}},
            )
            reason += (
                "; portfolio changed after selection"
                if changed
                else "; portfolio revision unchanged at completion check"
            )
            if not plan.jobs:
                reason = "EMPTY_PORTFOLIO; " + reason
        if "buy_collection" in run.configuration:
            if context_failures:
                reason += f"; {context_failures} shared context failures"
            reason += (
                "; Prototype collection only; incomplete sector coverage; "
                "full-sector top-ten scan not completed"
            )
        repo.set_run_status(run_id, status, reason)
    artifacts = ReportService(database, runtime.reports_dir).generate(run_id)
    if interrupted is not None:
        raise interrupted
    return run_id, status, artifacts
