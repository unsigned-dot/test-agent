# Installation du projet sur un PC Windows (robot PAPER Kraken + recherche).
#
# Usage, depuis le dossier du projet, dans PowerShell :
#   powershell -ExecutionPolicy Bypass -File install.ps1           # installation + vérification de la config
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Tests    # + les tests unitaires (~3 min)
#
# Ce script ne touche qu'au dossier du projet (.venv) : aucun service, aucune tâche planifiée,
# aucune modification du système. Le VPS (Linux) a son propre script : bot/deploy/install.sh.
param([switch]$Tests)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    # Python 3.11 minimum (tomllib) ; 3.13 recommandé (version de développement)
    foreach ($v in @("3.13", "3.12", "3.11")) {
        try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { return ,@("py", "-$v") } } catch {}
    }
    try {
        $ok = & python -c "import sys; print(int(sys.version_info >= (3, 11)))" 2>$null
        if ($ok -eq "1") { return ,@("python") }
    } catch {}
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.11+ introuvable. Installer Python 3.13 depuis https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "(cocher « Add python.exe to PATH » et garder le lanceur « py »), puis relancer ce script."
    exit 1
}
Write-Host "Python : $($py -join ' ')"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Création de l'environnement virtuel .venv ..."
    $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
    & $pyExe @pyArgs -m venv .venv
}
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt -r bot\requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "Échec de pip install." -ForegroundColor Red; exit 1 }
& $vpy -m pip freeze | Out-File -Encoding utf8 "requirements-lock-windows.txt"

Write-Host "`nVérification de la configuration du robot :"
& $vpy -m bot check-config
if ($LASTEXITCODE -ne 0) { Write-Host "Configuration refusée (voir ci-dessus)." -ForegroundColor Red; exit 1 }

if ($Tests) {
    Write-Host "`nTests unitaires (~3 min) ..."
    & $vpy -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { Write-Host "Des tests échouent : ne pas lancer le robot." -ForegroundColor Red; exit 1 }
}

$envFile = Join-Path $env:USERPROFILE ".config\btc_scalp\telegram.env"
Write-Host "`nInstallation terminée." -ForegroundColor Green
Write-Host "Étapes suivantes (voir docs/WINDOWS.md) :"
Write-Host "  1. sonde_kraken.bat        mesure l'accès Kraken / Binance et la profondeur d'historique"
Write-Host "  2. lancer_robot.bat        robot PAPER sur le flux Kraken réel (fenêtre à laisser ouverte)"
Write-Host "  3. tableau_de_bord.bat     suivi en direct dans le navigateur (http://127.0.0.1:8050)"
if (-not (Test-Path $envFile)) {
    Write-Host "Telegram (facultatif) : créer $envFile avec BTC_SCALP_TELEGRAM_TOKEN=... et BTC_SCALP_TELEGRAM_CHAT_ID=..."
}
