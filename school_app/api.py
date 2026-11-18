"""HTTP-интерфейс приложения (JSON API)."""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime
from math import isfinite

from flask import Blueprint, Response, current_app, jsonify, request, send_file

from .model import CalculationError, SchoolModel

bp = Blueprint("api", __name__, url_prefix="/api")

MAX_SCHOOLS_PER_SCENARIO = 50
MAX_CAPACITY = 100_000
MAX_COMPARE_SCENARIOS = 12

_INTEGER_RE = re.compile(r"^\d+$")


# --------------------------------------------------------------------- helpers


def model() -> SchoolModel:
    return current_app.extensions["model"]


def dataset():
    return current_app.extensions["dataset"]


def api_error(message: str, status: int = 400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def parse_capacity(value, field: str = "Вместимость") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}: укажите целое положительное число мест.")
    if isinstance(value, str):
        text = value.strip().replace(" ", "").replace("\u00a0", "")
        if not text:
            raise ValueError(f"{field}: поле не может быть пустым.")
        if not _INTEGER_RE.fullmatch(text):
            raise ValueError(f"{field}: укажите целое положительное число мест (например, 600).")
        number = int(text)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{field}: укажите целое число мест, дробные значения не поддерживаются.")
        number = int(value)
    else:
        raise ValueError(f"{field}: укажите целое положительное число мест.")

    if number <= 0:
        raise ValueError(f"{field}: вместимость должна быть больше нуля.")
    if number > MAX_CAPACITY:
        raise ValueError(f"{field}: слишком большое значение (максимум {MAX_CAPACITY:,} мест).".replace(",", " "))
    return number


def parse_block_id(value, field: str = "Квартал") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}: укажите идентификатор квартала.")
    if isinstance(value, str):
        text = value.strip()
        if not _INTEGER_RE.fullmatch(text):
            raise ValueError(f"{field}: идентификатор квартала должен быть целым числом.")
        number = int(text)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise ValueError(f"{field}: укажите идентификатор квартала.")

    if not model().has_block(number):
        raise ValueError(f"{field}: квартал {number} отсутствует в данных территории.")
    return number


def parse_schools(body: dict) -> list[dict]:
    raw = body.get("schools", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("Список школ должен быть массивом.")
    if len(raw) > MAX_SCHOOLS_PER_SCENARIO:
        raise ValueError(f"В одном сценарии можно разместить не более {MAX_SCHOOLS_PER_SCENARIO} школ.")
    schools = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Школа №{index}: ожидается объект с полями block_id и capacity.")
        block_id = parse_block_id(item.get("block_id"), f"Школа №{index}, квартал")
        capacity = parse_capacity(item.get("capacity"), f"Школа №{index}, вместимость")
        schools.append({"block_id": block_id, "capacity": capacity})
    return schools


def request_json() -> dict:
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValueError("Ожидается JSON-объект.")
    return body


# ------------------------------------------------------------------ авто-подбор


class AutoJob:
    """Фоновый перебор кварталов с отображением прогресса."""

    def __init__(self, school_model: SchoolModel, capacity: int):
        self.id = uuid.uuid4().hex
        self.model = school_model
        self.capacity = capacity
        self.state = "running"
        self.checked = 0
        self.total = len(school_model.auto_candidate_ids())
        self.best_reduction = None
        self.best_ids: list[int] = []
        self.error = None
        self.result = None
        self.started = time.perf_counter()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=f"auto-{self.id[:6]}", daemon=True)

    def start(self) -> "AutoJob":
        self._thread.start()
        return self

    def _progress(self, checked: int, total: int, best, best_ids: list[int]) -> None:
        with self._lock:
            self.checked = checked
            self.total = total
            self.best_reduction = best
            self.best_ids = list(best_ids)

    def _run(self) -> None:
        try:
            result = self.model.auto_search(self.capacity, progress=self._progress)
            with self._lock:
                self.result = result
                self.state = "done"
        except Exception as error:  # noqa: BLE001 - показываем текст ошибки в интерфейсе
            with self._lock:
                self.state = "error"
                self.error = str(error)

    def status(self) -> dict:
        with self._lock:
            elapsed = self.result.elapsed if self.result is not None else time.perf_counter() - self.started
            payload = {
                "id": self.id,
                "state": self.state,
                "capacity": self.capacity,
                "checked": self.checked,
                "total": self.total,
                "elapsed": round(elapsed, 2),
                "best_reduction": self.best_reduction,
                "best_ids": list(self.best_ids),
                "baseline_deficit": round(self.model.baseline_totals()["deficit_total"], 3),
                "error": self.error,
            }
            if self.result is not None:
                full = self.result.payload()
                payload.update(
                    {
                        "complete": full["complete"],
                        "rows": full["rows"],
                        "errors": full["errors"],
                        "best_reduction": full["best_reduction"],
                        "best_ids": full["best_ids"],
                        "elapsed": full["elapsed"],
                    }
                )
            else:
                payload["complete"] = False
                payload["errors"] = []
                payload["rows"] = []
            return payload


