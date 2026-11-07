#!/usr/bin/env python3
"""
Генератор PDF-досье (Due Diligence) по данным rusprofile.ru.

Usage:
    python3 dossier_report.py 7715635724                       # -> dossier_7715635724.pdf
    python3 dossier_report.py 7715635724 -o report.pdf
    python3 dossier_report.py 7715635724 -o report.html --html
    python3 dossier_report.py 1307077527 --json dump.json
    python3 dossier_report.py 7715635724 --acc rusprofile_acc.txt   # перелогиниться и обновить cookies

Скрипт самодостаточен (скрапер rusprofile встроен ниже, отдельный файл не нужен):
сам делает поиск по ИНН/ОГРН, извлекает аналитические показатели со страниц
и строит PDF через WeasyPrint в стиле шаблона company_dossier_oasis_print.pdf.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from typing import Any

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Требуется: pip install beautifulsoup4")

try:
    from weasyprint import HTML, CSS
except ImportError:
    sys.exit("Требуется: pip install weasyprint")

# ---- встроенный модуль скрапера (был rusprofile_scraper.py) ----

import argparse
import ast
import html as html_mod
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    sys.exit("Install dependencies:  pip install requests beautifulsoup4")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Install dependencies:  pip install requests beautifulsoup4")


BASE = "https://www.rusprofile.ru"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COOKIES = os.path.join(SCRIPT_DIR, "rusprofile_cookies.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

TABS = [
    ("Обзор", "overview"),
    ("Финансы", "finance"),
    ("Налоги", "taxes"),
    ("Арбитраж", "arbitr"),
    ("ФССП", "fssp"),
    ("Банкротство", "bankruptcy"),
    ("Закупки", "gz"),
    ("Связи", "connections"),
    ("Репутация", "reliability"),
    ("Источники", "sources"),
]

EXTRA_TABS = [
    ("Учредители", "founders"),
    ("Реквизиты", "requisites"),
    ("Виды деятельности", "okved"),
    ("История", "history"),
    ("Санкции", "sanctions"),
    ("Проверки", "inspections"),
    ("Лицензии", "licenses"),
    ("Суды общей юрисдикции", "sou"),
    ("Бухгалтерия (отчётность)", "accounting"),
    ("Выписка ЕГРЮЛ", "egrul"),
    ("Конкуренты", "competitors"),
]

RPF_KEYS = {
    "overview": ["ai_description", "dashboard_name", "store.check_counterparty"],
    "finance": [],
    "taxes": ["years"],
    "arbitr": [],
    "fssp": [],
    "facts": [],
    "gz": ["gz_summary"],
    "connections": [],
    "reliability": [],
    "accounting": ["years", "years_pdf"],
    "sources": [],
}

MASK_CHARS = "░"


def clean(node: Any) -> str:
    if node is None:
        return ""
    text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def masked_ratio(text: str) -> float:
    chars = [c for c in (text or "") if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if c == MASK_CHARS) / len(chars)


def collapse_masked(text: str) -> str:
    text = re.sub(r"[\s\u00a0]*[" + MASK_CHARS + r"][\s\u00a0" + MASK_CHARS + r"]*", " [скрыто] ", text or "")
    return re.sub(r"\s{2,}", " ", text).strip()


def _balance_json(html: str, start: int) -> str | None:
    while start < len(html) and html[start] in " \t\r\n":
        start += 1
    if start >= len(html) or html[start] not in "{[":
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
    return None


def extract_rpf(html: str, key: str) -> Any:
    pattern = r"RPF\." + re.escape(key) + r"\s*=\s*"
    for m in re.finditer(pattern, html):
        raw = _balance_json(html, m.end())
        if raw is None:
            tail = html[m.end() : m.end() + 400]
            val = re.match(r"[^;\n]+", tail)
            return val.group(0).strip() if val else None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return {"_raw": raw[:2000]}
    return None


def load_cookies(path: str, session: requests.Session) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if isinstance(data, dict):
        session.cookies.update(data)
        return True
    if isinstance(data, list):
        for c in data:
            if not isinstance(c, dict) or "name" not in c:
                continue
            session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
        return bool(data)
    return False


def save_cookies(path: str, cookies) -> None:
    data = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in cookies
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


def read_credentials(path: str) -> tuple[str, str]:
    login = passw = ""
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key in ("login", "email", "user"):
            login = value
        elif key in ("pass", "password", "pwd"):
            passw = value
    if not login or not passw:
        raise RuntimeError(f"Cannot parse credentials from {path!r} (use login:/pass:)")
    return login, passw


def selenium_login(acc_file: str, cookies_path: str) -> bool:
    """Log in using a headless browser (Selenium + chromedriver) and store cookies."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        sys.exit("Auto-login requires:  pip install selenium  (and chromedriver in PATH)")

    login, passw = read_credentials(acc_file)

    opts = Options()
    opts.binary_location = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
    opts.add_argument(f"--user-agent={USER_AGENT}")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    service = None
    chromedriver = os.environ.get("CHROMEDRIVER", "/usr/bin/chromedriver")
    if os.path.exists(chromedriver):
        service = Service(executable_path=chromedriver)

    drv = webdriver.Chrome(options=opts, service=service)
    try:
        drv.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
        )
        drv.get(BASE + "/")
        time.sleep(8)
        for b in drv.find_elements(By.TAG_NAME, "button"):
            if "Понятно" in (b.text or ""):
                drv.execute_script("arguments[0].click();", b)
                break
        drv.execute_script(
            "arguments[0].click();",
            drv.find_element(By.ID, "header-js-open-auth"),
        )
        wait = WebDriverWait(drv, 25)
        email = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='email']")))
        email.clear()
        email.send_keys(login)
        for b in drv.find_elements(By.TAG_NAME, "button"):
            if (b.text or "").strip() == "Продолжить":
                drv.execute_script("arguments[0].click();", b)
                break
        time.sleep(4)
        pwd = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[name='password'], input[type='password']")
            )
        )
        pwd.clear()
        pwd.send_keys(passw)
        for b in drv.find_elements(By.TAG_NAME, "button"):
            if (b.text or "").strip().lower() in ("войти", "вход", "enter", "продолжить", "подтвердить"):
                drv.execute_script("arguments[0].click();", b)
                break
        time.sleep(8)
        user = drv.execute_script("return (window.RPF && RPF.store && RPF.store.user) ? RPF.store.user : null")
        if not user or not user.get("confirmed"):
            return False
        save_cookies(cookies_path, drv.get_cookies())
        print(f"  ! logged in as {user.get('email')} (has_pro={user.get('has_pro')})", file=sys.stderr)
        return True
    finally:
        drv.quit()


@dataclass
class PageData:
    url: str
    status: int
    kv: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    json: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    masked: bool = False
    locked: bool = False
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "status": self.status,
            "masked": self.masked,
            "locked": self.locked,
            "kv": self.kv,
            "tables": self.tables,
            "sections": self.sections,
            "sources": self.sources,
            "json": self.json,
            "text": self.text,
        }


