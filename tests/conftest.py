"""Общие фикстуры тестов."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from school_app.data import DataNotReady, load_dataset  # noqa: E402
from school_app.model import SchoolModel  # noqa: E402


@pytest.fixture(scope="session")
def dataset():
    try:
        return load_dataset(ROOT / "data")
    except DataNotReady as error:
        pytest.exit(str(error), returncode=1)


@pytest.fixture(scope="session")
def model(dataset):
    return SchoolModel(
        dataset.blocks,
        dataset.matrix,
        accessibility=dataset.accessibility,
        demand_per_1000=dataset.demand_per_1000,
    )


@pytest.fixture(scope="session")
def app(model, dataset):
    from school_app import create_app

    application = create_app(ROOT / "data")
    # переиспользуем уже построенную модель, чтобы не считать baseline дважды
    application.extensions["model"] = model
    application.extensions["verification"] = model.verification()
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()