def auto_jobs() -> dict[str, AutoJob]:
    return current_app.extensions["auto_jobs"]


# -------------------------------------------------------------------- маршруты


@bp.get("/health")
def health():
    school_model = model()
    return jsonify(
        {
            "status": "ok",
            "blocks": int(len(school_model.block_ids)),
            "blocksnet": dataset().meta.get("blocksnet_version"),
        }
    )


@bp.get("/bootstrap")
def bootstrap():
    school_model = model()
    meta = dict(dataset().meta)
    meta["verification"] = current_app.extensions["verification"]
    meta["baseline"] = school_model.baseline_totals()
    return jsonify(
        {
            "meta": meta,
            "baseline": school_model.baseline_payload(),
        }
    )


@bp.get("/geometry")
def geometry():
    path = dataset().blocks_geojson
    return send_file(path, mimetype="application/geo+json", conditional=True, max_age=3600)


@bp.post("/scenario")
def scenario():
    try:
        body = request_json()
        schools = parse_schools(body)
    except ValueError as error:
        return api_error(str(error))

    try:
        result = model().evaluate(schools)
    except CalculationError as error:
        return api_error(f"Ошибка расчёта: {error}", 500)

    return jsonify(result.payload())


@bp.post("/auto")
def auto_start():
    try:
        body = request_json()
        capacity = parse_capacity(body.get("capacity"), "Вместимость новой школы")
    except ValueError as error:
        return api_error(str(error))

    jobs = auto_jobs()
    lock = current_app.extensions["auto_lock"]
    with lock:
        for job in jobs.values():
            if job.state == "running":
                return api_error(
                    "Перебор кварталов уже выполняется. Дождитесь завершения.",
                    409,
                    job_id=job.id,
                )

        job = AutoJob(model(), capacity)
        jobs[job.id] = job
        # держим не больше 10 задач, чтобы не копить память
        if len(jobs) > 10:
            for key in list(jobs)[:-10]:
                jobs.pop(key, None)
        job.start()
    return jsonify({"job_id": job.id, "total": job.total, "capacity": capacity}), 202


@bp.get("/auto/<job_id>")
def auto_status(job_id: str):
    job = auto_jobs().get(job_id)
    if job is None:
        return api_error("Задача автоматического подбора не найдена.", 404)
    return jsonify(job.status())


