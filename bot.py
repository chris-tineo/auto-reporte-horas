#!/usr/bin/env python3
"""
timesheet-bot — Rutina genérica multi-empresa para llenado de horas.

Autenticación por empresa:
  - "password": login desatendido con usuario/contraseña desde .env
  - "session":  reutiliza una sesión guardada (storageState) para empresas con MFA

Flujos de llenado (cfg["flow"]):
  - "simple" (default): un dropdown de proyecto + uno o varios campos de horas + save.
  - "modal_entries":    abre un modal por entrada, llena campos, "add to list" y al
                        final (o por día) hace "save". Las observaciones y los días
                        trabajados salen de week_note.txt.

Uso:
  python bot.py --company bertoni --login          # login manual (MFA) → guarda sesión
  python bot.py --company bertoni --dry-run        # llena el modal y captura, NO guarda
  python bot.py --company bertoni                  # semana ISO actual (lun-vie)
  python bot.py --company bertoni --month 2026-06  # todo junio
  python bot.py --company bertoni --week 2026-W26  # una semana ISO puntual
"""

import argparse
import calendar
import json
import os
import re
import sys
import glob
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = Path(__file__).parent
COMPANIES_DIR = BASE / "companies"
AUTH_DIR = BASE / "auth"
LOGS_DIR = BASE / "logs"
NOTE_FILE_DEFAULT = BASE / "week_note.txt"

load_dotenv(BASE / ".env")

# La consola de Windows (cp1252) no codifica acentos/símbolos del log → forzar UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logger(company: str) -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(company)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.FileHandler(LOGS_DIR / f"{company}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(company: str) -> dict:
    path = COMPANIES_DIR / f"{company}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No existe config: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_name"] = company
    return cfg


def list_active_companies() -> list[str]:
    names = []
    for path in glob.glob(str(COMPANIES_DIR / "*.yaml")):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if cfg.get("active", True):
            names.append(Path(path).stem)
    return sorted(names)


def list_companies_by_job(job: str) -> list[str]:
    names = []
    for path in glob.glob(str(COMPANIES_DIR / "*.yaml")):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if cfg.get("job") == job and cfg.get("active", True):
            names.append(Path(path).stem)
    return sorted(names)


def get_credential(company: str, kind: str) -> str:
    """Lee credenciales de variables de entorno: EMPRESA_A_USER / EMPRESA_A_PASS."""
    key = f"{company.upper()}_{kind.upper()}"
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Falta la variable de entorno {key} en .env")
    return val


# ---------------------------------------------------------------------------
# week_note.txt — observaciones y días trabajados por semana
# ---------------------------------------------------------------------------
def _parse_worked(spec: str) -> set[int]:
    """'Mon-Fri' / 'Mon,Wed,Fri' / 'none' -> set de weekday ints (Mon=0..Sun=6)."""
    spec = spec.strip().lower()
    if spec in ("none", "-", ""):
        return set()
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (p.strip() for p in part.split("-", 1))
            if a in WEEKDAYS and b in WEEKDAYS:
                for i in range(WEEKDAYS[a], WEEKDAYS[b] + 1):
                    out.add(i)
        elif part in WEEKDAYS:
            out.add(WEEKDAYS[part])
    return out


def parse_week_note(path: Path) -> dict:
    """Lee week_note.txt -> {company: {(year, isoweek): {'worked': set, 'obs': str}}}.

    Formato:
        [bertoni]
        week 2026-W23 | worked: Mon-Fri | obs: Texto de la semana
    """
    data: dict = {}
    if not path.exists():
        return data
    company = None
    line_re = re.compile(
        r"^week\s+(\d{4})-W(\d{1,2})\s*\|\s*worked:\s*([^|]*)\|\s*obs:\s*(.*)$",
        re.IGNORECASE,
    )
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            company = line[1:-1].strip().lower()
            data.setdefault(company, {})
            continue
        m = line_re.match(line)
        if m and company:
            year, week, worked, obs = m.groups()
            data[company][(int(year), int(week))] = {
                "worked": _parse_worked(worked),
                "obs": obs.strip(),
            }
    return data


