@echo off
rem Tableau de bord en lecture seule : http://127.0.0.1:8050 (s'ouvre dans le navigateur).
rem Peut etre ferme et relance a tout moment : aucun effet sur le robot.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Environnement absent : lancer d'abord install.ps1 ^(voir docs\WINDOWS.md^).
  pause
  exit /b 1
)
title Tableau de bord - robot BTC
".venv\Scripts\python.exe" -m bot dashboard
pause
