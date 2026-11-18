"""Загрузка подготовленных данных приложения."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_DIR / "data"


class DataNotReady(Exception):
    """Подготовленные данные отсутствуют или повреждены."""


@dataclass
class Dataset:
    data_dir: Path
    blocks: pd.DataFrame
    matrix: pd.DataFrame
    meta: dict

    @property
    def accessibility(self) -> int:
        return int(self.meta["params"]["accessibility_min"])

    @property
    def demand_per_1000(self) -> int:
        return int(self.meta["params"]["demand_per_1000"])

    @property
    def blocks_geojson(self) -> Path:
        return self.data_dir / "blocks.geojson"


def load_dataset(data_dir: str | Path | None = None) -> Dataset:
    directory = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    required = ["blocks.csv", "blocks.geojson", "accessibility_matrix.npz", "meta.json"]
    missing = [name for name in required if not (directory / name).exists()]
    if missing:
        raise DataNotReady(
            "не найдены подготовленные данные: "
            + ", ".join(missing)
            + f" (папка {directory}).\n"
            "Выполните один раз:\n"
            "  .venv\\Scripts\\python.exe scripts\\prepare_data.py\n"
            "Скрипт прочитает результаты практики 2 и создаст папку data. "
            "Повторный запуск приложения данные города не скачивает."
        )

    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    blocks = pd.read_csv(directory / "blocks.csv")
    if "block_id" not in blocks.columns:
        raise DataNotReady("в data/blocks.csv нет колонки block_id")
    blocks["block_id"] = blocks["block_id"].astype("int64")
    blocks = blocks.set_index("block_id")
    if not blocks.index.is_unique:
        raise DataNotReady("в data/blocks.csv повторяются block_id")

    archive = np.load(directory / "accessibility_matrix.npz")
    ids = archive["ids"].astype("int64")
    values = archive["matrix"].astype("float32")
    if values.shape != (len(ids), len(ids)):
        raise DataNotReady("матрица времени в пути повреждена (не квадратная)")
    matrix = pd.DataFrame(values, index=ids, columns=ids)

    if list(blocks.index) != list(ids):
        raise DataNotReady("порядок кварталов в blocks.csv не совпадает с матрицей времени в пути")
    if not set(meta.get("params", {})) >= {"accessibility_min", "demand_per_1000"}:
        raise DataNotReady("в data/meta.json нет параметров расчёта")

    return Dataset(data_dir=directory, blocks=blocks, matrix=matrix, meta=meta)
