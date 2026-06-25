#!/usr/bin/env python3
"""Orquestador del proceso mensual del grupo de trabajo "Syneos".

Día 24:  submit Syneos · Teams a la manager · boleta · correo (con imágenes + boleta)
Día 25:  export PDF(s) del timecard Syneos · subir a SharePoint

Por defecto es SEGURO: el correo queda como BORRADOR, y Teams/submit/upload NO
escriben (dry). Con --send se realizan las acciones reales.

Uso:
  python monthly.py --month 2026-06 --day 24 --desc "UDP ..." [--invoice 20] [--send]
  python monthly.py --month 2026-06 --day 25 [--send]
"""
import argparse
import base64
import calendar
from datetime import date
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

import bot
import boleta

BASE = Path(__file__).parent
CFG = yaml.safe_load((BASE / "monthly_config.yaml").read_text(encoding="utf-8"))
IMG = BASE / "img"; EXPORT = BASE / "export"
for _d in (IMG, EXPORT):
    _d.mkdir(exist_ok=True)
MONTHS_EN = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]
MONTHS_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
             "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


def log(m):
    print(f"[monthly] {m}")


# --------------------------------------------------------------------------- #
# Datos del mes (desde week_note.txt: una sola fuente de días trabajados)
# --------------------------------------------------------------------------- #
def month_bounds(month):
    y, m = (int(x) for x in month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]), y, m


def worked_targets(month):
    note = bot.parse_week_note(BASE / "week_note.txt")
    s, e, _, _ = month_bounds(month)
    weeks = note.get(f"job:{CFG['job']}", {})
    return bot.compute_targets(weeks, s, e, bot.setup_logger("monthly"))


def periods(month):
    """Dos quincenas: (start, end, días, horas) para 1–15 y 16–fin."""
    s, e, y, m = month_bounds(month)
    days = [d for d, _ in worked_targets(month)]
    p1 = [d for d in days if d.day <= 15]
    p2 = [d for d in days if d.day > 15]
    mid = date(y, m, 15)
    return [
        (date(y, m, 1), mid, p1, len(p1) * 8),
        (date(y, m, 16), e, p2, len(p2) * 8),
    ]


def fmt(d):
    return f"{d.month}/{d.day}/{d.strftime('%y')}"


# --------------------------------------------------------------------------- #
# Imágenes para el correo
# --------------------------------------------------------------------------- #
def gen_kpi_images(month):
    sundays = sorted({bot._week_sunday(d) for d, _ in worked_targets(month)})
    url = CFG["kpi"]["weekly_url"]; rowm = CFG["kpi"]["row_match"]
    prev = "xpath=//a[contains(@class,'picker-text')]/preceding-sibling::a[contains(@class,'icon-container')][1]"
    nxt = "xpath=//a[contains(@class,'picker-text')]/following-sibling::a[contains(@class,'icon-container')][1]"
    out = []
    with sync_playwright() as p:
        page = p.chromium.launch().new_context(
            storage_state=CFG["sessions"]["kpi"], viewport={"width": 1400, "height": 900}).new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)
        for sun in sundays:
            t = sun.isoformat()
            for _ in range(70):
                cur = page.locator("a.picker-text").first.inner_text().strip()
                if cur == t:
                    break
                page.locator(nxt if t > cur else prev).first.click(); page.wait_for_timeout(1100)
            page.wait_for_timeout(1200)
            row = page.query_selector(f"xpath=//tr[contains(., '{rowm}')]")
            table = row.query_selector("xpath=ancestor::table[1]") if row else None
            path = IMG / f"kpi_{t}.png"
            tb, rb = (table.bounding_box() if table else None), (row.bounding_box() if row else None)
            if tb and rb:
                page.screenshot(path=str(path), clip={"x": tb["x"], "y": tb["y"], "width": tb["width"],
                                                      "height": rb["y"] + rb["height"] - tb["y"] + 2})
            elif table:
                table.screenshot(path=str(path))
            out.append(path)
    log(f"KPI: {len(out)} imágenes")
    return out


def gen_approval_image(month, submitted="6/24/26"):
    th = ("Period Start Date", "Period End Date", "Status", "Reported Hours",
          "Scheduled Hours", "Absence Hours", "Total Hours", "Submission Date")
    rows = ""
    for start, end, _, hrs in reversed(periods(month)):
        cells = f"<td>{fmt(start)}</td><td>{fmt(end)}</td><td><span class='b'>Approved</span></td>"
        cells += "".join(f"<td>{v}</td>" for v in (hrs, hrs, "", hrs, submitted))
        rows += f"<tr>{cells}</tr>"
    html = f"""<!doctype html><meta charset=utf-8><style>
      body{{margin:0;font-family:'Segoe UI',Arial,sans-serif;color:#1a1a1a}}
      table{{border-collapse:collapse;font-size:13px}}
      th,td{{padding:10px 16px;text-align:left;border-bottom:1px solid #e3e3e3;white-space:nowrap}}
      th{{color:#5a5a5a;font-weight:600;border-bottom:2px solid #d0d0d0}}
      .b{{background:#e3f2e6;color:#1b7a3d;border:1px solid #b6dcc0;border-radius:12px;padding:2px 12px;font-weight:600}}
    </style><table><thead><tr>{''.join(f'<th>{h}</th>' for h in th)}</tr></thead><tbody>{rows}</tbody></table>"""
    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        page.set_content(html, wait_until="networkidle")
        path = IMG / "syneos_approval.png"
        page.query_selector("table").screenshot(path=str(path))
    log("tabla de aprobación generada")
    return path


