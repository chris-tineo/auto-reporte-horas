#!/usr/bin/env python3
"""
timesheet-bot — Rutina genérica multi-empresa para llenado de horas.

Soporta dos tipos de autenticación por empresa:
  - "password": login desatendido con usuario/contraseña desde .env
  - "session":  reutiliza una sesión guardada (storageState) para empresas con MFA

Uso:
  python bot.py --company empresa_a            # corre una empresa
  python bot.py                                # corre todas las activas
  python bot.py --company empresa_b --login    # login manual para guardar sesión (MFA)
  python bot.py --company empresa_a --dry-run   # navega y rellena pero NO hace submit
"""

import argparse
import os
import sys
import glob
import logging
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = Path(__file__).parent
COMPANIES_DIR = BASE / "companies"
AUTH_DIR = BASE / "auth"
LOGS_DIR = BASE / "logs"

load_dotenv(BASE / ".env")


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


# ---------------------------------------------------------------------------
# Rellenado de horas
# ---------------------------------------------------------------------------
def fill_timesheet(page, cfg: dict, log: logging.Logger, dry_run: bool):
    sel = cfg["selectors"]
    defaults = cfg.get("defaults", {})

    # Asegurar que estamos en la página de horas (por si la sesión expiró
    # y nos redirigió al login).
    page.goto(cfg["url"], wait_until="domcontentloaded")
    if cfg.get("login_marker") and page.locator(cfg["login_marker"]).count() > 0:
        raise RuntimeError(
            "Parece que la sesión expiró (se detectó la pantalla de login). "
            f"Vuelve a correr: python bot.py --company {cfg['_name']} --login"
        )

    log.info("Rellenando horas…")

    if sel.get("project_dropdown") and defaults.get("project"):
        page.select_option(sel["project_dropdown"], defaults["project"])

    if sel.get("hours_field") and defaults.get("hours") is not None:
        # Para timesheets de varios días, hours_field puede ser una lista de selectores.
        fields = sel["hours_field"]
        if isinstance(fields, str):
            fields = [fields]
        for f in fields:
            page.fill(f, str(defaults["hours"]))

    # Pasos extra opcionales (descripción, categoría, etc.)
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
# Runner por empresa
# ---------------------------------------------------------------------------
def run_company(company: str, dry_run: bool = False) -> bool:
    log = setup_logger(company)
    try:
        cfg = load_config(company)
        auth_mode = cfg.get("auth", "password")

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
                    help="Rellena pero NO hace submit.")
    args = ap.parse_args()

    if args.login:
        if not args.company:
            sys.exit("--login requiere --company")
        manual_login_flow(args.company)
        return

    companies = [args.company] if args.company else list_active_companies()
    if not companies:
        sys.exit("No hay empresas configuradas en companies/.")

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Empresas a procesar: {', '.join(companies)}")
    results = {c: run_company(c, dry_run=args.dry_run) for c in companies}

    ok = [c for c, r in results.items() if r]
    fail = [c for c, r in results.items() if not r]
    print(f"\nResumen: {len(ok)} OK, {len(fail)} fallaron.")
    if fail:
        print(f"  Fallaron: {', '.join(fail)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
