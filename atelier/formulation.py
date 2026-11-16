"""Formulation versioning, inheritance and comparability declarations.

Groups in an experiment spec describe the roles being compared (control,
process B, ...).  They are *not* enough to decide whether two measurements may
be pooled: two similarly named formulations may differ in a key parameter.
This module therefore keeps an explicit, versioned lineage for each
formulation and a small set of comparability rules.  Measurements always bind
to a concrete ``(formulation_id, version)`` reference.

A formulation version is part of the *usable* lineage only when:

* its parent version exists (``missing_parent`` otherwise),
* the resulting parent chain has no cycle (``lineage_cycle``), and
* a version number does not silently acquire a different definition
  (``definition_conflict``).

Invalid registrations are rejected before they are written, so every stored
version is usable.  Parents can be adjusted after the fact through
:meth:`Lineage.adjust`; adjustments are revision-checked, cycle checked and
audited.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import (
    DomainError,
    canonical,
    digest,
    integer,
    require_identifier,
    utc_now,
)
from .store import Store

PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,39}$")
BASIS_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


def ref_key(formulation_id: str, version: int) -> str:
    return f"{formulation_id}@{version}"


def entry_key(entry: dict[str, Any]) -> str:
    return ref_key(entry["formulation_id"], entry["version"])


def normalize_ref(value: Any, label: str = "formulation") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DomainError("invalid_formulation_ref", f"{label} reference must be an object")
    formulation_id = require_identifier(value.get("formulation_id"), f"{label}_id")
    raw_version = value.get("version")
    if isinstance(raw_version, str) and raw_version.isdigit():
        raw_version = int(raw_version)
    version = integer(raw_version, f"{label}_version", 1, 1000000)
    return {"formulation_id": formulation_id, "version": version}


def normalize_parent(value: Any, formulation_id: str) -> dict[str, Any] | None:
    """Accept ``None``, an explicit reference, ``id@version`` or a bare version."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise DomainError("invalid_parent", "parent version is invalid")
    if isinstance(value, int):
        return {"formulation_id": formulation_id,
                "version": integer(value, "parent_version", 1, 1000000)}
    if isinstance(value, str):
        if "@" in value:
            parent_id, _, raw = value.partition("@")
            if not raw.isdigit():
                raise DomainError("invalid_parent", "parent reference must use id@version")
            return {"formulation_id": require_identifier(parent_id, "parent_formulation_id"),
                    "version": integer(int(raw), "parent_version", 1, 1000000)}
        if not value.isdigit():
            raise DomainError("invalid_parent", "parent reference must use id@version")
        return {"formulation_id": formulation_id,
                "version": integer(int(value), "parent_version", 1, 1000000)}
    return normalize_ref(value, "parent")