def weeks_for(company: str, cfg: dict, note: dict) -> dict:
    """Días trabajados: sección [empresa] si existe, si no la del trabajo [job:<job>].
    Así un trabajo (varias empresas) se define una sola vez y no se desalinean."""
    if company in note:
        return note[company]
    job = cfg.get("job")
    return note.get(f"job:{job}", {}) if job else {}


def compute_targets(weeks: dict, start: date, end: date,
                    log: logging.Logger) -> list[tuple[date, str]]:
    """Lista (fecha, observación) para los días trabajados en [start, end]."""
    targets: list[tuple[date, str]] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # lun-vie
            y, w, _ = d.isocalendar()
            entry = weeks.get((y, w))
            if entry is None:
                log.warning(f"{d} (semana {y}-W{w}): sin info en week_note.txt → se omite.")
            elif d.weekday() in entry["worked"]:
                targets.append((d, entry["obs"]))
        d += timedelta(days=1)
    return targets


# ---------------------------------------------------------------------------
# Rango de fechas (scope CLI)
# ---------------------------------------------------------------------------
def scope_dates(month: str | None, week: str | None,
                frm: str | None = None, to: str | None = None) -> tuple[date, date]:
    if frm or to:  # rango explícito (o un solo día si solo se da uno)
        f = date.fromisoformat(frm or to)
        t = date.fromisoformat(to or frm)
        return f, t
    if month:
        y, m = (int(x) for x in month.split("-"))
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    if week:
        y, w = (int(x) for x in week.upper().split("-W"))
        monday = date.fromisocalendar(y, w, 1)
        return monday, monday + timedelta(days=4)
    # default: semana ISO actual (lun-vie)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def session_path(company: str) -> Path:
    return AUTH_DIR / f"{company}_state.json"


def do_password_login(page, cfg: dict, log: logging.Logger):
    company = cfg["_name"]
    log.info("Login con usuario/contraseña…")
    page.goto(cfg["url"], wait_until="domcontentloaded")

    # Login multi-paso (p. ej. BigTime: email → Next → user/pass → Login).
    login = cfg.get("login")
    if login and login.get("steps"):
        ctx = {"user": get_credential(company, "user"),
               "pass": get_credential(company, "pass")}
        for step in login["steps"]:
            if "wait_for" in step:
                page.wait_for_selector(step["wait_for"], timeout=20000)
            elif "fill" in step:
                val = str(step["value"])
                for k, v in ctx.items():
                    val = val.replace("${" + k + "}", v)
                page.fill(step["fill"], val)
            elif "click" in step:
                page.click(step["click"])
            page.wait_for_timeout(step.get("wait", 900))
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(3000)
        log.info("Login (multi-paso) enviado.")
        return

    # Login de un paso (selectores user/pass/submit).
    sel = cfg["selectors"]
    page.fill(sel["user"], get_credential(company, "user"))
    page.fill(sel["pass"], get_credential(company, "pass"))
    page.click(sel["submit"])
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001  (apps SAP no llegan a networkidle)
        page.wait_for_timeout(3000)
    log.info("Login enviado.")


