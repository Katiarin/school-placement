"""Запуск приложения «Оценка вариантов размещения школ».

    .venv\\Scripts\\python.exe app.py

Приложение откроется в браузере: http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from school_app import create_app  # noqa: E402
from school_app.data import DataNotReady  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Оценка вариантов размещения школ")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", type=Path, default=None, help="папка подготовленных данных (по умолчанию data)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    arguments = parser.parse_args()

    try:
        app = create_app(arguments.data)
    except DataNotReady as error:
        print("\nПриложение не может запуститься.\n", file=sys.stderr)
        print(error, file=sys.stderr)
        return 1

    verification = app.extensions["verification"]
    model = app.extensions["model"]
    totals = model.baseline_totals()
    print("=" * 72)
    print("Оценка вариантов размещения школ — Фрунзенский район")
    print("=" * 72)
    print(f"Кварталов: {len(model.block_ids)}   BlocksNet: {app.extensions['dataset'].meta.get('blocksnet_version')}")
    print(
        "Исходное состояние: население {:.0f} чел., вместимость {:.0f} мест, "
        "дефицит {:.0f} мест, обеспеченность {:.1f}%".format(
            totals["population_total"],
            totals["capacity_total"],
            totals["deficit_total"],
            totals["provision_total"] * 100,
        )
    )
    if verification.get("ok"):
        print(f"Сверка с практикой 2: OK (макс. отклонение {verification['max_abs_diff']:.2e})")
    else:
        print("ВНИМАНИЕ: расчёт не совпал с сохранёнными результатами практики 2!", file=sys.stderr)
        print(f"  {verification}", file=sys.stderr)

    url = f"http://{arguments.host}:{arguments.port}/"
    print(f"\nОткройте в браузере: {url}")
    print("Остановка: Ctrl+C\n")

    if not arguments.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=arguments.host, port=arguments.port, debug=False, threaded=True, use_reloader=False)
    except OSError as error:
        print(f"\nНе удалось запустить сервер: {error}", file=sys.stderr)
        print(f"Возможно, порт {arguments.port} занят. Запустите: python app.py --port 8010", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
