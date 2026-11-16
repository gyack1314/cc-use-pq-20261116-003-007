import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from atelier.analysis import Analyzer
from atelier.cohort import Cohorts
from atelier.domain import CampaignSpec, DomainError
from atelier.evidence import EvidenceService
from atelier.formulation import Lineage
from atelier.ingest import Ingestor
from atelier.lifecycle import Lifecycle
from atelier.reconcile import Reconciler
from atelier.release import Publisher
from atelier.store import Store

WINDOW = {"start": "2026-02-01T00:00:00Z", "end": "2026-02-02T00:00:00Z"}


def campaign_body():
    return {
        "campaign_id": "coat_series",
        "title": "Coating formulation series",
        "groups": ["control", "process_b"],
        "baseline": "control",
        "metrics": [{"name": "yield_pct", "unit": "percent", "minimum": "0",
                     "maximum": "100", "precision": 2}],
        "min_samples": 2,
        "max_bad_fraction": "0.2",
        "window_hours": 24,
    }


def sample(sample_id, group, value, formulation=None, hour=1):
    item = {"sample_id": sample_id, "group_id": group,
            "event_time": f"2026-02-01T{hour:02d}:00:00Z",
            "measurements": {"yield_pct": value}}
    if formulation is not None:
        item["formulation"] = formulation
    return item


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "lineage.sqlite3")
        self.spec = CampaignSpec.from_dict(campaign_body())
        self.store.create_campaign(self.spec, "operator")
        self.lineage = Lineage(self.store)
        self.cohorts = Cohorts(self.store)
        self.ingest = Ingestor(self.store)
        self.publisher = Publisher(self.store)
        self.analyzer = Analyzer(self.store)
        self.id = self.spec.campaign_id

    # -- helpers ----------------------------------------------------------
    def register(self, version, parent=None, parameters=None, basis=None, name="coat_v1",
                 formulation_id="coat"):
        payload = {"version": version, "name": name,
                   "parameters": parameters if parameters is not None else {"solvent": "water"},
                   "basis": basis if basis is not None else ["line_a_2026"],
                   "actor": "operator"}
        if parent is not None:
            payload["parent"] = parent
        return self.lineage.register(self.id, formulation_id, payload, "operator")

    def rule(self, basis="line_a_2026", include_lineage=False, distance=None, rule_id="same_line"):
        payload = {"description": "Same production line comparability",
                   "basis": basis, "include_lineage": include_lineage, "actor": "analyst"}
        if distance is not None:
            payload["max_lineage_distance"] = distance
        return self.lineage.declare_rule(self.id, rule_id, payload, "analyst")

    def load(self, samples, batch="batch_formulations"):
        return self.ingest.ingest(self.id, batch, samples, "operator")

    def cohort(self, analysis_id, rule_id="same_line"):
        return self.cohorts.build(self.id, analysis_id, rule_id, WINDOW["start"],
                                  WINDOW["end"], "analyst")

    # -- registration and inheritance ------------------------------------
    def test_version_binds_samples_and_parent_chain(self):
        self.register(1)
        self.register(2, parent=1, parameters={"solvent": "ethanol"})
        listed = self.lineage.list_versions(self.id)
        self.assertEqual(listed["families"], {"coat": [1, 2]})
        fetched = self.lineage.get(self.id, "coat", 2)
        self.assertEqual(fetched["parent"], {"formulation_id": "coat", "version": 1})
        self.rule(basis="line_a_2026")
        applicable = self.lineage.applicable_rules(self.id, "coat", 2)
        self.assertEqual([rule["rule_id"] for rule in applicable["rules"]], ["same_line"])
        listed = self.lineage.list_versions(self.id)
        self.assertEqual(listed["versions"][1]["applicable_rules"], ["same_line"])
        snapshot = self.lineage.snapshot(self.id, [{"formulation_id": "coat", "version": 2}])
        self.assertEqual([entry["version"] for entry in snapshot], [1, 2])

    def test_missing_parent_never_enters_lineage(self):
        with self.assertRaises(DomainError) as caught:
            self.register(2, parent=1)
        self.assertEqual(caught.exception.code, "missing_parent")
        self.assertEqual(self.lineage.list_versions(self.id)["versions"], [])

    def test_registration_is_idempotent_but_rejects_redefinition(self):
        first = self.register(1)
        second = self.register(1)
        self.assertTrue(second["replayed"])
        with self.assertRaises(DomainError) as caught:
            self.register(1, parameters={"solvent": "ethanol"})
        self.assertEqual(caught.exception.code, "definition_conflict")
        self.assertEqual(first["definition_sha"], second["definition_sha"])

    def test_lineage_adjustment_detects_cycles(self):
        self.register(1)
        second = self.register(2, parent=1)
        self.assertEqual(second["parent"]["version"], 1)
        with self.assertRaises(DomainError) as caught:
            self.lineage.adjust(self.id, "coat", 1, {"parent": 2, "actor": "operator"}, "operator")
        self.assertEqual(caught.exception.code, "lineage_cycle")
        with self.assertRaises(DomainError) as stale:
            self.lineage.adjust(self.id, "coat", 2, {"parent": None,
                                                     "expected_lineage_revision": 7,
                                                     "actor": "operator"}, "operator")
        self.assertEqual(stale.exception.code, "stale_lineage")

    def test_adjustment_moves_version_between_parents(self):
        self.register(1)
        self.register(2, parent=1)
        other = self.register(3, parent=1)
        self.assertEqual(other["parent"]["version"], 1)
        moved = self.lineage.adjust(self.id, "coat", 2, {"parent": 3, "actor": "operator"},
                                    "operator")
        self.assertTrue(moved["changed"])
        self.assertEqual(moved["lineage_revision"], 1)
        self.assertEqual(moved["parent"]["version"], 3)

    def test_parent_spanning_families_is_allowed(self):
        self.register(1, formulation_id="coat_a")
        self.register(1, formulation_id="coat_b", parent="coat_a@1")
        # A new formulation family can inherit from another family.
        second = self.register(2, formulation_id="coat_b", parent={"formulation_id": "coat_b",
                                                                   "version": 1})
        self.assertEqual(second["parent"]["formulation_id"], "coat_b")

    def test_samples_require_an_explicit_usable_version(self):
        self.load([sample("s1", "control", "71.20")])
        self.register(1)
        self.load([sample("s2", "control", "71.20")], batch="batch_two")
        rejected = self.ingest.list_rejections(self.id, "batch_two")
        self.assertEqual(rejected[0]["reason"], "missing_formulation")
        self.load([sample("s3", "control", "71.20", formulation={"formulation_id": "coat",
                                                                 "version": 9})],
                  batch="batch_three")
        rejected = self.ingest.list_rejections(self.id, "batch_three")
        self.assertEqual(rejected[0]["reason"], "unknown_formulation")

    # -- comparability rules ---------------------------------------------
    def test_versions_without_shared_basis_stay_separate(self):
        self.register(1, basis=["line_a_2026"])
        self.register(2, basis=["line_b_2026"], parameters={"solvent": "ethanol"})
        self.rule(basis="line_a_2026")
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "control", "72.00", {"formulation_id": "coat", "version": 2}),
                   sample("b2", "process_b", "79.00", {"formulation_id": "coat", "version": 2})])
        result = self.cohort("analysis_split")
        self.assertFalse(result["combinable"])
        self.assertEqual([report["formulation_versions"] for report in result["clusters"]],
                         [["coat@1"], ["coat@2"]])
        # Neither version's numbers are blended into the other's mean.
        first = result["clusters"][0]["groups"]["control"]["yield_pct"]
        second = result["clusters"][1]["groups"]["control"]["yield_pct"]
        self.assertEqual(first["mean"], "71.20")
        self.assertEqual(second["mean"], "72.00")

    def test_lineage_rule_combines_declared_relatives_only(self):
        self.register(1, basis=["line_a_2026"])
        self.register(2, parent=1, basis=["line_a_2026"], parameters={"solvent": "ethanol"})
        self.register(3, parent=2, basis=["line_a_2026"], parameters={"solvent": "ethanol",
                                                                      "cure": "hot"})
        self.rule(basis="shared_none", include_lineage=True, distance=1)
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "control", "72.00", {"formulation_id": "coat", "version": 2}),
                   sample("b2", "process_b", "79.00", {"formulation_id": "coat", "version": 2}),
                   sample("c1", "control", "73.00", {"formulation_id": "coat", "version": 3}),
                   sample("c2", "process_b", "80.00", {"formulation_id": "coat", "version": 3})])
        result = self.cohort("analysis_relatives")
        clusters = [report["formulation_versions"] for report in result["clusters"]]
        self.assertIn(["coat@1", "coat@2"], clusters)
        self.assertIn(["coat@3"], clusters)
        self.assertNotIn(["coat@1", "coat@2", "coat@3"], clusters)
        self.assertEqual(result["clusters"][1]["gates"],
                         ["insufficient_samples:control", "insufficient_samples:process_b"])
        self.assertEqual(result["clusters"][0]["gates"], [])

    def test_rule_declaration_versions_are_tracked(self):
        first = self.rule(basis="line_a_2026")
        self.assertEqual(first["revision"], 1)
        same = self.rule(basis="line_a_2026")
        self.assertTrue(same["replayed"])
        second = self.rule(basis="line_b_2026")
        self.assertEqual(second["revision"], 2)
        history = self.lineage.rule_history(self.id, "same_line")
        self.assertEqual([entry["revision"] for entry in history], [1, 2])

    # -- cohort membership ------------------------------------------------
    def test_every_window_sample_has_an_inclusion_or_exclusion_reason(self):
        self.register(1)
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "control", "72.00", {"formulation_id": "coat", "version": 1}),
                   sample("b2", "process_b", "79.00", {"formulation_id": "coat", "version": 1})])
        self.rule()
        target = self.store.list_samples(self.id)[0]
        self.ingest.void(self.id, target["sample_id"], target["content_sha"],
                         "Device calibration was invalid", "operator")
        result = self.cohort("analysis_trace")
        self.assertEqual(len(result["membership"]), 4)
        excluded = [item for item in result["membership"] if not item["included"]]
        self.assertEqual(excluded[0]["sample_id"], target["sample_id"])
        self.assertEqual(excluded[0]["reason"], "voided")
        self.assertEqual(result["excluded"], [{"sample_id": target["sample_id"],
                                               "reason": "voided"}])

    def test_voided_sample_stays_out_after_reclassification(self):
        self.register(1, basis=["line_a_2026"])
        self.register(2, basis=["line_a_2026"], parameters={"solvent": "ethanol"})
        self.rule(basis="line_a_2026")
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "control", "72.20", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1})])
        sample_a1 = self.store.list_samples(self.id)[0]
        self.ingest.void(self.id, sample_a1["sample_id"], sample_a1["content_sha"],
                         "Measurement device was recalibrated", "operator")
        result = self.cohort("analysis_void")
        self.assertNotIn(sample_a1["sample_id"],
                         [member for report in result["clusters"]
                          for member in report["member_ids"]])
        # Reprogramming version 2 to inherit from version 1 changes the cohort
        # lineage, but the independently voided measurement stays excluded.
        self.lineage.adjust(self.id, "coat", 2, {"parent": 1, "actor": "operator"}, "operator")
        self.rule(basis="line_a_2026", include_lineage=True, distance=1)
        self.load([sample("c1", "control", "70.00", {"formulation_id": "coat", "version": 2}),
                   sample("c2", "process_b", "77.00", {"formulation_id": "coat", "version": 2})],
                  batch="batch_two")
        self.assertFalse(self.cohorts.status(self.id, "analysis_void")["current"])
        rebuilt = self.cohort("analysis_void_v2")
        excluded = {item["sample_id"] for item in rebuilt["membership"] if not item["included"]}
        self.assertEqual(excluded, {sample_a1["sample_id"]})
        self.assertEqual([item["reason"] for item in rebuilt["membership"]
                          if item["sample_id"] == sample_a1["sample_id"]], ["voided"])

    # -- freshness and impact --------------------------------------------
    def test_rule_change_marks_cohort_affected_and_blocks_review(self):
        self.register(1, basis=["line_a_2026"])
        self.register(2, basis=["line_b_2026"], parameters={"solvent": "ethanol"})
        self.rule(basis="line_a_2026")
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "control", "72.00", {"formulation_id": "coat", "version": 2}),
                   sample("b2", "process_b", "79.00", {"formulation_id": "coat", "version": 2})])
        result = self.cohort("analysis_rule")
        self.ui = result
        self.rule(basis="line_a_2026", include_lineage=True, distance=1)
        status = self.cohorts.status(self.id, "analysis_rule")
        self.assertFalse(status["cohort_current"])
        self.assertTrue(status["rule_changed"])
        self.assertEqual(status["moved_members"], [])
        with self.assertRaises(DomainError) as caught:
            self.publisher.review(self.id, "analysis_rule", "reviewer_a", "approve",
                                  "Measurements independently checked", result["result_sha"])
        self.assertEqual(caught.exception.code, "stale_analysis")

    def test_lineage_adjustment_reassigns_clusters_and_reports_impact(self):
        self.register(1, basis=["line_a_2026"])
        self.register(2, basis=["line_b_2026"], parameters={"solvent": "ethanol"})
        self.register(3, basis=["line_a_2026"], parameters={"solvent": "water", "cure": "hot"})
        self.rule(basis="line_a_2026")
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "control", "72.00", {"formulation_id": "coat", "version": 2}),
                   sample("b2", "process_b", "79.00", {"formulation_id": "coat", "version": 2}),
                   sample("c1", "control", "73.00", {"formulation_id": "coat", "version": 3}),
                   sample("c2", "process_b", "80.00", {"formulation_id": "coat", "version": 3})])
        result = self.cohort("analysis_impact")
        report = self.cohorts.impact(self.id)
        self.assertEqual(report["pending_conclusions"], ["analysis_impact"])
        self.assertEqual(report["affected"], [])
        # Version 2 joins version 1's basis only if the user declares it.
        self.lineage.adjust(self.id, "coat", 2,
                            {"parent": 1, "actor": "operator"}, "operator")
        self.rule(basis="line_a_2026", include_lineage=True, distance=1)
        after = self.cohorts.impact(self.id)
        self.assertEqual(after["affected"], ["analysis_impact"])
        self.assertIn("comparability_changed", after["analyses"][0]["reasons"])
        status = self.cohorts.status(self.id, "analysis_impact")
        self.assertTrue(status["lineage_changed"])
        self.assertIn("comparability_changed", after["analyses"][0]["reasons"])

    def test_released_cohort_results_are_reissued_not_rewritten(self):
        self.register(1, basis=["line_a_2026"])
        self.rule(basis="line_a_2026")
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "control", "72.20", {"formulation_id": "coat", "version": 1}),
                   sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1})])
        analysis = self.cohort("analysis_release")
        self.assertTrue(analysis["quality"]["passed"])
        for reviewer in ("reviewer_a", "reviewer_b"):
            self.publisher.review(self.id, "analysis_release", reviewer, "approve",
                                  "Measurements independently checked", analysis["result_sha"])
        release = self.publisher.publish(self.id, "analysis_release", "release_01",
                                         "publisher", analysis["result_sha"])
        self.assertEqual(release["cohort_sha"], analysis["cohort_sha"])
        self.rule(basis="line_a_2026", include_lineage=True, distance=2)
        report = self.cohorts.impact(self.id)
        self.assertEqual(report["released_need_reissue"], ["analysis_release"])
        self.assertEqual(report["pending_need_rebuild"], [])
        # The frozen release is still readable exactly as published.
        historical = self.cohorts.historical_report(self.id, "release_01")
        self.assertEqual(historical["frozen_cohort_sha"], analysis["cohort_sha"])
        self.assertEqual(historical["release_sha"], release["release_sha"])
        self.assertFalse(historical["cohort_current"])
        self.assertEqual(historical["reviews_bound_to_frozen_cohort"], 2)
        self.assertEqual(historical["frozen_rule"]["revision"], 1)
        self.assertEqual(historical["lineage_snapshot"][0]["version"], 1)

    def test_stale_review_cannot_be_reused_after_reclassification(self):
        self.register(1, basis=["line_a_2026"])
        self.rule(basis="line_a_2026")
        self.load([sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                   sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                   sample("b1", "control", "72.20", {"formulation_id": "coat", "version": 1}),
                   sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1})])
        analysis = self.cohort("analysis_review")
        self.publisher.review(self.id, "analysis_review", "reviewer_a", "approve",
                              "Measurements independently checked", analysis["result_sha"])
        self.register(2, basis=["line_b_2026"], parameters={"solvent": "ethanol"})
        self.rule(basis="line_a_2026", include_lineage=True, distance=1)
        self.load([sample("c1", "control", "70.00", {"formulation_id": "coat", "version": 2}),
                   sample("c2", "process_b", "77.00", {"formulation_id": "coat", "version": 2})],
                  batch="batch_two")
        rebuilt = self.cohort("analysis_review_v2")
        self.assertNotEqual(rebuilt["cohort_sha"], analysis["cohort_sha"])
        # The old approval was bound to the old cohort and cannot carry over.
        status = self.cohorts.status(self.id, "analysis_review")
        self.assertFalse(status["revision_current"])
        self.assertFalse(status["current"])
        changes = {item["sample_id"]: item["change"] for item in status["moved_members"]}
        self.assertEqual(set(changes), {"c1", "c2"})
        self.assertEqual(set(changes.values()), {"newly_included"})
        # The old approval was bound to the old cohort and cannot carry over.
        with self.assertRaises(DomainError) as caught:
            self.publisher.publish(self.id, "analysis_review", "release_old", "publisher",
                                   analysis["result_sha"])
        self.assertEqual(caught.exception.code, "stale_analysis")


class MissingControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "control.sqlite3")
        self.spec = CampaignSpec.from_dict(campaign_body())
        self.store.create_campaign(self.spec, "operator")
        self.lineage = Lineage(self.store)
        self.cohorts = Cohorts(self.store)
        self.ingest = Ingestor(self.store)
        self.id = self.spec.campaign_id

    def test_cluster_without_control_is_reported_per_stratum(self):
        self.lineage.register(self.id, "coat", {"version": 1, "name": "coat_v1",
                                                "basis": ["line_a_2026"],
                                                "parameters": {"solvent": "water"},
                                                "actor": "operator"}, "operator")
        self.lineage.register(self.id, "coat", {"version": 2, "name": "coat_v1",
                                                "basis": ["line_b_2026"],
                                                "parameters": {"solvent": "ethanol"},
                                                "actor": "operator"}, "operator")
        self.lineage.declare_rule(self.id, "same_line", {"description": "Same production line",
                                                         "basis": "line_a_2026",
                                                         "actor": "analyst"}, "analyst")
        self.ingest.ingest(self.id, "b1", [
            sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
            sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
            sample("b1", "process_b", "79.00", {"formulation_id": "coat", "version": 2}),
            sample("b2", "process_b", "80.00", {"formulation_id": "coat", "version": 2}),
        ], "operator")
        result = self.cohorts.build(self.id, "analysis_control", "same_line",
                                    WINDOW["start"], WINDOW["end"], "analyst")
        self.assertFalse(result["quality"]["passed"])
        self.assertEqual(len(result["no_valid_control"]), 1)
        self.assertEqual(result["no_valid_control"][0]["formulation_versions"], ["coat@2"])
        self.assertIn("no_valid_control", result["clusters"][1]["gates"])
        self.assertNotIn("no_valid_control", result["clusters"][0]["gates"])


class ReleaseAndEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "release.sqlite3")
        self.spec = CampaignSpec.from_dict(campaign_body())
        self.store.create_campaign(self.spec, "operator")
        self.lineage = Lineage(self.store)
        self.cohorts = Cohorts(self.store)
        self.ingest = Ingestor(self.store)
        self.publisher = Publisher(self.store)
        self.id = self.spec.campaign_id

    def test_cohort_release_is_verified_and_exported(self):
        self.lineage.register(self.id, "coat", {"version": 1, "name": "coat_v1",
                                                "basis": ["line_a_2026"],
                                                "parameters": {"solvent": "water"},
                                                "actor": "operator"}, "operator")
        self.lineage.declare_rule(self.id, "same_line", {"description": "Same production line",
                                                         "basis": "line_a_2026",
                                                         "actor": "analyst"}, "analyst")
        self.ingest.ingest(self.id, "b1", [
            sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
            sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
            sample("b1", "control", "72.20", {"formulation_id": "coat", "version": 1}),
            sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1}),
        ], "operator")
        analysis = self.cohorts.build(self.id, "analysis_release", "same_line",
                                      WINDOW["start"], WINDOW["end"], "analyst")
        self.assertTrue(analysis["quality"]["passed"])
        for reviewer in ("reviewer_a", "reviewer_b"):
            self.publisher.review(self.id, "analysis_release", reviewer, "approve",
                                  "Measurements independently checked", analysis["result_sha"])
        self.publisher.publish(self.id, "analysis_release", "release_01", "publisher",
                               analysis["result_sha"])
        verification = EvidenceService(self.store).verify_campaign(self.id)
        self.assertTrue(verification["ok"], verification["issues"])
        export = EvidenceService(self.store).export(self.id)
        self.assertEqual(len(export["payload"]["formulations"]), 1)
        self.assertEqual(len(export["payload"]["comparability_rules"]), 1)
        self.assertTrue(Reconciler(self.store).full_report(self.id)["ok"])
        lifecycle = Lifecycle(self.store).inventory(self.id)
        self.assertEqual(lifecycle["state_counts"], {"published": 1})

    def test_rule_revision_blocks_publication_until_rebuilt(self):
        self.lineage.register(self.id, "coat", {"version": 1, "name": "coat_v1",
                                                "basis": ["line_a_2026"],
                                                "parameters": {"solvent": "water"},
                                                "actor": "operator"}, "operator")
        self.lineage.declare_rule(self.id, "same_line", {"description": "Same production line",
                                                         "basis": "line_a_2026",
                                                         "actor": "analyst"}, "analyst")
        self.ingest.ingest(self.id, "b1", [
            sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
            sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
            sample("b1", "control", "72.20", {"formulation_id": "coat", "version": 1}),
            sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1}),
        ], "operator")
        analysis = self.cohorts.build(self.id, "analysis_block", "same_line",
                                      WINDOW["start"], WINDOW["end"], "analyst")
        for reviewer in ("reviewer_a", "reviewer_b"):
            self.publisher.review(self.id, "analysis_block", reviewer, "approve",
                                  "Measurements independently checked", analysis["result_sha"])
        self.lineage.declare_rule(self.id, "same_line", {
            "description": "Same production line, revised", "basis": "line_a_2026",
            "include_lineage": True, "max_lineage_distance": 3, "actor": "analyst"}, "analyst")
        with self.assertRaises(DomainError) as caught:
            self.publisher.publish(self.id, "analysis_block", "release_01", "publisher",
                                   analysis["result_sha"])
        self.assertEqual(caught.exception.code, "stale_analysis")
        rebuilt = self.cohorts.build(self.id, "analysis_block_v2", "same_line",
                                     WINDOW["start"], WINDOW["end"], "analyst")
        self.assertNotEqual(rebuilt["cohort_sha"], analysis["cohort_sha"])

    def test_campaign_analysis_still_binds_a_lineage_snapshot(self):
        self.lineage.register(self.id, "coat", {"version": 1, "name": "coat_v1",
                                                "basis": ["line_a_2026"],
                                                "parameters": {"solvent": "water"},
                                                "actor": "operator"}, "operator")
        self.ingest.ingest(self.id, "b1", [
            sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
            sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
            sample("b1", "control", "72.20", {"formulation_id": "coat", "version": 1}),
            sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1}),
        ], "operator")
        result = Analyzer(self.store).build(self.id, "analysis_campaign", WINDOW["start"],
                                            WINDOW["end"], "analyst")
        self.assertEqual(result["kind"], "campaign")
        self.assertEqual([entry["version"] for entry in result["lineage_snapshot"]], [1])
        self.assertEqual(result["lineage_sha"],
                         Lineage.snapshot_sha(result["lineage_snapshot"]))


class HttpLineageTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from atelier.api import create_app
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.client = TestClient(create_app(str(Path(self.folder.name) / "http.sqlite3")))
        self.addCleanup(self.client.close)
        response = self.client.post("/campaigns", json={**campaign_body(),
                                                        "actor": "operator"})
        self.assertEqual(response.status_code, 201, response.text)
        self.base = "/campaigns/coat_series"

    def register(self, version, parent=None, basis=("line_a_2026",)):
        payload = {"version": version, "name": "coat_v1",
                   "parameters": {"solvent": "water"}, "basis": list(basis),
                   "actor": "operator"}
        if parent is not None:
            payload["parent"] = parent
        return self.client.post(self.base + "/formulations/coat", json=payload)

    def test_lineage_and_rule_endpoints(self):
        self.assertEqual(self.register(1).status_code, 201)
        self.assertEqual(self.register(2, parent=1).status_code, 201)
        listed = self.client.get(self.base + "/formulations").json()
        self.assertEqual(listed["families"], {"coat": [1, 2]})
        fetched = self.client.get(self.base + "/formulations/coat/2").json()
        self.assertEqual(fetched["parent"], {"formulation_id": "coat", "version": 1})
        missing = self.register(4, parent=3)
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["error"], "missing_parent")
        rule = self.client.put(self.base + "/comparability-rules/same_line",
                               json={"description": "Same production line",
                                     "basis": "line_a_2026", "actor": "analyst"})
        self.assertEqual(rule.status_code, 200, rule.text)
        self.assertEqual(rule.json()["revision"], 1)
        revised = self.client.put(self.base + "/comparability-rules/same_line",
                                  json={"description": "Same production line, revised",
                                        "basis": "line_a_2026", "include_lineage": True,
                                        "max_lineage_distance": 1, "actor": "analyst"})
        self.assertEqual(revised.json()["revision"], 2)
        history = self.client.get(self.base + "/comparability-rules/same_line/history").json()
        self.assertEqual([entry["revision"] for entry in history], [1, 2])

    def test_cohort_flow_http(self):
        self.register(1)
        self.client.put(self.base + "/comparability-rules/same_line",
                        json={"description": "Same production line",
                              "basis": "line_a_2026", "actor": "analyst"})
        loaded = self.client.post(self.base + "/batches/b1", json={
            "actor": "operator", "samples": [
                sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
                sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
                sample("b1", "control", "72.20", {"formulation_id": "coat", "version": 1}),
                sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1}),
            ]})
        self.assertEqual(loaded.status_code, 200, loaded.text)
        analysis = self.client.post(self.base + "/cohorts/analysis_http",
                                    json={"rule_id": "same_line", **WINDOW,
                                          "actor": "analyst"})
        self.assertEqual(analysis.status_code, 201, analysis.text)
        body = analysis.json()
        self.assertTrue(body["combinable"])
        self.assertTrue(body["quality"]["passed"])
        self.assertEqual(body["clusters"][0]["contrasts"]["process_b"]["yield_pct"]["difference"],
                         "7.30")
        for reviewer in ("reviewer_a", "reviewer_b"):
            reviewed = self.client.post(self.base + "/analyses/analysis_http/reviews",
                                        json={"reviewer": reviewer, "decision": "approve",
                                              "note": "Measurements independently checked",
                                              "expected_sha": body["result_sha"]})
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
        released = self.client.post(self.base + "/releases/release_http",
                                    json={"analysis_id": "analysis_http",
                                          "actor": "publisher",
                                          "expected_sha": body["result_sha"]})
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["cohort_sha"], body["cohort_sha"])
        impact = self.client.get(self.base + "/cohorts/impact").json()
        self.assertEqual(impact["affected"], [])
        historical = self.client.get(self.base + "/releases/release_http/lineage").json()
        self.assertEqual(historical["frozen_cohort_sha"], body["cohort_sha"])
        self.assertTrue(historical["cohort_current"])
        self.assertTrue(self.client.get(self.base + "/evidence/verify").json()["ok"])

    def test_missing_formulation_is_a_rejected_sample_row(self):
        self.register(1)
        response = self.client.post(self.base + "/batches/b1", json={
            "actor": "operator", "samples": [sample("a1", "control", "71.20")]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rejected"][0]["reason"], "missing_formulation")
        rejected = self.client.get(self.base + "/batches/b1/rejections").json()
        self.assertEqual(rejected[0]["reason"], "missing_formulation")


if __name__ == "__main__":
    unittest.main()


class DemoFixtureTests(unittest.TestCase):
    def test_formulation_demo_fixture_keeps_versions_separate(self):
        from atelier.cli import demo

        output = demo(Path(__file__).parents[1] / "fixtures" / "formulation_lineage.json")
        self.assertFalse(output["cohorts"]["combinable"])
        versions = [cluster["formulation_versions"]
                    for cluster in output["cohorts"]["clusters"]]
        self.assertEqual(versions, [["coat@1"], ["coat@2"]])
        means = [cluster["contrasts"]["process_b"]["yield_pct"]["difference"]
                 for cluster in output["cohorts"]["clusters"]]
        self.assertEqual(means, ["7.30", "7.00"])


class ReviewBindingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "binding.sqlite3")
        self.spec = CampaignSpec.from_dict(campaign_body())
        self.store.create_campaign(self.spec, "operator")
        self.lineage = Lineage(self.store)
        self.cohorts = Cohorts(self.store)
        self.ingest = Ingestor(self.store)
        self.publisher = Publisher(self.store)
        self.analyzer = Analyzer(self.store)
        self.id = self.spec.campaign_id

    def seed(self, basis="line_a_2026"):
        self.lineage.register(self.id, "coat", {"version": 1, "name": "coat_v1",
                                                "basis": [basis],
                                                "parameters": {"solvent": "water"},
                                                "actor": "operator"}, "operator")
        self.lineage.declare_rule(self.id, "same_line", {"description": "Same production line",
                                                         "basis": basis,
                                                         "actor": "analyst"}, "analyst")
        self.ingest.ingest(self.id, "b1", [
            sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
            sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
            sample("b1", "control", "72.20", {"formulation_id": "coat", "version": 1}),
            sample("b2", "process_b", "79.50", {"formulation_id": "coat", "version": 1}),
        ], "operator")
        return self.cohorts.build(self.id, "analysis_bind", "same_line",
                                  WINDOW["start"], WINDOW["end"], "analyst")

    def test_revised_cohort_requires_fresh_reviews(self):
        original = self.seed()
        for reviewer in ("reviewer_a", "reviewer_b"):
            self.publisher.review(self.id, "analysis_bind", reviewer, "approve",
                                  "Measurements independently checked", original["result_sha"])
        # A new basis means version 1 is no longer declared comparable.
        self.lineage.declare_rule(self.id, "same_line", {
            "description": "Same production line, corrected basis",
            "basis": "line_z_2026", "actor": "analyst"}, "analyst")
        rebuilt = self.cohorts.build(self.id, "analysis_bind_v2", "same_line",
                                     WINDOW["start"], WINDOW["end"], "analyst")
        self.assertNotEqual(rebuilt["cohort_sha"], original["cohort_sha"])
        self.assertFalse(rebuilt["clusters"][0]["basis_declared"])
        self.assertEqual(rebuilt["clusters"][0]["formulation_versions"], ["coat@1"])
        # The approval above was bound to the old cohort and cannot be reused.
        with self.assertRaises(DomainError) as caught:
            self.publisher.publish(self.id, "analysis_bind", "release_old", "publisher",
                                   original["result_sha"])
        self.assertEqual(caught.exception.code, "stale_analysis")
        # A fresh, fully comparable cohort still needs two fresh reviews.
        self.lineage.declare_rule(self.id, "same_line", {
            "description": "Same production line, reinstated basis",
            "basis": "line_a_2026", "actor": "analyst"}, "analyst")
        fresh = self.cohorts.build(self.id, "analysis_bind_v3", "same_line",
                                   WINDOW["start"], WINDOW["end"], "analyst")
        self.assertTrue(fresh["quality"]["passed"])
        with self.assertRaises(DomainError) as caught:
            self.publisher.publish(self.id, "analysis_bind_v3", "release_v3", "publisher",
                                   fresh["result_sha"])
        self.assertEqual(caught.exception.code, "reviews_missing")

    def test_missing_control_blocks_review(self):
        self.lineage.register(self.id, "coat", {"version": 1, "name": "coat_v1",
                                                "basis": ["line_a_2026"],
                                                "parameters": {"solvent": "water"},
                                                "actor": "operator"}, "operator")
        self.lineage.declare_rule(self.id, "same_line", {"description": "Same production line",
                                                         "basis": "line_a_2026",
                                                         "actor": "analyst"}, "analyst")
        self.ingest.ingest(self.id, "b1", [
            sample("a1", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
            sample("a2", "process_b", "79.50", {"formulation_id": "coat", "version": 1}),
        ], "operator")
        analysis = self.cohorts.build(self.id, "analysis_nocontrol", "same_line",
                                      WINDOW["start"], WINDOW["end"], "analyst")
        self.assertEqual(analysis["no_valid_control"][0]["formulation_versions"], ["coat@1"])
        self.assertFalse(analysis["quality"]["passed"])
        with self.assertRaises(DomainError) as caught:
            self.publisher.review(self.id, "analysis_nocontrol", "reviewer_a", "approve",
                                  "Measurements independently checked", analysis["result_sha"])
        self.assertEqual(caught.exception.code, "quality_failed")
        lifecycle = Lifecycle(self.store)
        with self.store.connect() as db:
            db.execute("UPDATE analyses SET source_revision=source_revision WHERE campaign_id=?",
                       (self.id,))
        self.assertIn("missing_control",
                      [item["state"] for item in lifecycle.inventory(self.id)["analyses"]])

    def test_campaign_analysis_refuses_to_blend_versions(self):
        self.lineage.register(self.id, "coat", {"version": 1, "name": "coat_v1",
                                                "basis": ["line_a_2026"],
                                                "parameters": {"solvent": "water"},
                                                "actor": "operator"}, "operator")
        self.lineage.register(self.id, "coat", {"version": 2, "name": "coat_v1",
                                                "basis": ["line_b_2026"],
                                                "parameters": {"solvent": "ethanol"},
                                                "actor": "operator"}, "operator")
        self.ingest.ingest(self.id, "b1", [
            sample("a1", "control", "71.20", {"formulation_id": "coat", "version": 1}),
            sample("a2", "process_b", "78.50", {"formulation_id": "coat", "version": 1}),
            sample("b1", "control", "72.00", {"formulation_id": "coat", "version": 2}),
            sample("b2", "process_b", "79.00", {"formulation_id": "coat", "version": 2}),
        ], "operator")
        analysis = self.analyzer.build(self.id, "analysis_blend", WINDOW["start"],
                                       WINDOW["end"], "analyst")
        self.assertIn("multiple_formulations", analysis["quality"]["gates"])
        self.assertFalse(analysis["quality"]["passed"])
        self.assertEqual(analysis["formulation_versions"], ["coat@1", "coat@2"])
        with self.assertRaises(DomainError) as caught:
            self.publisher.review(self.id, "analysis_blend", "reviewer_a", "approve",
                                  "Measurements independently checked", analysis["result_sha"])
        self.assertEqual(caught.exception.code, "quality_failed")


class LegacyBehaviourTests(unittest.TestCase):
    def test_campaign_without_formulations_keeps_old_workflow(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "legacy.sqlite3")
        spec = CampaignSpec.from_dict(campaign_body())
        store.create_campaign(spec, "operator")
        ingestor = Ingestor(store)
        ingestor.ingest(spec.campaign_id, "b1", [
            sample("a1", "control", "71.20"), sample("a2", "process_b", "78.50"),
            sample("b1", "control", "72.20"), sample("b2", "process_b", "79.50"),
        ], "operator")
        result = Analyzer(store).build(spec.campaign_id, "analysis_legacy", WINDOW["start"],
                                       WINDOW["end"], "analyst")
        self.assertTrue(result["quality"]["passed"])
        self.assertEqual(result["lineage_snapshot"], [])
        self.assertEqual(result["formulation_versions"], [])
        publisher = Publisher(store)
        for reviewer in ("reviewer_a", "reviewer_b"):
            publisher.review(spec.campaign_id, "analysis_legacy", reviewer, "approve",
                             "Measurements independently checked", result["result_sha"])
        release = publisher.publish(spec.campaign_id, "analysis_legacy", "release_legacy",
                                    "publisher", result["result_sha"])
        self.assertIsNone(release["cohort_sha"])
        self.assertTrue(EvidenceService(store).verify_campaign(spec.campaign_id)["ok"])
        self.assertTrue(Reconciler(store).full_report(spec.campaign_id)["ok"])
