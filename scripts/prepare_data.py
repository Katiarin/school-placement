"""Подготовка данных приложения из результатов практики 2.

Скрипт только читает исходный проект (папку ``frunzenskiy-ai``) и складывает
подготовленные данные в ``school-placement/data``. Повторный запуск приложения
уже не обращается к исходному проекту и не скачивает данные города.

Запуск:
    .venv\\Scripts\\python.exe scripts\\prepare_data.py
    .venv\\Scripts\\python.exe scripts\\prepare_data.py --source "C:\\путь\\к\\frunzenskiy-ai"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

SCHOOL_SERVICE = "school"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> None:
    print("\nОШИБКА: " + message, file=sys.stderr)
    sys.exit(1)


def check_source(source: Path) -> dict[str, Path]:
    files = {
        "indicators_csv": source / "outputs" / "frunzensky_indicators.csv",
        "blocks_geojson": source / "outputs" / "frunzensky_blocks.geojson",
        "matrix_parquet": source / "outputs" / "accessibility_matrix_drive.parquet",
        "matrix_csv": source / "outputs" / "accessibility_matrix_drive.csv",
    }
    missing = [str(path) for key, path in files.items() if key != "matrix_csv" and not path.exists()]
    if missing:
        fail(
            "в исходном проекте не найдены обязательные файлы:\n  "
            + "\n  ".join(missing)
            + "\n\nОни создаются ноутбуком examples/frunzensky_blocksnet.ipynb "
            "(разделы 3, 4, 8). Если файлов нет, выполните ноутбук или укажите "
            "другую папку через --source."
        )
    return files


def load_inputs(files: dict[str, Path]):
    import geopandas as gpd

    indicators = pd.read_csv(files["indicators_csv"])
    if "block_id" not in indicators.columns:
        fail("в frunzensky_indicators.csv нет колонки block_id")
    indicators["block_id"] = indicators["block_id"].astype("int64")
    if indicators["block_id"].duplicated().any():
        fail("в frunzensky_indicators.csv повторяются block_id")

    for column in ("population", "capacity_school"):
        if column not in indicators.columns:
            fail(f"в frunzensky_indicators.csv нет обязательной колонки {column}")
    if "provision_school" not in indicators.columns:
        fail("в frunzensky_indicators.csv нет колонки provision_school — не с чем сверять расчёт")

    blocks = gpd.read_file(files["blocks_geojson"])
    matrix = pd.read_parquet(files["matrix_parquet"]).astype("float32")
    return indicators, blocks, matrix


def validate_ids(indicators: pd.DataFrame, blocks, matrix: pd.DataFrame) -> None:
    ids = indicators["block_id"].to_numpy()
    if len(blocks) != len(indicators):
        fail(
            f"число кварталов не совпадает: в indicators {len(indicators)}, "
            f"в blocks.geojson {len(blocks)}"
        )

    block_ids = matrix.index.to_numpy()
    if not np.array_equal(matrix.index.to_numpy(), matrix.columns.to_numpy()):
        fail("матрица времени в пути не квадратная: index и columns различаются")
    if not set(ids.tolist()).issubset(set(block_ids.tolist())):
        missing = sorted(set(ids.tolist()) - set(block_ids.tolist()))[:10]
        fail(f"идентификаторы кварталов отсутствуют в матрице времени в пути: {missing}")

    # Строки geojson и CSV должны идти в одном порядке (geojson записан из того же слоя).
    for column in ("population", "capacity_school"):
        left = indicators[column].fillna(0).to_numpy(dtype=float)
        right = blocks[column].fillna(0).to_numpy(dtype=float) if column in blocks.columns else None
        if right is None:
            fail(f"в blocks.geojson нет колонки {column} — не удаётся проверить порядок кварталов")
        if not np.allclose(left, right, atol=1e-6):
            mismatch = int((np.abs(left - right) > 1e-6).sum())
            fail(
                "порядок кварталов в frunzensky_indicators.csv и frunzensky_blocks.geojson "
                f"не совпадает ({mismatch} расхождений по колонке {column}). "
                "Пересоберите outputs ноутбуком практики 2."
            )


def geometry_payload(blocks, ids) -> dict:
    """Упрощённая геометрия для карты: точность 0.1 м, упрощение 0.5 м, WGS84."""
    import shapely

    blocks = blocks.copy()

    def _simplify(geom):
        simplified = shapely.set_precision(geom.simplify(0.5, preserve_topology=True), 0.1)
        if simplified is None or simplified.is_empty:
            # В исходных данных есть кварталы нулевой площади (линейные «слайсы»):
            # упрощение их удаляет, поэтому оставляем исходную геометрию.
            return geom
        return simplified

    blocks["geometry"] = blocks.geometry.apply(_simplify)
    # Leaflet работает в WGS84; исходный слой практики 2 — в метрах EPSG:32636.
    blocks = blocks.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = blocks.total_bounds
    if not (-180 <= minx <= 180 and -90 <= miny <= 90 and -180 <= maxx <= 180 and -90 <= maxy <= 90):
        fail("геометрия кварталов не в WGS84: координаты выходят за допустимые значения широты/долготы")

    features = []
    empty = []
    for block_id, geom in zip(ids, blocks.geometry):
        if geom is None or geom.is_empty:
            empty.append(int(block_id))
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {"block_id": int(block_id)},
                "geometry": shapely.geometry.mapping(geom),
            }
        )
    if empty:
        fail("у кварталов нет геометрии: " + ", ".join(map(str, empty[:10])))
    outline = shapely.union_all(blocks.geometry.to_numpy())
    return {
        "type": "FeatureCollection",
        "features": features,
        "outline": shapely.geometry.mapping(outline),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Подготовка данных приложения из практики 2")
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_DIR.parent / "frunzenskiy-ai",
        help="папка исходного проекта практики 2 (по умолчанию ../frunzenskiy-ai)",
    )
    parser.add_argument("--out", type=Path, default=PROJECT_DIR / "data", help="папка для подготовленных данных")
    arguments = parser.parse_args()

    source = arguments.source.resolve()
    out_dir = arguments.out.resolve()
    print(f"Исходный проект: {source}")
    print(f"Папка данных:    {out_dir}")

    if not source.exists():
        fail(f"папка исходного проекта не найдена: {source}")

    files = check_source(source)
    started = time.time()
    indicators, blocks, matrix = load_inputs(files)
    validate_ids(indicators, blocks, matrix)

    from blocksnet.config import service_types_config

    import blocksnet

    service = service_types_config[SCHOOL_SERVICE]
    accessibility = int(service["accessibility"])
    demand_per_1000 = int(service["demand"])

    ids = indicators["block_id"].to_numpy(dtype="int64")
    source_matrix_size = int(matrix.shape[0])
    ids_in_index = bool(indicators["block_id"].isin(matrix.index).all())
    ids_in_columns = bool(indicators["block_id"].isin(matrix.columns).all())
    matrix = matrix.loc[ids, ids]

    population = indicators["population"].fillna(0).astype(float).to_numpy()
    capacity = indicators["capacity_school"].fillna(0).astype(float).round().astype("int64").to_numpy()

    # --- Воспроизведение исходного расчёта ---
    from school_app.model import SchoolModel

    blocks_df = pd.DataFrame(
        {
            "population": population,
            "capacity_school": capacity,
            "provision_saved": indicators["provision_school"].to_numpy(dtype=float),
            "provision_weak_saved": indicators["provision_weak_school"].to_numpy(dtype=float),
        },
        index=pd.Index(ids, name="block_id"),
    )
    model = SchoolModel(
        blocks_df,
        matrix,
        accessibility=accessibility,
        demand_per_1000=demand_per_1000,
    )
    verification = model.verification()
    if not verification["ok"]:
        fail(
            "расчёт не воспроизводит сохранённые значения provision_school: "
            f"максимальное отклонение {verification['max_abs_diff']:.3e}. "
            "Проверьте версию BlocksNet (нужна 1.0.0a9) и целостность outputs практики 2."
        )

    # --- Геометрия ---
    geometry = geometry_payload(blocks, ids)

    # --- Запись ---
    out_dir.mkdir(parents=True, exist_ok=True)
    blocks_csv = out_dir / "blocks.csv"
    blocks_df.reset_index().to_csv(blocks_csv, index=False, encoding="utf-8")

    geojson_path = out_dir / "blocks.geojson"
    with open(geojson_path, "w", encoding="utf-8", newline="\n") as file:
        json.dump(geometry, file, ensure_ascii=False, separators=(",", ":"))

    matrix_path = out_dir / "accessibility_matrix.npz"
    np.savez_compressed(matrix_path, ids=ids, matrix=matrix.to_numpy(dtype="float32"))

    totals = model.baseline_totals()
    meta = {
        "app_schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_dir": str(source),
        "blocksnet_version": getattr(blocksnet, "__version__", "unknown"),
        "params": {
            "service": SCHOOL_SERVICE,
            "accessibility_min": accessibility,
            "demand_per_1000": demand_per_1000,
            "self_supply": True,
            "max_depth": 1,
            "travel_mode": "car",
        },
        "counts": {
            "blocks": int(len(ids)),
            "context_blocks_in_source_matrix": source_matrix_size,
            "blocks_with_population": int((population > 0).sum()),
            "blocks_with_school_capacity": int((capacity > 0).sum()),
        },
        "matrix_check": {
            "district_blocks": int(len(ids)),
            "source_matrix_size": source_matrix_size,
            "all_ids_in_index": ids_in_index,
            "all_ids_in_columns": ids_in_columns,
        },
        "baseline": totals,
        "verification": {
            **verification,
            "compared_blocks": int(np.isfinite(blocks_df["provision_saved"].to_numpy()).sum()),
            "stored_results_file": "outputs/frunzensky_indicators.csv",
        },
        "inputs": {
            "frunzensky_indicators.csv": {
                "sha256": sha256(files["indicators_csv"]),
                "size": files["indicators_csv"].stat().st_size,
            },
            "frunzensky_blocks.geojson": {
                "sha256": sha256(files["blocks_geojson"]),
                "size": files["blocks_geojson"].stat().st_size,
            },
            "accessibility_matrix_drive.parquet": {
                "sha256": sha256(files["matrix_parquet"]),
                "size": files["matrix_parquet"].stat().st_size,
            },
        },
    }
    meta_path = out_dir / "meta.json"
    with open(meta_path, "w", encoding="utf-8", newline="\n") as file:
        json.dump(meta, file, ensure_ascii=False, indent=2)

    print("\nГотово за %.1f с" % (time.time() - started))
    print(f"  кварталов:            {len(ids)}")
    print(f"  население, чел.:      {totals['population_total']:.0f}")
    print(f"  вместимость школ:     {totals['capacity_total']:.0f} мест")
    print(f"  потребность:          {totals['demand_total']:.0f} мест")
    print(f"  дефицит:              {totals['deficit_total']:.1f} мест")
    print(f"  обеспеченность:       {totals['provision_total'] * 100:.1f}%")
    print(f"  сверка с практикой 2: макс. отклонение {verification['max_abs_diff']:.2e} — OK")
    for path in (blocks_csv, geojson_path, matrix_path, meta_path):
        print(f"  {path.name:26s} {path.stat().st_size / 1e6:8.2f} МБ")


if __name__ == "__main__":
    main()