def manual_login_flow(company: str):
    """Abre navegador visible para que el usuario haga login + MFA a mano.
    Guarda la sesión en auth/<company>_state.json para corridas futuras."""
    cfg = load_config(company)
    log = setup_logger(company)
    AUTH_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(cfg["url"], wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print(f"  LOGIN MANUAL — {company}")
        print("  Inicia sesión (y completa MFA) en la ventana del navegador.")
        print("  Cuando estés DENTRO del sistema, vuelve aquí y presiona ENTER.")
        print("=" * 60)
        input("  >> ENTER cuando termines de loguearte... ")

        context.storage_state(path=str(session_path(company)))
        log.info(f"Sesión guardada en {session_path(company)}")
        browser.close()


def _check_session(page, cfg: dict):
    if cfg.get("login_marker") and page.locator(cfg["login_marker"]).count() > 0:
        raise RuntimeError(
            "Parece que la sesión expiró (se detectó la pantalla de login). "
            f"Vuelve a correr: python bot.py --company {cfg['_name']} --login"
        )


# ---------------------------------------------------------------------------
# Flujo simple (un proyecto + horas + save)
# ---------------------------------------------------------------------------
def fill_timesheet(page, cfg: dict, log: logging.Logger, dry_run: bool):
    sel = cfg["selectors"]
    defaults = cfg.get("defaults", {})

    page.goto(cfg["url"], wait_until="domcontentloaded")
    _check_session(page, cfg)
    log.info("Rellenando horas…")

    if sel.get("project_dropdown") and defaults.get("project"):
        page.select_option(sel["project_dropdown"], defaults["project"])

    if sel.get("hours_field") and defaults.get("hours") is not None:
        fields = sel["hours_field"]
        if isinstance(fields, str):
            fields = [fields]
        for f in fields:
            page.fill(f, str(defaults["hours"]))

    for step in cfg.get("extra_fields", []):
        page.fill(step["selector"], str(step["value"]))

    if dry_run:
        log.info("DRY-RUN: campos rellenados, NO se hizo submit.")
        return

    if sel.get("save"):
        page.click(sel["save"])
        page.wait_for_load_state("networkidle")
        log.info("Horas guardadas (submit hecho).")
    else:
        log.warning("No hay selector 'save' configurado; no se hizo submit.")


# ---------------------------------------------------------------------------
# Flujo modal_entries (modal por entrada → add to list → save)
# ---------------------------------------------------------------------------
def _fill_modal_field(page, field: dict, ctx: dict):
    sel = field["selector"]
    kind = field.get("kind", "fill")
    val = str(field.get("value", ""))
    for k, v in ctx.items():
        val = val.replace("${" + k + "}", v)
    if kind == "select":
        page.select_option(sel, val)
    else:  # 'fill' y 'date' (input[type=date] espera YYYY-MM-DD)
        page.fill(sel, val)


def fill_modal_entries(page, cfg: dict, log: logging.Logger, dry_run: bool,
                       targets: list[tuple[date, str]]):
    me = cfg["modal_entries"]
    page.goto(cfg["url"], wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    _check_session(page, cfg)

    if not targets:
        log.warning("No hay días a llenar (revisa week_note.txt / rango). Nada que hacer.")
        return

    log.info(f"{len(targets)} día(s) a procesar: {targets[0][0]} … {targets[-1][0]}")

    for d, note in targets:
        iso = d.isoformat()
        if me.get("date_register"):
            page.fill(me["date_register"], iso)
            page.wait_for_timeout(700)

        page.click(me["open_modal"])
        page.wait_for_selector(me["modal"], state="visible", timeout=15000)
        page.wait_for_timeout(400)

        ctx = {"date": iso, "note": note or ""}
        for field in me["fields"]:
            _fill_modal_field(page, field, ctx)
        log.info(f"{iso}: modal lleno (8h · obs={note!r}).")

        if dry_run:
            shot = LOGS_DIR / f"{cfg['_name']}_dry_{iso}.png"
            page.screenshot(path=str(shot))
            page.click(me["cancel"])
            page.wait_for_timeout(500)
            log.info(f"{iso}: DRY-RUN, modal cerrado sin guardar. Captura: {shot.name}")
            continue

        page.click(me["add_to_list"])
        page.wait_for_timeout(900)
        if me.get("save_per_day"):
            page.click(me["save"])
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)
            log.info(f"{iso}: guardado (Save).")

    if not dry_run and not me.get("save_per_day"):
        page.click(me["save"])
        page.wait_for_load_state("networkidle")
        log.info("Save final hecho.")


# ---------------------------------------------------------------------------
# Flujo weekly_grid (grilla semanal tipo BigTime: una fila, horas por día)
# ---------------------------------------------------------------------------
def _week_sunday(d: date) -> date:
    """Domingo que inicia la semana Sun–Sat que contiene a d (BigTime)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _goto_week(page, wg: dict, target_sunday: date, log: logging.Logger):
    picker = wg["week_picker"]
    for _ in range(70):
        cur = date.fromisoformat(page.locator(picker).first.inner_text().strip())
        if cur == target_sunday:
            return
        arrow = wg["week_next"] if target_sunday > cur else wg["week_prev"]
        page.locator(arrow).first.click()
        page.wait_for_timeout(1300)
    raise RuntimeError(f"No se pudo navegar a la semana {target_sunday}")


def _submit_week(page, wg: dict, sun: date, log: logging.Logger):
    """Abre el modal 'Submit Timesheets' y envía SOLO la semana `sun`.
    Guarda: marca únicamente el período cuyo rango empieza en `sun`; si no
    aparece exactamente ese período, NO envía nada."""
    expected = f"{sun.month}/{sun.day}/{sun.strftime('%y')}"  # ej. 6/21/26
    page.locator(wg["submit_open"]).first.click()
    page.wait_for_selector(".modal:visible", timeout=15000)
    page.wait_for_timeout(1000)
    modal = page.locator(".modal:visible").first
    rows = modal.locator("tbody tr")
    target = 0
    for i in range(rows.count()):
        r = rows.nth(i)
        cb = r.locator("input[type=checkbox]").first
        if expected in r.inner_text():
            if not cb.is_checked():
                cb.check()
            target += 1
        elif cb.is_checked():
            cb.uncheck()
    if target != 1:
        log.warning(f"Submit {sun}: no se halló exactamente el período {expected} "
                    f"(coincidencias={target}); NO se envió.")
        modal.locator("xpath=.//button[contains(.,'Submit Hours')]").first  # no-op
        page.keyboard.press("Escape")
        return
    modal.locator("xpath=.//button[contains(.,'Submit Hours')]").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    log.info(f"semana {sun}: Submit enviado (período {expected}).")


def fill_weekly_grid(page, cfg: dict, log: logging.Logger, dry_run: bool,
                     targets: list[tuple[date, str]], submit: bool = False):
    wg = cfg["weekly_grid"]
    hours = str(wg.get("hours", "8"))
    page.goto(cfg["url"], wait_until="networkidle")
    page.wait_for_selector(wg["week_picker"], timeout=30000)
    page.wait_for_timeout(3000)
    _check_session(page, cfg)

    if not targets:
        log.warning("No hay días a llenar (revisa week_note.txt / rango).")
        return

    weeks: dict[date, list[date]] = {}
    for d, _ in targets:
        weeks.setdefault(_week_sunday(d), []).append(d)

    log.info(f"{len(targets)} día(s) en {len(weeks)} semana(s) BigTime.")
    for sun in sorted(weeks):
        days = sorted(weeks[sun])
        _goto_week(page, wg, sun, log)
        page.wait_for_timeout(1500)
        # Celdas de día: td absolutos 4..8 = Mon..Fri (Sun=3, Sat=9, Total=10).
        # Se re-busca la fila en cada día porque Angular re-renderiza al editar
        # (los ElementHandle quedarían obsoletos).
        xp = f"xpath=//tr[contains(., '{wg['row_match']}')]"
        for d in days:
            handles = page.query_selector_all(xp)
            row_h = next((h for h in handles if h.is_visible()), None)
            if row_h is None:
                raise RuntimeError(f"No se encontró fila visible '{wg['row_match']}' en {sun}.")
            tds = row_h.query_selector_all("td")
            cell = tds[d.weekday() + 4]
            cell.click()                  # ElementHandle.click auto-scrollea y enfoca
            page.wait_for_timeout(400)
            page.keyboard.type(hours)     # escribe en el input enfocado de la celda
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
        log.info(f"semana {sun}: {hours}h en {[x.isoformat() for x in days]}.")

        if dry_run:
            shot = LOGS_DIR / f"{cfg['_name']}_dry_{sun}.png"
            page.screenshot(path=str(shot))
            page.reload()
            page.wait_for_timeout(3000)
            log.info(f"semana {sun}: DRY-RUN, sin Save (recargado). Captura: {shot.name}")
        else:
            page.locator(wg["save"]).first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1800)
            log.info(f"semana {sun}: Save hecho.")
            if submit:
                _submit_week(page, wg, sun, log)


# ---------------------------------------------------------------------------
# Flujo oracle_api (Oracle Fusion: escribe vía la API backend, no la grilla)
# ---------------------------------------------------------------------------
def _oj_items(x):
    return x.get("items", []) if isinstance(x, dict) else (x or [])


def _find_timecard(o):
    if isinstance(o, dict):
        if "TimeCardId" in o and "timeEntries" in o:
            return o
        if isinstance(o.get("timeCards"), list) and o["timeCards"]:
            return _find_timecard(o["timeCards"][0])
        for v in o.values():
            r = _find_timecard(v)
            if r:
                return r
    if isinstance(o, list):
        for v in o:
            r = _find_timecard(v)
            if r:
                return r
    return None


def fill_oracle_api(page, cfg: dict, log: logging.Logger, dry_run: bool,
                    targets: list[tuple[date, str]], submit: bool = False):
    """Replica las llamadas de la UI Redwood al backend: captura token + timecard
    actual, y hace POST (TIME_SAVE para agregar días, TIME_SUBMIT con --submit).
    El endpoint se deriva del tráfico (el rv:<UUID> cambia entre sesiones)."""
    oa = cfg["oracle_api"]
    cap = {"token": None, "tc": None, "url": None}

    def on_req(r):
        a = r.headers.get("authorization", "")
        if a.startswith("Bearer ") and not cap["token"]:
            cap["token"] = a

    def on_resp(resp):
        u = resp.url
        if "hcmRestApi" in u and resp.request.method == "GET" and "timeCard" in u and not cap["tc"]:
            try:
                b = resp.text()
                if "TimeCardId" in b and "timeEntries" in b:
                    cap["tc"] = b
                    cap["url"] = u
            except Exception:  # noqa: BLE001
                pass

    page.on("request", on_req)
    page.on("response", on_resp)
    page.goto(cfg["url"], wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(12000)
    _check_session(page, cfg)
    if not cap["token"] or not cap["tc"]:
        raise RuntimeError("No se capturó token o timecard de Oracle (¿sesión expirada?).")

    tc = _find_timecard(json.loads(cap["tc"]))
    if not tc:
        raise RuntimeError("No se encontró el timecard en la respuesta de Oracle.")
    entries = _oj_items(tc.get("timeEntries"))
    existing_dates = {str(e.get("EntryDate"))[:10] for e in entries}
    start, stop = str(tc.get("StartDate", ""))[:10], str(tc.get("StopDate", ""))[:10]
    log.info(f"Timecard {tc.get('TimeCardId')} período {start}..{stop}, {len(entries)} entradas.")

    # Campos constantes (proyecto/tarea/tipo): de una entrada existente, o del YAML.
    if entries:
        const_fields = [{"TimeCardFieldId": f.get("TimeCardFieldId"), "Value": f.get("Value")}
                        for f in _oj_items(entries[0].get("timeCardFieldValues"))]
    else:
        const_fields = oa.get("fields", [])
    if not const_fields:
        raise RuntimeError("No hay campos de referencia (timecard vacío y sin 'fields' en el YAML).")

    # Entradas existentes en formato POST (reenviadas tal cual).
    existing = []
    for e in entries:
        fvs = [{"TimeCardFieldId": f.get("TimeCardFieldId"), "TimeEntryId": f.get("TimeEntryId"),
                "Value": f.get("Value")} for f in _oj_items(e.get("timeCardFieldValues"))]
        existing.append({
            "EntryDate": e.get("EntryDate"), "TimeEntryId": e.get("TimeEntryId"),
            "TimeEntryVersion": e.get("TimeEntryVersion"), "UnitOfMeasure": e.get("UnitOfMeasure"),
            "Measure": e.get("Measure"), "Comments": e.get("Comments"), "timeCardFieldValues": fvs,
        })

    hours = oa.get("hours", 8)
    new = []
    for i, (d, _) in enumerate(targets):
        iso = d.isoformat()
        if not (start <= iso <= stop) or iso in existing_dates:
            continue
        new.append({
            "EntryDate": f"{iso}T00:00:00",
            "TimeEntryId": str(6703139780000000 + int(d.strftime("%m%d")) * 100 + i),
            "TimeEntryVersion": 0, "UnitOfMeasure": "HR", "Measure": hours,
            "timeCardFieldValues": [dict(f) for f in const_fields],
        })

    if new:
        log.info(f"A agregar: {[n['EntryDate'][:10] for n in new]}")
    elif not submit:
        log.info(f"Nada que agregar: todos los días objetivo ya están en {start}..{stop}.")
        return

    # Endpoint derivado del tráfico (host + rv:<UUID> de la GET); el rv cambia por sesión.
    m = re.search(r"(https://[^/]+/hcmRestApi/rest/rv:[0-9a-fA-F-]+)", cap["url"] or "")
    if not m:
        raise RuntimeError("No se pudo derivar el endpoint del timecard.")
    endpoint = f"{m.group(1)}/en/{oa.get('version', '11.13.18.05:9')}/timeCards"

    def post(mode: str, entries: list) -> int:
        body = {
            "TimeCardId": tc.get("TimeCardId"), "TimeCardVersion": tc.get("TimeCardVersion"),
            "PersonId": tc.get("PersonId"), "StartDate": tc.get("StartDate"),
            "StopDate": tc.get("StopDate"), "ProcessMode": mode, "timeEntries": entries,
            "UserContext": "WORKER", "IgnoreWarningsFlag": False,
        }
        resp = page.context.request.post(
            endpoint, headers={"Authorization": cap["token"], "Content-Type": oa["content_type"]},
            data=json.dumps(body))
        if resp.status not in (200, 201):
            raise RuntimeError(f"POST {mode} falló: {resp.status} {resp.text()[:200]}")
        return resp.status

    if dry_run:
        if new:
            log.info(f"DRY-RUN: se agregarían {len(new)} día(s); no se hace POST.")
        if submit:
            log.info(f"DRY-RUN: se haría SUBMIT del período {start}..{stop}; no se hace POST.")
        return

    if new:
        log.info(f"TIME_SAVE OK ({post('TIME_SAVE', existing + new)}). {len(new)} día(s) agregados.")
    if submit:
        if new:
            log.warning("Había días sin guardar; se guardaron y ahora se envía el estado actual.")
        # Para submit reenviamos las entradas vigentes (las nuevas ya quedaron guardadas arriba).
        log.info(f"TIME_SUBMIT OK ({post('TIME_SUBMIT', existing + new)}). "
                 f"Período {start}..{stop} ENVIADO.")


# ---------------------------------------------------------------------------
# Flujo fieldglass (SAP Fieldglass: time sheet semanal, fila ST/Hr por día)
# ---------------------------------------------------------------------------
def fill_fieldglass(page, cfg: dict, log: logging.Logger, dry_run: bool,
                    targets: list[tuple[date, str]], submit: bool = False):
    fg = cfg["fieldglass"]
    hours = str(fg.get("hours", "8"))
    page.on("dialog", lambda d: d.accept())  # confirma diálogos (p. ej. al cancelar)

    page.wait_for_timeout(8000)   # deja cargar el nav tras el login
    _check_session(page, cfg)
    if not targets:
        log.warning("No hay días a llenar (revisa week_note.txt / rango).")
        return

    # Navegar por el menú: Time & Expense -> Time Sheets (más fiable que goto directo)
    page.wait_for_selector("text=Time & Expense", timeout=30000)
    page.get_by_text("Time & Expense", exact=False).first.click()
    page.wait_for_timeout(2500)
    page.get_by_text("Time Sheets", exact=False).first.click()
    page.wait_for_timeout(6000)
    u = page.get_by_text("Understood", exact=False)
    if u.count():
        u.first.click()
        page.wait_for_timeout(800)

    # Abrir el time sheet en Draft (la fila con estado "Draft").
    page.wait_for_selector("a[href*='time_sheet_detail']", timeout=30000)
    draft = page.locator("[role=row]").filter(has_text="Draft").locator("a[href*='time_sheet_detail']")
    (draft.first if draft.count() else
     page.locator("a[href*='time_sheet_detail']").first).click()
    page.wait_for_timeout(7000)
    u = page.get_by_text("Understood", exact=False)
    if u.count():
        u.first.click()
        page.wait_for_timeout(800)
    # SALVAGUARDA: el detalle debe estar en estado Draft.
    if "Draft" not in (page.query_selector("body").inner_text() or ""):
        raise RuntimeError("El time sheet abierto no está en Draft; abortando.")

    page.get_by_text("Edit", exact=True).first.click()
    page.wait_for_timeout(12000)
    # buscar el frame con los inputs de horas (puede tardar en renderizar).
    frame = None
    for i in range(15):
        counts = []
        for f in page.frames:
            try:
                n = len(f.query_selector_all("input[title*='/']"))
                counts.append((f.url[:40], n))
                if n >= 5:
                    frame = f
                    break
            except Exception:  # noqa: BLE001
                pass
        if frame:
            break
        if i in (0, 5, 10):
            log.info(f"esperando form… frames={counts}")
        page.wait_for_timeout(2500)
    if frame is None:
        page.screenshot(path=str(LOGS_DIR / f"{cfg['_name']}_noform.png"))
        raise RuntimeError("No se encontró el form de edición de Fieldglass.")

    # ST/Hr = primer input de cada día (la fila ST va antes que OT en el DOM).
    # Targeteo por fecha "D/M" del title (único dentro de una semana Sun-Sat).
    filled = []
    for d, _ in targets:
        daystr = f"{d.day}/{d.month}"
        inp = frame.locator(f"input[title*='{daystr}']").first
        inp.fill(hours)
        inp.dispatch_event("change")
        filled.append(d.isoformat())
        page.wait_for_timeout(300)
    log.info(f"ST/Hr {hours}h en {filled}.")

    if dry_run:
        page.screenshot(path=str(LOGS_DIR / f"{cfg['_name']}_dry.png"))
        page.get_by_text("Cancel", exact=True).first.click()
        page.wait_for_timeout(1500)
        log.info("DRY-RUN: cancelado sin guardar.")
        return

    if submit:
        page.get_by_text("Submit", exact=True).first.click()
        page.wait_for_timeout(4000)
        log.info("Submit hecho.")
    else:
        page.get_by_text("Complete Later", exact=False).first.click()
        page.wait_for_timeout(4000)
        log.info("Guardado como borrador (Complete Later).")


# ---------------------------------------------------------------------------
# Runner por empresa
# ---------------------------------------------------------------------------
def run_company(company: str, dry_run: bool, note: dict,
                start: date, end: date, submit: bool = False) -> bool:
    log = setup_logger(company)
    try:
        cfg = load_config(company)
        auth_mode = cfg.get("auth", "password")
        flow = cfg.get("flow", "simple")

        targets: list[tuple[date, str]] = []
        if flow in ("modal_entries", "weekly_grid", "oracle_api", "fieldglass"):
            targets = compute_targets(weeks_for(company, cfg, note), start, end, log)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=cfg.get("headless", True))

            if auth_mode == "session":
                state = session_path(company)
                if not state.exists():
                    raise RuntimeError(
                        f"No hay sesión guardada. Corre primero: "
                        f"python bot.py --company {company} --login"
                    )
                context = browser.new_context(storage_state=str(state))
                page = context.new_page()
            else:  # password
                context = browser.new_context()
                page = context.new_page()
                do_password_login(page, cfg, log)

            if flow == "modal_entries":
                fill_modal_entries(page, cfg, log, dry_run, targets)
            elif flow == "weekly_grid":
                fill_weekly_grid(page, cfg, log, dry_run, targets, submit)
            elif flow == "oracle_api":
                fill_oracle_api(page, cfg, log, dry_run, targets, submit)
            elif flow == "fieldglass":
                fill_fieldglass(page, cfg, log, dry_run, targets, submit)
            else:
                fill_timesheet(page, cfg, log, dry_run)
            browser.close()

        log.info("✓ OK")
        return True

    except (PWTimeout, RuntimeError, FileNotFoundError) as e:
        log.error(f"✗ FALLÓ: {e}")
        notify_failure(company, str(e))
        return False
    except Exception as e:  # noqa: BLE001
        log.exception(f"✗ Error inesperado: {e}")
        notify_failure(company, str(e))
        return False


# ---------------------------------------------------------------------------
# Notificaciones (stub — completar con Telegram/correo)
# ---------------------------------------------------------------------------
def notify_failure(company: str, msg: str):
    """Hook para avisar de fallos. Implementa Telegram/correo aquí.
    Ej. Telegram: requests.post(api_url, data={chat_id, text}). """
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": f"[timesheet-bot] {company} falló:\n{msg}"},
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Rutina de llenado de horas multi-empresa.")
    ap.add_argument("--company", help="Nombre de la empresa (archivo en companies/).")
    ap.add_argument("--job", help="Corre todas las empresas de un trabajo (campo job: en el YAML).")
    ap.add_argument("--login", action="store_true",
                    help="Login manual para guardar sesión (empresas con MFA).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Llena pero NO guarda (captura el modal por día).")
    ap.add_argument("--submit", action="store_true",
                    help="weekly_grid: tras Save, envía la semana (Submit Hours). Opt-in.")
    ap.add_argument("--month", help="Rango = todo el mes, formato YYYY-MM.")
    ap.add_argument("--week", help="Rango = una semana ISO, formato YYYY-Www.")
    ap.add_argument("--from", dest="frm", help="Inicio del rango, YYYY-MM-DD.")
    ap.add_argument("--to", help="Fin del rango, YYYY-MM-DD (default = --from).")
    ap.add_argument("--note-file", default=str(NOTE_FILE_DEFAULT),
                    help="Ruta del week_note.txt (default: ./week_note.txt).")
    args = ap.parse_args()

    if args.login:
        if not args.company:
            sys.exit("--login requiere --company")
        manual_login_flow(args.company)
        return

    if args.company:
        companies = [args.company]
    elif args.job:
        companies = list_companies_by_job(args.job)
        if not companies:
            sys.exit(f"No hay empresas activas con job: {args.job}")
    else:
        companies = list_active_companies()
    if not companies:
        sys.exit("No hay empresas configuradas en companies/.")

    start, end = scope_dates(args.month, args.week, args.frm, args.to)
    note = parse_week_note(Path(args.note_file))

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Rango {start} → {end} | "
          f"Empresas: {', '.join(companies)}{' (DRY-RUN)' if args.dry_run else ''}")
    results = {c: run_company(c, args.dry_run, note, start, end, args.submit)
               for c in companies}

    ok = [c for c, r in results.items() if r]
    fail = [c for c, r in results.items() if not r]
    print(f"\nResumen: {len(ok)} OK, {len(fail)} fallaron.")
    if fail:
        print(f"  Fallaron: {', '.join(fail)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
