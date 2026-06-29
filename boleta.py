#!/usr/bin/env python3
"""Genera la boleta/invoice mensual en PDF, renderizando HTML con Playwright.

Multi-empresa: cada una tiene su template y datos en boleta/<company>/
(Bertoni queda en boleta/ por compatibilidad). Default: bertoni.

Uso:
  python boleta.py --date 2026-05-27 --invoice 19 --hours 168 \
      --desc "UDP Orchestrator setup..." --out "Boleta Mayo 2026.pdf"
  python boleta.py --company taller --date 2026-07-01 --invoice 11 --hours 168 \
      --month June --out "Christian Tineo JUN2026.pdf"
"""
import argparse
from datetime import date as date_cls
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
BOLETA_DIR = BASE / "boleta"
_MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]


def _company_dir(company: str) -> Path:
    # Bertoni vive en boleta/ (legacy); el resto en boleta/<company>/.
    return BOLETA_DIR if company == "bertoni" else BOLETA_DIR / company


def load_cfg(company: str = "bertoni") -> dict:
    return yaml.safe_load((_company_dir(company) / "config.yaml").read_text(encoding="utf-8"))


def fmt_eu(n: float) -> str:
    """5440 -> '5.440,00' (punto de miles, coma decimal)."""
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render_html(cfg: dict, company: str, fecha: str, invoice: int, hours: float,
                description: str, month: str = "") -> str:
    rate = cfg["rate"]
    amount = hours * rate
    european = cfg.get("number_format") == "european"
    if european:
        hours_s, rate_s, amount_s = fmt_eu(hours), fmt_eu(rate), fmt_eu(amount)
    else:
        hours_s, rate_s, amount_s = str(hours), str(rate), str(amount)

    bt = cfg["bill_to"]
    if "lines" in bt:  # taller: bloque multilínea
        bill_to_block = "".join(f"<p>{ln}</p>" for ln in bt["lines"])
    else:  # bertoni
        bill_to_block = f"<p>{bt['name']}</p><p>Dirección: {bt['address']}</p>"

    tpl = (_company_dir(company) / "template.html").read_text(encoding="utf-8")
    repl = {
        "name": cfg["issuer"]["name"], "id": cfg["issuer"].get("id", ""),
        "address": cfg["issuer"].get("address", ""),
        "bill_to_name": bt.get("name", ""), "bill_to_address": bt.get("address", ""),
        "bill_to_block": bill_to_block,
        "date": fecha, "invoice": str(invoice), "month": month,
        "description": description, "hours": hours_s, "rate": rate_s,
        "amount": amount_s,
    }
    for k, v in repl.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    return tpl


def make_boleta(out: Path, fecha: str, invoice: int, hours: float, description: str,
                company: str = "bertoni", month: str = ""):
    cfg = load_cfg(company)
    html = render_html(cfg, company, fecha, invoice, hours, description, month)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(path=str(out), format="A4", print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        browser.close()
    total = hours * cfg["rate"]
    print(f"Boleta generada: {out}  ({hours}h × {cfg['rate']} = {total})")


def fmt_date(iso: str) -> str:
    """Bertoni: 'dd / mm / yyyy'."""
    d = date_cls.fromisoformat(iso)
    return f"{d.day:02d} / {d.month:02d} / {d.year}"


def fmt_date_es(iso: str) -> str:
    """Taller: '03-jun-26' (dd-mmm-yy, mes en español)."""
    d = date_cls.fromisoformat(iso)
    return f"{d.day:02d}-{_MESES_ES[d.month - 1]}-{d.year % 100:02d}"


def main():
    ap = argparse.ArgumentParser(description="Genera la boleta/invoice mensual en PDF.")
    ap.add_argument("--company", default="bertoni", help="Empresa (default: bertoni).")
    ap.add_argument("--date", required=True, help="Fecha de la boleta, YYYY-MM-DD.")
    ap.add_argument("--invoice", type=int, required=True, help="N° de factura.")
    ap.add_argument("--hours", type=float, required=True, help="Horas del mes.")
    ap.add_argument("--desc", help="Descripción del trabajo (default: config de la empresa).")
    ap.add_argument("--month", default="", help="MONTH OF SERVICE (taller), ej. June.")
    ap.add_argument("--out", required=True, help="Ruta del PDF de salida.")
    args = ap.parse_args()

    cfg = load_cfg(args.company)
    desc = args.desc or cfg.get("description")
    if not desc:
        ap.error("--desc es obligatorio para esta empresa.")
    fecha = fmt_date_es(args.date) if cfg.get("number_format") == "european" else fmt_date(args.date)
    make_boleta(Path(args.out), fecha, args.invoice, args.hours, desc,
                company=args.company, month=args.month)


if __name__ == "__main__":
    main()
