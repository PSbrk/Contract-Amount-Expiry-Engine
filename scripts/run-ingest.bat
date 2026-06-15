@echo off
REM Daily Tableau ingest -- called by Task Scheduler (see install-scheduler.ps1).
REM Also safe to double-click for an ad-hoc run; the log captures stdout/stderr.

REM Resolve the bundle root absolutely (%~dp0 = this script's folder, with
REM trailing backslash). We pass an absolute path to EngineApp.exe rather
REM than relying on cwd lookup, because cmd does not always honor cwd for
REM bare-name exe lookup (App Execution Aliases, group-policy hardening,
REM etc.). Cwd is still switched so logs\ and data\ resolve to bundle-relative
REM paths the operator expects.
set BUNDLE_ROOT=%~dp0..
cd /d "%BUNDLE_ROOT%"

REM Locale-safe date stamp: %date% varies by Windows region settings, but
REM PowerShell's Get-Date is deterministic. Format: 2026-06-15.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

if not exist logs mkdir logs

"%BUNDLE_ROOT%\EngineApp.exe" --ingest >> "logs\ingest-%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
