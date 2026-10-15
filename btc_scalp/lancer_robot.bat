@echo off
rem Robot PAPER sur le flux Kraken reel. Laisser cette fenetre ouverte ; Ctrl+C pour arreter.
rem Le PC ne doit pas se mettre en veille pendant que le robot tourne (voir docs\WINDOWS.md).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Environnement absent : lancer d'abord install.ps1 ^(voir docs\WINDOWS.md^).
  pause
  exit /b 1
)
title Robot BTC paper - Kraken
".venv\Scripts\python.exe" -m bot run
echo.
echo Robot arrete (code %errorlevel%). Les positions paper restent protegees par leur stop.
pause
