#!/usr/bin/env bash
# Installation du robot PAPER sur un VPS Debian/Ubuntu (≤ 10 €/mois, 1 vCPU, 1–2 Go RAM).
# Usage (depuis le dossier du projet copié sur le VPS) :  sudo bash bot/deploy/install.sh
# Idempotent : peut être relancé pour une mise à jour du code.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
APP=/opt/btc_scalp
USER_NAME=btcbot

[ "$(id -u)" -eq 0 ] || { echo "à lancer avec sudo"; exit 1; }

echo "== paquets système (Python >= 3.11 requis pour tomllib)"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync logrotate chrony sqlite3
systemctl enable --now chrony                     # horloge synchronisée (contrôlée aussi vs Kraken)
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
  || { echo "Python 3.11+ requis"; exit 1; }

echo "== utilisateur dédié sans shell ni sudo : $USER_NAME"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin "$USER_NAME"

echo "== copie du code vers $APP (var/ et .venv/ préservés)"
mkdir -p "$APP"
# exclusions ANCRÉES à la racine (« /data ») : un motif « data » excluait aussi le paquet scalp/data
# (démo et tests cassés sur le VPS). Pas de secrets ni d'exécutions dans la copie.
rsync -a --delete --exclude '/.venv' --exclude '/var' --exclude '/data' --exclude '/runs' \
      --exclude '__pycache__' --exclude '.git' --exclude '*.env' "$SRC/" "$APP/"
mkdir -p "$APP/var/log" "$APP/var/data"
# code : propriété de root, lisible par tous, modifiable par personne d'autre (y compris le service) ;
# var/ (données, état) n'est pas parcouru
find "$APP" -path "$APP/var" -prune -o -exec chown root:root {} + -exec chmod u=rwX,go=rX {} +

echo "== environnement virtuel"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/bot/requirements.txt"
"$APP/.venv/bin/pip" freeze > "$APP/var/pip-freeze.txt"

echo "== secrets Telegram (hors dépôt, lisibles par root et le service seulement)"
mkdir -p /etc/btc_scalp
if [ ! -f /etc/btc_scalp/telegram.env ]; then
  cat > /etc/btc_scalp/telegram.env <<'ENV'
# Renseigner puis : sudo systemctl restart btc-scalp-bot
BTC_SCALP_TELEGRAM_TOKEN=
BTC_SCALP_TELEGRAM_CHAT_ID=
ENV
fi
chown root:"$USER_NAME" /etc/btc_scalp/telegram.env
chmod 640 /etc/btc_scalp/telegram.env

chown -R "$USER_NAME":"$USER_NAME" "$APP/var"      # seul répertoire inscriptible par le service (venv : root)
chmod 750 "$APP/var"

echo "== vérification de la configuration (refus si mode != paper)"
cd "$APP" && sudo -u "$USER_NAME" "$APP/.venv/bin/python" -m bot check-config

echo "== systemd + logrotate"
install -m 644 "$APP/bot/deploy/btc-scalp-bot.service" /etc/systemd/system/btc-scalp-bot.service
install -m 644 "$APP/bot/deploy/logrotate.conf" /etc/logrotate.d/btc-scalp-bot
install -m 755 "$APP/bot/deploy/emergency_stop.sh" /usr/local/bin/btc-scalp-emergency-stop
systemctl daemon-reload
systemctl enable btc-scalp-bot
echo
echo "Installation terminée. Étapes suivantes :"
echo "  1) sudo nano /etc/btc_scalp/telegram.env   (token + chat id)"
echo "  2) sudo systemctl start btc-scalp-bot && journalctl -u btc-scalp-bot -f"
echo "  3) suivre bot/deploy/RUNBOOK.md, section « Vérifications après démarrage »"