class Rusprofile:
    def __init__(
        self,
        delay: float = 1.0,
        timeout: int = 30,
        retries: int = 3,
        cookies_path: str | None = DEFAULT_COOKIES,
    ):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.cookies_path = cookies_path
        self.logged_in: bool | None = None
        self.user: dict | None = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )
        if load_cookies(cookies_path, self.session):
            self.logged_in = True

    def update_auth(self, html: str) -> None:
        user = extract_rpf(html, "store.user")
        if isinstance(user, dict) and user.get("confirmed"):
            self.user = user
            self.logged_in = True
        else:
            self.logged_in = False

    def ensure_auth(self) -> None:
        if self.user is not None:
            return
        try:
            page = self.get(f"{BASE}/id/2835629")
            self.update_auth(page.text)
        except Exception:
            self.logged_in = False

    def _sleep(self):
        time.sleep(self.delay + random.uniform(0, self.delay * 0.3))

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        """HTTP-запрос с диагностикой кода ответа без вывода cookies и URL."""
        last_exc = None
        last_status = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                last_status = r.status_code
                if r.status_code in (429, 403, 503):
                    print(
                        f"  ! Rusprofile HTTP {r.status_code}; "
                        f"attempt {attempt}/{self.retries}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if attempt < self.retries:
                        wait = self.delay * attempt * 3
                        time.sleep(wait)
                    continue
                r.encoding = r.apparent_encoding or "utf-8"
                return r
            except requests.RequestException as exc:
                last_exc = exc
                print(
                    f"  ! Rusprofile network error: {type(exc).__name__}; "
                    f"attempt {attempt}/{self.retries}",
                    file=sys.stderr,
                    flush=True,
                )
                if attempt < self.retries:
                    time.sleep(self.delay * attempt)
        if last_exc is not None:
            raise RuntimeError(
                f"Rusprofile network error after {self.retries} attempts: "
                f"{type(last_exc).__name__}"
            ) from last_exc
        if last_status is not None:
            raise RuntimeError(
                f"Rusprofile HTTP {last_status} after {self.retries} attempts"
            )
        raise RuntimeError("Rusprofile request failed without a response")

    def search(self, query: str) -> list[dict]:
        query = re.sub(r"\s+", "", query)
        r = self.get(
            f"{BASE}/ajax.php",
            params={"action": "search", "query": query},
        )
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Search failed for {query!r} (not JSON, HTTP {r.status_code})")
        results = []
        for group in ("ul", "ip", "fl"):
            for item in data.get(group, []) or []:
                item = dict(item)
                item["_stripped_inn"] = re.sub(r"[!~]", "", item.get("inn") or "")
                item["_stripped_ogrn"] = re.sub(r"[!~]", "", item.get("ogrn") or "")
                results.append(item)
        return results

    def resolve(self, query: str) -> dict:
        results = self.search(query)
        if not results:
            raise LookupError(f"Nothing found for {query!r}")

        q = re.sub(r"\D", "", query)
        for it in results:
            if q and q in (it.get("_stripped_inn"), it.get("_stripped_ogrn")):
                return it
        return results[0]

    def _tab_url(self, key: str, link: str, info: dict) -> str | None:
        cid = link.rstrip("/").split("/")[-1]
        ogrn = info.get("_stripped_ogrn") or cid
        if key == "overview":
            return urljoin(BASE, link)
        if key == "egrip":
            return f"{BASE}/egrip?ogrnip={cid}"
        if key == "egrul":
            return f"{BASE}/egrul?ogrn={ogrn}"
        if key == "accounting":
            if link.startswith("/ip/"):
                return None
            return f"{BASE}/accounting?ogrn={ogrn}"
        return f"{BASE}/{key}/{cid}"

    def download_pdf(self, link: str, out_dir: str, info: dict) -> str | None:
        os.makedirs(out_dir, exist_ok=True)
        key = info.get("_stripped_inn") or info.get("_stripped_ogrn") or link.rstrip("/").split("/")[-1]
        path = os.path.join(out_dir, f"rusprofile_{key}.pdf")
        if os.path.exists(path):
            return path
        base = urljoin(BASE, f"{link}?print=pdf")
        stub = self.get(base)
        m = re.search(r"cachekey=([0-9.]+)", stub.text)
        if not m:
            return None
        pdf = self.get(f"{base}&status=downloading&cachekey={m.group(1)}")
        if pdf.content[:4] != b"%PDF":
            return None
        with open(path, "wb") as fh:
            fh.write(pdf.content)
        return path

    def fetch_tab(self, key: str, link: str, info: dict) -> PageData | None:
        candidates = [self._tab_url(key, link, info)]
        if key == "bankruptcy":
            candidates.append(self._tab_url("facts", link, info))
        for url in filter(None, candidates):
            r = self.get(url)
            if r.status_code == 404:
                continue
            try:
                return self.parse(r.text, url, key)
            except Exception as exc:
                print(f"  ! parse error for {key}: {exc}", file=sys.stderr)
                return None
        return None

    def parse(self, html: str, url: str, key: str) -> PageData:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        data = PageData(url=url, status=200)
        data.masked = MASK_CHARS in html
        data.locked = "Оформите профессиональный доступ" in html

        data.sources = self._extract_sources(html)
        for rpf_key in RPF_KEYS.get(key, []):
            value = extract_rpf(html, rpf_key)
            if value not in (None, "", [], {}):
                data.json[rpf_key] = value

        root = soup.select_one(".company-main") or soup.body
        if root is None:
            data.text = clean(soup)
            return data

        for sel in (
            ".company-header",
            ".flexpoint",
            ".flexpoint-wrapper",
            ".breadcrumbs",
            ".company-main__menu",
            ".tiles-content .ad",
        ):
            for el in root.select(sel):
                el.decompose()

        data.kv = self._extract_kv(root)
        data.tables = self._extract_tables(root)
        data.sections = self._extract_sections(root)

        full_text = clean(root)
        data.text = full_text

        if not (data.kv or data.tables or data.sections):
            data.sections = [{"title": "(раздел)", "text": full_text[:4000]}]
        return data

    @staticmethod
    def _extract_kv(root) -> list:
        seen, out = set(), []

        def add(t, v):
            t = clean(t)
            v = clean(v)
            if not t:
                return
            key = (t, v)
            if key in seen:
                return
            seen.add(key)
            out.append([t, v])

        for row in root.select(".company-row"):
            title = row.select_one(".company-info__title")
            value = row.select_one(".company-info__text, .company-info__value, p, .company-row__value")
            if title is not None:
                add(title, value)
            else:
                add(row.select_one("dt"), row.select_one("dd"))

        for dl in root.find_all("dl"):
            for dt in dl.find_all("dt", recursive=False):
                dd = dt.find_next_sibling("dd")
                add(dt, dd)
        return out

    @staticmethod
    def _extract_tables(root) -> list:
        tables = []
        for table in root.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [clean(c) for c in tr.find_all(["th", "td"])]
                if any(cells):
                    rows.append(cells)
            if len(rows) >= 1:
                tables.append(rows)
        return tables

    @staticmethod
    def _extract_sections(root) -> list:
        sections = []
        for tile in root.select(".tile-item, .content-frame"):
            title_el = tile.select_one(".tile-item__title, h2, h3")
            title = clean(title_el)
            text = clean(tile)
            if title and text.startswith(title):
                text = text[len(title) :].strip()
            if not title:
                continue
            if title in {s["title"] for s in sections}:
                continue
            sections.append({"title": title, "text": text[:3000]})

        if not sections:
            for heading in root.find_all(["h2", "h3"]):
                title = clean(heading)
                if not title:
                    continue
                parts = []
                sib = heading.find_next_sibling()
                while sib is not None and sib.name not in ("h2", "h3"):
                    t = clean(sib)
                    if t:
                        parts.append(t)
                    sib = sib.find_next_sibling()
                sections.append({"title": title, "text": " ".join(parts)[:3000]})
        return sections

    @staticmethod
    def _extract_sources(html: str) -> list:
        found = set()
        for m in re.finditer(r"data-quetip=['\"](.*?)['\"]", html, re.S):
            q = html_mod.unescape(m.group(1))
            if "Источник" not in q:
                continue
            text = re.sub(r"<[^>]+>", " ", q)
            text = re.sub(r"\s+", " ", text).strip()
            if text and text != "Источник :":
                found.add(text)
        return sorted(found)

    def company(
        self,
        query: str,
        tab_keys: list[str],
        include_extra: bool = False,
        save_pdf: str | None = None,
    ) -> dict:
        info = self.resolve(query)
        link = info.get("link") or info.get("url")
        if not link:
            raise LookupError(f"Cannot resolve link for {query!r}")
        self.ensure_auth()

        result = {
            "query": query,
            "type": info.get("ref_type"),
            "id": link.rstrip("/").split("/")[-1],
            "link": urljoin(BASE, link),
            "name": clean(info.get("name")),
            "raw_name": clean(info.get("raw_name")),
            "inn": info.get("_stripped_inn"),
            "ogrn": info.get("_stripped_ogrn"),
            "region": info.get("region"),
            "address": info.get("address"),
            "status": "inactive" if info.get("inactive") else "active",
            "ceo": {
                "name": info.get("ceo_name"),
                "position": info.get("ceo_type"),
            },
            "auth": {
                "logged_in": bool(self.logged_in),
                "account": (self.user or {}).get("email"),
                "has_pro": bool((self.user or {}).get("has_pro")),
            },
            "okved": info.get("main_okved_id"),
            "okved_description": info.get("okved_descr"),
            "registration_date": info.get("reg_date"),
            "authorized_capital": info.get("authorized_capital"),
            "tabs": {},
        }

        ordered = list(TABS)
        if include_extra:
            ordered += EXTRA_TABS

        jobs = [(n, k) for n, k in ordered if k != "sources"]
        if any(k == "sources" for _, k in ordered):
            jobs.append(("Источники", "sources"))

        wanted = set(tab_keys)
        for name, key in jobs:
            if wanted and name not in wanted and key not in wanted:
                continue
            if key == "sources":
                sources = set()
                for tname, sub in result["tabs"].items():
                    if isinstance(sub, dict):
                        sources.update(sub.get("sources", []))
                result["tabs"]["Источники"] = {
                    "url": f"{BASE}/sources",
                    "sources": sorted(sources),
                    "note": "Источники собраны из подсказок (data-quetip) на страницах компании.",
                }
                continue
            print(f"  + {name} ...", file=sys.stderr)
            page = self.fetch_tab(key, link, info)
            if page is None:
                result["tabs"][name] = {
                    "url": self._tab_url(key, link, info),
                    "status": 404,
                    "error": "not available",
                }
            else:
                result["tabs"][name] = page.to_dict()
            self._sleep()

        if save_pdf:
            print("  + PDF-отчёт ...", file=sys.stderr)
            pdf = self.download_pdf(link, save_pdf, info)
            if pdf:
                result["pdf"] = os.path.abspath(pdf)
            else:
                result["pdf"] = None
            self._sleep()

        return result


def render(company: dict, compact: bool = False, stream=sys.stdout) -> None:
    w = stream.write
    w("=" * 78 + "\n")
    auth = company.get("auth") or {}
    if auth.get("logged_in"):
        w(f"Аккаунт: {auth.get('account') or 'подключён'}  [PRO={'да' if auth.get('has_pro') else 'нет'}]\n")
    else:
        w("[ без авторизации — часть чисел будет скрыта (░), используйте --acc/--cookies ]\n")
    w(f"{company['name']}  [{company['type']}]\n")
    w(f"ИНН: {company.get('inn')}   ОГРН: {company.get('ogrn')}   id: {company.get('id')}\n")
    w(f"Ссылка: {company['link']}\n")
    if company.get("address"):
        w(f"Адрес: {company['address']}\n")
    if company.get("region"):
        w(f"Регион: {company['region']}\n")
    if company.get("ceo", {}).get("name"):
        w(f"Руководитель: {company['ceo']['name']} ({company['ceo'].get('position')})\n")
    if company.get("pdf"):
        w(f"PDF-отчёт: {company['pdf']}\n")
    w("=" * 78 + "\n")

    for name, page in company["tabs"].items():
        w("\n" + "#" * 78 + "\n")
        w(f"# {name}\n")
        w("#" * 78 + "\n")

        if page.get("status") == 404:
            w("(раздел недоступен для этой организации)\n")
            continue

        if page.get("locked"):
            w("[ ! ] Часть раздела закрыта платной подпиской.\n")
        if page.get("masked"):
            w("[ ! ] Числовые значения скрыты (░) — авторизуйтесь (--acc).\n")

        if page.get("kv"):
            w("\n-- Реквизиты / показатели --\n")
            for k, v in page["kv"]:
                if compact:
                    v = collapse_masked(v)
                w(f"  {k}: {v}\n")

        if page.get("json"):
            w("\n-- Данные (JSON) --\n")
            w(json.dumps(page["json"], ensure_ascii=False, indent=2) + "\n")

        skipped = 0
        for section in page.get("sections", []):
            text = collapse_masked(section["text"]) if compact else section["text"]
            w(f"\n-- {section['title']} --\n")
            w(f"  {text}\n")

        for i, table in enumerate(page.get("tables", []), 1):
            flat = " ".join(" ".join(row) for row in table)
            if compact and masked_ratio(flat) > 0.6:
                skipped += 1
                continue
            w(f"\n-- Таблица #{i} --\n")
            for row in table:
                w("  | " + " | ".join(row) + " |\n")
        if skipped and compact:
            w(f"\n  ({skipped} таблиц(а) пропущено: полностью скрыты подпиской)\n")

        if page.get("sources"):
            w("\n-- Источники --\n")
            for s in page["sources"]:
                w(f"  - {s}\n")

# позволяет обращаться к встроенным именам как rs.<NAME> (совместимость с прежним импортом)
rs = sys.modules[__name__]
# ---- конец встроенного модуля скрапера ----

MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


# --------------------------------------------------------------------------- util

def norm(text: Any) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def clean_addr(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"\s*;\s*", "; ", s)
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"^\s*,|,\s*$", "", s).strip(" ,;")
    return re.sub(r"\s{2,}", " ", s)


STREET_SUFFIX = r"(?:ул|просп|пр-т|проспект|пер|переул|шос|б-р|бульвар|наб|проезд|пл|площад|алле|алл|туп|тупик|ш\.)"


def short_addr(s: str) -> str:
    """Сокращённый адрес для KPI: улица + дом, напр. «ул. Старобитцевская, д. 15»."""
    s = clean_addr(s)
    if not s:
        return "—"
    street = None
    m = re.search(r"([А-ЯЁ][\w\-. ]*?)\s+(" + STREET_SUFFIX + r")\b\.?", s)
    if m:
        street = f"{m.group(2)}. {m.group(1).strip()}"
    else:
        m = re.search(r"\b(" + STREET_SUFFIX + r")\b\.?\s+([А-ЯЁ][\w\-. ]+)", s)
        if m:
            street = f"{m.group(1).rstrip('.')}. {m.group(2).strip()}"
    house = re.search(r"д\.?\s*(\d+[А-Яа-я]?)", s)
    parts = []
    if street:
        parts.append(street)
    if house:
        parts.append(f"д. {house.group(1)}")
    if parts:
        return ", ".join(parts)
    return (s[:52] + "…") if len(s) > 52 else s


def parse_num(text: Any) -> float | None:
    t = norm(text)
    if not t or t in ("-", "—", "—", "Нет данных"):
        return None
    t = t.replace("\u00a0", " ").replace(" ", "")
    t = t.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def fmt_rub(thou: float | None) -> str:
    """thou — значение в тысячах рублей."""
    if thou is None:
        return "—"
    v = abs(thou)
    if v >= 1_000_000:
        return f"{thou / 1_000_000:,.1f}".replace(",", "\u00a0").replace(".", ",") + " млрд ₽"
    if v >= 100_000:
        return f"{thou / 1_000:,.1f}".replace(",", "\u00a0").replace(".", ",") + " млн ₽"
    if v >= 1_000:
        return f"{int(round(thou)):,}".replace(",", "\u00a0") + " тыс. ₽"
    if abs(thou - round(thou)) < 0.05:
        return f"{int(round(thou)):,}".replace(",", "\u00a0") + " тыс. ₽"
    return f"{thou:,.1f}".replace(",", "\u00a0").replace(".", ",") + " тыс. ₽"


def fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    if s == "":
        s = "0"
    return s.replace(".", ",") + "%"


def fmt_space(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}".replace(",", "\u00a0")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def years_range_year(reg: str) -> int | None:
    m = re.search(r"20\d{2}", reg or "")
    return int(m.group(0)) if m else None


# --------------------------------------------------------------------- parsers

def parse_html_tables(html: str) -> list[dict]:
    """Разбирает все `<table>` со страницы в структурированные блоки.
    Учёт: таблицы rusprofile разбиты — отдельная «шапка» (её `<th>` содержат
    годы) предшествует одной или нескольким таблицам с данными (только `<td>`).
    Годы наследуются от последней встреченной шапки до следующей.
    Каждому блоку: {title, years:[int], rows:[[label, code, {year:value}]]}
    """
    out = []
    soup = BeautifulSoup(html, "html.parser")
    cur_years: list[int] = []
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        ths = trs[0].find_all("th")
        th_years = []
        for th in ths:
            t = norm(th.get_text(" "))
            if re.fullmatch(r"20\d{2}", t):
                th_years.append(int(t))
        header_only = (
            bool(ths)
            and len(trs) == 1
            and len(th_years) > 0
            and th_years == sorted(th_years)
        )
        if header_only:
            cur_years = list(dict.fromkeys(th_years))
            continue

        title = norm(ths[0].get_text(" ")) if ths else ""
        years = cur_years
        if th_years:
            years = list(dict.fromkeys(th_years))
            cur_years = years

        start = 1 if ths else 0
        rows = []
        for tr in trs[start:]:
            tds = tr.find_all(["td", "th"])
            cells = [norm(td.get_text(" ")) for td in tds]
            if not cells or not cells[0]:
                continue
            label = cells[0]
            code = ""
            if len(cells) > 1 and re.fullmatch(r"\d{3,4}", cells[1]):
                code = cells[1]
                vals = cells[2:]
            elif len(cells) > 1:
                vals = cells[1:]
            else:
                vals = []
            row_years = years
            if len(vals) < len(years):
                row_years = years[: len(vals)]
            row = {"label": label, "code": code, "data": {}}
            for i, y in enumerate(row_years):
                row["data"][y] = parse_num(vals[i]) if i < len(vals) else None
            rows.append(row)
        if rows and (rows[0]["label"] != "Источник отчетности" or len(rows) > 1):
            out.append({"title": title, "years": list(years), "rows": rows})
    return out


