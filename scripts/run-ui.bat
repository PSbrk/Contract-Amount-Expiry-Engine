@echo off
REM Launch the local web UI. Double-click this to open the dashboard /
REM needs-tagging editor / run-log viewer in your default browser.
REM EngineApp.exe --ui already opens the browser after a brief delay,
REM so we deliberately do NOT call `start "" "http://localhost:8080"`
REM -- that would race and hit a not-yet-listening server.

REM Resolve the bundle root absolutely (see run-ingest.bat for the rationale
REM on not relying on cwd lookup for bare-name exe resolution).
set BUNDLE_ROOT=%~dp0..
cd /d "%BUNDLE_ROOT%"
"%BUNDLE_ROOT%\EngineApp.exe" --ui
