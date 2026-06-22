@echo off
REM Interactive ingest -- double-click this to run an ingest with live
REM progress visible in the console window. Output is teed to the same
REM logs\ingest-<date>.log that the scheduled run writes to, so the audit
REM trail is identical either way. Window pauses at the end so the
REM operator can read the final exit code before it closes.
REM
REM For Task Scheduler / silent runs, use run-ingest.bat instead.

set BUNDLE_ROOT=%~dp0..
cd /d "%BUNDLE_ROOT%"

REM Locale-safe date stamp; matches run-ingest.bat's log naming so both
REM interactive and scheduled runs share one daily log file.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

if not exist logs mkdir logs
set LOGFILE=logs\ingest-%TODAY%.log

echo.
echo Running ingest. Live progress below; full log: %LOGFILE%
echo --------------------------------------------------------------------
echo.

REM Tee EngineApp.exe stdout+stderr to BOTH the console and the log file.
REM PowerShell's Tee-Object is the cleanest portable way to do this; cmd
REM has no native tee. `2>&1` merges native stderr into the pipeline as
REM text so Tee-Object can capture it. `exit $LASTEXITCODE` propagates
REM the engine's exit code through PowerShell back to this .bat.
powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%BUNDLE_ROOT%\EngineApp.exe' --ingest 2>&1 | Tee-Object -FilePath '%LOGFILE%' -Append; exit $LASTEXITCODE"
set RC=%ERRORLEVEL%

echo.
echo --------------------------------------------------------------------
if %RC% EQU 0 (
    echo Ingest finished SUCCESSFULLY ^(exit code 0^).
) else (
    echo Ingest exited with code %RC% -- review log for errors.
)
echo Log: %LOGFILE%
echo.
pause
exit /b %RC%
