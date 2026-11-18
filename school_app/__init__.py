"""Приложение «Оценка вариантов размещения школ»."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, render_template
from loguru import logger

from .data import DataNotReady, load_dataset
from .model import SchoolModel

PROJECT_DIR = Path(__file__).resolve().parents[1]


def create_app(data_dir: str | Path | None = None) -> Flask:
    # BlocksNet/PuLP пишут отладочные сообщения в stderr — в веб-приложении они лишние.
    logger.remove()

    app = Flask(
        __name__,
        static_folder=str(PROJECT_DIR / "static"),
        template_folder=str(PROJECT_DIR / "templates"),
    )
    app.config.update(
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        TEMPLATES_AUTO_RELOAD=True,
    )
    app.json.ensure_ascii = False

    dataset = load_dataset(data_dir)
    model = SchoolModel(
        dataset.blocks,
        dataset.matrix,
        accessibility=dataset.accessibility,
        demand_per_1000=dataset.demand_per_1000,
    )

    app.extensions["dataset"] = dataset
    app.extensions["model"] = model
    app.extensions["verification"] = model.verification()
    app.extensions["auto_jobs"] = {}
    app.extensions["auto_lock"] = Lock()

    from .api import bp

    app.register_blueprint(bp)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.errorhandler(DataNotReady)
    def handle_data_error(error: DataNotReady):
        return jsonify({"error": str(error)}), 503

    @app.errorhandler(404)
    def handle_not_found(_error):
        return jsonify({"error": "Страница или ресурс не найдены."}), 404

    @app.after_request
    def add_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    return app
