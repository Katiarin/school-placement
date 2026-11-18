"""Проверки HTTP-интерфейса и полного автоматического перебора."""

from __future__ import annotations

import time

import pytest


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["blocks"] == 208


def test_bootstrap(client):
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    payload = response.get_json()

    meta = payload["meta"]
    assert meta["verification"]["ok"] is True
    assert meta["params"]["accessibility_min"] == 15
    assert meta["params"]["demand_per_1000"] == 120
    assert meta["baseline"]["deficit_total"] == pytest.approx(23311.0)

    blocks = payload["baseline"]["blocks"]
    assert len(blocks) == 208
    assert all(isinstance(row["block_id"], int) for row in blocks[:20])
    assert len({row["block_id"] for row in blocks}) == 208


def test_bootstrap_ids_match_geometry(client):
    baseline = client.get("/api/bootstrap").get_json()["baseline"]["blocks"]
    geometry = client.get("/api/geometry").get_json()["features"]
    baseline_ids = {row["block_id"] for row in baseline}
    geometry_ids = {feature["properties"]["block_id"] for feature in geometry}
    assert baseline_ids == geometry_ids
    assert 58 in baseline_ids


def test_geometry(client):
    response = client.get("/api/geometry")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 208
    ids = [feature["properties"]["block_id"] for feature in payload["features"]]
    assert len(set(ids)) == 208
    assert all(isinstance(value, int) for value in ids)
    assert all(feature["geometry"]["type"] in {"Polygon", "MultiPolygon"} for feature in payload["features"])
    assert payload["outline"]["type"] in {"Polygon", "MultiPolygon", "GeometryCollection"}


def test_scenario_endpoint(client):
    response = client.post("/api/scenario", json={"schools": [{"block_id": 294, "capacity": 600}]})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["capacity_added"] == 600
    assert payload["totals"]["capacity_total"] == 25322
    assert payload["totals"]["deficit_total"] == pytest.approx(22711.0)
    assert payload["totals"]["deficit_reduction"] == pytest.approx(600.0)

    row = next(item for item in payload["blocks"] if item["block_id"] == 294)
    assert row["capacity_base"] == 0
    assert row["capacity"] == 600
    assert row["provision"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "schools",
    [
        [{"block_id": 294, "capacity": 0}],
        [{"block_id": 294, "capacity": -10}],
        [{"block_id": 294, "capacity": 1.5}],
        [{"block_id": 294, "capacity": "1.5"}],
        [{"block_id": 294, "capacity": "abc"}],
        [{"block_id": 294, "capacity": True}],
        [{"block_id": 294}],
        [{"block_id": 999999, "capacity": 600}],
        [{"capacity": 600}],
        "not-a-list",
    ],
)
def test_scenario_validation_errors(client, schools):
    response = client.post("/api/scenario", json={"schools": schools})
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_scenario_does_not_mutate_baseline(client):
    baseline = client.get("/api/bootstrap").get_json()["baseline"]["totals"]

    client.post("/api/scenario", json={"schools": [{"block_id": 294, "capacity": 600}]})
    client.post("/api/scenario", json={"schools": [{"block_id": 305, "capacity": 3000}]})

    after = client.post("/api/scenario", json={"schools": []}).get_json()
    assert after["totals"]["capacity_total"] == baseline["capacity_total"]
    assert after["totals"]["deficit_total"] == pytest.approx(baseline["deficit_total"])
    assert after["totals"]["capacity_added"] == 0


def test_export_csv(client):
    response = client.post("/api/export", json={"schools": [{"block_id": 294, "capacity": 600}]})
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.data.startswith(b"\xef\xbb\xbf")

    text = response.data.decode("utf-8-sig")
    lines = text.splitlines()
    assert len(lines) == 1 + 208 + 1
    header = lines[0].split(";")
    assert header[0] == "Квартал"
    assert "Прирост вместимости, мест" in header
    assert lines[-1].startswith("ИТОГО;399750;48033;24722;25322;600;")

    row_294 = next(line for line in lines if line.startswith("294;"))
    columns = row_294.split(";")
    assert columns[0] == "294"
    assert columns[5] == "600"


def test_export_csv_validation(client):
    response = client.post("/api/export", json={"schools": [{"block_id": 294, "capacity": 0}]})
    assert response.status_code == 400


def test_compare_endpoint(client):
    scenarios = [
        {"name": "Одна школа 600", "schools": [{"block_id": 294, "capacity": 600}]},
        {
            "name": "Две по 300",
            "schools": [
                {"block_id": 294, "capacity": 300},
                {"block_id": 305, "capacity": 300},
            ],
        },
        {"name": "Ошибочный", "schools": [{"block_id": 294, "capacity": 0}]},
    ]
    response = client.post("/api/compare", json={"scenarios": scenarios})
    assert response.status_code == 200
    rows = response.get_json()["rows"]
    assert len(rows) == 3
    assert rows[0]["capacity_added"] == 600
    assert rows[1]["capacity_added"] == 600
    assert rows[0]["deficit_reduction"] == pytest.approx(600.0)
    assert rows[1]["deficit_reduction"] == pytest.approx(600.0)
    assert rows[2]["error"]
    assert rows[2]["deficit_reduction"] is None
    assert rows[2]["local"] is None

    local_one = rows[0]["local"]
    assert len(local_one["blocks"]) == 1
    assert local_one["blocks"][0]["block_id"] == 294
    assert local_one["deficit_before"] > local_one["deficit_after"]
    assert local_one["deficit_reduction"] == pytest.approx(
        local_one["deficit_before"] - local_one["deficit_after"]
    )
    assert local_one["deficit_reduction"] > 0
    assert local_one["provision_after"] > local_one["provision_before"]
    assert local_one["affected_blocks"] > 0
    assert local_one["affected_population"] > 0

    local_two = rows[1]["local"]
    assert len(local_two["blocks"]) == 2
    assert {block["block_id"] for block in local_two["blocks"]} == {294, 305}
    assert sum(block["capacity_added"] for block in local_two["blocks"]) == 600
    assert local_two["deficit_reduction"] > 0