def _parameter_text(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise DomainError("invalid_parameter", f"parameter {name} must be finite") from exc
        if not number.is_finite():
            raise DomainError("invalid_parameter", f"parameter {name} must be finite")
        return str(number)
    if isinstance(value, str) and 0 <= len(value) <= 200:
        return value
    raise DomainError("invalid_parameter",
                      f"parameter {name} must be a short scalar value")


def normalize_parameters(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DomainError("invalid_parameters", "parameters must be an object")
    if len(value) > 32:
        raise DomainError("invalid_parameters", "at most 32 parameters are allowed")
    normalized: dict[str, str] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not PARAMETER_NAME.fullmatch(name):
            raise DomainError("invalid_parameter", "parameter names use letters, digits, '_', '.', '-'")
        normalized[name] = _parameter_text(name, raw)
    return normalized


def normalize_basis(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > 8:
        raise DomainError("invalid_basis", "basis must list at most 8 comparability tokens")
    tokens = []
    for token in value:
        if not isinstance(token, str) or not BASIS_TOKEN.fullmatch(token):
            raise DomainError("invalid_basis", "comparability basis token is invalid")
        tokens.append(token)
    return sorted(set(tokens))


def definition_digest(name: str, parameters: dict[str, str], basis: list[str]) -> str:
    return digest({"name": name, "parameters": parameters, "basis": basis})


class Lineage:
    def __init__(self, store: Store):
        self.store = store

    # -- internal helpers -------------------------------------------------
    @staticmethod
    def _row_body(row: Any) -> dict[str, Any]:
        body = json.loads(row["body"])
        body["lineage_revision"] = row["lineage_revision"]
        body["state"] = row["state"]
        body["usable"] = row["state"] == "usable"
        return body

    def _require_ref(self, campaign_id: str, db: Any, ref: dict[str, Any],
                     code: str = "not_found") -> dict[str, Any]:
        row = db.execute("SELECT * FROM formulations WHERE campaign_id=? AND formulation_id=?"
                         " AND version=? AND state='usable'",
                         (campaign_id, ref["formulation_id"], ref["version"])).fetchone()
        if row is None:
            raise DomainError(code, "formulation version does not exist in the usable lineage")
        return self._row_body(row)

    def _index(self, campaign_id: str, db: Any) -> dict[str, dict[str, Any]]:
        rows = db.execute("SELECT * FROM formulations WHERE campaign_id=? AND state='usable'"
                          " ORDER BY formulation_id,version", (campaign_id,)).fetchall()
        return {entry_key(self._row_body(row)): self._row_body(row) for row in rows}

    def has_versions(self, campaign_id: str, db: Any | None = None) -> bool:
        if db is None:
            with self.store.connect() as local:
                return self.has_versions(campaign_id, local)
        return db.execute("SELECT 1 FROM formulations WHERE campaign_id=? AND state='usable' LIMIT 1",
                          (campaign_id,)).fetchone() is not None

    # -- registration -----------------------------------------------------
    def register(self, campaign_id: str, formulation_id: str, payload: dict[str, Any],
                 actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(formulation_id, "formulation_id")
        require_identifier(actor, "actor")
        if not isinstance(payload, dict):
            raise DomainError("invalid_formulation", "formulation version must be an object")
        version = integer(payload.get("version"), "version", 1, 1000000)
        name = payload.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
            raise DomainError("invalid_formulation", "formulation name must have 1-160 characters")
        parameters = normalize_parameters(payload.get("parameters"))
        basis = normalize_basis(payload.get("basis"))
        parent = normalize_parent(payload.get("parent"), formulation_id)
        definition_sha = definition_digest(name.strip(), parameters, basis)
        with self.store.transaction() as db:
            self.store.get_campaign(campaign_id, db)
            existing = db.execute("SELECT * FROM formulations WHERE campaign_id=? AND formulation_id=?"
                                  " AND version=?", (campaign_id, formulation_id, version)).fetchone()
            if existing:
                body = self._row_body(existing)
                if body["definition_sha"] != definition_sha:
                    raise DomainError("definition_conflict",
                                      "this version number already names a different definition")
                if body["parent"] != parent:
                    raise DomainError("lineage_conflict",
                                      "parent differs from the stored version; use lineage adjustment")
                return {**body, "replayed": True}
            if parent is not None:
                if ref_key(parent["formulation_id"], parent["version"]) == ref_key(formulation_id, version):
                    raise DomainError("lineage_cycle", "a version cannot inherit from itself")
                self._require_ref(campaign_id, db, parent, "missing_parent")
            body = {"campaign_id": campaign_id, "formulation_id": formulation_id,
                    "version": version, "name": name.strip(), "parameters": parameters,
                    "basis": basis, "parent": parent, "definition_sha": definition_sha}
            now = utc_now()
            db.execute("INSERT INTO formulations(campaign_id,formulation_id,version,definition_sha,"
                       "state,lineage_revision,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (campaign_id, formulation_id, version, definition_sha, "usable", 0,
                        canonical(body), now, now))
            self.store.audit(db, campaign_id, "formulation_registered", actor,
                             ref_key(formulation_id, version), None, body)
            return {**body, "lineage_revision": 0, "state": "usable", "usable": True,
                    "replayed": False}

    def get(self, campaign_id: str, formulation_id: str, version: int) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(formulation_id, "formulation_id")
        integer(version, "version", 1, 1000000)
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            return self._require_ref(campaign_id, db, {"formulation_id": formulation_id,
                                                       "version": version})

    def list_versions(self, campaign_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            index = self._index(campaign_id, db)
            versions = [index[key] for key in sorted(index)]
            families: dict[str, list[int]] = {}
            for entry in versions:
                families.setdefault(entry["formulation_id"], []).append(entry["version"])
            rules = {row["rule_id"]: {**json.loads(row["body"]), "revision": row["revision"]}
                     for row in db.execute("SELECT * FROM comparability_rules WHERE campaign_id=?",
                                           (campaign_id,)).fetchall()}
            lineage_sha = digest(versions)
            for entry in versions:
                entry["applicable_rules"] = sorted(
                    rule_id for rule_id, rule in rules.items()
                    if rule["basis"] in entry["basis"])
            return {"campaign_id": campaign_id, "versions": versions,
                    "families": {name: sorted(numbers) for name, numbers in sorted(families.items())},
                    "lineage_sha": lineage_sha}

    def applicable_rules(self, campaign_id: str, formulation_id: str,
                         version: int) -> dict[str, Any]:
        entry = self.get(campaign_id, formulation_id, version)
        rules = [rule for rule in self.list_rules(campaign_id) if rule["basis"] in entry["basis"]]
        return {"formulation_id": formulation_id, "version": version,
                "basis": entry["basis"], "rules": rules}

    # -- inheritance ------------------------------------------------------
    def _ancestors(self, index: dict[str, dict[str, Any]],
                   start: dict[str, Any]) -> dict[str, int]:
        """Return ancestor keys and their distance from ``start`` (1 = parent)."""
        found: dict[str, int] = {}
        current = start
        distance = 1
        seen = {entry_key(start)}
        while current.get("parent") is not None:
            key = ref_key(current["parent"]["formulation_id"], current["parent"]["version"])
            if key in seen:
                raise DomainError("lineage_cycle", "stored lineage contains a cycle")
            seen.add(key)
            found.setdefault(key, distance)
            current = index.get(key)
            if current is None:
                break
            distance += 1
        return found

    def adjust(self, campaign_id: str, formulation_id: str, version: int,
               payload: dict[str, Any], actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(formulation_id, "formulation_id")
        require_identifier(actor, "actor")
        integer(version, "version", 1, 1000000)
        if not isinstance(payload, dict):
            raise DomainError("invalid_lineage", "lineage adjustment must be an object")
        parent = normalize_parent(payload.get("parent"), formulation_id)
        expected = payload.get("expected_lineage_revision")
        if expected is not None:
            expected = integer(expected, "expected_lineage_revision", 0, 1000000)
        with self.store.transaction() as db:
            self.store.get_campaign(campaign_id, db)
            row = db.execute("SELECT * FROM formulations WHERE campaign_id=? AND formulation_id=?"
                             " AND version=?", (campaign_id, formulation_id, version)).fetchone()
            if row is None:
                raise DomainError("not_found", "formulation version does not exist")
            body = self._row_body(row)
            if expected is not None and body["lineage_revision"] != expected:
                raise DomainError("stale_lineage", "formulation lineage changed since it was read")
            if body["parent"] == parent:
                return {**body, "changed": False, "replayed": True}
            index = self._index(campaign_id, db)
            if parent is not None:
                self._require_ref(campaign_id, db, parent, "missing_parent")
                self_ref = ref_key(formulation_id, version)
                if ref_key(parent["formulation_id"], parent["version"]) == self_ref:
                    raise DomainError("lineage_cycle", "a version cannot inherit from itself")
                parent_entry = index[ref_key(parent["formulation_id"], parent["version"])]
                if self_ref in self._ancestors(index, parent_entry):
                    raise DomainError("lineage_cycle",
                                      "lineage adjustment would introduce a cycle")
            updated = {**body, "parent": parent}
            updated.pop("state", None)
            updated.pop("usable", None)
            lineage_revision = body["lineage_revision"] + 1
            now = utc_now()
            db.execute("UPDATE formulations SET body=?,lineage_revision=?,updated_at=?"
                       " WHERE campaign_id=? AND formulation_id=? AND version=?",
                       (canonical(updated), lineage_revision, now,
                        campaign_id, formulation_id, version))
            self.store.audit(db, campaign_id, "lineage_adjusted", actor,
                             ref_key(formulation_id, version), body, updated)
            return {**updated, "lineage_revision": lineage_revision, "state": "usable",
                    "usable": True, "changed": True, "replayed": False}

    # -- comparability rules ---------------------------------------------
    @staticmethod
    def _rule_definition(rule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        description = payload.get("description")
        if not isinstance(description, str) or not 3 <= len(description.strip()) <= 200:
            raise DomainError("invalid_rule", "rule description must have 3-200 characters")
        basis = payload.get("basis")
        if not isinstance(basis, str) or not BASIS_TOKEN.fullmatch(basis):
            raise DomainError("invalid_basis", "comparability rule needs a valid basis token")
        include_lineage = payload.get("include_lineage", False)
        if not isinstance(include_lineage, bool):
            raise DomainError("invalid_rule", "include_lineage must be boolean")
        distance = payload.get("max_lineage_distance")
        if distance is not None:
            if not include_lineage:
                raise DomainError("invalid_rule", "max_lineage_distance requires include_lineage")
            distance = integer(distance, "max_lineage_distance", 1, 64)
        return {"rule_id": rule_id, "description": description.strip(), "basis": basis,
                "include_lineage": include_lineage, "max_lineage_distance": distance}

    def declare_rule(self, campaign_id: str, rule_id: str, payload: dict[str, Any],
                     actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(rule_id, "rule_id")
        require_identifier(actor, "actor")
        if not isinstance(payload, dict):
            raise DomainError("invalid_rule", "comparability rule must be an object")
        definition = self._rule_definition(rule_id, payload)
        content_sha = digest(definition)
        with self.store.transaction() as db:
            self.store.get_campaign(campaign_id, db)
            existing = db.execute("SELECT * FROM comparability_rules WHERE campaign_id=? AND rule_id=?",
                                  (campaign_id, rule_id)).fetchone()
            if existing and existing["content_sha"] == content_sha:
                body = json.loads(existing["body"])
                return {**body, "revision": existing["revision"], "content_sha": content_sha,
                        "replayed": True, "changed": False}
            revision = (existing["revision"] + 1) if existing else 1
            body = {**definition, "revision": revision}
            now = utc_now()
            if existing:
                db.execute("UPDATE comparability_rules SET revision=?,body=?,content_sha=?,updated_at=?"
                           " WHERE campaign_id=? AND rule_id=?",
                           (revision, canonical(body), content_sha, now, campaign_id, rule_id))
            else:
                db.execute("INSERT INTO comparability_rules(campaign_id,rule_id,revision,body,"
                           "content_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           (campaign_id, rule_id, revision, canonical(body), content_sha, now, now))
            db.execute("INSERT INTO comparability_rule_versions(campaign_id,rule_id,revision,body,"
                       "content_sha,created_at) VALUES(?,?,?,?,?,?)",
                       (campaign_id, rule_id, revision, canonical(definition), content_sha, now))
            kind = "comparability_updated" if existing else "comparability_declared"
            self.store.audit(db, campaign_id, kind, actor, rule_id,
                             json.loads(existing["body"]) if existing else None, body)
            return {**body, "content_sha": content_sha, "replayed": False, "changed": True}

    def get_rule(self, campaign_id: str, rule_id: str,
                 revision: int | None = None) -> dict[str, Any] | None:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(rule_id, "rule_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            if revision is None:
                row = db.execute("SELECT * FROM comparability_rules WHERE campaign_id=? AND rule_id=?",
                                 (campaign_id, rule_id)).fetchone()
                if row is None:
                    return None
                return {**json.loads(row["body"]), "content_sha": row["content_sha"],
                        "revision": row["revision"]}
            row = db.execute("SELECT * FROM comparability_rule_versions WHERE campaign_id=? AND rule_id=?"
                             " AND revision=?", (campaign_id, rule_id, revision)).fetchone()
            if row is None:
                raise DomainError("not_found", "comparability rule revision does not exist")
            return {**json.loads(row["body"]), "content_sha": row["content_sha"], "revision": revision}

    def require_rule(self, campaign_id: str, rule_id: str, db: Any | None = None) -> dict[str, Any]:
        if db is None:
            rule = self.get_rule(campaign_id, rule_id)
        else:
            row = db.execute("SELECT * FROM comparability_rules WHERE campaign_id=? AND rule_id=?",
                             (campaign_id, rule_id)).fetchone()
            rule = None if row is None else {**json.loads(row["body"]),
                                             "content_sha": row["content_sha"],
                                             "revision": row["revision"]}
        if rule is None:
            raise DomainError("unknown_rule", "comparability rule does not exist")
        return rule

    def list_rules(self, campaign_id: str) -> list[dict[str, Any]]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT * FROM comparability_rules WHERE campaign_id=? ORDER BY rule_id",
                              (campaign_id,)).fetchall()
            return [{**json.loads(row["body"]), "content_sha": row["content_sha"],
                     "revision": row["revision"]} for row in rows]

    def rule_history(self, campaign_id: str, rule_id: str) -> list[dict[str, Any]]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(rule_id, "rule_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT * FROM comparability_rule_versions WHERE campaign_id=? AND rule_id=?"
                              " ORDER BY revision", (campaign_id, rule_id)).fetchall()
            if not rows:
                raise DomainError("not_found", "comparability rule does not exist")
            return [{**json.loads(row["body"]), "content_sha": row["content_sha"],
                     "revision": row["revision"], "created_at": row["created_at"]} for row in rows]

    # -- comparability and snapshots -------------------------------------
    @staticmethod
    def _ancestors_static(index: dict[str, dict[str, Any]],
                          start: dict[str, Any]) -> dict[str, int]:
        """Ancestor keys with their distance; the entry itself is distance 0."""
        found: dict[str, int] = {entry_key(start): 0}
        current = start
        distance = 1
        while current.get("parent") is not None:
            key = ref_key(current["parent"]["formulation_id"], current["parent"]["version"])
            if key in found:
                return found
            found[key] = distance
            current = index.get(key)
            if current is None:
                break
            distance += 1
        return found

    @classmethod
    def distance(cls, index: dict[str, dict[str, Any]], left: str, right: str) -> int | None:
        """Undirected lineage distance; siblings meet at their common ancestor."""
        if left == right:
            return 0
        left_entry, right_entry = index.get(left), index.get(right)
        if left_entry is None or right_entry is None:
            return None
        up_left = cls._ancestors_static(index, left_entry)
        up_right = cls._ancestors_static(index, right_entry)
        shared = set(up_left) & set(up_right)
        if not shared:
            return None
        return min(up_left[key] + up_right[key] for key in shared)

    @classmethod
    def comparable(cls, rule: dict[str, Any], left: str, right: str,
                   index: dict[str, dict[str, Any]]) -> bool:
        if left == right:
            return True
        left_entry, right_entry = index.get(left), index.get(right)
        if left_entry is None or right_entry is None:
            return False
        if rule["basis"] in left_entry.get("basis", []) and rule["basis"] in right_entry.get("basis", []):
            return True
        if not rule.get("include_lineage"):
            return False
        distance = cls.distance(index, left, right)
        if distance is None:
            return False
        maximum = rule.get("max_lineage_distance")
        return maximum is None or distance <= maximum

    def snapshot(self, campaign_id: str, refs: Any, db: Any | None = None) -> list[dict[str, Any]]:
        """Freeze the versions named by ``refs`` together with their ancestors."""
        if db is None:
            with self.store.connect() as local:
                return self.snapshot(campaign_id, refs, local)
        index = self._index(campaign_id, db)
        needed: set[str] = set()
        pending = [ref_key(item["formulation_id"], item["version"]) for item in refs]
        while pending:
            key = pending.pop()
            if key in needed:
                continue
            entry = index.get(key)
            if entry is None:
                continue
            needed.add(key)
            if entry.get("parent") is not None:
                pending.append(ref_key(entry["parent"]["formulation_id"], entry["parent"]["version"]))
        return [index[key] for key in sorted(needed)]

    @staticmethod
    def snapshot_sha(snapshot: list[dict[str, Any]]) -> str:
        return digest(snapshot)

    # -- sample binding ---------------------------------------------------
    def sample_binding(self, campaign_id: str, item: dict[str, Any], db: Any,
                       required: bool) -> dict[str, Any] | None:
        """Resolve the explicit formulation version carried by a sample."""
        raw = item.get("formulation")
        formulation_id = item.get("formulation_id")
        version = item.get("formulation_version")
        if raw is None and formulation_id is None and version is None:
            if required:
                raise DomainError("missing_formulation",
                                  "samples must name a formulation version")
            return None
        if raw is None:
            if formulation_id is None or version is None:
                raise DomainError("invalid_formulation_ref",
                                  "formulation_id and formulation_version must be supplied together")
            raw = {"formulation_id": formulation_id, "version": version}
        elif formulation_id is not None or version is not None:
            raise DomainError("invalid_formulation_ref",
                              "use either formulation or formulation_id/formulation_version")
        ref = normalize_ref(raw)
        self._require_ref(campaign_id, db, ref, "unknown_formulation")
        return ref
