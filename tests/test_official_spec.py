import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from backend.app.api.orchestrator import load_default_official_data
from backend.app.main import app

client = TestClient(app)
examples_dir = Path(__file__).resolve().parents[1] / "api" / "examples"


def test_official_spec_orchestrator():
    try:
        health_res = client.get("/api/v1/health")
        assert health_res.status_code == 200
        assert health_res.json()["status"] == "ok"

        with open(examples_dir / "ReferenceDataExample.json", encoding="utf-8") as f:
            ref_data = json.load(f)
        ref_res = client.put("/api/v1/reference", json=ref_data)
        assert ref_res.status_code == 200
        assert ref_res.json()["status"] == "loaded"

        with open(examples_dir / "TravelMatrixExample.json", encoding="utf-8") as f:
            matrix_data = json.load(f)
        mat_res = client.put("/api/v1/reference/travel-matrix", json=matrix_data)
        assert mat_res.status_code == 200
        assert mat_res.json()["status"] == "loaded"

        with open(examples_dir / "DayInputExample.json", encoding="utf-8") as f:
            day_data = json.load(f)

        invalid_day = client.put("/api/v1/days/2026-10-26", json={})
        assert invalid_day.status_code == 422
        invalid_body = invalid_day.json()
        assert "detail" not in invalid_body
        assert invalid_body["error"]["code"] == "UNPROCESSABLE_ENTITY"
        assert isinstance(invalid_body["error"]["details"], list) and invalid_body["error"]["details"]

        day_res = client.put("/api/v1/days/2026-10-26", json=day_data)
        assert day_res.status_code == 200
        assert day_res.json()["status"] == "loaded"

        plan_url = "/api/v1/days/2026-10-26/plan"
        event_url = "/api/v1/days/2026-10-26/events"

        missing = client.get(plan_url)
        assert missing.status_code == 404
        assert "detail" not in missing.json()
        assert missing.json()["error"]["code"] == "NOT_FOUND"

        malformed = client.post(
            event_url,
            content="{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert malformed.status_code == 400
        assert "detail" not in malformed.json()
        assert malformed.json()["error"]["code"] == "BAD_REQUEST"

        for bad_limit in (0, 301):
            bad_plan = client.post(plan_url, json={"time_limit_sec": bad_limit})
            assert bad_plan.status_code == 422
            assert "detail" not in bad_plan.json()
            assert "error" in bad_plan.json()

        unknown_staff = client.post(event_url, json={
            "event_id": "unknown-staff",
            "type": "STAFF_UNAVAILABLE",
            "timestamp": day_data["now"],
            "staff_id": "UNKNOWN",
            "reason": "OTHER",
        })
        assert unknown_staff.status_code == 422
        assert "detail" not in unknown_staff.json()
        assert "error" in unknown_staff.json()
        assert client.get(plan_url).status_code == 404

        naive_event = client.post(event_url, json={
            "event_id": "naive-ts",
            "type": "TIME_TICK",
            "timestamp": "2026-10-26T09:00:00",
        })
        assert naive_event.status_code == 422
        assert "detail" not in naive_event.json()

        plan_res = client.post(plan_url, json={"time_limit_sec": 1})
        assert plan_res.status_code == 200
        plan = plan_res.json()
        assert plan["plan_version"] >= 1
        assert len(plan["routes"]) > 0

        initial_rebuild = client.post(plan_url, json={"time_limit_sec": 1})
        assert initial_rebuild.status_code == 200
        assert initial_rebuild.json()["plan_version"] == 1

        with open(examples_dir / "VisitDelayedEventExample.json", encoding="utf-8") as f:
            event_data = json.load(f)
        for field in ("timestamp", "expected_end"):
            event_data[field] = datetime.fromisoformat(event_data[field]).astimezone(timezone.utc).isoformat()

        evt_http = client.post(event_url, json=event_data)
        assert evt_http.status_code == 200, evt_http.text
        evt_res = evt_http.json()
        assert evt_res["accepted"] is True
        assert evt_res["as_of"].endswith("+05:00")

        repeated = client.post(event_url, json=event_data).json()
        assert repeated["duplicate"] is True
        assert repeated["plan_version"] == evt_res["plan_version"]

        conflict = client.post(plan_url, json={"time_limit_sec": 1})
        assert conflict.status_code == 409
        assert "detail" not in conflict.json()
        assert "error" in conflict.json()

        final_res = client.get(plan_url)
        assert final_res.status_code == 200
        assert final_res.json()["plan_version"] == evt_res["plan_version"]
    finally:
        load_default_official_data()


if __name__ == "__main__":
    test_official_spec_orchestrator()
    print("[OK] ALL 7 ORCHESTRATOR STEPS PASSED")