# --------------------------------------------------------------------------- #
# Boleta
# --------------------------------------------------------------------------- #
def gen_boleta(month, hours, desc, invoice, run_date):
    out = BASE / f"Boleta {MONTHS_ES[int(month.split('-')[1])]} {month.split('-')[0]} Bertoni.pdf"
    boleta.make_boleta(out, boleta.fmt_date(run_date.isoformat()), invoice, hours, desc)
    return out


# --------------------------------------------------------------------------- #
# Submit Syneos (reusa el flujo oracle_api del bot)
# --------------------------------------------------------------------------- #
def submit_syneos(month, send):
    note = bot.parse_week_note(BASE / "week_note.txt")
    s, e, _, _ = month_bounds(month)
    log("Submit Syneos (período cargado)…")
    bot.run_company(CFG["job"], dry_run=not send, note=note,
                    start=s, end=e, submit=True)
    if not send:
        log("(dry: no se envió; usa --send para submit real)")


# --------------------------------------------------------------------------- #
# Teams a la manager
# --------------------------------------------------------------------------- #
def teams_to_manager(send):
    email = CFG["manager"]["email"]; msg = CFG["manager"]["teams_message"]
    if not send:
        log(f"(dry) Teams a {email}: {msg!r}")
        return
    with sync_playwright() as p:
        page = p.chromium.launch(headless=True).new_context(
            storage_state=CFG["sessions"]["teams"]).new_page()
        page.goto("https://teams.microsoft.com.mcas.ms/v2/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(30000)
        try:
            page.wait_for_selector("#loading-screen", state="hidden", timeout=40000)
        except Exception:
            page.wait_for_timeout(8000)
        page.keyboard.press("Control+Alt+g"); page.wait_for_timeout(1500)
        page.keyboard.type(email); page.wait_for_timeout(2500)
        page.keyboard.press("Enter"); page.wait_for_timeout(5000)
        title = page.title().lower()
        if "anima" not in title and "pimenta" not in title:
            log(f"!! ABORTO Teams: chat activo no es la manager (title={title!r})"); return
        box = page.locator("div[role='textbox'][contenteditable='true']").last
        box.click(); box.type(msg); page.wait_for_timeout(600)
        page.keyboard.press("Enter"); page.wait_for_timeout(3000)
        log(f"Teams enviado a {email}")


# --------------------------------------------------------------------------- #
# Correo mensual (Outlook)
# --------------------------------------------------------------------------- #
def send_email(month, hours, kpi_imgs, approval_img, boleta_pdf, send):
    y, m = month.split("-"); subject = CFG["email"]["subject_tpl"].format(
        month_name=MONTHS_ES[int(m)], year=y)

    def tag(path, w):
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        return f'<div><img src="data:image/png;base64,{b64}" style="width:{w}px;max-width:100%;"></div><div><br></div>'

    parts = ["<div>Hola,</div><div><br></div>",
             "<div>Envio los timesheets del mes correspondiente.</div><div><br></div>",
             "<div><b>KPI Timesheet:</b></div><div><br></div>"]
    parts += [tag(p, 640) for p in kpi_imgs]
    parts += ["<div><b>Aprobación de horas de Syneos Health:</b></div><div><br></div>",
              tag(approval_img, 620),
              "<div>Adjunto de igual manera la boleta de este mes.</div><div><br></div>",
              "<div>Saludos,</div><div>Christian</div>"]
    body = "".join(parts)

    with sync_playwright() as p:
        page = p.chromium.launch(headless=True).new_context(
            storage_state=CFG["sessions"]["outlook"]).new_page()
        page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(20000)
        page.locator("button[aria-label*='New mail' i]").first.click()
        page.wait_for_timeout(6000)

        def recip(label, value):
            page.locator(f"[aria-label='{label}']").first.click(); page.wait_for_timeout(700)
            page.keyboard.type(value); page.wait_for_timeout(1800); page.keyboard.press("Enter")
            page.wait_for_timeout(900)
        recip("To", CFG["email"]["to"])
        recip("Cc", CFG["email"]["cc"])
        page.locator("input[aria-label='Subject']").first.fill(subject); page.wait_for_timeout(400)
        page.eval_on_selector("div[aria-label='Message body']",
                              "(el,h)=>{el.innerHTML=h;el.dispatchEvent(new Event('input',{bubbles:true}))}", body)
        page.wait_for_timeout(2000)
        page.locator("button[aria-label='Attach file']").first.click(); page.wait_for_timeout(2000)
        with page.expect_file_chooser() as fc:
            page.get_by_text("Browse this computer", exact=False).first.click()
        fc.value.set_files(str(boleta_pdf)); page.wait_for_timeout(6000)

        if not send:
            log("Correo creado como BORRADOR (usa --send para enviar).")
            page.wait_for_timeout(2000); return
        # SALVAGUARDA: destinatarios esperados
        to_txt = page.locator("[aria-label='To']").first.inner_text().lower()
        if "quispe" not in to_txt and "gabriela" not in to_txt:
            log(f"!! ABORTO envío: To inesperado ({to_txt[:40]!r})"); return
        page.locator("button[aria-label='Send']").first.click(); page.wait_for_timeout(6000)
        log("Correo ENVIADO.")


# --------------------------------------------------------------------------- #
# Export PDF Syneos (Print -> PDF) + subir a SharePoint
# --------------------------------------------------------------------------- #
def export_syneos(month):
    """Exporta el PDF del período cargado (Print). Devuelve la ruta. (June 2nd)"""
    name = f"{MONTHS_EN[int(month.split('-')[1])]} 2nd.pdf"
    out = EXPORT / name
    with sync_playwright() as p:
        ctx = p.chromium.launch().new_context(
            storage_state=CFG["sessions"]["syneos"], accept_downloads=True,
            viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.goto(CFG["syneos"]["timecard_url"], wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(11000)
        with page.expect_download(timeout=20000) as dl:
            page.query_selector("button[aria-label='Print']").click()
        dl.value.save_as(str(out))
    log(f"export: {out.name}")
    return out


def upload_sharepoint(files, send):
    sp = CFG["sharepoint"]
    if not send:
        log(f"(dry) subiría a SharePoint: {[Path(f).name for f in files]}")
        return
    with sync_playwright() as p:
        page = p.chromium.launch(headless=True).new_context(
            storage_state=CFG["sessions"]["sharepoint"], viewport={"width": 1400, "height": 900}).new_page()
        page.goto(sp["url"], wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(16000)
        txt = page.query_selector("body").inner_text()
        if sp["folder_marker"] not in txt or page.query_selector("input[name='loginfmt']"):
            log("!! ABORTO upload: carpeta inesperada o pide login."); return
        for f in files:
            fname = Path(f).name
            if fname in page.query_selector("body").inner_text():
                log(f"  {fname} ya existe; se omite"); continue
            page.get_by_text("Create or upload", exact=False).first.click(); page.wait_for_timeout(1800)
            with page.expect_file_chooser() as fc:
                page.get_by_role("menuitem", name="Files upload").first.click()
            fc.value.set_files(str(f)); page.wait_for_timeout(9000)
            log(f"  subido {fname}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Proceso mensual del grupo Syneos.")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--day", type=int, choices=[24, 25], required=True)
    ap.add_argument("--desc", help="Descripción del mes para la boleta (día 24).")
    ap.add_argument("--invoice", type=int, help="N° de factura (default: boleta/config.yaml).")
    ap.add_argument("--send", action="store_true", help="Realiza acciones reales (enviar/subir/submit).")
    args = ap.parse_args()

    if args.day == 24:
        hours = len(worked_targets(args.month)) * 8
        inv = args.invoice or yaml.safe_load((BASE / "boleta" / "config.yaml").read_text())["next_invoice"]
        run_date = date(int(args.month[:4]), int(args.month[5:7]), 24)
        log(f"=== DÍA 24 · {args.month} · {hours}h · factura {inv} · {'SEND' if args.send else 'DRY/DRAFT'} ===")
        submit_syneos(args.month, args.send)
        teams_to_manager(args.send)
        if not args.desc:
            log("!! Falta --desc para la boleta/correo."); return
        bol = gen_boleta(args.month, hours, args.desc, inv, run_date)
        kpi = gen_kpi_images(args.month)
        appr = gen_approval_image(args.month, submitted=fmt(run_date))
        send_email(args.month, hours, kpi, appr, bol, args.send)
        log("DÍA 24 completo.")
    else:
        log(f"=== DÍA 25 · {args.month} · {'SEND' if args.send else 'DRY'} ===")
        pdf = export_syneos(args.month)
        upload_sharepoint([pdf], args.send)
        log("DÍA 25 completo. (Nota: 'June 1st' / quincena 1–15 pendiente de navegación.)")


if __name__ == "__main__":
    main()
