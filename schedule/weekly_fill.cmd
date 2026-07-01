@echo off
REM Llenado semanal (viernes) de todas las empresas activas. Lo dispara la tarea
REM programada "TimesheetBot\WeeklyFill". Fallos/dudas se avisan por Claudia.
cd /d "%~dp0.."
".venv\Scripts\python.exe" bot.py >> "logs\schedule_weekly.log" 2>&1
