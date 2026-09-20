@echo off
rem Start the Codex usage overlay (named pythonw copy; no console window)
set "PYDIR=%LOCALAPPDATA%\Programs\Python\Python312"
if exist "%PYDIR%\codex-usage.exe" (
  start "" "%PYDIR%\codex-usage.exe" "%~dp0codex_usage_overlay.py"
) else (
  start "" "%PYDIR%\pythonw.exe" "%~dp0codex_usage_overlay.py"
)
