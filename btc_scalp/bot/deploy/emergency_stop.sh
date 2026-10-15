#!/usr/bin/env bash
# ARRÊT D'URGENCE du robot paper (voir RUNBOOK.md, section « Arrêt d'urgence »).
# 1) pose l'arrêt persistant (aucune entrée, même après redémarrage) ;
# 2) laisse au robot quelques secondes pour fermer la position paper au marché ;
# 3) arrête le service ;
# 4) annule les entrées paper restantes (les stops reduce-only sont conservés).
# En paper, aucune position réelle n'existe : rien à fermer chez Kraken.
set -euo pipefail
APP=/opt/btc_scalp
PY="$APP/.venv/bin/python"
cd "$APP"
sudo -u btcbot "$PY" -m bot stop || true
sleep 3                       # fermeture au marché = 1er trade après la latence simulée
sudo systemctl stop btc-scalp-bot || true
sudo -u btcbot "$PY" -m bot emergency-stop
sudo -u btcbot "$PY" -m bot status 2>/dev/null | head -20 || true
echo "Robot arrêté. Pour reprendre : voir RUNBOOK.md (« Reprise après /stop »)."
