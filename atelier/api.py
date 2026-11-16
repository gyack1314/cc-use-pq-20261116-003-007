from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .analysis import Analyzer
from .cohort import Cohorts
from .domain import CampaignSpec, DomainError, integer, parse_time, require_identifier
from .formulation import Lineage
from .ingest import Ingestor
from .inspection import Inspector
from .evidence import EvidenceService
from .release import Publisher
from .reconcile import Reconciler
from .query import QueryService
from .explore import Explorer
from .lifecycle import Lifecycle
from .store import Store


def create_app(database_path: str | None = None) -> FastAPI:
    store = Store(database_path or os.environ.get("ATELIER_DB", "atelier.sqlite3"))
    ingestor = Ingestor(store)
    analyzer = Analyzer(store)
    publisher = Publisher(store)
    inspector = Inspector(store)
    evidence = EvidenceService(store)
    reconciler = Reconciler(store)
    query_service = QueryService(store)
    explorer = Explorer(store)
    lifecycle = Lifecycle(store)
    lineage = Lineage(store)
    cohorts = Cohorts(store)
    app = FastAPI(title="Atelier Experiment Ledger", version="1.0.0")

    @app.exception_handler(DomainError)
    async def domain_error_handler(_, error: DomainError):
        status = 404 if error.code == "not_found" else 409 if error.code.endswith("conflict") or error.code in {
            "stale_analysis", "inactive_sample", "release_conflict", "review_rejected",
            "reviews_missing", "self_publish", "quality_failed", "incompatible_spec",
            "missing_parent", "lineage_cycle", "definition_conflict", "stale_lineage"} else 422
        return JSONResponse(status_code=status, content={"error": error.code, "detail": str(error)})

    @app.post("/campaigns", status_code=201)
    def create_campaign(payload: dict[str, Any]):
        actor = require_identifier(payload.get("actor"), "actor")
        spec = CampaignSpec.from_dict(payload)
        return store.create_campaign(spec, actor)

    @app.get("/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str):
        return store.get_campaign(require_identifier(campaign_id, "campaign_id"))

    @app.get("/campaigns/{campaign_id}/overview")
    def overview(campaign_id: str):
        return inspector.campaign_overview(campaign_id)

    @app.get("/campaigns/{campaign_id}/coverage")
    def coverage(campaign_id: str, start: str, end: str):
        return inspector.coverage(campaign_id, start, end)

    @app.get("/campaigns/{campaign_id}/distribution/{metric}")
    def distribution(campaign_id: str, metric: str, start: str, end: str):
        return explorer.distribution(campaign_id, metric, start, end)

    @app.get("/campaigns/{campaign_id}/paired/{first_metric}/{second_metric}")
    def paired_metrics(campaign_id: str, first_metric: str, second_metric: str, start: str, end: str):
        return explorer.paired_metrics(campaign_id, first_metric, second_metric, start, end)

    @app.get("/campaigns/{campaign_id}/evidence/verify")
    def verify(campaign_id: str):
        return evidence.verify_campaign(campaign_id)

    @app.get("/campaigns/{campaign_id}/evidence/export")
    def export(campaign_id: str):
        return evidence.export(campaign_id)

    @app.get("/campaigns/{campaign_id}/reconciliation")
    def reconciliation(campaign_id: str):
        return reconciler.full_report(campaign_id)

    @app.get("/campaigns/{campaign_id}/lifecycle")
    def lifecycle_inventory(campaign_id: str):
        return lifecycle.inventory(campaign_id)

    @app.put("/campaigns/{campaign_id}/spec")
    def update_spec(campaign_id: str, payload: dict[str, Any]):
        actor = require_identifier(payload.get("actor"), "actor")
        expected = integer(payload.get("expected_version"), "expected_version", 1, 1000000)
        return store.update_spec(campaign_id, payload, expected, actor)

    @app.post("/campaigns/{campaign_id}/formulations/{formulation_id}", status_code=201)
    def register_formulation(campaign_id: str, formulation_id: str, payload: dict[str, Any]):
        return lineage.register(campaign_id, formulation_id, payload, payload.get("actor"))

    @app.get("/campaigns/{campaign_id}/formulations")
    def list_formulations(campaign_id: str):
        return lineage.list_versions(campaign_id)

    @app.get("/campaigns/{campaign_id}/formulations/{formulation_id}/{version}")
    def get_formulation(campaign_id: str, formulation_id: str, version: int):
        return lineage.get(campaign_id, formulation_id, version)

    @app.get("/campaigns/{campaign_id}/formulations/{formulation_id}/{version}/rules")
    def formulation_rules(campaign_id: str, formulation_id: str, version: int):
        return lineage.applicable_rules(campaign_id, formulation_id, version)

    @app.get("/campaigns/{campaign_id}/formulations/{formulation_id}/{version}/ancestors")
    def formulation_ancestors(campaign_id: str, formulation_id: str, version: int):
        entry = lineage.get(campaign_id, formulation_id, version)
        snapshot = lineage.snapshot(campaign_id, [{"formulation_id": formulation_id,
                                                    "version": version}])
        return {"formulation_id": formulation_id, "version": version,
                "parent": entry["parent"], "ancestors": snapshot,
                "lineage_sha": Lineage.snapshot_sha(snapshot)}

    @app.put("/campaigns/{campaign_id}/formulations/{formulation_id}/{version}/lineage")
    def adjust_lineage(campaign_id: str, formulation_id: str, version: int, payload: dict[str, Any]):
        return lineage.adjust(campaign_id, formulation_id, version, payload, payload.get("actor"))

    @app.put("/campaigns/{campaign_id}/comparability-rules/{rule_id}")
    def declare_rule(campaign_id: str, rule_id: str, payload: dict[str, Any]):
        return lineage.declare_rule(campaign_id, rule_id, payload, payload.get("actor"))

    @app.get("/campaigns/{campaign_id}/comparability-rules")
    def list_rules(campaign_id: str):
        return lineage.list_rules(campaign_id)

    @app.get("/campaigns/{campaign_id}/comparability-rules/{rule_id}")
    def get_rule(campaign_id: str, rule_id: str):
        rule = lineage.get_rule(campaign_id, rule_id)
        if rule is None:
            raise DomainError("not_found", "comparability rule does not exist")
        return rule

    @app.get("/campaigns/{campaign_id}/comparability-rules/{rule_id}/history")
    def rule_history(campaign_id: str, rule_id: str):
        return lineage.rule_history(campaign_id, rule_id)

    @app.post("/campaigns/{campaign_id}/batches/{batch_id}")
    def ingest(campaign_id: str, batch_id: str, payload: dict[str, Any]):
        return ingestor.ingest(campaign_id, batch_id, payload.get("samples"), payload.get("actor"))

    @app.get("/campaigns/{campaign_id}/batches/{batch_id}")
    def get_batch(campaign_id: str, batch_id: str):
        return store.get_batch(campaign_id, batch_id)

    @app.get("/campaigns/{campaign_id}/batches/{batch_id}/report")
    def batch_report(campaign_id: str, batch_id: str):
        return inspector.batch_report(campaign_id, batch_id)

    @app.get("/campaigns/{campaign_id}/rejection-trends")
    def rejection_trends(campaign_id: str):
        return inspector.rejection_trends(campaign_id)

    @app.get("/campaigns/{campaign_id}/batches/{batch_id}/rejections")
    def rejections(campaign_id: str, batch_id: str):
        return ingestor.list_rejections(campaign_id, batch_id)

    @app.get("/campaigns/{campaign_id}/samples")
    def samples(campaign_id: str, status: str | None = None,
                start: str | None = None, end: str | None = None):
        if status is not None and status not in {"active", "void"}:
            raise DomainError("invalid_status", "status must be active or void")
        return store.list_samples(campaign_id, status=status,
                                  start=parse_time(start) if start else None,
                                  end=parse_time(end) if end else None)

    @app.get("/campaigns/{campaign_id}/sample-query")
    def sample_query(campaign_id: str, group_id: str | None = None,
                     status: str | None = None, metric: str | None = None,
                     minimum: str | None = None, maximum: str | None = None,
                     after: str | None = None, limit: int = 100):
        return query_service.samples(campaign_id, group_id=group_id, status=status,
                                     metric=metric, minimum=minimum, maximum=maximum,
                                     after=after, limit=limit)

    @app.get("/campaigns/{campaign_id}/batch-query")
    def batch_query(campaign_id: str, after: str | None = None, limit: int = 100):
        return query_service.batches(campaign_id, after=after, limit=limit)

    @app.put("/campaigns/{campaign_id}/samples/{sample_id}")
    def correct_sample(campaign_id: str, sample_id: str, payload: dict[str, Any]):
        return ingestor.correct(campaign_id, sample_id, payload.get("replacement"),
                                payload.get("expected_sha"), payload.get("actor"))

    @app.post("/campaigns/{campaign_id}/samples/{sample_id}/void")
    def void_sample(campaign_id: str, sample_id: str, payload: dict[str, Any]):
        return ingestor.void(campaign_id, sample_id, payload.get("expected_sha"),
                             payload.get("reason"), payload.get("actor"))

    @app.post("/campaigns/{campaign_id}/analyses/{analysis_id}")
    def build_analysis(campaign_id: str, analysis_id: str, payload: dict[str, Any]):
        return analyzer.build(campaign_id, analysis_id, payload.get("start"),
                              payload.get("end"), payload.get("actor"))

    @app.post("/campaigns/{campaign_id}/cohorts/{analysis_id}", status_code=201)
    def build_cohort(campaign_id: str, analysis_id: str, payload: dict[str, Any]):
        return cohorts.build(campaign_id, analysis_id, payload.get("rule_id"),
                             payload.get("start"), payload.get("end"), payload.get("actor"))

    @app.get("/campaigns/{campaign_id}/cohorts/{analysis_id}/status")
    def cohort_status(campaign_id: str, analysis_id: str):
        return cohorts.status(campaign_id, analysis_id)

    @app.get("/campaigns/{campaign_id}/cohorts/impact")
    def cohort_impact(campaign_id: str):
        return cohorts.impact(campaign_id)

    @app.get("/campaigns/{campaign_id}/analyses")
    def list_analyses(campaign_id: str, limit: int = Query(100, ge=1, le=500)):
        return analyzer.list_analyses(campaign_id, limit)

    @app.get("/campaigns/{campaign_id}/analyses/{analysis_id}")
    def get_analysis(campaign_id: str, analysis_id: str):
        return store.get_analysis(campaign_id, analysis_id)["body"]

    @app.get("/campaigns/{campaign_id}/analyses/{analysis_id}/status")
    def analysis_status(campaign_id: str, analysis_id: str):
        return analyzer.status(campaign_id, analysis_id)

    @app.get("/campaigns/{campaign_id}/analyses/{first_id}/compare/{second_id}")
    def compare_analyses(campaign_id: str, first_id: str, second_id: str):
        return inspector.compare_analyses(campaign_id, first_id, second_id)

    @app.post("/campaigns/{campaign_id}/analyses/{analysis_id}/reviews")
    def review(campaign_id: str, analysis_id: str, payload: dict[str, Any]):
        return publisher.review(campaign_id, analysis_id, payload.get("reviewer"),
                                payload.get("decision"), payload.get("note"), payload.get("expected_sha"))

    @app.get("/campaigns/{campaign_id}/analyses/{analysis_id}/reviews")
    def list_reviews(campaign_id: str, analysis_id: str):
        return publisher.list_reviews(campaign_id, analysis_id)

    @app.post("/campaigns/{campaign_id}/releases/{release_id}")
    def publish(campaign_id: str, release_id: str, payload: dict[str, Any]):
        return publisher.publish(campaign_id, payload.get("analysis_id"), release_id,
                                 payload.get("actor"), payload.get("expected_sha"))

    @app.get("/campaigns/{campaign_id}/releases")
    def list_releases(campaign_id: str):
        return publisher.list_releases(campaign_id)

    @app.get("/campaigns/{campaign_id}/releases/{release_id}")
    def get_release(campaign_id: str, release_id: str):
        return store.get_release(campaign_id, release_id)["body"]

    @app.get("/campaigns/{campaign_id}/releases/{release_id}/context")
    def release_context(campaign_id: str, release_id: str):
        return inspector.release_context(campaign_id, release_id)

    @app.get("/campaigns/{campaign_id}/releases/{release_id}/lineage")
    def release_lineage(campaign_id: str, release_id: str):
        return cohorts.historical_report(campaign_id, release_id)

    @app.get("/campaigns/{campaign_id}/audits")
    def audits(campaign_id: str, since_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return store.list_audits(campaign_id, since_id, limit)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
