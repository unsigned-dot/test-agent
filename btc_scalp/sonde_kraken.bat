@echo off
rem Mesure l'acces Kraken / Binance, la profondeur de l'historique Kraken et le flux WebSocket.
rem Aucune cle API, aucun ordre. Le rapport JSON est ecrit dans le dossier runs\.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Environnement absent : lancer d'abord install.ps1 ^(voir docs\WINDOWS.md^).
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\kraken_probe.py
pause