def extract_accounting(html: str) -> dict[str, dict[int, float]]:
    """Код (2110/2400/3600...) → {год: тыс.руб}."""
    by_code: dict[str, dict[int, float]] = {}
    by_name: dict[str, dict[int, float]] = {}
    for b in parse_html_tables(html):
        for row in b["rows"]:
            if row["code"] and row["data"] and all(v is None for v in row["data"].values()):
                continue
            if row["code"]:
                by_code[row["code"]] = row["data"]
            if row["label"]:
                by_name[row["label"]] = row["data"]
    return {"by_code": by_code, "by_name": by_name}


def extract_taxes(html: str) -> dict:
    taxes: dict[int, float] = defaultdict(float)
    contributions: dict[int, float] = {}
    DEBT_ROWS = {"Недоимки", "Пени", "Штрафы"}
    for b in parse_html_tables(html):
        head = b["title"]
        years = b["years"]
        labels = {r["label"] for r in b["rows"]}
        if labels & DEBT_ROWS:
            continue  # таблицы «задолженности/пеней/штрафов», не уплаченные суммы
        if head == "Налоги" or b["rows"] and b["rows"][0]["label"] in ("Налог на прибыль", "Налог на добавленную стоимость", "Транспортный налог"):
            for row in b["rows"]:
                if row["label"].startswith("Всего по налогам"):
                    continue
                for y in years:
                    if row["data"].get(y) is not None:
                        taxes[y] += row["data"][y]
        elif head.startswith("Страховые взносы"):
            for row in b["rows"]:
                if row["label"] == "Всего по взносам":
                    found = {y: (v / 1000) for y, v in row["data"].items() if v is not None}
                    if found:
                        contributions = found
    return {
        "taxes_by_year": {y: round(v / 1000) for y, v in taxes.items()},
        "contributions_by_year": contributions,
    }


def extract_overview_kv(html: str) -> dict[str, str]:
    out = {}
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".company-main") or soup.body
    for row in root.select(".company-row"):
        title = row.select_one(".company-info__title, dt")
        value = row.select_one(".company-info__text, .company-info__value, dd, p, .company-row__value")
        if title is None or value is None:
            continue
        t = norm(title.get_text(" "))
        v = norm(value.get_text(" "))
        if t and v:
            out[t] = v
    for dl in root.find_all("dl"):
        for dt in dl.find_all("dt", recursive=False):
            dd = dt.find_next_sibling("dd")
            if dd is None:
                continue
            t = norm(dt.get_text(" "))
            v = norm(dd.get_text(" "))
            if t and v:
                out[t] = v
    return out


def extract_founders(html: str) -> dict:
    """Возвращает {'actual': [...], 'historical': [...]}"""
    soup = BeautifulSoup(html, "html.parser")
    result = {"actual": [], "historical": []}
    current = None
    for el in soup.find_all(["h2", "ul"]):
        if el.name == "h2":
            txt = norm(el.get_text(" "))
            if "Актуальные" in txt:
                current = "actual"
            elif "Исторические" in txt:
                current = "historical"
            else:
                current = None
            continue
        if el.name != "ul" or current is None:
            continue
        for li in el.select("li.filters-results__list-item"):
            el_ = li.select_one(".list-element")
            if el_ is None:
                continue
            name = norm(el_.select_one(".list-element__title").get_text(" "))
            info = {}
            for item in el_.select(".list-element__info-box-item"):
                spans = item.find_all("span")
                key = norm((spans[0] if spans else item).get_text(" "))
                key = key.rstrip(":")
                val = norm(item.get_text(" "))
                if key:
                    info[key] = val
            region = norm(el_.select_one(".list-element__address").get_text(" ")) if el_.select_one(".list-element__address") else ""
            inn_m = re.search(r"ИНН:?\s*(\d+)", norm(li.get_text(" ")))
            def v_of(key):
                return re.sub(rf"^{re.escape(key)}:?\s*", "", info.get(key, "")).strip()
            person = {
                "name": name.replace(" подробнее", ""),
                "inn": inn_m.group(1) if inn_m else "",
                "region": region,
                "period": v_of("Период"),
                "share": v_of("Доля"),
                "role_orgs": v_of("Руководитель"),
                "founder_orgs": v_of("Учредитель"),
                "links_orgs": v_of("Связи"),
            }
            result[current].append(person)
    return result


def extract_okved_count(html: str) -> int | None:
    m = re.search(r"ОКВЭД\s*\((\d+)\)", html)
    return int(m.group(1)) if m else None


def extract_okved_codes(html: str) -> tuple[str | None, list[str]]:
    """Возвращает (код основного ОКВЭД, список кодов дополнительных ОКВЭД)."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return None, []
    seen: set[str] = set()
    addl: list[str] = []
    for num in soup.select(".okved-item__num"):
        t = norm(num.get_text(" "))
        if re.fullmatch(r"\d{2}\.\d{2}(?:\.\d)?", t) and t not in seen:
            seen.add(t)
            addl.append(t)
    main = None
    for tb in soup.find_all("div", class_="text-box"):
        txt = tb.get_text(" ")
        if "Основной вид деятельности" in txt:
            m = re.search(r"\((\d{2}\.\d{2}(?:\.\d)?)\)", txt)
            if m:
                main = m.group(1)
                break
    if main in seen:
        addl.remove(main)
    return main, addl


def extract_company_industry(html: str) -> str:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""
    for tb in soup.find_all("div", class_="text-box"):
        txt = norm(tb.get_text(" "))
        m = re.match(r"Отрасль\s+(.+?)($|Основной вид деятельности|Регион)", txt)
        if m:
            return m.group(1).strip()
    return ""


def extract_description(html: str) -> str:
    """Краткое AI-описание компании со страницы «Сводка» (без служебного заголовка)."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""
    el = soup.find("div", class_="company-description__text")
    if not el:
        return ""
    txt = norm(el.get_text(" "))
    return re.sub(r"^Главное о компании за 1 минуту\s*", "", txt).strip()


