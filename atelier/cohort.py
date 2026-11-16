"""Cohort interpretation under an explicit comparability rule.

A cohort is never assembled from group names alone.  Each active sample names a
formulation version; the selected comparability rule decides which versions may
be interpreted together.  Versions without a shared basis stay in separate
clusters and are reported separately -- they are never averaged into one number.
Every sample in the window is accounted for with an inclusion or exclusion
reason.

The cohort result freezes the rule revision, the lineage snapshot (versions and
their ancestors) and the exact sample contents.  Reviews and releases bind to
that ``cohort_sha`` so a re-declared comparability rule, an adjusted lineage or
a newly voided sample invalidates approval of a result that no longer names the
same cohort.
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from typing import Any

from .analysis import statistics
from .domain import DomainError, canonical, digest, require_identifier, utc_now
from .formulation import Lineage, ref_key
from .quality import validate_window
from .store import Store


def _contrasts(spec: Any, groups: dict[str, Any]) -> dict[str, Any]:
    if spec.baseline not in groups:
        return {}
    baseline = groups[spec.baseline]
    result: dict[str, Any] = {}
    for group, metrics in groups.items():
        if group == spec.baseline:
            continue
        values = {}
        for rule in spec.rules:
            reference = baseline.get(rule.name)
            candidate = metrics.get(rule.name)
            if reference is None or candidate is None:
                values[rule.name] = {"difference": None, "relative_percent": None}
                continue
            mean_a = Decimal(reference["mean"])
            mean_b = Decimal(candidate["mean"])
            difference = mean_b - mean_a
            relative = None if mean_a == 0 else str(difference / abs(mean_a) * Decimal(100))
            values[rule.name] = {"difference": str(difference), "relative_percent": relative}
        result[group] = values
    return result


def _group_statistics(spec: Any, samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for sample in samples:
        for name, value in sample["measurements"].items():
            grouped[(sample["group_id"], name)].append(Decimal(value))
    return {group: {rule.name: statistics(grouped[(group, rule.name)]) for rule in spec.rules}
            for group in spec.groups}


def _clusters(nodes: list[str], comparable: Any) -> list[list[str]]:
    """Greedy complete-linkage clustering.

    Every member of a cluster is comparable with every other member, so two
    versions can never be merged merely because a third version bridges them.
    """
    clusters: list[list[str]] = []
    for node in sorted(nodes):
        for cluster in clusters:
            if all(comparable(node, member) for member in cluster):
                cluster.append(node)
                break
        else:
            clusters.append([node])
    return [sorted(cluster) for cluster in clusters]


def _sample_ref(sample: dict[str, Any]) -> dict[str, Any] | None:
    if not sample.get("formulation_id"):
        return None
    return {"formulation_id": sample["formulation_id"],
            "version": sample["formulation_version"]}


class Cohorts:
    def __init__(self, store: Store):
        self.store = store

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _samples(campaign_id: str, db: Any, start: str, end: str) -> list[dict[str, Any]]:
        rows = db.execute("SELECT * FROM samples WHERE campaign_id=? AND event_time>=? AND event_time<?"
                          " ORDER BY event_time,sample_id", (campaign_id, start, end)).fetchall()
        return [{**dict(row), "measurements": json.loads(row["measurements"])} for row in rows]

    @staticmethod
    def _evaluate_members(spec: Any, rule: dict[str, Any], lineage: Lineage,
                          index: dict[str, Any], rows: list[dict[str, Any]],
                          campaign_id: str, db: Any) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        eligible: list[dict[str, Any]] = []
        for sample in rows:
            ref = _sample_ref(sample)
            entry = None if ref is None else index.get(ref_key(ref["formulation_id"], ref["version"]))
            note: dict[str, Any] = {"sample_id": sample["sample_id"],
                                    "group_id": sample["group_id"],
                                    "batch_id": sample["batch_id"],
                                    "event_time": sample["event_time"],
                                    "formulation": ref}
            if sample["status"] != "active":
                note.update({"included": False, "reason": "voided",
                             "detail": "independently voided measurements stay excluded"})
            elif ref is None:
                note.update({"included": False, "reason": "unbound_formulation",
                             "detail": "sample does not name a formulation version"})
            elif entry is None:
                note.update({"included": False, "reason": "unknown_formulation",
                             "detail": "named formulation version is not in the usable lineage"})
            else:
                note.update({"included": True, "reason": None, "detail": None})
                eligible.append({**sample, "entry": entry})
            trace.append(note)
        versions = sorted({ref_key(item["entry"]["formulation_id"], item["entry"]["version"])
                           for item in eligible})
        comparable = lambda left, right: lineage.comparable(rule, left, right, index)
        clusters = _clusters(versions, comparable)
        cluster_of = {version: position for position, cluster in enumerate(clusters)
                      for version in cluster}
        for note in trace:
            if note["included"]:
                note["cluster"] = cluster_of[ref_key(note["formulation"]["formulation_id"],
                                                     note["formulation"]["version"])]
            else:
                note["cluster"] = None
        snapshot = lineage.snapshot(campaign_id, [
            {"formulation_id": item["entry"]["formulation_id"],
             "version": item["entry"]["version"]} for item in eligible], db)
        return {"eligible": eligible, "membership": trace, "versions": versions,
                "clusters": clusters, "cluster_of": cluster_of,
                "lineage_snapshot": snapshot,
                "lineage_sha": Lineage.snapshot_sha(snapshot)}

    @staticmethod
    def _identity(rule: dict[str, Any], spec_version: int, eligible: list[dict[str, Any]],
                  cluster_of: dict[str, int], lineage_sha: str,
                  gates: list[str] | None = None) -> dict[str, Any]:
        return {
            "rule_id": rule["rule_id"], "rule_sha": rule["content_sha"],
            "spec_version": spec_version, "gates": sorted(gates or []),
            "members": sorted(({"sample_id": item["sample_id"],
                                "content_sha": item["content_sha"],
                                "formulation_id": item["formulation_id"],
                                "formulation_version": item["formulation_version"],
                                "cluster": cluster_of[ref_key(item["formulation_id"],
                                                              item["formulation_version"])]}
                               for item in eligible), key=lambda item: item["sample_id"]),
            "lineage_sha": lineage_sha}

    @staticmethod
    def _strata(spec: Any, rule: dict[str, Any], eligible: list[dict[str, Any]],
                clusters: list[list[str]], cluster_of: dict[str, int],
                window_rejections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        reports = []
        for position, cluster in enumerate(clusters):
            members = [item for item in eligible
                       if cluster_of[ref_key(item["entry"]["formulation_id"],
                                             item["entry"]["version"])] == position]
            groups = _group_statistics(spec, members)
            counts = {group: sum(1 for item in members if item["group_id"] == group)
                      for group in spec.groups}
            gates: list[str] = []
            if not members:
                gates.append("no_samples")
            if counts[spec.baseline] == 0:
                gates.append("no_valid_control")
            for group in spec.groups:
                if 0 < counts[group] < spec.min_samples:
                    gates.append(f"insufficient_samples:{group}")
            for metric in spec.rules:
                if metric.required and any(metric.name not in item["measurements"]
                                           for item in members):
                    gates.append(f"missing_required_metric:{metric.name}")
            reports.append({"cluster": position, "formulation_versions": cluster,
                            "member_ids": [item["sample_id"] for item in members],
                            "group_counts": counts, "groups": groups,
                            "contrasts": _contrasts(spec, groups), "gates": sorted(set(gates)),
                            "combines_versions": len(cluster) > 1,
                            "basis_declared": all(rule["basis"] in item["entry"]["basis"]
                                                  for item in members),
                            "lineage_related": (len(cluster) > 1)})
        if not clusters:
            reports = []
        if window_rejections:
            accepted = len(eligible)
            rejected = len(window_rejections)
            denominator = accepted + rejected
            if denominator and Decimal(rejected) / Decimal(denominator) > spec.max_bad_fraction:
                for report in reports:
                    report["gates"] = sorted(set(report["gates"]) |
                                             {"campaign_excess_rejection_fraction"})
        return reports

    # -- cohort build -----------------------------------------------------
    def build(self, campaign_id: str, analysis_id: str, rule_id: str,
              start: str, end: str, actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(analysis_id, "analysis_id")
        require_identifier(rule_id, "rule_id")
        require_identifier(actor, "actor")
        with self.store.transaction() as db:
            spec = self.store.get_spec(campaign_id, db=db)
            start, end = validate_window(spec, start, end)
            lineage = Lineage(self.store)
            rule = lineage.require_rule(campaign_id, rule_id, db)
            request = {"kind": "cohort", "campaign_id": campaign_id, "rule_id": rule_id,
                       "start": start, "end": end, "spec_version": spec.version}
            request_sha = digest(request)
            current = self.store.get_campaign(campaign_id, db)
            old = db.execute("SELECT request_sha,source_revision,body FROM analyses"
                             " WHERE campaign_id=? AND analysis_id=?",
                             (campaign_id, analysis_id)).fetchone()
            if old:
                if old["request_sha"] != request_sha:
                    raise DomainError("analysis_conflict", "analysis id names a different request")
                body = json.loads(old["body"])
                fresh = self.refresh(body, db)
                return {**body, "replayed": True,
                        "revision_current": old["source_revision"] == current["revision"],
                        "cohort_current": fresh["cohort_current"],
                        "current_cohort_sha": fresh["cohort_sha"]}
            index = lineage._index(campaign_id, db)
            rows = self._samples(campaign_id, db, start, end)
            evaluated = self._evaluate_members(spec, rule, lineage, index, rows,
                                               campaign_id, db)
            rejection_rows = db.execute("SELECT reason FROM rejections WHERE campaign_id=?"
                                        " AND event_time>=? AND event_time<?",
                                        (campaign_id, start, end)).fetchall()
            rejections = [dict(row) for row in rejection_rows]
            strata = self._strata(spec, rule, evaluated["eligible"], evaluated["clusters"],
                                  evaluated["cluster_of"], rejections)
            no_control = [{"cluster": report["cluster"],
                           "formulation_versions": report["formulation_versions"],
                           "groups_present": sorted(group for group, count in
                                                    report["group_counts"].items() if count)}
                          for report in strata if "no_valid_control" in report["gates"]]
            identity = self._identity(rule, spec.version, evaluated["eligible"],
                                      evaluated["cluster_of"], evaluated["lineage_sha"],
                                      [gate for report in strata for gate in report["gates"]])
            cohort_sha = digest(identity)
            gates = sorted({gate for report in strata for gate in report["gates"]})
            if not strata:
                gates = ["no_samples"]
            quality = {"passed": bool(strata) and not gates, "gates": gates,
                       "strata": [{"cluster": report["cluster"],
                                   "gates": report["gates"]} for report in strata],
                       "accepted_count": len(evaluated["eligible"]),
                       "rejected_count": len(rejections)}
            result = {"campaign_id": campaign_id, "analysis_id": analysis_id,
                      "kind": "cohort", "request": request,
                      "rule": {"rule_id": rule["rule_id"], "revision": rule["revision"],
                               "content_sha": rule["content_sha"],
                               "basis": rule["basis"],
                               "include_lineage": rule["include_lineage"],
                               "max_lineage_distance": rule["max_lineage_distance"]},
                      "spec_version": spec.version,
                      "cohort_sha": cohort_sha, "cohort_current": True,
                      "lineage_snapshot": evaluated["lineage_snapshot"],
                      "lineage_sha": evaluated["lineage_sha"],
                      "clusters": strata, "no_valid_control": no_control,
                      "membership": evaluated["membership"],
                      "excluded": [{"sample_id": item["sample_id"], "reason": item["reason"]}
                                   for item in evaluated["membership"] if not item["included"]],
                      "combinable": len(strata) <= 1,
                      "quality": quality,
                      "created_at": utc_now(), "replayed": False}
            result["source_revision"] = current["revision"]
            result["result_sha"] = digest(result)
            db.execute("INSERT INTO analyses(campaign_id,analysis_id,request_sha,source_revision,kind,"
                       "cohort_sha,rule_id,rule_revision,lineage_sha,body,result_sha,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (campaign_id, analysis_id, request_sha, current["revision"], "cohort",
                        cohort_sha, rule_id, rule["revision"], evaluated["lineage_sha"],
                        canonical(result), result["result_sha"], result["created_at"]))
            self.store.audit(db, campaign_id, "cohort_analysis_created", actor, analysis_id, None,
                             {"result_sha": result["result_sha"], "cohort_sha": cohort_sha,
                              "rule_id": rule_id, "rule_revision": rule["revision"],
                              "clusters": [cluster for cluster in evaluated["clusters"]]})
            return result

    # -- freshness --------------------------------------------------------
    def refresh(self, body: dict[str, Any], db: Any | None = None) -> dict[str, Any]:
        """Recompute a frozen cohort against the current lineage and rule."""
        campaign_id = body["campaign_id"]
        if db is None:
            with self.store.connect() as local:
                return self.refresh(body, local)
        lineage = Lineage(self.store)
        index = lineage._index(campaign_id, db)
        rule = lineage.require_rule(campaign_id, body["rule"]["rule_id"], db)
        request = body["request"]
        spec = self.store.get_spec(campaign_id, request["spec_version"], db)
        rows = self._samples(campaign_id, db, request["start"], request["end"])
        evaluated = self._evaluate_members(spec, rule, lineage, index, rows, campaign_id, db)
        rejection_rows = db.execute("SELECT reason FROM rejections WHERE campaign_id=?"
                                    " AND event_time>=? AND event_time<?",
                                    (campaign_id, request["start"], request["end"])).fetchall()
        strata = self._strata(spec, rule, evaluated["eligible"], evaluated["clusters"],
                              evaluated["cluster_of"], [dict(row) for row in rejection_rows])
        identity = self._identity(rule, spec.version, evaluated["eligible"],
                                  evaluated["cluster_of"], evaluated["lineage_sha"],
                                  [gate for report in strata for gate in report["gates"]])
        current_sha = digest(identity)
        before = {item["sample_id"]: item for item in body.get("membership", [])
                  if item["included"]}
        after = {item["sample_id"]: item for item in evaluated["membership"]
                 if item["included"]}
        moved = []
        for sample_id in sorted(set(before) | set(after)):
            old, new = before.get(sample_id), after.get(sample_id)
            if old is None:
                moved.append({"sample_id": sample_id, "change": "newly_included"})
            elif new is None:
                moved.append({"sample_id": sample_id, "change": "no_longer_included"})
            elif old.get("cluster") != new.get("cluster"):
                moved.append({"sample_id": sample_id, "change": "cluster_changed",
                              "from_cluster": old.get("cluster"),
                              "to_cluster": new.get("cluster")})
        excluded_before = {item["sample_id"]: item["reason"]
                           for item in body.get("membership", []) if not item["included"]}
        excluded_after = {item["sample_id"]: item["reason"]
                          for item in evaluated["membership"] if not item["included"]}
        exclusion_changes = sorted(
            ({"sample_id": sample_id, "before": excluded_before.get(sample_id),
              "after": excluded_after.get(sample_id)}
             for sample_id in set(excluded_before) | set(excluded_after)
             if excluded_before.get(sample_id) != excluded_after.get(sample_id)),
            key=lambda item: item["sample_id"])
        no_control = [{"cluster": report["cluster"],
                       "formulation_versions": report["formulation_versions"],
                       "groups_present": sorted(group for group, count in
                                                report["group_counts"].items() if count)}
                      for report in strata if "no_valid_control" in report["gates"]]
        return {"cohort_sha": current_sha,
                "cohort_current": current_sha == body.get("cohort_sha"),
                "rule": {"revision": rule["revision"], "content_sha": rule["content_sha"]},
                "rule_changed": rule["content_sha"] != body["rule"]["content_sha"],
                "lineage_sha": evaluated["lineage_sha"],
                "lineage_changed": evaluated["lineage_sha"] != body.get("lineage_sha"),
                "moved_members": moved, "exclusion_changes": exclusion_changes,
                "no_valid_control": no_control,
                "clusters": [report["formulation_versions"] for report in strata],
                "cluster_changed": ([report["formulation_versions"] for report in strata] !=
                                    [report["formulation_versions"]
                                     for report in body.get("clusters", [])]),
                "quality_gates": sorted({gate for report in strata
                                         for gate in report["gates"]}),
                "membership": evaluated["membership"],
                "excluded": [{"sample_id": item["sample_id"], "reason": item["reason"]}
                             for item in evaluated["membership"] if not item["included"]],
                "strata": strata, "eligible": evaluated["eligible"]}

    def status(self, campaign_id: str, analysis_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(analysis_id, "analysis_id")
        with self.store.connect() as db:
            analysis = self.store.get_analysis(campaign_id, analysis_id, db)
            campaign = self.store.get_campaign(campaign_id, db)
            body = analysis["body"]
            if body.get("kind") != "cohort":
                return {"analysis_id": analysis_id, "kind": body.get("kind", "campaign"),
                        "source_revision": analysis["source_revision"],
                        "current_revision": campaign["revision"],
                        "current": analysis["source_revision"] == campaign["revision"],
                        "result_sha": analysis["result_sha"]}
            fresh = self.refresh(body, db)
            return {"analysis_id": analysis_id, "kind": "cohort",
                    "source_revision": analysis["source_revision"],
                    "current_revision": campaign["revision"],
                    "revision_current": analysis["source_revision"] == campaign["revision"],
                    "current": (analysis["source_revision"] == campaign["revision"]
                                and fresh["cohort_current"]),
                    "result_sha": analysis["result_sha"],
                    "cohort_sha": analysis["cohort_sha"],
                    "current_cohort_sha": fresh["cohort_sha"],
                    "cohort_current": fresh["cohort_current"],
                    "rule_changed": fresh["rule_changed"],
                    "lineage_changed": fresh["lineage_changed"],
                    "moved_members": fresh["moved_members"],
                    "exclusion_changes": fresh["exclusion_changes"],
                    "no_valid_control": fresh["no_valid_control"]}

    # -- impact re-evaluation --------------------------------------------
    def impact(self, campaign_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT * FROM analyses WHERE campaign_id=? AND kind='cohort'"
                              " ORDER BY created_at,analysis_id", (campaign_id,)).fetchall()
            releases = db.execute("SELECT analysis_id,release_id FROM releases"
                                  " WHERE campaign_id=?", (campaign_id,)).fetchall()
            released = {row["analysis_id"]: row["release_id"] for row in releases}
            items = []
            for row in rows:
                body = json.loads(row["body"])
                fresh = self.refresh(body, db)
                reviews = db.execute("SELECT reviewer,decision,result_sha,cohort_sha FROM reviews"
                                     " WHERE campaign_id=? AND analysis_id=?",
                                     (campaign_id, row["analysis_id"])).fetchall()
                rebound = sum(1 for review in reviews
                              if review["result_sha"] == row["result_sha"]
                              and review["cohort_sha"] == row["cohort_sha"])
                reasons = []
                if fresh["rule_changed"]:
                    reasons.append("comparability_changed")
                if fresh["lineage_changed"]:
                    reasons.append("lineage_changed")
                if fresh["cluster_changed"]:
                    reasons.append("cluster_changed")
                if fresh["moved_members"]:
                    reasons.append("membership_changed")
                if fresh["exclusion_changes"]:
                    reasons.append("exclusion_changed")
                if not fresh["cohort_current"] and not reasons:
                    reasons.append("cohort_identity_changed")
                items.append({
                    "analysis_id": row["analysis_id"], "rule_id": row["rule_id"],
                    "frozen_rule_revision": row["rule_revision"],
                    "current_rule_revision": fresh["rule"]["revision"],
                    "frozen_cohort_sha": row["cohort_sha"],
                    "current_cohort_sha": fresh["cohort_sha"],
                    "affected": not fresh["cohort_current"],
                    "reasons": reasons,
                    "moved_members": fresh["moved_members"],
                    "exclusion_changes": fresh["exclusion_changes"],
                    "no_valid_control": fresh["no_valid_control"],
                    "reviews_bound_to_frozen_cohort": rebound,
                    "released": row["analysis_id"] in released,
                    "release_id": released.get(row["analysis_id"]),
                })
            pending = [item for item in items if not item["released"]]
            return {"campaign_id": campaign_id,
                    "affected": [item["analysis_id"] for item in items if item["affected"]],
                    "unaffected": [item["analysis_id"] for item in items if not item["affected"]],
                    "no_valid_control": [{"analysis_id": item["analysis_id"],
                                          "strata": item["no_valid_control"]}
                                         for item in items if item["no_valid_control"]],
                    "pending_conclusions": [item["analysis_id"] for item in pending],
                    "pending_need_rebuild": [item["analysis_id"] for item in pending
                                             if item["affected"]],
                    "released_need_reissue": [item["analysis_id"] for item in items
                                              if item["released"] and item["affected"]],
                    "analyses": items}

    # -- historical report ------------------------------------------------
    def historical_report(self, campaign_id: str, release_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(release_id, "release_id")
        with self.store.connect() as db:
            release = self.store.get_release(campaign_id, release_id)
            body = release["body"]
            analysis = self.store.get_analysis(campaign_id, release["analysis_id"], db)
            frozen = analysis["body"]
            kind = frozen.get("kind", "campaign")
            current = None
            if kind == "cohort":
                current = self.refresh(frozen, db)
            reviews = db.execute("SELECT reviewer,decision,result_sha,cohort_sha,created_at"
                                 " FROM reviews WHERE campaign_id=? AND analysis_id=?"
                                 " ORDER BY reviewer", (campaign_id, release["analysis_id"])).fetchall()
            return {"campaign_id": campaign_id, "release_id": release_id,
                    "release_sha": body["release_sha"],
                    "analysis_id": release["analysis_id"], "analysis_kind": kind,
                    "frozen_rule": frozen.get("rule"),
                    "frozen_cohort_sha": frozen.get("cohort_sha"),
                    "lineage_snapshot": frozen.get("lineage_snapshot"),
                    "lineage_sha": frozen.get("lineage_sha"),
                    "lineage_current": (None if current is None
                                        else not current["lineage_changed"]),
                    "current_cohort_sha": None if current is None else current["cohort_sha"],
                    "cohort_current": None if current is None else current["cohort_current"],
                    "current_no_valid_control": (None if current is None
                                                 else current["no_valid_control"]),
                    "clusters": frozen.get("clusters"),
                    "membership": frozen.get("membership"),
                    "excluded": frozen.get("excluded"),
                    "reviews": [dict(review) for review in reviews],
                    "reviews_bound_to_frozen_cohort": sum(
                        1 for review in reviews
                        if review["cohort_sha"] == frozen.get("cohort_sha"))}