def test_compare_validation(client):
    assert client.post("/api/compare", json={}).status_code == 400
    assert client.post("/api/compare", json={"scenarios": []}).status_code == 400


def test_auto_capacity_validation(client):
    response = client.post("/api/auto", json={"capacity": 0})
    assert response.status_code == 400
    assert "больше нуля" in response.get_json()["error"]


def test_auto_status_not_found(client):
    assert client.get("/api/auto/unknown").status_code == 404


@pytest.mark.slow
def test_auto_search_all_blocks(client, model):
    """Полный перебор всех кварталов; рекомендация только там, где нужна школа."""
    candidates = model.auto_candidate_ids()
    assert len(candidates) == 208
    assert candidates == [int(block_id) for block_id in model.block_ids]
    start = client.post("/api/auto", json={"capacity": 600})
    assert start.status_code == 202
    payload = start.get_json()
    job_id = payload["job_id"]
    assert payload["total"] == len(candidates)

    # второй запуск во время работы запрещён
    busy = client.post("/api/auto", json={"capacity": 600})
    assert busy.status_code == 409

    deadline = time.time() + 240
    status = None
    while time.time() < deadline:
        response = client.get(f"/api/auto/{job_id}")
        assert response.status_code == 200
        status = response.get_json()
        assert status["checked"] <= status["total"]
        if status["state"] != "running":
            break
        time.sleep(0.5)

    assert status is not None
    assert status["state"] == "done", status
    assert status["complete"] is True
    assert status["errors"] == []
    assert status["checked"] == len(candidates)
    assert status["total"] == len(candidates)
    assert len(status["rows"]) == len(candidates)
    assert len({row["block_id"] for row in status["rows"]}) == len(candidates)

    rows = status["rows"]
    block_ids = {row["block_id"] for row in rows}
    assert block_ids == {int(block_id) for block_id in model.block_ids}
    eligible = [row for row in rows if row["eligible"]]
    assert len(eligible) == 111
    assert all(row["population"] > 0 and row["deficit_before"] > 0 for row in eligible)
    assert all(
        row["population"] <= 0 or row["deficit_before"] <= 0
        for row in rows
        if not row["eligible"]
    )
    no_population = next(row for row in rows if row["block_id"] == 58)
    assert no_population["eligible"] is False
    no_deficit = next(row for row in rows if row["block_id"] == 4232)
    assert no_deficit["eligible"] is False
    reductions = [row["deficit_reduction"] for row in rows]
    assert reductions == sorted(reductions, reverse=True)
    order_keys = [
        (-row["deficit_reduction"], -row["deficit_before"], row["block_id"]) for row in rows
    ]
    assert order_keys == sorted(order_keys)
    best = max(row["deficit_reduction"] for row in eligible)
    tied = [row for row in eligible if abs(row["deficit_reduction"] - best) <= 1e-6]
    best_local = max(row["deficit_before"] for row in tied)
    expected_best = {
        row["block_id"] for row in tied if abs(row["deficit_before"] - best_local) <= 1e-6
    }
    assert set(status["best_ids"]) == expected_best
    assert status["best_reduction"] == pytest.approx(best)

    baseline = model.baseline_totals()
    for row in rows[:3]:
        manual = model.evaluate([{"block_id": row["block_id"], "capacity": 600}])
        assert row["deficit_before"] == pytest.approx(
            model.baseline.loc[row["block_id"], "deficit"], abs=1e-6
        )
        assert row["deficit_after_block"] == pytest.approx(
            manual.rows.loc[row["block_id"], "deficit"], abs=1e-6
        )
        assert manual.totals["deficit_total"] == pytest.approx(row["deficit_after"], abs=1e-6)
        assert manual.totals["deficit_reduction"] == pytest.approx(row["deficit_reduction"], abs=1e-6)
        # независимость: вместимость не накапливается между вариантами
        assert manual.totals["capacity_total"] == baseline["capacity_total"] + 600

    # ручное размещение в найденном квартале воспроизводит автоматический результат
    best_block = status["best_ids"][0]
    manual = model.evaluate([{"block_id": best_block, "capacity": 600}])
    best_row = next(row for row in rows if row["block_id"] == best_block)
    assert manual.totals["deficit_total"] == pytest.approx(best_row["deficit_after"], abs=1e-6)
    assert manual.totals["deficit_reduction"] == pytest.approx(best_row["deficit_reduction"], abs=1e-6)
