from __future__ import annotations

from collections import Counter
import json
from typing import Any

from .cohort import Cohorts
from .domain import require_identifier
from .store import Store


class Lifecycle:
    def __init__(self, store: Store):
        self.store = store

    def inventory(self, campaign_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        cohorts = Cohorts(self.store)
        with self.store.connect() as db:
            campaign = self.store.get_campaign(campaign_id, db)
            analyses = db.execute("SELECT analysis_id,kind,source_revision,cohort_sha,body,created_at"
                                  " FROM analyses WHERE campaign_id=? ORDER BY created_at,analysis_id",
                                  (campaign_id,)).fetchall()
            releases = db.execute("SELECT analysis_id,release_id,created_at FROM releases"
                                  " WHERE campaign_id=? ORDER BY created_at,release_id",
                                  (campaign_id,)).fetchall()
            published = {row["analysis_id"] for row in releases}
            items = []
            for row in analyses:
                body = json.loads(row["body"])
                reviews = db.execute("SELECT decision,count(*) AS count FROM reviews"
                                     " WHERE campaign_id=? AND analysis_id=?"
                                     " AND cohort_sha IS ? GROUP BY decision",
                                     (campaign_id, row["analysis_id"], row["cohort_sha"])).fetchall()
                counts = {review["decision"]: review["count"] for review in reviews}
                cohort_current = None
                no_control = []
                if row["kind"] == "cohort":
                    refreshed = cohorts.refresh(body, db)
                    cohort_current = refreshed["cohort_current"]
                    no_control = refreshed["no_valid_control"]
                if row["analysis_id"] in published:
                    state = "published"
                elif row["source_revision"] != campaign["revision"] or cohort_current is False:
                    state = "stale"
                elif no_control:
                    state = "missing_control"
                elif not body["quality"]["passed"]:
                    state = "quality_blocked"
                elif counts.get("reject", 0):
                    state = "review_rejected"
                elif counts.get("approve", 0) >= 2 and (row["kind"] != "cohort" or not no_control):
                    state = "ready_to_publish"
                else:
                    state = "awaiting_reviews"
                items.append({"analysis_id": row["analysis_id"], "kind": row["kind"],
                              "state": state, "source_revision": row["source_revision"],
                              "cohort_sha": row["cohort_sha"], "cohort_current": cohort_current,
                              "no_valid_control": no_control,
                              "quality_gates": body["quality"]["gates"],
                              "reviews": counts, "created_at": row["created_at"]})
            return {"campaign_id": campaign_id, "current_revision": campaign["revision"],
                    "analyses": items, "state_counts": dict(Counter(row["state"] for row in items)),
                    "releases": [dict(row) for row in releases]}
