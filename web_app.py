from __future__ import annotations

import hmac
import io
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, Response, render_template_string, request, send_file, session


ROOT = Path(__file__).resolve().parent
REPORT_SCRIPT = ROOT / "dossier_report.py"
COOKIES_FILE = Path(os.environ.get("RUSPROFILE_COOKIES", ROOT / "rusprofile_cookies.json"))
AI_CONFIG_FILE = Path(os.environ.get("AI_CONFIG", ROOT / "ai_config.json"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
REPORT_TIMEOUT_SECONDS = int(os.environ.get("REPORT_TIMEOUT_SECONDS", "1800"))

if len(APP_PASSWORD) < 32:
    raise RuntimeError("APP_PASSWORD must contain at least 32 characters")
if REPORT_TIMEOUT_SECONDS <= 0:
    raise RuntimeError("REPORT_TIMEOUT_SECONDS must be a positive integer")

app = Flask(__name__)
app.secret_key = hmac.digest(APP_PASSWORD.encode("utf-8"), b"KontrAgent session signing", "sha256")
app.config.update(
    MAX_CONTENT_LENGTH=4096,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
)

PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KontrAgent — отчёт</title>
  <style>
    body { max-width: 42rem; margin: 3rem auto; padding: 0 1rem; font: 16px/1.5 system-ui, sans-serif; color: #18212f; }
    label, input, button { display: block; width: 100%; box-sizing: border-box; }
    input, button { margin-top: .5rem; padding: .75rem; font: inherit; }
    button { margin-top: 1rem; cursor: pointer; }
    .error { padding: .75rem; border: 1px solid #b42318; color: #b42318; }
    .note { color: #526071; font-size: .9rem; }
  </style>
</head>
<body>
  <h1>Генератор досье</h1>
  <p>Введите ИНН или ОГРН организации, чтобы получить PDF-отчёт.</p>
  {% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
  <form method="post">
    <label for="query">ИНН или ОГРН</label>
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <input id="query" name="query" inputmode="numeric" autocomplete="off"
           maxlength="15" pattern="(?:[0-9]{10}|[0-9]{12}|[0-9]{13}|[0-9]{15})"
           value="{{ query }}" required>
    <button type="submit">Сформировать PDF</button>
  </form>
  <p class="note">Отчёт формируется на сервере и не сохраняется после скачивания.
  Если AI включён в конфигурации, данные отчёта могут передаваться указанному
  AI-провайдеру.</p>
</body>
</html>"""


def render_page(error: str = "", query: str = "", status: int = 200) -> tuple[str, int]:
    csrf_token = session.get("csrf_token")
    if csrf_token is None:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token
    return render_template_string(PAGE, error=error, query=query, csrf_token=csrf_token), status


@app.before_request
def require_basic_auth() -> Response | None:
    auth = request.authorization
    username_ok = hmac.compare_digest(auth.username if auth and auth.username else "", "kontragent")
    password_ok = hmac.compare_digest(auth.password if auth and auth.password else "", APP_PASSWORD)
    if not (auth and auth.type.lower() == "basic" and username_ok and password_ok):
        return Response(
            "Требуется авторизация.",
            401,
            {"WWW-Authenticate": 'Basic realm="KontrAgent", charset="UTF-8"'},
        )
    return None


@app.after_request
def add_security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_page()

    expected_token = session.get("csrf_token", "")
    submitted_token = request.form.get("csrf_token", "")
    if not expected_token or not hmac.compare_digest(expected_token, submitted_token):
        return Response("Недействительная форма. Обновите страницу.", 403)

    query = request.form.get("query", "").strip()
    if not re.fullmatch(r"(?:\d{10}|\d{12}|\d{13}|\d{15})", query):
        return render_page("Введите корректный ИНН или ОГРН (10, 12, 13 или 15 цифр).", query, 400)
    if not COOKIES_FILE.is_file():
        return render_page("На сервере не найден файл cookies Rusprofile.", query, 503)
    if not AI_CONFIG_FILE.is_file():
        return render_page("На сервере не найден файл конфигурации AI.", query, 503)

    with tempfile.TemporaryDirectory(prefix="kontragent-") as temp_dir:
        pdf_path = Path(temp_dir) / f"dossier_{query}.pdf"
        command = [
            sys.executable,
            str(REPORT_SCRIPT),
            query,
            "--out",
            str(pdf_path),
            "--cookies",
            str(COOKIES_FILE),
            "--ai-config",
            str(AI_CONFIG_FILE),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=REPORT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            app.logger.error("Report generation exceeded the configured timeout")
            return render_page("Формирование отчёта заняло слишком много времени.", query, 504)

        if result.returncode != 0:
            app.logger.error("Report generator exited with status %s", result.returncode)
            return render_page(
                "Не удалось сформировать отчёт. Проверьте cookies, настройки и журналы сервера.",
                query,
                502,
            )
        if not pdf_path.is_file():
            app.logger.error("Report generator completed without creating a PDF")
            return render_page("Генератор завершился без PDF-файла.", query, 502)

        pdf = io.BytesIO(pdf_path.read_bytes())
        session["csrf_token"] = secrets.token_urlsafe(32)

    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"dossier_{query}.pdf",
    )
