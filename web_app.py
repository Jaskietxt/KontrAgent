from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, abort, redirect, render_template, request, send_file, session, url_for


ROOT = Path(__file__).resolve().parent
REPORT_SCRIPT = ROOT / "dossier_report.py"
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
COOKIES_FILE = Path(os.environ.get("RUSPROFILE_COOKIES", ROOT / "rusprofile_cookies.json"))
AI_CONFIG_FILE = Path(os.environ.get("AI_CONFIG", ROOT / "ai_config.json"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
REPORT_TIMEOUT_SECONDS = int(os.environ.get("REPORT_TIMEOUT_SECONDS", "1800"))
QUERY_PATTERN = re.compile(r"(?:\d{10}|\d{12}|\d{13}|\d{15})")
RISK_LABELS = {
    "critical": "Критический",
    "high": "Высокий",
    "medium": "Средний",
    "low": "Умеренный",
    "unknown": "Недостаточно данных",
}
RISK_LEVEL_LABELS = {
    "ВЫСОКИЙ РИСК": "Высокий риск",
    "СРЕДНИЙ РИСК": "Средний риск",
    "НИЗКИЙ РИСК": "Низкий риск",
    "НЕДОСТАТОЧНО ДАННЫХ": "Недостаточно данных",
}


class ReportFailure(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


if len(APP_PASSWORD) < 32:
    raise RuntimeError("APP_PASSWORD must contain at least 32 characters")
if REPORT_TIMEOUT_SECONDS <= 0:
    raise RuntimeError("REPORT_TIMEOUT_SECONDS must be a positive integer")

DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
app.secret_key = hmac.digest(APP_PASSWORD.encode("utf-8"), b"KontrAgent session signing", "sha256")
app.config.update(
    MAX_CONTENT_LENGTH=4096,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
)


def report_path(key: str, extension: str) -> Path:
    if not QUERY_PATTERN.fullmatch(key):
        abort(404)
    return DATA_DIR / f"{key}.{extension}"


def load_report(key: str) -> dict:
    path = report_path(key, "json")
    if not path.is_file():
        abort(404)
    try:
        with path.open(encoding="utf-8") as report_file:
            saved = json.load(report_file)
    except (OSError, json.JSONDecodeError):
        app.logger.exception("Unable to read saved report %s", key)
        abort(500)
    if not isinstance(saved, dict) or not isinstance(saved.get("report"), dict):
        app.logger.error("Saved report %s has an invalid format", key)
        abort(500)
    return saved


def recent_reports(limit: int = 10) -> list[dict]:
    reports = []
    for path in DATA_DIR.glob("*.json"):
        if not QUERY_PATTERN.fullmatch(path.stem):
            continue
        try:
            with path.open(encoding="utf-8") as report_file:
                saved = json.load(report_file)
            data = saved["report"]
            company = data["company"]
            analytics = data["analytics"]
            if not isinstance(company, dict) or not isinstance(analytics, dict):
                continue
            reports.append(
                {
                    "key": path.stem,
                    "name": company.get("raw_name") or company.get("name") or path.stem,
                    "inn": company.get("inn") or "",
                    "ogrn": company.get("ogrn") or "",
                    "created_at": saved.get("created_at", ""),
                    "level": _risk_class(analytics.get("level")),
                    "verdict": _risk_verdict(analytics.get("verdict")),
                    "flags_count": len(analytics.get("flags") or []),
                    "coverage": _coverage(analytics),
                }
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            app.logger.warning("Skipping invalid saved report %s", path.name)
    reports.sort(key=lambda item: item["created_at"], reverse=True)
    return reports[:limit]


def _risk_class(level: str | None) -> str:
    return level if level in RISK_LABELS else "unknown"


def _risk_verdict(verdict: str | None) -> str:
    return RISK_LEVEL_LABELS.get(verdict or "", "Недостаточно данных")


def _coverage(analytics: dict) -> int:
    total = analytics.get("present_blocks")
    blocks = analytics.get("data_blocks")
    if not isinstance(total, int) or not isinstance(blocks, dict) or not blocks:
        return 0
    return round(total * 100 / len(blocks))


def _format_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "Дата не указана"
    return parsed.astimezone().strftime("%d.%m.%Y, %H:%M")


def generate_report(query: str) -> None:
    if not COOKIES_FILE.is_file():
        raise ReportFailure("На сервере не найден файл cookies Rusprofile.", 503)
    if not AI_CONFIG_FILE.is_file():
        raise ReportFailure("На сервере не найден файл конфигурации AI.", 503)

    with tempfile.TemporaryDirectory(prefix="kontragent-", dir=DATA_DIR) as temp_dir:
        temp_path = Path(temp_dir)
        pdf_path = temp_path / f"{query}.pdf"
        html_path = temp_path / f"{query}.html"
        json_path = temp_path / f"{query}.json"
        command = [
            sys.executable,
            str(REPORT_SCRIPT),
            query,
            "--out",
            str(pdf_path),
            "--html",
            "--json",
            str(json_path),
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
        except subprocess.TimeoutExpired as exc:
            raise ReportFailure("Формирование отчёта заняло слишком много времени.", 504) from exc

        if result.returncode != 0:
            app.logger.error("Report generator exited with status %s", result.returncode)
            raise ReportFailure("Не удалось сформировать отчёт. Проверьте cookies, настройки и журналы сервера.")
        if not all(path.is_file() for path in (pdf_path, html_path, json_path)):
            app.logger.error("Report generator did not create all expected report files")
            raise ReportFailure("Генератор завершился без полного набора файлов досье.")

        try:
            with json_path.open(encoding="utf-8") as report_file:
                data = json.load(report_file)
        except (OSError, json.JSONDecodeError) as exc:
            app.logger.exception("Generated report JSON is invalid")
            raise ReportFailure("Не удалось прочитать сформированные данные отчёта.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("company"), dict) or not isinstance(data.get("analytics"), dict):
            app.logger.error("Generated report has an invalid data structure")
            raise ReportFailure("Генератор вернул данные в неожиданном формате.")

        saved = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "report": data,
        }
        json_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        for extension, source in (("pdf", pdf_path), ("html", html_path), ("json", json_path)):
            os.replace(source, report_path(query, extension))


@app.template_filter("dt")
def format_datetime(value: str) -> str:
    return _format_date(value)


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
        "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: blob:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.endpoint == "dossier_html":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'self'"
        )
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["X-Frame-Options"] = "DENY"
    return response


def render_error(error: str = "", query: str = "", status: int = 200):
    csrf_token = session.get("csrf_token")
    if csrf_token is None:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token
    return (
        render_template(
            "index.html",
            error=error,
            query=query,
            csrf_token=csrf_token,
            recent=recent_reports(),
        ),
        status,
    )


def validate_csrf() -> bool:
    expected = session.get("csrf_token", "")
    submitted = request.form.get("csrf_token", "")
    return bool(expected) and hmac.compare_digest(expected, submitted)


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if not validate_csrf():
            return Response("Недействительная форма. Обновите страницу.", 403)
        query = request.form.get("query", "").strip()
        if not QUERY_PATTERN.fullmatch(query):
            return render_error("Введите ИНН или ОГРН: 10, 12, 13 или 15 цифр.", query, 400)
        try:
            generate_report(query)
        except ReportFailure as exc:
            app.logger.warning("Report request failed for %s: %s", query, exc)
            return render_error(str(exc), query, exc.status)
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("company", key=query), code=303)

    csrf_token = session.get("csrf_token")
    if csrf_token is None:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token
    return render_template(
        "index.html",
        error="",
        query="",
        csrf_token=csrf_token,
        recent=recent_reports(),
    )


@app.route("/company/<key>", methods=["GET"])
def company(key: str):
    saved = load_report(key)
    data = saved["report"]
    analytics = data["analytics"]
    company_data = data["company"]
    blocks = [
        {"name": name, "found": bool(found)}
        for name, found in (analytics.get("data_blocks") or {}).items()
    ]
    csrf_token = session.get("csrf_token")
    if csrf_token is None:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token
    return render_template(
        "company.html",
        key=key,
        c=company_data,
        a=analytics,
        ai=data.get("ai") or {},
        ai_flags=(data.get("ai") or {}).get("flags") or {},
        created_at=saved.get("created_at", ""),
        level=_risk_class(analytics.get("level")),
        verdict=_risk_verdict(analytics.get("verdict")),
        coverage=_coverage(analytics),
        blocks=blocks,
        recent=recent_reports(),
        csrf_token=csrf_token,
    )


@app.route("/company/<key>/refresh", methods=["POST"])
def refresh_company(key: str):
    if not validate_csrf():
        return Response("Недействительная форма. Обновите страницу.", 403)
    load_report(key)
    try:
        generate_report(key)
    except ReportFailure as exc:
        app.logger.warning("Report refresh failed for %s: %s", key, exc)
        return render_error(str(exc), key, exc.status)
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("company", key=key), code=303)


@app.route("/company/<key>/report.pdf", methods=["GET"])
def report_pdf(key: str):
    path = report_path(key, "pdf")
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=f"dossier_{key}.pdf")


@app.route("/company/<key>/dossier.html", methods=["GET"])
def dossier_html(key: str):
    path = report_path(key, "html")
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="text/html; charset=utf-8")