def extract_registry_checks(html: str) -> list[dict]:
    """Список экспресс-проверок по реестрам: [{name, status, ok}]."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return []
    out = []
    for li in soup.select("ul.reesters-tile__list li"):
        full = norm(li.get_text(" "))
        fl = li.select_one(".flag")
        status = norm(fl.get_text(" ")) if fl else ""
        label = full[len(status):].strip() if status and full.startswith(status) else full
        cls = " ".join(fl.get("class") or []) if fl else ""
        ok = ("success" in cls) or not fl
        if label:
            out.append({"name": label, "status": status, "ok": ok})
    return out


def extract_records_total(html: str) -> int | None:
    """Итоговое число записей из пагинации вида «Показаны дела 1–20 из 815 Показать ещё»."""
    try:
        txt = norm(BeautifulSoup(html or "", "html.parser").get_text(" "))
    except Exception:
        return None
    m = re.search(r"\d+\s*[–\-]\s*\d+\s+из\s+([\d\s\u00a0]+?)\s*Показать", txt)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def extract_reliability(html: str) -> str:
    m = re.search(r"Надёжность\s*([А-Яа-яёЁ]+)", html)
    return m.group(1) if m else ""


def extract_requisites(html: str) -> dict[str, str]:
    out = {}
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return out
    for li in soup.select(".requisites-item"):
        name = li.select_one(".requisites-item__name")
        val = li.select_one(".requisites-item__value")
        if name is not None and val is not None:
            t = norm(name.get_text(" "))
            v = norm(val.get_text(" "))
            v = re.sub(r"[СC]копировать$", "", v).strip()
            if t and v:
                out[t] = v
    return out


def extract_gov_contracts(html: str) -> int | None:
    m = re.search(r"(\d+)\s+госконтракт[а-я]*", html)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------ dossier builder

class Dossier:
    def __init__(self, scraper, query: str):
        self.s = scraper
        self.query = query
        self.info = scraper.resolve(query)
        link = self.info.get("link") or self.info.get("url")
        self.link = link
        self.cid = link.rstrip("/").split("/")[-1] if link else ""
        self.s.ensure_auth()
        self.base_ogrn = (self.info.get("_stripped_ogrn") or "") or self.cid
        self.raw = {}
        self.m = {}

    def fetch(self, path: str, params: dict | None = None) -> str:
        url = f"{rs.BASE}/{path}" if not path.startswith("http") else path
        r = self.s.get(url, params=params)
        return r.text if r.status_code == 200 else ""

    def _url_map(self) -> dict[str, str]:
        cid = self.cid
        hit = {
            key: f"{key}/{cid}"
            for key in ("taxes", "founders", "requisites", "okved", "reliability",
                        "arbitr", "fssp", "bankruptcy", "gz", "licenses", "history",
                        "sanctions", "inspections")
        }
        hit["overview"] = self.link
        if not self.link.startswith("/ip/"):
            hit["accounting"] = f"accounting?ogrn={self.base_ogrn}"
        return hit

    def collect(self) -> "Dossier":
        for key, url in self._url_map().items():
            self.raw[key] = self.fetch(url)
        self._build_metrics()
        return self

    # ---- metric assembly ----------------------------------------------------
    def _build_metrics(self):
        ov_html = self.raw.get("overview", "")
        kv = extract_overview_kv(ov_html)
        acc = extract_accounting(self.raw.get("accounting", ""))
        taxes = extract_taxes(self.raw.get("taxes", ""))
        founders = extract_founders(self.raw.get("founders", ""))

        by_code, by_name = acc["by_code"], acc["by_name"]
        years = [2023, 2024, 2025]
        rev = {y: by_code.get("2110", {}).get(y) for y in years}
        profit = {y: by_code.get("2400", {}).get(y) for y in years}
        net_assets = by_code.get("3600", {}) or by_name.get("Чистые активы на конец года", {})
        if not net_assets:
            net_assets = by_code.get("1300", {})
        taxes_by_year = taxes["taxes_by_year"]
        contrib_by_year = taxes["contributions_by_year"]

        # headcount / salary
        def first_int(txt):
            m = re.search(r"\d[\d\s]*", txt or "")
            return int(re.sub(r"\s", "", m.group(0))) if m else None

        headcount = first_int(kv.get("Среднесписочная численность", ""))
        avg_salary = first_int(kv.get("Среднемесячная зарплата", ""))

        mass_addr = 0
        m_ = re.search(r"connections_address[^>]*>\s*(\d+)\s*организац", ov_html)
        if not m_:
            m_ = re.search(r"(\d+)\s*организаци\w*\s*</a>\s*по этому адресу", ov_html)
        if m_:
            mass_addr = int(m_.group(1))
        address = kv.get("Юридический адрес", "")

        msp = kv.get("Реестр МСП") or kv.get("Категория субъекта", "")
        spec_regime = kv.get("Специальный налоговый режим") or kv.get("Применяется", "")
        okved_desc = kv.get("Основной вид деятельности") or self.info.get("okved_descr", "")
        okved_main = re.sub(r"\s*\((\d{2}\.\d{2})\)\s*$", r" (\1)", okved_desc)
        capital = kv.get("Уставный капитал", "")
        reg_date = re.sub(r"\s*г\.?\s*$", "", kv.get("Дата регистрации", "")).strip()
        inn_kpp = kv.get("ИНН/КПП", "")
        req_kv = extract_requisites(self.raw.get("requisites", ""))
        kpp = req_kv.get("КПП", "")
        if not kpp:
            m_ = re.search(r"КПП[^\d]{0,24}(\d{9})", self.raw.get("requisites", ""))
            if m_:
                kpp = m_.group(1)
        kpp = re.sub(r"[!~]", "", kpp)
        address = clean_addr(kv.get("Юридический адрес") or kv.get("Адрес", ""))
        tax_org = clean_addr(kv.get("Налоговый орган") or kv.get("Наименование налогового органа") or kv.get("Наименование территориального органа", ""))

        leading = None
        ceo_m = re.search(r"Руководитель:?\s*([А-ЯЁ][а-яё]+\s[А-ЯЁ][а-яё]+(?:\s[А-ЯЁ][а-яё]+)?)", kv.get("Руководитель", "") + " " + self.info.get("ceo_name", ""))
        if ceo_m:
            leading = ceo_m.group(1)
        else:
            leading = self.info.get("ceo_name")

        okved_count = extract_okved_count(self.raw.get("okved", "")) or 0
        okved_main_code, okved_add = extract_okved_codes(self.raw.get("okved", ""))
        if okved_add:
            okved_count = len(okved_add) + (1 if okved_main_code else 0)
        industry = extract_company_industry(self.raw.get("okved", ""))
        description = extract_description(ov_html)
        registry_checks = extract_registry_checks(ov_html)
        arbitr_count = extract_records_total(self.raw.get("arbitr", ""))
        fssp_count = extract_records_total(self.raw.get("fssp", ""))
        reliability = extract_reliability(self.raw.get("reliability", ""))
        gov_contracts = extract_gov_contracts(self.raw.get("gz", "") or ov_html)

        lawsuit_years = self.raw.get("arbitr", "")
        debtor = "не состоит в реестре ФНС" not in self.raw.get("taxes", "")
        bankruptcy_debt = "Банкротство:" in self.raw.get("overview", "")

        self.m = {
            "id": self.cid,
            "link": f"{rs.BASE}{self.link}",
            "name": norm(self.info.get("name")),
            "raw_name": norm(self.info.get("raw_name") or self.info.get("name")),
            "inn": self.info.get("_stripped_inn"),
            "ogrn": self.info.get("_stripped_ogrn") or self.info.get("ogrnip") or self.info.get("ogrn") or (self.cid if self.link.startswith("/ip/") else None),
            "kpp": kpp,
            "type": self.info.get("ref_type"),
            "region": self.info.get("region") or self.info.get("region_name"),
            "address": address,
            "mass_addr": mass_addr,
            "ceo": leading,
            "capital": capital,
            "msp": msp,
            "spec_regime": spec_regime if spec_regime and "не применяется" not in spec_regime else "",
            "tax_org": tax_org,
            "reg_date": reg_date,
            "years_active": years_range_year(reg_date),
            "okved_main": okved_desc,
            "okved_main_code": okved_main_code,
            "okved_add": okved_add,
            "industry": industry,
            "description": description,
            "registry_checks": registry_checks,
            "arbitr_count": arbitr_count,
            "fssp_count": fssp_count,
            "okved_count": okved_count,
            "headcount": headcount,
            "avg_salary": avg_salary,
            "reliability": reliability,
            "gov_contracts": gov_contracts,
            "taxes_by_year": taxes_by_year,
            "contrib_by_year": contrib_by_year,
            "rev": rev,
            "profit": profit,
            "net_assets": net_assets,
            "founders": founders,
            "has_arbitr": bool(arbitr_count) or bool(re.search(r"АС\s|арбитражн\w+|Суд", lawsuit_years)),
            "has_fssp": bool(fssp_count) or ("розыск" in self.raw.get("fssp", "")) or bool(re.search(r"\d+\s*(исполнительн\w*\s+производств)", self.raw.get("fssp", ""))),
            "has_bankruptcy": bool(re.search(r"Банкротство|банкрот", self.raw.get("bankruptcy", ""))),
        }

    # ---- analytics ----------------------------------------------------------
    def analyze(self) -> dict:
        m = self.m
        flags = []

        def latest(d):
            for y in (2025, 2024, 2023, 2022, 2021):
                if d.get(y) is not None:
                    return y
            return None

        lt = latest(m["rev"])
        lv = m["rev"].get(lt) if lt else None
        prev_vals = [m["rev"][y] for y in sorted(m["rev"]) if lt is not None and y < lt and m["rev"][y] is not None]
        max_prev = max(prev_vals) if prev_vals else None
        jump = None
        if lv and max_prev and max_prev > 0:
            jump = (lv - max_prev) / max_prev

        # taxes
        tax_lt = latest(m["taxes_by_year"])
        tax_lv = m["taxes_by_year"].get(tax_lt) if tax_lt else None
        burden = (tax_lv / lv * 100) if (tax_lv is not None and lv) else None

        contrib_lt = latest(m["contrib_by_year"])
        contrib_lv = m["contrib_by_year"].get(contrib_lt) if contrib_lt else None
        per_head = None
        if contrib_lv is not None and m["headcount"]:
            per_head = contrib_lv / m["headcount"]

        na_lt = latest(m["net_assets"])
        na_lv = m["net_assets"].get(na_lt) if na_lt else None

        # --- фактор 1: спящая компания / оборотный скачок
        lvl = None
        if jump and jump >= 3 and lv >= 10_000:
            lvl = "critical" if (na_lv is None or na_lv < 0.4 * lv) else "high"
        if lvl:
            pct = f"+{fmt_space(jump * 100)}%"
            flags.append({
                "num": 1,
                "title": "Аномальный оборотный скачок (" + ("«спящая» компания / Dormant Company)" if jump >= 10 else "резкий рост выручки)"),
                "badge": lvl,
                "essence": (
                    f"Выручка за {lt} г. достигла {fmt_rub(lv)}, при росте к предыдущему максимуму в {fmt_space(jump + 1)} раз "
                    f"({pct}). При этом чистые активы составляют {fmt_rub(na_lv)} ({fmt_pct(na_lv / lv * 100)} от выручки) "
                    f"при уставном капитале {(m['capital'] or '—').rstrip('.')}."
                ),
                "risk": (
                    "Классический сценарий использования юридического лица со стажем для транзитных операций или "
                    "агрессивной оптимизации НДС. Рекомендуется верификация реальности хозяйственных операций."
                ),
            })

        # --- фактор 2: налоговая нагрузка
        if lv and burden is not None and not m["spec_regime"]:
            lvl2 = "critical" if burden < 1 else ("high" if burden < 3 else None)
            if lvl2:
                flags.append({
                    "num": len(flags) + 1,
                    "title": "Критически низкая налоговая нагрузка (Налоговый разрыв)",
                    "badge": lvl2,
                    "essence": (
                        f"При выручке {fmt_rub(lv)} уплачено налогов всего {fmt_rub(tax_lv)} "
                        f"({fmt_pct(burden)} от выручки). Специальный режим не применяется (ОСНО). "
                        f"Доля налоговых вычетов по НДС близка к 100%."
                    ),
                    "risk": (
                        "Системы ФНС («АСК НДС-2») относят таких налогоплательщиков к группе высокого риска; "
                        "контрагенту грозит отказ в вычетах и снятие расходов (ст. 54.1 НК РФ)."
                    ),
                })

        # --- фактор 3: мёртвые души
        monthly = (per_head * 1000 / 12) if per_head is not None else None
        if m["headcount"] and monthly is not None and monthly < 5_000:
            badge = "high" if monthly < 2_000 else "medium"
            flags.append({
                "num": len(flags) + 1,
                "title": "Признаки фиктивности трудовых ресурсов (" + ("«Мёртвые души» / чёрная зарплата)" if badge == "high" else "низкий фонд оплаты труда)"),
                "badge": badge,
                "essence": (
                    f"В штате {m['headcount']} {plural(m['headcount'], 'сотрудник', 'сотрудника', 'сотрудников')}, "
                    f"а страховых взносов уплачено {fmt_rub(contrib_lv)} за год — это {fmt_space(monthly)} ₽/мес. "
                    f"на одного человека. При стандартной ставке 30% официальная зарплата составила бы "
                    f"около {fmt_space(monthly / 0.3)} ₽/мес."
                ),
                "risk": (
                    "Персонал числится номинально для имитации деловой активности либо компания нарушает трудовое "
                    "законодательство, что создаёт риск доначислений НДФЛ/взносов и блокировки счетов."
                ),
            })

        # --- фактор 4: массовый адрес
        if m["mass_addr"] >= 50:
            flags.append({
                "num": len(flags) + 1,
                "title": f"Адрес массовой регистрации ({m['mass_addr']} {plural(m['mass_addr'], 'организация', 'организации', 'организаций')})",
                "badge": "medium",
                "essence": (f"Юридический адрес совпадает с адресом регистрации {m['mass_addr']} других юридических лиц."),
                "risk": (
                    "Вероятно внесение отметки о недостоверности в ЕГРЮЛ, что блокирует банковские счета и электронную подпись."
                ),
            })

        # --- фактор 5: МСП / широта деятельности
        reason = []
        if "микропредприятие" in (m["msp"] or "") and lv and lv > 120_000:
            reason.append("реестр МСП — «микропредприятие» при выручке свыше 120 млн ₽")
        if m["okved_count"] and m["okved_count"] >= 20:
            reason.append(f"{m['okved_count']} видов деятельности без явной специализации")
        if reason:
            flags.append({
                "num": len(flags) + 1,
                "title": "Несоответствие профилю и реестру МСП",
                "badge": "medium" if len(reason) > 1 else "low",
                "essence": "Обнаружено: " + "; ".join(reason) + ".",
                "risk": (
                    "Размытый профиль деятельности и несоответствие заявленному масштабу повышают внимание Банков и "
                    "налоговых органов к операциям компании."
                ),
            })

        # --- фактор 6: динамика чистой прибыли / рентабельность
        prof_lt = latest(m["profit"])
        prof_lv = m["profit"].get(prof_lt) if prof_lt else None
        prev_prof = None
        if prof_lt is not None:
            for y in range(prof_lt - 1, prof_lt - 6, -1):
                if m["profit"].get(y) is not None:
                    prev_prof = m["profit"][y]
                    break
        if prof_lv is not None and lv:
            margin = prof_lv / lv * 100 if lv > 0 else 0
            if prof_lv < 0:
                flags.append({
                    "num": len(flags) + 1,
                    "title": f"Убыток по итогам {prof_lt} г.",
                    "badge": "medium",
                    "essence": (
                        f"По итогам {prof_lt} г. получен чистый убыток {fmt_rub(abs(prof_lv))} "
                        f"при выручке {fmt_rub(lv)}."
                    ),
                    "risk": (
                        "Убыточная деятельность свидетельствует о неэффективности модели либо о выводе прибыли; "
                        "повышает риск неплатёжеспособности контрагента."
                    ),
                })
            elif margin < 0.5:
                drop = ""
                if prev_prof and prev_prof > 0 and prof_lv < prev_prof:
                    drop = (f" При этом чистая прибыль снизилась с {fmt_rub(prev_prof)} "
                            f"в {prof_lt - 1} г. до {fmt_rub(prof_lv)} в {prof_lt} г.")
                flags.append({
                    "num": len(flags) + 1,
                    "title": "Крайне низкая чистая рентабельность" if margin < 0.1 else "Низкая чистая рентабельность",
                    "badge": "medium" if margin < 0.1 else "low",
                    "essence": (
                        f"Чистая рентабельность за {prof_lt} г. составила {fmt_pct(margin)} "
                        f"({fmt_rub(prof_lv)} чистой прибыли при выручке {fmt_rub(lv)}).{drop}"
                    ),
                    "risk": (
                        "Микроскопическая прибыль при значительном обороте характерна для транзитных схем "
                        "и искусственного «схлопывания» налоговой базы."
                    ),
                })

        # --- дополнительные маркеры
        extra = []
        if m["has_bankruptcy"]:
            flags.append({
                "num": len(flags) + 1,
                "title": "Признаки банкротства",
                "badge": "critical",
                "essence": "В открытых источниках присутствуют сведения о процедурах банкротства.",
                "risk": "Прямое ограничение для заключения крупных контрактов. Рекомендуется отказ от сделки.",
            })
        if m["has_fssp"]:
            cnt = m.get("fssp_count")
            flags.append({
                "num": len(flags) + 1,
                "title": "Исполнительные производства" + (f" ({cnt})" if cnt else ""),
                "badge": "medium",
                "essence": (
                    (f"В отношении компании открыто {cnt} {plural(cnt, 'исполнительное производство', 'исполнительных производства', 'исполнительных производств')}."
                     if cnt else "В отношении компании возбуждены исполнительные производства.")
                ),
                "risk": "Свидетельствует о финансовой недисциплинированности контрагента и наличии взысканий.",
            })
        if m["has_arbitr"]:
            cnt = m.get("arbitr_count")
            flags.append({
                "num": len(flags) + 1,
                "title": "Судебные споры" + (f" ({cnt})" if cnt else ""),
                "badge": "low",
                "essence": (
                    (f"Компания фигурирует в {cnt} {plural(cnt, 'арбитражном деле', 'арбитражных делах', 'арбитражных делах')} в качестве истца и/или ответчика."
                     if cnt else "Компания участвует в арбитражных спорах (истцы/ответчики).")
                ),
                "risk": "Оценить характер и объём споров, а также долю дел, проигранных компанией, перед заключением сделки.",
            })

        # сводный риск
        order = {"critical": 3, "high": 2, "medium": 1, "low": 0}
        worst = max((order[f["badge"]] for f in flags), default=0)
        verdict = {0: "НИЗКИЙ РИСК", 1: "СРЕДНИЙ РИСК", 2: "ВЫСОКИЙ РИСК", 3: "ВЫСОКИЙ РИСК"}[worst]
        level = {0: "low", 1: "medium", 2: "high", 3: "critical"}[worst]

        # ---- полнота исходных данных
        # Ключевые аналитические блоки: если их нет, отсутствие «красных флагов»
        # НЕ означает низкий риск — оценка ограничена неполнотой данных.
        data_blocks = {
            "финансовая отчётность": bool(m["rev"] or m["profit"] or m["net_assets"]),
            "налоговая отчётность и взносы": bool(m["taxes_by_year"] or m["contrib_by_year"]),
            "численность персонала": bool(m["headcount"]),
            "сведения об участниках": bool(m["founders"].get("actual")),
        }
        missing_blocks = [k for k, ok in data_blocks.items() if not ok]
        present_blocks = len(data_blocks) - len(missing_blocks)
        # финансовая отчётность — базовая: без неё финансовая состоятельность не оценивается
        data_limited = not data_blocks["финансовая отчётность"]

        # если нет базовой отчётности и не выявлено высоких рисков — вердикт не «зелёный»
        if data_limited and worst < 2:
            verdict = "НЕДОСТАТОЧНО ДАННЫХ"
            level = "unknown"

        m_flags = {"critical": [], "high": [], "medium": [], "low": []}
        for f in flags:
            m_flags[f["badge"]].append(f)

        return {
            "flags": flags,
            "verdict": verdict,
            "level": level,
            "verdict_color": {0: "#2563EB", 1: "#D97706", 2: "#DC2626", 3: "#DC2626"}[worst],
            "lt": lt, "lv": lv, "jump": jump,
            "tax_lt": tax_lt, "tax_lv": tax_lv, "burden": burden,
            "contrib_lt": contrib_lt, "contrib_lv": contrib_lv, "per_head": per_head, "monthly": monthly,
            "na_lv": na_lv,
            "counts": {k: len(v) for k, v in m_flags.items()},
            "worst": verdict,
            "data_blocks": data_blocks,
            "missing_blocks": missing_blocks,
            "present_blocks": present_blocks,
            "data_limited": data_limited,
        }

    # ---- собрать всё ---------------------------------------------------------
    def build(self) -> dict:
        a = self.analyze()
        now = date.today()
        return {
            "company": self.m,
            "analytics": a,
            "date": {"year": now.year, "month": MONTHS_RU.get(now.month, "")},
        }

    # ---- исходники текста ----------------------------------------------------


# --------------------------------------------------------------------- html

PALETTE = {
    "navy": "#0F1727", "ink": "#1E293B", "slate": "#64748B", "muted": "#475569",
    "line": "#E2E8F0", "page": "#F8FAFC",
    "thead": "#991B1B", "rule": "#B91C1C", "chip": "#DC2626",
    "crit": "#BE123C", "high": "#E11D48", "med": "#D97706", "low": "#2563EB",
    "tint_red": "#FEE2E2", "tint_amber": "#FEF3C7", "tint_blue": "#EFF6FF",
}

BADGE_TEXT = {"critical": "КРИТИЧЕСКИЙ", "high": "ВЫСОКИЙ", "medium": "СРЕДНИЙ", "low": "УМЕРЕННЫЙ"}
BADGE_BG = {"critical": PALETTE["crit"], "high": PALETTE["high"], "medium": PALETTE["med"], "low": PALETTE["low"]}
TINT_BG = {"critical": PALETTE["tint_red"], "high": PALETTE["tint_red"], "medium": PALETTE["tint_amber"], "low": PALETTE["tint_blue"]}

# ---- цветовые темы по уровню риска -----------------------------------------
# Ключ соответствует a["level"]. Меняет цвет шапки, плашек секций, заголовков
# таблиц и рамки заключения, чтобы документ «не кричал» красным у спокойных
# компаний.
THEMES = {
    "critical": {"grad_end": "#7F1D1D", "thead": "#991B1B", "rule": "#B91C1C", "chip": "#DC2626", "verdict": "#B91C1C", "vbg": "#FEF2F2"},
    "high":     {"grad_end": "#991B1B", "thead": "#991B1B", "rule": "#B91C1C", "chip": "#DC2626", "verdict": "#B91C1C", "vbg": "#FEF2F2"},
    "medium":   {"grad_end": "#B45309", "thead": "#B45309", "rule": "#D97706", "chip": "#D97706", "verdict": "#B45309", "vbg": "#FFFBEB"},
    "low":      {"grad_end": "#047857", "thead": "#047857", "rule": "#059669", "chip": "#059669", "verdict": "#047857", "vbg": "#ECFDF5"},
    "unknown":  {"grad_end": "#334155", "thead": "#334155", "rule": "#475569", "chip": "#475569", "verdict": "#334155", "vbg": "#F1F5F9"},
}

MONTHS_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def theme_of(a: dict) -> dict:
    return THEMES.get(a.get("level") or "high", THEMES["high"])


def fmt_date_ru(s: str) -> str:
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s or "")
    if m:
        return f"{int(m.group(1))} {MONTHS_GEN.get(int(m.group(2)), '')} {m.group(3)}".strip()
    return s or ""


def esc(t: Any) -> str:
    return html_mod.escape(str(t or ""))


def badge_html(level: str) -> str:
    return (f'<span class="badge badge-{level}">{BADGE_TEXT[level]}</span>')


def kpi_card(title: str, value: str, sub: str, tint: str) -> str:
    return (
        f'<div class="kpi">'
        f'<div class="kpi-label">{esc(title)}</div>'
        f'<div class="kpi-value">{esc(value)}</div>'
        f'<div class="kpi-sub {"kpi-sub-" + tint}">{esc(sub)}</div>'
        f'</div>'
    )


def render_html(d: dict, theme: dict | None = None) -> str:
    m, a, dt = d["company"], d["analytics"], d["date"]
    P = PALETTE
    c = lambda k: P[k]
    theme = theme or theme_of(a)
    ai = d.get("ai") or {}
    ai_flags = ai.get("flags") or {}

    # ---- KPI
    lt = a["lt"]
    rev_txt = fmt_rub(d.get("analytics", {}).get("lv"))
    rev_sub = (f"Год {lt}" if lt else "нет данных") + (f", рост в {fmt_space(a['jump'] + 1)} раз" if a["jump"] and a["jump"] >= 3 else "")
    if a["jump"] and a["jump"] >= 3:
        rev_sub = f"Аномальный скачок (+{fmt_space(a['jump'] * 100)}%)"
    rev_tint = "warn" if (a.get("jump") or 0) >= 3 else "info"

    tax_txt = fmt_rub(a["tax_lv"])
    tax_sub = (f"{fmt_pct(a['burden'])} выручки" + (" (Налоговый разрыв)" if a["burden"] is not None and a["burden"] < 1 and not m["spec_regime"] else "")) if a["burden"] is not None else "нет данных"
    tax_tint = "danger" if a["burden"] is not None and a["burden"] < 1 else "warn"

    contrib_txt = (fmt_rub(round(a["contrib_lv"])) + " / год") if a["contrib_lv"] is not None else "—"
    if m["headcount"] and a["monthly"]:
        contrib_sub = f"{m['headcount']} сотр. / ~{fmt_space(a['monthly'])} ₽/мес на чел."
    elif m["headcount"]:
        contrib_sub = f"{m['headcount']} сотр."
    else:
        contrib_sub = "нет данных"
    contrib_tint = "danger" if a["monthly"] is not None and a["monthly"] < 2_000 else "warn"

    addr_txt = f"{m['mass_addr']} {plural(m['mass_addr'], 'юрлицо', 'юрлица', 'юрлиц')}" if m["mass_addr"] else "не массовый"
    addr_sub = short_addr(m["address"]) if m["address"] else "—"
    addr_tint = "warn" if m["mass_addr"] >= 50 else "info"

    # ---- финансовая таблица
    def dyn_row(series):
        cur, cur_y = None, None
        for y in sorted(series):
            if series[y] is not None:
                cur, cur_y = series[y], y
        return cur, cur_y

    def dyn_rev():
        if a["jump"] and a["jump"] >= 3:
            return f"Аномальный скачок в {fmt_space(a['jump'] + 1)} раз (+{fmt_space(a['jump'] * 100)}%)"
        cur, cur_y = None, None
        for y in sorted(m["rev"]):
            if m["rev"][y] is not None:
                cur, cur_y = m["rev"][y], y
        if cur is None:
            return "—"
        prev = None
        for y in range(cur_y - 1, cur_y - 8, -1):
            if m["rev"].get(y) is not None:
                prev = m["rev"][y]
                break
        if prev:
            ch = (cur - prev) / prev * 100
            return f"{'Рост' if ch >= 0 else 'Спад'} {fmt_pct(abs(ch))}"
        return "—"

    def dyn_profit():
        cur, cur_y = None, None
        for y in sorted(m["profit"]):
            if m["profit"][y] is not None:
                cur, cur_y = m["profit"][y], y
        if cur is None:
            return "—"
        r = m["rev"].get(cur_y)
        marg = cur / r * 100 if r and r > 0 else 0
        return f"Рентабельность ≈ {fmt_pct(marg)}"

    def dyn_na():
        cur, cur_y = None, None
        for y in sorted(m["net_assets"]):
            if m["net_assets"][y] is not None:
                cur, cur_y = m["net_assets"][y], y
        if cur is None:
            return "—"
        r = m["rev"].get(cur_y)
        if r and cur < 0.4 * r:
            return "Отсутствие прироста капитала"
        return "—"

    def dyn_tax():
        _, cur_y = dyn_row(m["taxes_by_year"])
        if cur_y is None:
            return "—"
        v, r = m["taxes_by_year"][cur_y], m["rev"].get(cur_y)
        if not r:
            return "—"
        p = v / r * 100
        return f"{fmt_pct(p)} от выручки" + (" (критически)" if p < 1 else "")

    def dyn_contrib():
        _, cur_y = dyn_row(m["contrib_by_year"])
        if cur_y is None:
            return "—"
        v, hc = m["contrib_by_year"][cur_y], m["headcount"]
        if hc:
            return f"≈ {fmt_space(v*1000/hc/12)} ₽/мес на 1 сотрудника"
        return "—"

    def dyn_head():
        return f"{m['headcount']} {plural(m['headcount'],'человек','человека','человек')}" if m["headcount"] else "—"

    def fin_row(name, code, cells, dyn):
        tds = "".join(f'<td class="fy {"muted" if str(x) in ("—",) else ""}">{x}</td>' for x in cells)
        cspan = f' <span class="fcode">(код {code})</span>' if code else ""
        return f'<tr><td class="fm">{esc(name)}{cspan}</td>{tds}<td class="fd">{esc(dyn)}</td></tr>'

    years = [2023, 2024, 2025]
    fin_rows = []
    fin_rows.append(fin_row("Выручка", "2110", [fmt_rub(m["rev"].get(y)) for y in years], dyn_rev()))
    fin_rows.append(fin_row("Чистая прибыль", "2400", [fmt_rub(m["profit"].get(y)) for y in years], dyn_profit()))
    fin_rows.append(fin_row("Стоимость чистых активов", "", [fmt_rub(m["net_assets"].get(y)) for y in years], dyn_na()))
    fin_rows.append(fin_row("Уплаченные налоги (всего)", "", [fmt_rub(m["taxes_by_year"].get(y)) for y in years], dyn_tax()))
    fin_rows.append(fin_row("Страховые взносы", "", [fmt_rub(m["contrib_by_year"].get(y)) for y in years], dyn_contrib()))
    fin_rows.append(fin_row("Численность персонала", "", [dyn_head() if y == lt else "—" for y in years], "—"))

    # ---- регистрационные параметры
    def reg_row(label, value, strong=False):
        return f'<tr><td class="rk">{esc(label)}</td><td class="rv {"strong" if strong else ""}">{value}</td></tr>'

    kpp_txt = m["kpp"] or "—"
    reg_date_ru = fmt_date_ru(m["reg_date"]) if m["reg_date"] else ""
    age_txt = ""
    m_age = re.search(r"(\d{4})", m["reg_date"] or "")
    if m_age:
        yrs = date.today().year - int(m_age.group(1))
        if yrs > 0:
            age_txt = f" (действует {yrs}+ {plural(yrs, 'год', 'года', 'лет')})"
    reg_html = (
        reg_row("Полное наименование", esc(m["raw_name"] or m["name"]), strong=True)
        + reg_row("ОГРН / Дата регистрации", f"{esc(m['ogrn'] or '—')} от {esc(reg_date_ru or '—')} г.{esc(age_txt)}")
        + reg_row("ИНН / КПП", f"{esc(m['inn'] or '—')} / {esc(kpp_txt)}")
        + reg_row("Налоговый орган", esc(m["tax_org"] or "—"))
        + reg_row("Юридический адрес", esc(m["address"] or "—") + (f" <span class='rg'>массовый адрес: {m['mass_addr']} {plural(m['mass_addr'], 'юрлицо', 'юрлица', 'юрлиц')}</span>" if m["mass_addr"] else ""))
        + reg_row("Уставный капитал", esc(m["capital"] or "—"))
        + reg_row("Реестр МСП", esc(m["msp"] or "—") )
        + (reg_row("Отрасль", esc(m["industry"])) if m.get("industry") else "")
        + reg_row("Основной вид деятельности", esc(m["okved_main"] or "—"))
        + reg_row("Доп. коды ОКВЭД", (f"всего {m['okved_count']}" + (": " + ", ".join(esc(c) for c in m["okved_add"]) if m.get("okved_add") else " видов деятельности")) if (m["okved_count"] or 0) else "—")
        + reg_row("Лицензии / Филиалы", "Сведения о лицензиях и филиалах не выявлены.")
        + reg_row("Должность руководителя", esc(m["ceo"] or "—"))
    )

    # ---- founders
    def founder_rows(group):
        rows = ""
        for f_ in group:
            role = "Генеральный директор и единственный участник" if f_.get("share") and "100%" in f_.get("share") else "Участник (совладелец)"
            share = f_.get("share") or "—"
            conn = []
            if f_.get("role_orgs"):
                conn.append(f_.get("role_orgs"))
            if f_.get("founder_orgs"):
                conn.append("учредитель: " + f_.get("founder_orgs"))
            conn_txt = "; ".join(conn)
            if f_.get("region"):
                conn_txt += (" " + f_["region"]) if conn_txt else f_["region"]
            rows += (
                f"<tr><td class='fm'>{esc(role)}</td>"
                f"<td>{esc(f_['name'])}<span class='fcode'>{'ИНН ' + esc(f_['inn']) if f_.get('inn') else ''}</span></td>"
                f"<td>{esc(share)}</td>"
                f"<td class='fl'>{esc(conn_txt or '—')}</td></tr>"
            )
        return rows

    hdr_row = '<tr><th>Должность / Роль</th><th>ФИО / Данные</th><th>Доля / ИНН</th><th>Связи и оценка профиля</th></tr>'
    founders_html = ""
    if m["founders"].get("actual"):
        founders_html += f"<table class='t founders'><thead>{hdr_row}</thead><tbody>{founder_rows(m['founders']['actual'])}</tbody></table>"
    if m["founders"].get("historical"):
        founders_html += (
            f"<div class='subhead'>Исторические участники (экс)</div>"
            f"<table class='t founders'><thead>{hdr_row}</thead><tbody>{founder_rows(m['founders']['historical'])}</tbody></table>"
        )
    if not founders_html:
        founders_html = "<p class='muted'>Данные об участниках не найдены.</p>"

    # ---- risk items
    risk_html = ""
    for f_ in a["flags"]:
        lvl = f_["badge"]
        note = ai_flags.get(f_.get("num")) or {}
        essence = note.get("essence") or f_["essence"]
        risk_txt = note.get("risk") or f_["risk"]
        risk_html += (
            f'<div class="risk">'
            f'<div class="risk-head"><div class="risk-title">{f_["num"]}. {esc(f_["title"])}</div>'
            f'{badge_html(lvl)}</div>'
            f'<p><b>Суть фактора:</b> {esc(essence)}</p>'
            f'<p><b>Риск:</b> {esc(risk_txt)}</p>'
            f'</div>'
        )
    if not risk_html:
        risk_html = '<p class="muted">Существенных «красных флагов» не выявлено.</p>'

    # ---- аннотация / verdict
    n_crit = a["counts"].get("critical", 0)
    n_high = a["counts"].get("high", 0)
    n_med = a["counts"].get("medium", 0)
    vc = a["verdict"]

    if vc == "ВЫСОКИЙ РИСК":
        verdict_body = (
            "не рекомендуется для заключения крупных контрактов и перечисления предоплат ввиду критических "
            "признаков технической / транзитной структуры. Сотрудничество допустимо только по схеме постоплаты "
            "при условии полной документальной верификации реальности оказываемых услуг."
        )
    elif vc == "СРЕДНИЙ РИСК":
        verdict_body = (
            "допускается к сотрудничеству при соблюдении контрольных требований проверки. Рекомендуется "
            "проверка по месту нахождения и мониторинг налоговой отчётности."
        )
    elif vc == "НЕДОСТАТОЧНО ДАННЫХ":
        verdict_body = (
            "итоговая оценка благонадёжности НЕ МОЖЕТ быть дана по открытым источникам: ключевая отчётность "
            "(финансовая, налоговая, кадровая) отсутствует в публичном доступе. Ограниченность данных не является "
            "подтверждением низкого риска. Перед сделкой необходимо запросить первичные документы, справку об "
            "исполнении обязанности по уплате налогов (КНД 1120101) и провести проверку по месту нахождения."
        )
    else:
        verdict_body = (
            "допускается к сотрудничеству. Рекомендуется стандартный контроль документов и периодический мониторинг."
        )

    # ИИ может переписать заключение (с сохранением фактов)
    if ai.get("verdict"):
        verdict_body = ai["verdict"]

    name_b = f"<b>{esc(m['raw_name'] or m['name'])}</b>"


    # абзац 1 — профиль компании (AI-описание со «Сводки», иначе — базовые факты)
    if m.get("description"):
        para1 = esc(m["description"])
    else:
        para1 = f"Компания {name_b}"
        if m["reg_date"]:
            para1 += f" действует с {esc(fmt_date_ru(m['reg_date']))} г."
        para1 += "."

    # абзац 2 — результаты комплексной проверки
    if a["flags"]:
        nf = len(a["flags"])
        para2 = (
            f"По результатам комплексной проверки выявлено {nf} "
            f"{plural(nf, 'фактор риска', 'фактора риска', 'факторов риска')}"
        )
        labs = []
        if n_crit:
            labs.append(f"{n_crit} {plural(n_crit,'критический','критических','критических')}")
        if n_high:
            labs.append(f"{n_high} {plural(n_high,'высокий','высоких','высоких')}")
        if n_med:
            labs.append(f"{n_med} {plural(n_med,'средний','средних','средних')}")
        if labs:
            para2 += " (" + ", ".join(labs) + ")"
        para2 += "."
        para2 += (
            " Профиль близок к «транзитному контрагенту»."
            if vc in ("ВЫСОКИЙ РИСК", "СРЕДНИЙ РИСК")
            else " Профиль в целом благоприятный, но требует подтверждения задекларированных показателей."
        )
    else:
        para2 = "Выраженных негативных маркеров не выявлено. Рекомендуется стандартная процедура проверки."

    bullet_html = "".join(
        f"<li><b>{esc(x['title'])}.</b> {esc((ai_flags.get(x.get('num')) or {}).get('essence') or x['essence'])}</li>"
        for x in a["flags"]
    )
    bullets = f"<ul>{bullet_html}</ul>" if a["flags"] else ""
    vline = f'<p class="vline"><b>Вердикт:</b> {esc(verdict_body[:1].upper() + verdict_body[1:])}</p>'

    # ---- оговорка о неполноте данных
    missing = a.get("missing_blocks", [])
    data_note = ""
    if missing:
        miss_txt = ", ".join(missing)
        if a.get("data_limited"):
            tail = (
                "Финансовая и налоговая состоятельность контрагента по публичным данным не проверяется, "
                "поэтому отсутствие выявленных «красных флагов» НЕ является подтверждением низкого уровня риска."
            )
        else:
            tail = (
                "Соответствующие направления оценки ограничены и подлежат подтверждению первичными документами."
            )
        data_note = (
            f'<div class="note"><b>Ограниченность исходных данных.</b> '
            f'В открытых источниках отсутствуют сведения по разделам: {esc(miss_txt)}. {tail} '
            f'Перед принятием решения необходимо запросить отчётность и подтверждающие документы напрямую у контрагента.</div>'
        )

    summary_html = (
        f'<p class="lead">Ключевой аналитический вывод:</p>'
        f'<p>{para1}</p>'
        f'<p>{para2}</p>'
        f'{data_note}'
        f'{bullets}{vline}'
    )

    # ИИ может заменить резюме на отредактированную версию
    if ai.get("executive_summary"):
        paras = [p.strip() for p in re.split(r"\n{2,}", ai["executive_summary"]) if p.strip()]
        body = "".join(f"<p>{esc(p)}</p>" for p in paras)
        summary_html = (
            f'<p class="lead">Ключевой аналитический вывод:</p>'
            f'{body}'
            f'{data_note}'
            f'{bullets}{vline}'
        )

    # ---- экспресс-проверка по реестрам
    registry_html = ""
    if m.get("registry_checks"):
        rrows = ""
        for rc in m["registry_checks"]:
            color = "#047857" if rc["ok"] else "#B91C1C"
            mark = "✔" if rc["ok"] else "✘"
            rrows += (
                f"<tr><td class='rk'>{esc(rc['name'])}</td>"
                f"<td class='rv'><span style='color:{color};font-weight:700'>{mark} {esc(rc['status'])}</span></td></tr>"
            )
        registry_html = (
            "<div class='subhead'>Экспресс-проверка по реестрам и открытым источникам</div>"
            f"<table class='t reg reestr'>{rrows}</table>"
        )

    # рекоммендации
    rec_rows = ""
    rec_hdr = '<tr><th>Направление проверки</th><th>Обязательные действия</th><th>Критерий допуска к сделке</th></tr>'
    rec = [
        ("Реальность хозяйственной деятельности",
         "Провести физический осмотр офиса/производства, складов и оборудования.",
         "Подтверждение фактического нахождения персонала и оборудования."),
        ("Финансовая чистота",
         "Запросить справку об исполнении обязанности по уплате налогов (КНД 1120101) и книги покупок/продаж.",
         "Отсутствие скрытых налоговых долгов и сомнительных контрагентов в цепочке."),
        ("Условия расчётов",
         ("Категорически исключить авансирование и предоплаты." if vc == "ВЫСОКИЙ РИСК" else "Оценить целесообразность авансирования; отдать предпочтение постоплате."),
         "Расчёт строго по факту поставки и подписания товарных накладных / УПД."),
        ("Подписание договоров",
         f"Личная идентификация руководителя ({esc(m['ceo'] or '—')}) при подписании документов.",
         "Подписание лично руководителем в присутствии представителя заказчика."),
    ]
    for a_, b_, c_ in rec:
        rec_rows += f"<tr><td class='fm'>{esc(a_)}</td><td>{esc(b_)}</td><td>{esc(c_)}</td></tr>"

    # ИИ может предложить свои контрольные требования
    if ai.get("recommendations"):
        rec_rows = ""
        for r_ in ai["recommendations"]:
            rec_rows += (
                f"<tr><td class='fm'>{esc(r_['direction'])}</td>"
                f"<td>{esc(r_['actions'])}</td>"
                f"<td>{esc(r_['criteria'])}</td></tr>"
            )

    name_esc = esc(m['name']).replace('&quot;', '"').replace('&#34;', '"')
    verdict_body_esc = esc(verdict_body).replace('&quot;', '"').replace('&#34;', '"').replace('&amp;quot;', '"')
    verdict_txt = f"Итоговое заключение Службы Безопасности: {name_esc} {verdict_body_esc}"

    # ---- верхняя шапка
    risk_lbl = {
        "ВЫСОКИЙ РИСК": ("ВЫСОКИЙ РИСК", theme["chip"]),
        "СРЕДНИЙ РИСК": ("СРЕДНИЙ РИСК", theme["chip"]),
        "НИЗКИЙ РИСК": ("НИЗКИЙ РИСК", theme["chip"]),
        "НЕДОСТАТОЧНО ДАННЫХ": ("НЕДОСТАТОЧНО ДАННЫХ", theme["chip"]),
    }[vc]

    meta_html = (
        f"ОГРН: {esc(m['ogrn'] or '—')} <span class='hsep'>|</span> "
        f"ИНН: {esc(m['inn'] or '—')} <span class='hsep'>|</span> "
        f"КПП: {esc(kpp_txt)} <span class='hsep'>|</span> "
        f"Юрисдикция: РФ ({esc(m['region'] or '—')})"
    )

    title_ru = "Аналитическое досье Due Diligence / Проверка контрагента"
    name_disp = (m["raw_name"] or m["name"])


    # -- KPI row
    kpis = (
        kpi_card(f"Оборот {lt} г." if lt else "Оборот", rev_txt, rev_sub, rev_tint)
        + kpi_card("Налоговая отдача", tax_txt, tax_sub, tax_tint)
        + kpi_card("ФОТ и взносы", contrib_txt, contrib_sub, contrib_tint)
        + kpi_card("Массовый адрес", addr_txt, addr_sub, addr_tint)
    )

    head_style = f"background:linear-gradient(90deg, {P['navy']} 0%, {theme['grad_end']} 100%);"

    summary_h2 = ("1", "Резюме комплексной проверки", "Executive summary")
    reg_h2 = ("2", "Регистрационные параметры и реквизиты", "")
    own_h2 = ("3", "Структура владения, руководство и аффилированность", "")
    fin_h2 = ("4", "Детальный финансовый анализ и динамика показателей", "")
    risk_h2 = ("5", "Карта рисков и детальные зоны внимания", "Red flags")
    rec_h2 = ("6", "Рекомендации и контрольные требования для СБ и юристов", "")

    def sect_h(num, title, en):
        en_html = f' <span class="en">({esc(en)})</span>' if en else ""
        return f'<h2 class="section"><span class="num">{num}.</span><span class="t">{esc(title)}</span>{en_html}</h2>'

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>{esc(name_disp)} — Due Diligence</title>
<style>{themed_css(theme)}</style></head>
<body>

<div class="header" style="{head_style}">
  <div class="hdr-row hdr-top">
    <div class="doc-title">{esc(title_ru)}</div>
    <div class="risk-chip" style="background:{risk_lbl[1]}">{esc(risk_lbl[0])}</div>
  </div>
  <div class="hdr-row hdr-mid">
    <div class="h-name">{esc(name_disp)}</div>
    <div class="h-date">Дата анализа: {esc(dt['month'])} {esc(dt['year'])} г.</div>
  </div>
  <div class="h-meta">{meta_html}</div>
</div>

<div class="kpis">{kpis}</div>

{sect_h(*summary_h2)}
<div class="summary">
{summary_html}
</div>

{sect_h(*reg_h2)}
<table class="t reg">{reg_html}</table>

{sect_h(*own_h2)}
{founders_html}

{sect_h(*fin_h2)}
<table class="t fin">
  <thead><tr><th>Показатель</th><th>2023 г.</th><th>2024 г.</th><th>2025 г.</th><th>Динамика и маркеры</th></tr></thead>
  <tbody>{"".join(fin_rows)}</tbody>
</table>

{sect_h(*risk_h2)}
{risk_html}
{registry_html}

{sect_h(*rec_h2)}
<table class="t rec">
  <thead>{rec_hdr}</thead>
  <tbody>{rec_rows}</tbody>
</table>

<div class="verdict"><span class="vlabel">{esc(verdict_txt.split(':')[0])}:</span>{esc(verdict_txt.split(':', 1)[1] if ':' in verdict_txt else verdict_txt)}</div>

<div class="foot-space"></div>
</body></html>"""
    return html


