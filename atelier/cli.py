from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from .analysis import Analyzer
from .cohort import Cohorts
from .domain import CampaignSpec
from .formulation import Lineage
from .ingest import Ingestor
from .release import Publisher
from .store import Store


def demo(fixture: Path) -> dict:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "experiment.sqlite3")
        spec = CampaignSpec.from_dict(payload["campaign"])
        campaign = store.create_campaign(spec, "operator")
        ingestor = Ingestor(store)
        lineage = Lineage(store)
        for formulation in payload.get("formulations", []):
            entity = formulation["formulation_id"]
            lineage.register(spec.campaign_id, entity, {**formulation, "actor": "operator"},
                             "operator")
        batches = []
        for batch in payload["batches"]:
            batches.append(ingestor.ingest(spec.campaign_id, batch["batch_id"],
                                           batch["samples"], "operator"))
        if "comparability_rule" in payload:
            rule = payload["comparability_rule"]
            lineage.declare_rule(spec.campaign_id, rule["rule_id"],
                                 {**rule, "actor": "analyst"}, "analyst")
            result = Cohorts(store).build(spec.campaign_id, "analysis_01", rule["rule_id"],
                                          payload["window"]["start"], payload["window"]["end"],
                                          "analyst")
            analysis_id = "analysis_01"
        else:
            result = Analyzer(store).build(spec.campaign_id, "analysis_01",
                                           payload["window"]["start"], payload["window"]["end"],
                                           "analyst")
            analysis_id = "analysis_01"
        if not result["quality"]["passed"]:
            raise RuntimeError("synthetic fixture did not pass quality gates")
        publisher = Publisher(store)
        for reviewer in ("reviewer_a", "reviewer_b"):
            publisher.review(spec.campaign_id, analysis_id, reviewer,
                             "approve", "Synthetic measurements reviewed", result["result_sha"])
        release = publisher.publish(spec.campaign_id, analysis_id, "release_01",
                                    "publisher", result["result_sha"])
        summary = {"campaign": campaign["campaign_id"],
                   "accepted": sum(len(x["accepted"]) for x in batches),
                   "quality": result["quality"], "release_sha": release["release_sha"],
                   "audit_events": len(store.list_audits(spec.campaign_id))}
        if result.get("kind") == "cohort":
            summary["cohorts"] = {
                "cohort_sha": result["cohort_sha"], "combinable": result["combinable"],
                "clusters": [{"formulation_versions": report["formulation_versions"],
                              "contrasts": report["contrasts"]}
                             for report in result["clusters"]]}
        else:
            summary["contrasts"] = result["contrasts"]
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an offline synthetic experiment demo")
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(demo(args.fixture), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
