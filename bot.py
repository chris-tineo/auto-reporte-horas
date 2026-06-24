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


def compute_targets(company: str, note: dict, start: date, end: date,
                    log: logging.Logger) -> list[tuple[date, str]]:
    """Lista (fecha, observación) para los días trabajados en [start, end]."""
    weeks = note.get(company, {})
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
def scope_dates(month: str | None, week: str | None) -> tuple[date, date]:
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
    sel = cfg["selectors"]
    company = cfg["_name"]
    log.info("Login con usuario/contraseña…")
    page.goto(cfg["url"], wait_until="domcontentloaded")
    page.fill(sel["user"], get_credential(company, "user"))
    page.fill(sel["pass"], get_credential(company, "pass"))
    page.click(sel["submit"])
    page.wait_for_load_state("networkidle")
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
# Runner por empresa
# ---------------------------------------------------------------------------
def run_company(company: str, dry_run: bool, note: dict,
                start: date, end: date) -> bool:
    log = setup_logger(company)
    try:
        cfg = load_config(company)
        auth_mode = cfg.get("auth", "password")
        flow = cfg.get("flow", "simple")

        targets: list[tuple[date, str]] = []
        if flow == "modal_entries":
            targets = compute_targets(company, note, start, end, log)

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
    ap.add_argument("--login", action="store_true",
                    help="Login manual para guardar sesión (empresas con MFA).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Llena pero NO guarda (captura el modal por día).")
    ap.add_argument("--month", help="Rango = todo el mes, formato YYYY-MM.")
    ap.add_argument("--week", help="Rango = una semana ISO, formato YYYY-Www.")
    ap.add_argument("--note-file", default=str(NOTE_FILE_DEFAULT),
                    help="Ruta del week_note.txt (default: ./week_note.txt).")
    args = ap.parse_args()

    if args.login:
        if not args.company:
            sys.exit("--login requiere --company")
        manual_login_flow(args.company)
        return

    companies = [args.company] if args.company else list_active_companies()
    if not companies:
        sys.exit("No hay empresas configuradas en companies/.")

    start, end = scope_dates(args.month, args.week)
    note = parse_week_note(Path(args.note_file))

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Rango {start} → {end} | "
          f"Empresas: {', '.join(companies)}{' (DRY-RUN)' if args.dry_run else ''}")
    results = {c: run_company(c, args.dry_run, note, start, end) for c in companies}

    ok = [c for c, r in results.items() if r]
    fail = [c for c, r in results.items() if not r]
    print(f"\nResumen: {len(ok)} OK, {len(fail)} fallaron.")
    if fail:
        print(f"  Fallaron: {', '.join(fail)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