STYLE_CSS = """
@page {
  size: A4;
  margin: 12mm 12mm 15mm 12mm;
  @bottom-left {
    content: "КОНФИДЕНЦИАЛЬНО | СЛУЖБА БЕЗОПАСНОСТИ И КОМПЛАЕНС-КОНТРОЛЯ";
    font-family: "Noto Sans", sans-serif;
    font-size: 8pt;
    color: #64748B;
    border-top: 0.8pt solid #E2E8F0;
    padding-top: 4pt;
    width: 330pt;
  }
  @bottom-right {
    content: "Стр. " counter(page) " из " counter(pages);
    font-family: "Noto Sans", sans-serif;
    font-size: 8pt;
    color: #64748B;
    border-top: 0.8pt solid #E2E8F0;
    padding-top: 4pt;
    width: 80pt;
    text-align: right;
  }
}
* { box-sizing: border-box; }
body {
  font-family: "Noto Sans", "DejaVu Sans", sans-serif;
  font-size: 10.5pt;
  color: #1E293B;
  background: #F8FAFC;
  line-height: 1.55;
  margin: 0;
}
p { margin: 0 0 8pt; text-align: justify; }
ul { margin: 6pt 0 0 14pt; padding: 0; }
ul li { margin: 2pt 0; list-style: disc; }
.muted { color: #64748B; }
b { font-weight: 700; }

/* ---- шапка ---- */
.header {
  color: #fff;
  border-radius: 10pt;
  padding: 15pt 20pt 13pt;
}
.hdr-row { display: flex; justify-content: space-between; align-items: baseline; gap: 16pt; }
.hdr-top { align-items: center; }
.doc-title {
  flex: 1 1 auto;
  min-width: 0;
  font-weight: 600;
  font-size: 8.5pt;
  letter-spacing: 1.8px;
  text-transform: uppercase;
  margin: 0 0 6pt;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: clip;
  color: #fff;
}
.h-name { flex: 1 1 auto; min-width: 0; font-weight: 900; font-size: 19pt; line-height: 1.15; margin: 0 0 7pt; color: #fff; }
.h-meta { font-size: 9.8pt; color: #F1F5F9; }
.h-meta .hsep { color: rgba(255,255,255,0.45); margin: 0 6pt; }
.risk-chip {
  flex: 0 0 auto;
  color: #fff;
  font-weight: 900;
  font-size: 12pt;
  letter-spacing: 1px;
  padding: 6pt 15pt;
  border-radius: 999pt;
  white-space: nowrap;
  text-transform: uppercase;
}
.h-date { flex: 0 0 auto; font-size: 9.8pt; color: #F1F5F9; white-space: nowrap; }

/* ---- KPI ---- */
.kpis { display: flex; gap: 12pt; margin: 14pt 0 4pt; }
.kpi { flex: 1 1 0; min-width: 0; }
.kpi-label { font-weight: 700; font-size: 9.5pt; letter-spacing: 0.6px; color: #64748B; margin-bottom: 3pt; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-transform: uppercase; }
.kpi-value { font-weight: 900; font-size: 14pt; color: #0F1727; white-space: nowrap; }
.kpi-sub {
  display: inline-block;
  margin-top: 6pt;
  padding: 2.5pt 8pt;
  border-radius: 999pt;
  font-size: 8.6pt;
  font-weight: 600;
  color: #475569;
  line-height: 1.4;
}
.kpi-sub-danger { background: #FEE2E2; color: #7F1D1D; }
.kpi-sub-warn { background: #FEF3C7; color: #78350F; }
.kpi-sub-info { background: #EFF6FF; color: #1E40AF; }

/* ---- секции ---- */
.section {
  font-weight: 900;
  font-size: 12.5pt;
  letter-spacing: 0.2px;
  color: #0F1727;
  text-transform: uppercase;
  border-bottom: 2pt solid §RULE§;
  padding-bottom: 5pt;
  margin: 20pt 0 10pt;
  line-height: 1.2;
  page-break-after: avoid;
}
.section .num { margin-right: 5pt; }
.section .en {
  font-style: normal;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: #64748B;
  font-size: 8.5pt;
  margin-left: 6pt;
}
.subhead { font-weight: 700; color: #475569; margin: 10pt 0 5pt; font-size: 10.5pt; }

/* ---- таблицы ---- */
.t { width: 100%; border-collapse: collapse; background: #fff; }
.t thead th {
  background: §THEAD§;
  color: #fff;
  font-weight: 700;
  font-size: 9.6pt;
  text-align: left;
  padding: 6pt 8pt;
  border: none;
}
.t tbody td {
  border-bottom: 1pt solid #F1F5F9;
  padding: 5.5pt 8pt;
  font-size: 9.9pt;
  vertical-align: top;
  color: #1E293B;
}
.t tbody tr:nth-child(even) { background: #F8FAFC; }
.t .fm { font-weight: 700; color: #0F1727; }
.t .fcode { display: block; font-weight: 400; font-size: 8pt; color: #94A3B8; }
.t .fl { color: #475569; font-size: 9.2pt; }

/* registration */
.t.reg .rk { color: #64748B; font-weight: 600; width: 210pt; }
.t.reg .rv.strong { font-weight: 700; color: #0F1727; }
.t.reg .rg { color: #B45309; font-weight: 700; }
.t.reestr { page-break-inside: avoid; }
.t.reestr .rk { width: 260pt; }

/* finance */
.t.fin td { text-align: right; }
.t.fin td.fm, .t.fin td.fd { text-align: left; }
.t.fin .fm .fcode { display: inline; }
.t.fin .fy { color: #0F1727; font-weight: 600; white-space: nowrap; }
.t.fin .fy.muted { color: #94A3B8; }
.t.fin .fd { color: #B45309; font-size: 9.2pt; font-weight: 600; }

/* founders */
.t.founders td:nth-child(2) { min-width: 130pt; }
.t.founders td:nth-child(3) { white-space: nowrap; }

/* ---- risk cards ---- */
.risk {
  background: #fff;
  border: 1pt solid #E2E8F0;
  border-radius: 7pt;
  padding: 9pt 12pt;
  margin: 0 0 9pt;
  page-break-inside: avoid;
}
.risk-head { display: flex; justify-content: space-between; align-items: center; gap: 8pt; margin-bottom: 5pt; }
.risk-title { font-weight: 900; font-size: 11pt; color: #0F1727; }
.badge {
  color: #fff;
  font-weight: 900;
  font-size: 8.8pt;
  letter-spacing: 0.6px;
  padding: 3pt 9pt;
  border-radius: 999pt;
  white-space: nowrap;
}
.badge-critical { background: #BE123C; }
.badge-high { background: #E11D48; }
.badge-medium { background: #D97706; }
.badge-low { background: #2563EB; }

/* summary */
.summary { background: #fff; border: 1pt solid #E2E8F0; border-radius: 7pt; padding: 10pt 13pt; }
.summary .lead { font-weight: 700; color: #0F1727; margin-bottom: 3pt; }
.summary .vline { margin-top: 8pt; margin-bottom: 0; color: #0F1727; }

/* data-completeness note */
.note {
  background: #F8FAFC;
  border: 1pt solid #CBD5E1;
  border-left: 3pt solid #475569;
  border-radius: 5pt;
  padding: 7pt 10pt;
  margin: 7pt 0;
  font-size: 9.8pt;
  color: #334155;
  page-break-inside: avoid;
}

/* verdict */
.verdict {
  background: §VBG§;
  border: 1pt solid §RULE§;
  border-radius: 7pt;
  padding: 10pt 13pt;
  margin-top: 10pt;
  font-size: 10.4pt;
  line-height: 1.6;
  color: #1E293B;
  page-break-inside: avoid;
}
.verdict .vlabel { font-weight: 900; color: §RULE§; }
.foot-space { height: 4pt; }
"""


