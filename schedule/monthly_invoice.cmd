@echo off
REM Día 1 de cada mes: genera y envía el invoice de Taller del mes anterior
REM (horas del PSA -> boleta -> Google Form). Tarea "TimesheetBot\MonthlyInvoice".
cd /d "%~dp0.."
".venv\Scripts\python.exe" bot.py --company taller --invoice-run >> "logs\schedule_monthly.log" 2>&1
