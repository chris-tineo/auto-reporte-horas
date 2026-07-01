"""Envío de invoice (PDF) a un Google Form, reutilizando el perfil de Chrome de la
empresa. Encapsula los quirks de Google Forms que costó resolver:

  - snackbar 'Borrador guardado' (.QSj8ac) que intercepta clicks -> pointer-events:none
  - Forms duplica los <option>/botones en nodos ocultos -> clickear el VISIBLE
  - dropdowns como [role=listbox] + [role=option]
  - upload vía el Picker de Drive (input[type=file] dentro del iframe docs...picker)
  - viewport alto para que popups y el form quepan sin scroll

Config de la empresa (YAML), bloque `invoice_form`:
  url, month_question, amount_question, reimbursement_question, reimbursement_value,
  register_email_checkbox. El perfil sale de `profile:` (mismo que el flow de horas).

Uso desde bot.py:  python bot.py --company taller --submit-invoice \
    --pdf "Christian Tineo JUN2026.pdf" --month JUN2026 --amount 5440 [--dry-run]
"""
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
LOGS_DIR = BASE / "logs"

_UPLOAD_LABELS = "[aria-label='Añadir archivo'], [aria-label='Add file']"
_SUBMIT_LABELS = "[role=button][aria-label='Enviar'], [role=button][aria-label='Submit']"


def submit_invoice(cfg: dict, pdf: str, month: str, amount: str,
                   log: logging.Logger, dry_run: bool = False) -> bool:
    """Llena y (salvo dry_run) envía el Google Form de invoice. Devuelve True si
    quedó enviado (o llenado OK en dry_run)."""
    form = cfg["invoice_form"]
    company = cfg.get("_name", "company")
    pcfg = cfg.get("profile", {})
    user_data = str((BASE / pcfg.get("user_data_dir", "auth/taller_userdata")).resolve())
    pdf_path = str(Path(pdf).resolve())
    if not Path(pdf_path).exists():
        raise FileNotFoundError(f"PDF no encontrado: {pdf_path}")
    LOGS_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data, channel=pcfg.get("channel", "chrome"),
            headless=cfg.get("headless_invoice", True),
            viewport={"width": 1440, "height": 2400},
            args=[f"--profile-directory={pcfg.get('profile_directory', 'Default')}"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def li(text):
            return page.locator("[role=listitem]").filter(has_text=text)

        def kill_overlay():
            try:
                page.evaluate("() => document.querySelectorAll('.QSj8ac, .uBdAJe')"
                              ".forEach(e => { e.style.pointerEvents='none'; })")
            except Exception:  # noqa: BLE001
                pass

        def click_visible(loc) -> bool:
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    loc.nth(i).scroll_into_view_if_needed()
                    kill_overlay()
                    loc.nth(i).click()
                    return True
            return False

        def pick(question, option):
            box = li(question).locator("[role=listbox]").first
            box.scroll_into_view_if_needed()
            kill_overlay()
            box.click()
            page.wait_for_timeout(1200)
            if not click_visible(page.get_by_role("option", name=option, exact=True)):
                raise RuntimeError(f"No hay option visible '{option}' para '{question}'")
            page.wait_for_timeout(800)

        try:
            page.goto(form["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            if "/login" in page.url or page.locator(_UPLOAD_LABELS).count() == 0 \
                    and page.get_by_text(form["month_question"]).count() == 0:
                raise RuntimeError(
                    "El form no cargó logueado (¿sesión del perfil expirada? corre --login).")

            # Cada campo sobrescribe el borrador; NO usar 'Borrar formulario'
            # (deja un overlay del contenedor que bloquea los clicks).
            pick(form["month_question"], month)
            log.info(f"Month: {month}")

            li(form["amount_question"]).locator("input[type=text], textarea").first.fill(amount)
            log.info(f"Amount: {amount}")

            pick(form["reimbursement_question"], form.get("reimbursement_value", "No"))
            log.info(f"Reimbursement: {form.get('reimbursement_value', 'No')}")

            kill_overlay()
            if click_visible(page.locator(_UPLOAD_LABELS)):
                page.wait_for_timeout(6000)
                uploaded = False
                for fr in page.frames:
                    try:
                        inp = fr.locator("input[type=file]")
                        if inp.count() > 0:
                            inp.first.set_input_files(pdf_path)
                            uploaded = True
                            break
                    except Exception:  # noqa: BLE001
                        pass
                page.wait_for_timeout(9000)
                log.info(f"Upload: {'OK' if uploaded else 'FALLÓ'} ({Path(pdf_path).name})")
                if not uploaded:
                    raise RuntimeError("No pude subir el PDF al Picker de Drive.")
            else:
                log.info("Upload: ya hay un archivo en el borrador (se reutiliza).")

            if form.get("register_email_checkbox", True):
                kill_overlay()
                cb = page.locator("[role=checkbox]").first
                if cb.count() and cb.get_attribute("aria-checked") != "true":
                    cb.click()
                log.info("Checkbox correo marcado.")

            page.screenshot(path=str(LOGS_DIR / f"{company}_invoice_filled.png"), full_page=True)

            if dry_run:
                log.info("DRY-RUN: form lleno, NO se envió.")
                return True

            if not click_visible(page.locator(_SUBMIT_LABELS)):
                raise RuntimeError("No encontré el botón Enviar/Submit.")
            page.wait_for_timeout(8000)
            page.screenshot(path=str(LOGS_DIR / f"{company}_invoice_done.png"), full_page=True)
            body = page.locator("body").inner_text()
            ok = "Thank you" in body or "response has been recorded" in body \
                or "respuesta" in body.lower()
            log.info("Invoice enviada ✓" if ok else "Enviado (sin confirmación clara en pantalla).")
            return True
        finally:
            ctx.close()