def themed_css(theme: dict) -> str:
    return (STYLE_CSS
            .replace("§THEAD§", theme["thead"])
            .replace("§RULE§", theme["rule"])
            .replace("§VBG§", theme["vbg"]))


# --------------------------------------------------------------------- ai
#
# Необязательный слой «улучшения нейросетью». Получает уже готовый dict `data`
# (company + analytics) и просит модель переписать текстовую часть досье:
# резюме, формулировки факторов и рекомендации. Результат кладётся в data["ai"]
# и подмешивается в render_html(). Любая ошибка/таймаут -> стандартный вывод.

AI_CONFIG_NAME = "ai_config.json"
DEFAULT_AI_CONFIG = os.path.join(SCRIPT_DIR, AI_CONFIG_NAME)

AI_DEFAULTS = {
    "enabled": True,
    "provider": "opencode",
    # OpenAI-совместимый endpoint. Для смены провайдера достаточно поменять
    # base_url + model + api_key (Gemini/OpenAI/Groq и т.п. — те же /chat/completions).
    "base_url": "https://opencode.ai/inference/v1/chat/completions",
    "api_key": "",
    "model": "mimo-v2.6-flash-free",
    "timeout": 90,
    "temperature": 0.4,
    "max_tokens": 4000,
}

AI_SYSTEM_PROMPT = (
    "Ты — старший аналитик службы экономической безопасности и комплаенс-контроля. "
    "На вход подаётся JSON с фактами о компании и уже найденными автоматикой факторами риска. "
    "Задача — улучшить текстовую часть досье: сделать резюме и формулировки профессиональными, "
    "точными и обдуманными, без «воды». СТРОГО ЗАПРЕЩЕНО придумывать факты, числа, названия и "
    "события, которых нет во входных данных. Используй только вход. Если каких-то данных нет — "
    "прямо скажи об этом. Не используй markdown. Отвечай ТОЛЬКО валидным JSON по заданной схеме, "
    "без пояснений и без обрамляющих ```. Язык ответа — русский."
)


