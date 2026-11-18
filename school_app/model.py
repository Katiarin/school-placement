"""Расчёт обеспеченности населения местами в школах (метод практики 2).

Используется та же функция BlocksNet, что и в исходном ноутбуке:
``blocksnet.analysis.provision.competitive_provision`` версии 1.0.0a9.

Метод:
  1. потребность квартала = ceil(население / 1000 * норматив), норматив 120 мест на 1000 жителей;
  2. собственные мощности квартала закрывают его потребность (self-supply);
  3. оставшийся спрос распределяется линейной оптимизацией между школами,
     до которых можно добраться за 15 минут (матрица времени в пути на автомобиле);
  4. обеспеченность квартала = закрытый в нормативе спрос / потребность;
  5. дефицит мест = потребность − закрытый в нормативе спрос (по всей территории — сумма по кварталам).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from blocksnet.analysis.provision import competitive_provision

# PuLP/CBC не рассчитан на одновременные вызовы из разных потоков:
# перебор и пользовательские сценарии сериализуются этим замком.
_SOLVER_LOCK = threading.Lock()

# Допуск, с которым результаты считаются равными (места).
TIE_TOLERANCE = 1e-6

BLOCK_FIELDS = (
    "block_id",
    "population",
    "demand",
    "capacity_base",
    "capacity",
    "within",
    "deficit",
    "provision",
    "provision_base",
    "deficit_base",
    "deficit_delta",
    "provision_delta_pp",
)


class CalculationError(Exception):
    """Ошибка расчёта, которую нужно показать пользователю."""


@dataclass
class ScenarioResult:
    schools: list[dict]
    capacity_added: int
    rows: pd.DataFrame
    totals: dict

    def payload(self) -> dict:
        return {
            "schools": self.schools,
            "capacity_added": int(self.capacity_added),
            "totals": self.totals,
            "blocks": frame_payload(self.rows),
        }


@dataclass
class AutoSearchResult:
    capacity: int
    started_at: float
    elapsed: float
    baseline_deficit: float = 0.0
    rows: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    checked: int = 0
    total: int = 0
    complete: bool = False
    best_ids: list[int] = field(default_factory=list)
    best_reduction: float | None = None

    def payload(self) -> dict:
        return {
            "capacity": int(self.capacity),
            "checked": int(self.checked),
            "total": int(self.total),
            "complete": bool(self.complete),
            "elapsed": round(float(self.elapsed), 2),
            "baseline_deficit": self.baseline_deficit,
            "best_ids": [int(value) for value in self.best_ids],
            "best_reduction": self.best_reduction,
            "rows": self.rows,
            "errors": self.errors,
        }


def frame_payload(frame: pd.DataFrame) -> list[dict]:
    """DataFrame → список словарей с «чистыми» типами для JSON."""
    payload = []
    columns = list(frame.columns)
    for block_id in frame.index:
        item = {"block_id": int(block_id)}
        for column in columns:
            if column == "block_id":
                continue
            value = frame.at[block_id, column]
            if isinstance(value, (np.integer,)):
                item[column] = int(value)
            elif isinstance(value, (np.floating, float)):
                item[column] = None if not np.isfinite(value) else float(value)
            elif isinstance(value, (np.bool_,)):
                item[column] = bool(value)
            else:
                item[column] = value
        payload.append(item)
    return payload


def normalize_schools(schools: list[dict]) -> list[dict]:
    """Проверяет и объединяет школы, стоящие в одном квартале."""
    merged: dict[int, int] = {}
    order: list[int] = []
    for school in schools:
        block_id = int(school["block_id"])
        capacity = int(school["capacity"])
        if block_id not in merged:
            order.append(block_id)
            merged[block_id] = 0
        merged[block_id] += capacity
    return [{"block_id": block_id, "capacity": merged[block_id]} for block_id in order]


def equal_reductions(left: float, right: float) -> bool:
    """Равенство результатов с учётом точности расчёта (места)."""
    return abs(left - right) <= TIE_TOLERANCE * max(1.0, abs(left), abs(right))


def best_variant_ids(rows: list[dict]) -> tuple[float | None, list[int]]:
    """Возвращает лучшее сокращение дефицита и рекомендованные кварталы.

    Сначала максимизируется сокращение дефицита по всей территории среди
    кварталов, где школа действительно нужна (есть население и дефицит).
    Если несколько кварталов дают одинаковый результат, предпочтение отдаётся
    кварталу с наибольшим собственным дефицитом. Остальные проверенные
    кварталы остаются в таблице, но не рекомендуются.
    """
    eligible = [row for row in rows if row.get("eligible")]
    if not eligible:
        return None, []
    best_reduction = max(row["deficit_reduction"] for row in eligible)
    tied = [
        row for row in eligible if equal_reductions(row["deficit_reduction"], best_reduction)
    ]
    best_local_deficit = max(row["deficit_before"] for row in tied)
    best_ids = [
        row["block_id"]
        for row in tied
        if equal_reductions(row["deficit_before"], best_local_deficit)
    ]
    return float(best_reduction), best_ids


class SchoolModel:
    def __init__(
        self,
        blocks_df: pd.DataFrame,
        matrix: pd.DataFrame,
        accessibility: int,
        demand_per_1000: int,
    ):
        self.accessibility = int(accessibility)
        self.demand_per_1000 = int(demand_per_1000)

        self.block_ids = blocks_df.index.to_numpy(dtype="int64")
        self._population = blocks_df["population"].fillna(0).astype("float64")
        self._capacity = blocks_df["capacity_school"].fillna(0).round().astype("int64")
        self._provision_saved = (
            blocks_df["provision_saved"].astype("float64") if "provision_saved" in blocks_df else None
        )

        if not np.array_equal(matrix.index.to_numpy(), matrix.columns.to_numpy()):
            raise CalculationError("матрица времени в пути должна быть квадратной")
        missing = [int(value) for value in self.block_ids if value not in matrix.index]
        if missing:
            raise CalculationError("в матрице времени в пути нет кварталов: " + ", ".join(map(str, missing[:10])))

        self.matrix = matrix
        self._base_data = pd.DataFrame(
            {"population": self._population.to_numpy(), "capacity": self._capacity.to_numpy()},
            index=pd.Index(self.block_ids, name="block_id"),
        )
        self.baseline = self._run(self._base_data.copy())
        self.baseline_totals_value = self._totals(self.baseline)

    # ------------------------------------------------------------------ расчёт

    def _run(self, data: pd.DataFrame) -> pd.DataFrame:
        """Пересчёт обеспеченности для произвольного набора мощностей."""
        try:
            with _SOLVER_LOCK:
                provision, _links = competitive_provision(
                    data[["population", "capacity"]].copy(),
                    self.matrix,
                    accessibility=self.accessibility,
                    demand=self.demand_per_1000,
                )
        except Exception as error:  # noqa: BLE001 - сообщение уходит в интерфейс
            raise CalculationError(f"не удалось решить задачу распределения: {error}") from error

        frame = pd.DataFrame(index=data.index)
        frame["population"] = data["population"].astype("float64")
        frame["demand"] = provision["demand"].round().astype("int64")
        frame["capacity"] = data["capacity"].round().astype("int64")
        frame["within"] = provision["demand_within"].astype("float64")
        frame["deficit"] = (frame["demand"] - frame["within"]).clip(lower=0.0)
        frame["provision"] = frame["within"] / frame["demand"].where(frame["demand"] > 0)
        return frame

    @staticmethod
    def _totals(frame: pd.DataFrame) -> dict:
        demand_total = float(frame["demand"].sum())
        within_total = float(frame["within"].sum())
        capacity_total = int(frame["capacity"].sum())
        population_total = float(frame["population"].sum())
        deficit_total = float(frame["deficit"].sum())
        with_population = frame["demand"] > 0
        return {
            "population_total": population_total,
            "demand_total": demand_total,
            "capacity_total": capacity_total,
            "within_total": within_total,
            "deficit_total": deficit_total,
            "provision_total": float(within_total / demand_total) if demand_total > 0 else 0.0,
            "mean_provision": float(frame.loc[with_population, "provision"].mean()) if with_population.any() else 0.0,
            "blocks_total": int(len(frame)),
            "blocks_with_population": int(with_population.sum()),
            "blocks_with_deficit": int((frame["deficit"] > 1e-9).sum()),
            "blocks_with_school_capacity": int((frame["capacity"] > 0).sum()),
        }

    #address()
    def has_block(self, block_id: int) -> bool:
        return int(block_id) in self._base_data.index

    def auto_candidate_ids(self) -> list[int]:
        """Все кварталы территории, которые проверяет автоподбор."""
        return [int(block_id) for block_id in self.block_ids]

    def baseline_totals(self) -> dict:
        return dict(self.baseline_totals_value)

    def baseline_payload(self) -> dict:
        return {"totals": self.baseline_totals(), "blocks": frame_payload(self.baseline)}

    def evaluate(self, schools: list[dict]) -> ScenarioResult:
        """Оценивает сценарий. Исходные данные не изменяются."""
        normalized = normalize_schools(schools)
        for school in normalized:
            if school["block_id"] not in self._base_data.index:
                raise CalculationError(f"квартал {school['block_id']} отсутствует в данных")

        data = self._base_data.copy()
        for school in normalized:
            data.loc[school["block_id"], "capacity"] += int(school["capacity"])

        scenario = self._run(data)
        scenario["capacity_base"] = self._base_data["capacity"].astype("int64")
        scenario["provision_base"] = self.baseline["provision"]
        scenario["deficit_base"] = self.baseline["deficit"]
        scenario["deficit_delta"] = scenario["deficit"] - scenario["deficit_base"]
        scenario["provision_delta_pp"] = (scenario["provision"] - scenario["provision_base"]) * 100.0

        totals = self._totals(scenario)
        totals["capacity_added"] = int(sum(school["capacity"] for school in normalized))
        totals["deficit_reduction"] = float(self.baseline_totals_value["deficit_total"] - totals["deficit_total"])
        totals["provision_delta_pp"] = float(
            (totals["provision_total"] - self.baseline_totals_value["provision_total"]) * 100.0
        )
        totals["schools_count"] = len(normalized)

        columns = [
            "population",
            "demand",
            "capacity_base",
            "capacity",
            "within",
            "deficit",
            "provision",
            "provision_base",
            "deficit_base",
            "deficit_delta",
            "provision_delta_pp",
        ]
        rows = scenario[columns].copy()
        rows.insert(0, "capacity_added", 0)
        for school in normalized:
            rows.loc[school["block_id"], "capacity_added"] = school["capacity"]

        return ScenarioResult(
            schools=normalized,
            capacity_added=totals["capacity_added"],
            rows=rows,
            totals=totals,
        )

    def verification(self) -> dict:
        """Сверка исходного расчёта с сохранёнными результатами практики 2."""
        if self._provision_saved is None:
            return {"ok": False, "max_abs_diff": None, "reason": "нет сохранённых значений provision_school"}
        calculated = self.baseline["provision"].to_numpy(dtype="float64")
        saved = self._provision_saved.to_numpy(dtype="float64")
        mask = np.isfinite(saved) & np.isfinite(calculated)
        if not mask.any():
            return {"ok": False, "max_abs_diff": None, "reason": "нет общих кварталов для сверки"}
        diff = np.abs(calculated[mask] - saved[mask])
        max_diff = float(diff.max())
        return {
            "ok": max_diff <= 1e-6,
            "max_abs_diff": max_diff,
            "compared_blocks": int(mask.sum()),
            "mean_abs_diff": float(diff.mean()),
        }

    # ------------------------------------------------------- автоматический поиск

    def auto_search(self, capacity: int, progress=None) -> AutoSearchResult:
        """Перебирает все кварталы территории независимо, без накопления вместимости.

        Каждый вариант — одна школа. Кварталы без населения или без дефицита
        тоже проверяются и остаются в таблице, но не рекомендуются: школа там
        не нужна. progress(checked, total, best_reduction, best_ids) вызывается
        после каждого квартала.
        """
        candidate_ids = [int(block_id) for block_id in self.block_ids]
        total = len(candidate_ids)
        capacity = int(capacity)
        baseline_deficit = float(self.baseline_totals_value["deficit_total"])
        result = AutoSearchResult(
            capacity=capacity,
            started_at=time.perf_counter(),
            elapsed=0.0,
            baseline_deficit=baseline_deficit,
            total=total,
        )

        for position, block_id in enumerate(candidate_ids, start=1):
            block_id = int(block_id)
            try:
                scenario = self.evaluate([{"block_id": block_id, "capacity": capacity}])
                deficit = float(scenario.totals["deficit_total"])
                reduction = baseline_deficit - deficit
                population = float(self._base_data.loc[block_id, "population"])
                deficit_before = float(self.baseline.loc[block_id, "deficit"])
                result.rows.append(
                    {
                        "block_id": block_id,
                        "population": population,
                        "capacity_base": int(self._base_data.loc[block_id, "capacity"]),
                        "capacity_after": int(self._base_data.loc[block_id, "capacity"]) + capacity,
                        # Проверяем все кварталы, но рекомендуем только те, где школа нужна.
                        "eligible": population > 0 and deficit_before > 1e-9,
                        # Дефицит по всей территории после варианта.
                        "deficit_after": deficit,
                        # Дефицит именно в квартале, куда добавляется школа.
                        "deficit_before": deficit_before,
                        "deficit_after_block": float(scenario.rows.loc[block_id, "deficit"]),
                        "deficit_reduction": float(reduction),
                        "provision_after": float(scenario.totals["provision_total"]),
                    }
                )
            except Exception as error:  # noqa: BLE001 - ошибка квартала не должна ронять перебор
                result.errors.append({"block_id": block_id, "error": str(error)})

            result.checked = position
            if progress is not None:
                best, best_ids = best_variant_ids(result.rows)
                progress(position, total, best, best_ids)

        result.elapsed = time.perf_counter() - result.started_at
        result.complete = result.checked == total and not result.errors
        result.rows.sort(
            key=lambda row: (-row["deficit_reduction"], -row["deficit_before"], row["block_id"])
        )
        result.best_reduction, result.best_ids = best_variant_ids(result.rows)
        return result