def _as_float(value):
    """Число для JSON или None, если значение пустое/нечисловое."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _local_effect(result) -> dict:
    """Показатели в кварталах, куда добавляются школы.

    Общие итоги по району у вариантов с одинаковой вместимостью часто совпадают.
    Локальные показатели показывают, где именно меняются дефицит и обеспеченность.
    """
    blocks = []
    for school in result.schools:
        block_id = int(school["block_id"])
        row = result.rows.loc[block_id]
        deficit_before = _as_float(row["deficit_base"]) or 0.0
        deficit_after = _as_float(row["deficit"]) or 0.0
        provision_before = _as_float(row["provision_base"])
        provision_after = _as_float(row["provision"])
        blocks.append(
            {
                "block_id": block_id,
                "population": _as_float(row["population"]) or 0.0,
                "demand": _as_float(row["demand"]) or 0.0,
                "capacity_base": int(row["capacity_base"]),
                "capacity_after": int(row["capacity"]),
                "capacity_added": int(row["capacity_added"]),
                "deficit_before": deficit_before,
                "deficit_after": deficit_after,
                "deficit_reduction": deficit_before - deficit_after,
                "provision_before": None if provision_before is None else provision_before * 100.0,
                "provision_after": None if provision_after is None else provision_after * 100.0,
                "provision_delta_pp": (
                    None
                    if provision_before is None or provision_after is None
                    else (provision_after - provision_before) * 100.0
                ),
            }
        )

    demand_total = sum(block["demand"] for block in blocks)
    weighted_before = sum(
        block["provision_before"] * block["demand"]
        for block in blocks
        if block["provision_before"] is not None
    )
    weighted_after = sum(
        block["provision_after"] * block["demand"]
        for block in blocks
        if block["provision_after"] is not None
    )
    provision_before = weighted_before / demand_total if demand_total > 0 else None
    provision_after = weighted_after / demand_total if demand_total > 0 else None

    affected_blocks = 0
    affected_population = 0.0
    max_provision_delta = 0.0
    max_deficit_reduction = 0.0
    for _, row in result.rows.iterrows():
        deficit_delta = _as_float(row["deficit_delta"]) or 0.0
        provision_delta = _as_float(row["provision_delta_pp"]) or 0.0
        if deficit_delta < -1e-6 or provision_delta > 1e-6:
            affected_blocks += 1
            affected_population += _as_float(row["population"]) or 0.0
        max_provision_delta = max(max_provision_delta, provision_delta)
        max_deficit_reduction = max(max_deficit_reduction, -deficit_delta)

    return {
        "blocks": blocks,
        "deficit_before": sum(block["deficit_before"] for block in blocks),
        "deficit_after": sum(block["deficit_after"] for block in blocks),
        "deficit_reduction": sum(block["deficit_reduction"] for block in blocks),
        "provision_before": provision_before,
        "provision_after": provision_after,
        "provision_delta_pp": (
            None if provision_before is None or provision_after is None
            else provision_after - provision_before
        ),
        "population": sum(block["population"] for block in blocks),
        "demand": demand_total,
        "affected_blocks": affected_blocks,
        "affected_population": affected_population,
        "max_provision_delta_pp": max_provision_delta,
        "max_deficit_reduction": max_deficit_reduction,
    }


@bp.post("/compare")
def compare():
    try:
        body = request_json()
        scenarios = body.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("Передайте список сценариев для сравнения.")
        if len(scenarios) > MAX_COMPARE_SCENARIOS:
            raise ValueError(f"За один раз можно сравнить не более {MAX_COMPARE_SCENARIOS} сценариев.")
    except ValueError as error:
        return api_error(str(error))

    rows = []
    for index, scenario_item in enumerate(scenarios, start=1):
        name = str(scenario_item.get("name") or f"Сценарий {index}").strip()[:120]
        try:
            schools = parse_schools(scenario_item if isinstance(scenario_item, dict) else {})
            result = model().evaluate(schools)
            row = {
                "name": name,
                "capacity_added": result.totals["capacity_added"],
                "schools_count": result.totals["schools_count"],
                "schools": result.schools,
                "deficit_total": result.totals["deficit_total"],
                "deficit_reduction": result.totals["deficit_reduction"],
                "provision_total": result.totals["provision_total"],
                "capacity_total": result.totals["capacity_total"],
                "local": _local_effect(result),
                "error": None,
            }
        except (ValueError, CalculationError) as error:
            row = {
                "name": name,
                "capacity_added": None,
                "schools_count": None,
                "schools": [],
                "deficit_total": None,
                "deficit_reduction": None,
                "provision_total": None,
                "capacity_total": None,
                "local": None,
                "error": str(error),
            }
        rows.append(row)

    return jsonify({"rows": rows, "baseline": model().baseline_totals()})


def _format(value, digits: int = 1) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:  # NaN
        return ""
    text = f"{number:.{digits}f}"
    return text.replace(".", ",")


def build_csv(result) -> str:
    rows = result.rows.reset_index()
    header = [
        "Квартал",
        "Население, чел.",
        "Потребность, мест",
        "Вместимость базовая, мест",
        "Вместимость сценария, мест",
        "Прирост вместимости, мест",
        "Обеспечено в нормативе, мест",
        "Дефицит базовая, мест",
        "Дефицит сценарий, мест",
        "Сокращение дефицита, мест",
        "Обеспеченность базовая, %",
        "Обеспеченность сценарий, %",
        "Изменение обеспеченности, п.п.",
    ]
    lines = [";".join(header)]
    for _, row in rows.iterrows():
        population = row["population"]
        provision_base = row["provision_base"]
        provision = row["provision"]
        lines.append(
            ";".join(
                [
                    str(int(row["block_id"])),
                    _format(population, 0),
                    str(int(row["demand"])),
                    str(int(row["capacity_base"])),
                    str(int(row["capacity"])),
                    str(int(row["capacity_added"])),
                    _format(row["within"], 1),
                    _format(row["deficit_base"], 1),
                    _format(row["deficit"], 1),
                    _format(row["deficit_base"] - row["deficit"], 1),
                    _format(None if provision_base != provision_base else provision_base * 100, 1),
                    _format(None if provision != provision else provision * 100, 1),
                    _format(row["provision_delta_pp"], 1),
                ]
            )
        )

    totals = result.totals
    lines.append(
        ";".join(
            [
                "ИТОГО",
                _format(totals["population_total"], 0),
                _format(totals["demand_total"], 0),
                str(int(totals["capacity_total"] - totals["capacity_added"])),
                str(int(totals["capacity_total"])),
                str(int(totals["capacity_added"])),
                _format(totals["within_total"], 1),
                _format(totals["deficit_total"] + totals["deficit_reduction"], 1),
                _format(totals["deficit_total"], 1),
                _format(totals["deficit_reduction"], 1),
                _format(totals["provision_total"] * 100 - totals["provision_delta_pp"], 1),
                _format(totals["provision_total"] * 100, 1),
                _format(totals["provision_delta_pp"], 1),
            ]
        )
    )
    return "\r\n".join(lines)


@bp.post("/export")
def export_csv():
    try:
        body = request_json()
        schools = parse_schools(body)
    except ValueError as error:
        return api_error(str(error))

    try:
        result = model().evaluate(schools)
    except CalculationError as error:
        return api_error(f"Ошибка расчёта: {error}", 500)

    content = "\ufeff" + build_csv(result)
    filename = f"school_scenario_{datetime.now():%Y-%m-%d_%H%M}.csv"
    return Response(
        content,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