def load_ai_config(path: str | None = None, overrides: dict | None = None) -> dict:
    """Собирает конфиг ИИ: defaults < ai_config.json < переменные окружения < CLI."""
    cfg = dict(AI_DEFAULTS)
    path = path or DEFAULT_AI_CONFIG
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg.update(json.load(fh) or {})
        except Exception as exc:
            print(f"  ! не удалось прочитать {path}: {exc}", file=sys.stderr)
    env_map = {
        "DOSSIER_AI_BASE_URL": "base_url",
        "DOSSIER_AI_API_KEY": "api_key",
        "DOSSIER_AI_MODEL": "model",
        "DOSSIER_AI_PROVIDER": "provider",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if os.environ.get("DOSSIER_AI_TIMEOUT"):
        try:
            cfg["timeout"] = float(os.environ["DOSSIER_AI_TIMEOUT"])
        except ValueError:
            pass
    for k, v in (overrides or {}).items():
        if v not in (None, ""):
            cfg[k] = v
    return cfg


def _ai_facts(data: dict) -> dict:
    """Компактный набор фактов для промпта (без служебных полей)."""
    m = data.get("company", {})
    a = data.get("analytics", {})
    return {
        "company": m,
        "analytics": {
            "verdict": a.get("verdict"),
            "level": a.get("level"),
            "counts": a.get("counts"),
            "burden_pct": a.get("burden"),
            "revenue_jump_ratio": a.get("jump"),
            "contrib_per_head_month_rub": a.get("monthly"),
            "data_limited": a.get("data_limited"),
            "missing_blocks": a.get("missing_blocks"),
            "flags": a.get("flags"),
        },
    }


def build_ai_prompt(data: dict) -> tuple[str, str]:
    schema = (
        "Верни JSON строго такой структуры:\n"
        "{\n"
        '  "executive_summary": "2-4 абзаца аналитического резюме; абзацы разделяй двойным '
        'переводом строки; опиши профиль, масштаб, ключевые отклонения и общий вывод; '
        'не пересказывай список факторов дословно",\n'
        '  "verdict": "1-2 предложения — итоговое заключение и рекомендация по сделке",\n'
        '  "flags": [{"num": <целое число из analytics.flags>, '
        '"essence": "улучшенная Суть фактора с числами из входа", '
        '"risk": "улучшенное пояснение Риска"}],\n'
        '  "recommendations": [{"direction": "...", "actions": "...", "criteria": "..."}]\n'
        "}\n"
        "Верни запись для КАЖДОГО фактора из analytics.flags (по его num). "
        "Рекомендации — 4 направления проверки."
    )
    facts = json.dumps(_ai_facts(data), ensure_ascii=False, default=str)
    user = f"{schema}\n\nДанные о компании (JSON):\n{facts}"
    return AI_SYSTEM_PROMPT, user


def _parse_ai_json(text: str) -> dict | None:
    """Достаёт JSON-объект из ответа модели (в т.ч. из ```-блока)."""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except ValueError:
            return None
    return None


def ai_chat(cfg: dict, system: str, user: str) -> str:
    """POST в OpenAI-совместимый /chat/completions. Бросает при ошибке/таймауте."""
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.get("temperature", 0.4),
        "max_tokens": cfg.get("max_tokens", 4000),
        "stream": False,
    }
    if isinstance(cfg.get("reasoning"), dict):
        payload["reasoning"] = cfg["reasoning"]
    r = requests.post(cfg["base_url"], headers=headers, json=payload,
                      timeout=cfg.get("timeout", 90))
    r.raise_for_status()
    obj = r.json()
    try:
        msg = obj["choices"][0]["message"]
    except Exception:
        return ""
    content = msg.get("content") or ""
    if not content:
        if msg.get("reasoning"):
            content = msg["reasoning"]
        if not content and msg.get("reasoning_details"):
            parts = []
            for d in msg["reasoning_details"]:
                if isinstance(d, dict) and d.get("text"):
                    parts.append(d["text"])
            content = "\n".join(parts)
    if not content:
        # fallback: dump whole message
        try:
            content = json.dumps(msg, ensure_ascii=False)
        except Exception:
            content = json.dumps(obj, ensure_ascii=False)
    return content


