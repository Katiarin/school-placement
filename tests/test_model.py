"""Проверки расчётной модели."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from school_app.api import parse_capacity
from school_app.model import SchoolModel, equal_reductions, normalize_schools


def test_baseline_reproduces_practice_results(model):
    verification = model.verification()
    assert verification["ok"], verification
    assert verification["max_abs_diff"] < 1e-9
    assert verification["compared_blocks"] == 117


def test_baseline_totals_match_practice(model):
    totals = model.baseline_totals()
    assert totals["blocks_total"] == 208
    assert totals["population_total"] == pytest.approx(399750.0)
    assert totals["capacity_total"] == 24722
    assert totals["demand_total"] == pytest.approx(48033.0)
    assert totals["deficit_total"] == pytest.approx(23311.0)
    assert totals["provision_total"] == pytest.approx(0.5146878187912477, rel=1e-9)


def test_empty_scenario_equals_baseline(model):
    result = model.evaluate([])
    assert result.capacity_added == 0
    assert result.totals["capacity_total"] == model.baseline_totals()["capacity_total"]
    assert result.totals["deficit_total"] == pytest.approx(model.baseline_totals()["deficit_total"])
    assert result.totals["deficit_reduction"] == pytest.approx(0.0)


def test_new_school_increases_capacity_and_reduces_deficit(model):
    baseline = model.baseline_totals()
    result = model.evaluate([{"block_id": 294, "capacity": 600}])

    assert result.capacity_added == 600
    assert result.totals["capacity_total"] == baseline["capacity_total"] + 600
    assert result.totals["deficit_total"] == pytest.approx(baseline["deficit_total"] - 600)
    assert result.totals["deficit_reduction"] == pytest.approx(600)

    row = result.rows.loc[294]
    assert row["capacity_base"] == 0
    assert row["capacity"] == 600
    assert row["capacity_added"] == 600
    assert row["provision"] == pytest.approx(1.0)


def test_capacity_is_counted_once_per_block(model):
    result = model.evaluate(
        [
            {"block_id": 294, "capacity": 300},
            {"block_id": 294, "capacity": 300},
        ]
    )
    assert result.capacity_added == 600
    assert len(result.schools) == 1
    assert result.rows.loc[294, "capacity"] == 600
    assert result.totals["capacity_total"] == model.baseline_totals()["capacity_total"] + 600


def test_two_schools_in_different_blocks(model):
    result = model.evaluate(
        [
            {"block_id": 294, "capacity": 300},
            {"block_id": 305, "capacity": 300},
        ]
    )
    assert result.capacity_added == 600
    assert result.rows.loc[294, "capacity"] == 300
    assert result.rows.loc[305, "capacity"] == 300
    assert result.totals["deficit_reduction"] == pytest.approx(600)


def test_scenarios_do_not_change_source_data(model, dataset):
    population_before = model._base_data["population"].copy()  # noqa: SLF001
    capacity_before = model._base_data["capacity"].copy()  # noqa: SLF001
    baseline_provision_before = model.baseline["provision"].copy()
    matrix_before = dataset.matrix.to_numpy().copy()

    model.evaluate([{"block_id": 294, "capacity": 600}])
    model.evaluate([{"block_id": 305, "capacity": 300}, {"block_id": 188, "capacity": 900}])

    pd.testing.assert_series_equal(model._base_data["population"], population_before)  # noqa: SLF001
    pd.testing.assert_series_equal(model._base_data["capacity"], capacity_before)  # noqa: SLF001
    pd.testing.assert_series_equal(model.baseline["provision"], baseline_provision_before)
    assert np.array_equal(dataset.matrix.to_numpy(), matrix_before)
    assert model.verification()["ok"]


def test_capacity_validation():
    for value in (600, "600", 600.0, " 600 "):
        assert parse_capacity(value) == 600

    for value in (0, -1, 1.5, "1.5", "abc", "", "  ", True, False, None, 100001, [600]):
        with pytest.raises(ValueError):
            parse_capacity(value)


def test_normalize_schools():
    schools = normalize_schools(
        [
            {"block_id": 1, "capacity": 300},
            {"block_id": 2, "capacity": 600},
            {"block_id": 1, "capacity": 300},
        ]
    )
    assert schools == [{"block_id": 1, "capacity": 600}, {"block_id": 2, "capacity": 600}]


def test_equal_reductions_tolerance():
    assert equal_reductions(600.0, 600.0)
    assert equal_reductions(600.0, 600.0 + 1e-9)
    assert not equal_reductions(600.0, 601.0)


def _fake_model(reductions, failing=(), deficits=None):
    blocks = pd.DataFrame(
        {"population": [100.0, 200.0, 300.0], "capacity": [0, 0, 0]},
        index=pd.Index([1, 2, 3], name="block_id"),
    )
    if deficits is None:
        deficits = {1: 50.0, 2: 50.0, 3: 50.0}
    fake = SimpleNamespace(
        block_ids=np.array([1, 2, 3]),
        baseline_totals_value={"deficit_total": 100.0},
        baseline=pd.DataFrame(
            {"deficit": [deficits[1], deficits[2], deficits[3]]},
            index=pd.Index([1, 2, 3], name="block_id"),
        ),
        _base_data=blocks,
    )

    def evaluate(schools):
        block_id = schools[0]["block_id"]
        if block_id in failing:
            raise RuntimeError("тестовая ошибка квартала")
        return SimpleNamespace(
            totals={
                "deficit_total": 100.0 - reductions[block_id],
                "provision_total": 0.5,
            },
            rows=pd.DataFrame(
                {"deficit": [10.0]},
                index=pd.Index([block_id], name="block_id"),
            ),
        )

    fake.evaluate = evaluate
    return fake


def test_auto_search_marks_all_equal_best_variants():
    fake = _fake_model({1: 40.0, 2: 40.0 + 1e-9, 3: 20.0})
    result = SchoolModel.auto_search(fake, 600)

    assert result.checked == 3
    assert result.complete is True
    assert {row["block_id"] for row in result.rows} == {1, 2, 3}
    assert {row["block_id"] for row in result.rows[:2]} == {1, 2}
    assert result.rows[-1]["block_id"] == 3
    assert set(result.best_ids) == {1, 2}
    assert result.best_reduction == pytest.approx(40.0)


def test_auto_search_checks_all_blocks_but_does_not_recommend_without_population():
    fake = _fake_model({1: 40.0, 2: 30.0, 3: 20.0})
    fake._base_data.loc[2, "population"] = 0.0

    result = SchoolModel.auto_search(fake, 600)

    assert result.total == 3
    assert result.checked == 3
    assert result.complete is True
    assert {row["block_id"] for row in result.rows} == {1, 2, 3}
    assert next(row for row in result.rows if row["block_id"] == 2)["eligible"] is False
    assert result.best_ids == [1]


def test_auto_search_checks_all_blocks_but_does_not_recommend_without_deficit():
    fake = _fake_model({1: 40.0, 2: 40.0, 3: 40.0}, deficits={1: 50.0, 2: 0.0, 3: 30.0})

    result = SchoolModel.auto_search(fake, 600)

    assert result.total == 3
    assert result.checked == 3
    assert result.complete is True
    assert {row["block_id"] for row in result.rows} == {1, 2, 3}
    assert next(row for row in result.rows if row["block_id"] == 2)["eligible"] is False
    assert result.best_ids == [1]


def test_auto_search_prefers_largest_local_deficit_on_tie():
    fake = _fake_model(
        {1: 40.0, 2: 40.0, 3: 40.0},
        deficits={1: 70.0, 2: 90.0, 3: 100.0},
    )

    result = SchoolModel.auto_search(fake, 600)

    assert [row["block_id"] for row in result.rows] == [3, 2, 1]
    assert result.best_ids == [3]
    best = result.rows[0]
    assert best["deficit_before"] == pytest.approx(100.0)
    assert best["deficit_after_block"] == pytest.approx(10.0)


def test_auto_search_reports_incomplete_search_explicitly():
    fake = _fake_model({1: 40.0, 2: 30.0, 3: 20.0}, failing={2})
    result = SchoolModel.auto_search(fake, 600)

    assert result.checked == 3
    assert result.complete is False
    assert result.errors == [{"block_id": 2, "error": "тестовая ошибка квартала"}]
    assert result.best_ids == [1]


def test_auto_search_recalculates_each_block_independently(model):
    """Вместимость не накапливается между вариантами: каждый вариант — база + 600."""
    baseline_capacity = model.baseline_totals()["capacity_total"]
    block_ids = [294, 305, 188, 58]

    totals = []
    for block_id in block_ids:
        result = model.evaluate([{"block_id": block_id, "capacity": 600}])
        totals.append(result.totals["capacity_total"])
        assert result.capacity_added == 600
        assert result.totals["capacity_total"] == baseline_capacity + 600

    assert totals == [baseline_capacity + 600] * len(block_ids)
    assert model.baseline_totals()["capacity_total"] == baseline_capacity
