# timesheet-bot

Rutina genérica multi-empresa (Python + Playwright) para el llenado de horas.
Una sola base de código; cada empresa es un YAML en `companies/`.

## Setup

```powershell
cd timesheet-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env      # luego edita .env con tus credenciales
```

## Configurar una empresa

1. Crea `companies/<nombre>.yaml` (copia `empresa_a.yaml` o `empresa_b.yaml`).
2. Abre la página real con DevTools (F12), inspecciona cada campo y copia
   los selectores (`#id`, `input[name=...]`, etc.) al YAML.
3. Define `auth`:
   - `password` → login automático (agrega `<NOMBRE>_USER` y `<NOMBRE>_PASS` al `.env`).
   - `session`  → para empresas con MFA (ver abajo).

### Tip para sacar selectores en DevTools
Click derecho sobre el elemento → Inspect → en el HTML, click derecho →
Copy → Copy selector. Prefiere `id` o `name` (estables) sobre clases largas.

## Empresas con MFA (sesión persistida)

```powershell
# Una vez (abre navegador, te logueas + MFA a mano):
python bot.py --company empresa_b --login
# Después corre normal; reutiliza la sesión hasta que expire:
python bot.py --company empresa_b
```
Cuando la sesión caduca, el bot lo detecta (vía `login_marker`) y avisa para
re-loguear. Es el único paso manual recurrente de las empresas con MFA.

## Uso diario

```powershell
python bot.py --company empresa_a          # una empresa
python bot.py                              # todas las marcadas active: true
python bot.py --company empresa_a --dry-run # rellena pero NO hace submit (prueba)
```

**Siempre prueba con `--dry-run` y `headless: false` al configurar una empresa nueva.**

## Agendar (cada viernes) — Programador de tareas de Windows

Crea un `.bat`:
```bat
@echo off
cd /d C:\ruta\timesheet-bot
call .venv\Scripts\activate.bat
python bot.py >> logs\cron.log 2>&1
```
Luego: Task Scheduler → Create Task → Trigger semanal (viernes, hora) →
Action: ejecutar el `.bat`. Marca "Run whether user is logged on or not".

(Alternativa: WSL2 + cron, si prefieres mantenerlo en Linux.)

## Notas importantes

- **MFA desatendido no es posible** por diseño; por eso el flujo de sesión.
- Las páginas internas cambian sin avisar y rompen selectores → revisa los logs
  en `logs/` y configura Telegram en `.env` para alertas de fallo.
- Registrar horas automáticamente puede chocar con políticas internas/cumplimiento
  de cada empresa. Evalúalo antes de dejarlo desatendido.
```