def _normalize_ai(obj: dict, data: dict) -> dict:
    """Валидирует ответ модели; нечисловые/пустые куски отбрасываются."""
    out: dict[str, Any] = {}
    if isinstance(obj.get("executive_summary"), str) and obj["executive_summary"].strip():
        out["executive_summary"] = obj["executive_summary"].strip()
    if isinstance(obj.get("verdict"), str) and obj["verdict"].strip():
        out["verdict"] = obj["verdict"].strip()
    valid_nums = {f["num"] for f in data.get("analytics", {}).get("flags", [])}
    notes = {}
    if isinstance(obj.get("flags"), list):
        for f in obj["flags"]:
            if not isinstance(f, dict):
                continue
            try:
                num = int(f.get("num"))
            except (TypeError, ValueError):
                continue
            if num not in valid_nums:
                continue
            note = {}
            if isinstance(f.get("essence"), str) and f["essence"].strip():
                note["essence"] = f["essence"].strip()
            if isinstance(f.get("risk"), str) and f["risk"].strip():
                note["risk"] = f["risk"].strip()
            if note:
                notes[num] = note
    if notes:
        out["flags"] = notes
    recs = []
    if isinstance(obj.get("recommendations"), list):
        for r_ in obj["recommendations"]:
            if not isinstance(r_, dict):
                continue
            d_ = str(r_.get("direction") or "").strip()
            a_ = str(r_.get("actions") or "").strip()
            c_ = str(r_.get("criteria") or "").strip()
            if d_ and a_ and c_:
                recs.append({"direction": d_, "actions": a_, "criteria": c_})
    if recs:
        out["recommendations"] = recs
    return out


def enrich_with_ai(data: dict, cfg: dict) -> bool:
    """Пытается улучшить data через ИИ. Никогда не бросает; при сбое — False."""
    if not cfg.get("enabled"):
        return False
    if not cfg.get("api_key"):
        print("  ! ИИ пропущен: не задан api_key", file=sys.stderr)
        return False
    try:
        system, user = build_ai_prompt(data)
        raw = ai_chat(cfg, system, user)
        parsed = _parse_ai_json(raw)
        if not parsed:
            print("  ! ИИ вернул неразборчивый ответ — стандартный вывод", file=sys.stderr)
            return False
        norm = _normalize_ai(parsed, data)
        if not norm:
            print("  ! ИИ не дал пригодных полей — стандартный вывод", file=sys.stderr)
            return False
        data["ai"] = norm
        data["ai_meta"] = {"provider": cfg.get("provider"), "model": cfg.get("model"), "ok": True}
        print(f"  ✓ ИИ: {cfg.get('provider')} / {cfg.get('model')}", file=sys.stderr)
        return True
    except Exception as exc:
        stderr = str(exc) or exc.__class__.__name__
        print(f"  ! ИИ недоступен ({stderr[:160]}) — используется стандартный вывод", file=sys.stderr)
        data["ai_meta"] = {"provider": cfg.get("provider"), "model": cfg.get("model"),
                           "ok": False, "error": stderr[:300]}
        return False


# --------------------------------------------------------------------- cli

def render_pdf(html: str, out: str, css_text: str = "") -> None:
    sheets = [CSS(string=css_text)] if css_text else None
    HTML(string=html).write_pdf(out, stylesheets=sheets)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description="Due Diligence PDF-досье по данным rusprofile.ru")
    p.add_argument("query", help="ИНН или ОГРН организации")
    p.add_argument("-o", "--out", default="", help="выходной PDF (по умолчанию dossier_<ИНН>.pdf)")
    p.add_argument("--html", action="store_true", help="сохранить ещё и HTML-версию")
    p.add_argument("--json", metavar="FILE", help="сохранить структуру данных в JSON")
    p.add_argument("--cookies", metavar="FILE", help="файл cookies (по умолчанию rusprofile_cookies.json)")
    p.add_argument("--acc", metavar="FILE", help="перелогиниться через браузер (login:/pass:)")
    p.add_argument("--delay", type=float, default=1.0, help="задержка между запросами, сек")
    p.add_argument("--no-pdf", action="store_true", help="только печать/JSON без генерации PDF")
    g = p.add_argument_group("нейросеть (улучшение выводных данных)")
    g.add_argument("--ai", dest="ai", action="store_true", default=None,
                   help="принудительно включить обработку нейросетью")
    g.add_argument("--no-ai", dest="ai", action="store_false",
                   help="отключить обработку нейросетью (стандартный вывод)")
    g.add_argument("--ai-config", metavar="FILE",
                   help=f"файл конфигурации ИИ (по умолчанию {AI_CONFIG_NAME})")
    g.add_argument("--ai-model", metavar="MODEL", help="модель (напр. mimo-v2.6-flash-free)")
    g.add_argument("--ai-base", metavar="URL", help="OpenAI-совместимый endpoint /chat/completions")
    g.add_argument("--ai-key", metavar="KEY", help="API-ключ провайдера (иначе из конфига/ENV)")
    g.add_argument("--ai-timeout", type=float, metavar="SEC", help="таймаут запроса к ИИ, сек")
    args = p.parse_args(argv)

    cookies_path = args.cookies or rs.DEFAULT_COOKIES
    if args.acc:
        if not rs.selenium_login(args.acc, cookies_path):
            print("Ошибка входа в аккаунт", file=sys.stderr)
            return 1

    scraper = rs.Rusprofile(delay=args.delay, cookies_path=cookies_path)
    print(f">>> {args.query}", file=sys.stderr)
    d = Dossier(scraper, args.query).collect()
    data = d.build()

    # --- необязательное улучшение нейросетью (при сбое — стандартный вывод)
    ai_cfg = load_ai_config(
        args.ai_config,
        {
            "base_url": args.ai_base,
            "api_key": args.ai_key,
            "model": args.ai_model,
            "timeout": args.ai_timeout,
        },
    )
    if args.ai is not None:
        ai_cfg["enabled"] = args.ai
    if ai_cfg.get("enabled"):
        enrich_with_ai(data, ai_cfg)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
        print(f"Сохранено: {args.json}", file=sys.stderr)

    html = render_html(data)
    if not args.out:
        key = (d.m.get("inn") or d.m.get("ogrn") or args.query)
        args.out = f"dossier_{key}.pdf"
    if args.html:
        open(args.out.rsplit(".pdf", 1)[0] + ".html", "w", encoding="utf-8").write(html)
    if args.no_pdf:
        print(html[:2000])
        return 0
    render_pdf(html, args.out)
    print(f"Готово: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
