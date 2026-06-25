#!/usr/bin/env python3
"""Genera la boleta (factura) a Bertoni en PDF, renderizando HTML con Playwright.

Uso:
  python boleta.py --date 2026-05-27 --invoice 19 --hours 168 \
      --desc "UDP Orchestrator setup..." --out "Boleta Mayo 2026.pdf"
"""
import argparse
from datetime import date as date_cls
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
BOLETA_DIR = BASE / "boleta"


def load_cfg() -> dict:
    return yaml.safe_load((BOLETA_DIR / "config.yaml").read_text(encoding="utf-8"))


def render_html(cfg: dict, fecha: str, invoice: int, hours: int,
                description: str) -> str:
    rate = cfg["rate"]
    amount = hours * rate
    tpl = (BOLETA_DIR / "template.html").read_text(encoding="utf-8")
    repl = {
        "name": cfg["issuer"]["name"], "id": cfg["issuer"]["id"],
        "address": cfg["issuer"]["address"],
        "bill_to_name": cfg["bill_to"]["name"], "bill_to_address": cfg["bill_to"]["address"],
        "date": fecha, "invoice": str(invoice),
        "description": description, "hours": str(hours), "rate": str(rate),
        "amount": str(amount),
    }
    for k, v in repl.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    return tpl


def make_boleta(out: Path, fecha: str, invoice: int, hours: int, description: str):
    cfg = load_cfg()
    html = render_html(cfg, fecha, invoice, hours, description)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(path=str(out), format="A4", print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        browser.close()
    print(f"Boleta generada: {out}  ({hours}h × {cfg['rate']} = {hours * cfg['rate']})")


def fmt_date(iso: str) -> str:
    d = date_cls.fromisoformat(iso)
    return f"{d.day:02d} / {d.month:02d} / {d.year}"


def main():
    ap = argparse.ArgumentParser(description="Genera la boleta a Bertoni en PDF.")
    ap.add_argument("--date", required=True, help="Fecha de la boleta, YYYY-MM-DD.")
    ap.add_argument("--invoice", type=int, required=True, help="N° de factura.")
    ap.add_argument("--hours", type=int, required=True, help="Horas del mes (quincenas aprobadas).")
    ap.add_argument("--desc", required=True, help="Descripción del trabajo del mes (inglés).")
    ap.add_argument("--out", required=True, help="Ruta del PDF de salida.")
    args = ap.parse_args()
    make_boleta(Path(args.out), fmt_date(args.date), args.invoice, args.hours, args.desc)


if __name__ == "__main__":
    main()
