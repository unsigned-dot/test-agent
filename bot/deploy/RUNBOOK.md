# RUNBOOK — Robot BTC PF_XBTUSD, MODE PAPER

Ce robot **n'envoie aucun ordre réel**. Il reçoit le flux public Kraken Derivatives
(PF_XBTUSD), enregistre les données, applique la stratégie configurée (par défaut :
**aucune**, donc aucune entrée), simule les exécutions et prévient par Telegram.
Seules les commandes `/status` et `/stop` existent. Aucune commande ne peut relever une
limite de risque. Pour modifier le risque, il faut changer `config/default.toml`
(changement versionné, sur autorisation d'Hugo).

---

## 1. Installation sur le VPS

Prérequis : VPS Debian 12 ou Ubuntu 22.04+ (1 vCPU, 1–2 Go RAM, ≥ 20 Go disque,
≤ 10 €/mois), accès SSH avec un utilisateur sudo. Choisir la région après une mesure de
latence vers `futures.kraken.com` (`curl -o /dev/null -s -w '%{time_connect}\n' https://futures.kraken.com/derivatives/api/v3/instruments`).

```bash
# depuis le PC : copier le projet
scp -r btc_scalp/ user@vps:~/
# sur le VPS
cd ~/btc_scalp
sudo bash bot/deploy/install.sh
```

Le script :
- installe Python, chrony (synchronisation de l'heure), logrotate et sqlite3 ;
- crée l'utilisateur système `btcbot` (sans shell) ;
- copie le code dans `/opt/btc_scalp` (en lecture seule pour le service) et crée le venv ;
- crée `/etc/btc_scalp/telegram.env` (droits 640, hors dépôt) ;
- vérifie la configuration (**le robot refuse de démarrer si `mode` ≠ `"paper"`**) ;
- installe le service systemd `btc-scalp-bot` (redémarrage automatique), la
  compression des journaux et `btc-scalp-emergency-stop`.

### Telegram
1. Créer un bot avec @BotFather et noter le **token**.
2. Écrire un message au bot, puis lire le **chat id** sur
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (champ `message.chat.id`).
3. `sudo nano /etc/btc_scalp/telegram.env` : renseigner les deux valeurs.
4. Seul ce chat id peut envoyer `/status` et `/stop`. Les autres expéditeurs sont ignorés.

### Test sous Windows (sans VPS)
```powershell
cd btc_scalp
py -3.13 -m venv .venv ; .venv\Scripts\activate
pip install -r bot\requirements.txt
python -m bot check-config
python -m bot demo --minutes 120          # faux flux, aucun réseau
python -m unittest discover -s tests -p "test_bot_*.py" -v
$env:BTC_SCALP_TELEGRAM_TOKEN="..." ; $env:BTC_SCALP_TELEGRAM_CHAT_ID="..."
python -m bot run                          # flux Kraken réel (Ctrl+C pour arrêter)
```

## 2. Démarrage

```bash
sudo systemctl start btc-scalp-bot
journalctl -u btc-scalp-bot -f             # suivi en direct
```
Le robot n'ouvre aucune entrée pendant le préchauffage (**25 h** : le facteur `vol_regime`
utilise une médiane sur 24 h de la volatilité 1 h ; avant, il diffère de la recherche), ni
tant que l'horloge n'a pas été contrôlée face à Kraken.

## 3. Vérifications après démarrage (dans la première heure)

| Vérification | Commande / attendu |
|---|---|
| Service actif | `systemctl status btc-scalp-bot` affiche `active (running)` |
| Message Telegram « Robot démarré — mode PAPER » | reçu |
| `/status` sur Telegram | `Flux : connecté`, dernier message il y a moins de 2 s, horloge inférieure à 1000 ms |
| Données enregistrées | `sudo ls -R /opt/btc_scalp/var/data/kraken/PF_XBTUSD/` contient des fichiers `trades_*`, `book_*` et `ticker_*` |
| Fichiers relisibles | `cd /opt/btc_scalp && sudo -u btcbot .venv/bin/python -c "from bot.recorder import *; f=list_files('var/data','PF_XBTUSD','trades')[-1]; print(sum(1 for _ in read_records(f)))"` |
| Règles de l'instrument | aucune alerte `instrument_rules` (tick 1 USD et lot 0,0001 confirmés par `/instruments`) |
| Spread observé | `/status` montre la ligne « Spread carnet » ; une médiane de plus de 1 tick signifie que l'hypothèse du backtest est optimiste (à signaler) |
| Stratégie | `stratégie none` (par défaut) : la table `decisions` ne contient que `no_signal` ou `paused` |
| Disque | `df -h /opt/btc_scalp` ; prévoir environ 0,5 à 2 Go par jour selon l'activité du carnet (**à mesurer** la première semaine) |

Une fois par jour : `/status`, l'espace disque, et le journal
`sudo -u btcbot /opt/btc_scalp/.venv/bin/python -m bot status`.

## 4. Pannes : que faire

Rappel (cahier des charges, section 8) : arrêter le programme **ne sécurise pas** une
position. Chaque position paper a un stop reduce-only simulé dès l'entrée.

### 4.1 Perte du flux Kraken
- **Automatique :** après 15 s sans message, plus aucune entrée et alerte CRIT
  `stale_feed`. La reconnexion se fait avec un backoff exponentiel (1 s puis jusqu'à
  60 s). Après une reconnexion, pause de 15 min (les fenêtres de facteurs contiennent un
  trou). Si la coupure dépasse 120 s avec une position ouverte, celle-ci est fermée au
  marché (au premier trade disponible).
- **Manuel :** si la coupure dure, vérifier le réseau du VPS
  (`curl -I https://futures.kraken.com`) et la page de statut Kraken. Rien d'autre à
  faire : le robot reprend seul.

### 4.2 Crash du programme
- **Automatique :** systemd redémarre le programme 5 s plus tard. Au démarrage, le robot
  **rapproche** son état SQLite (`var/state.sqlite`) de la plateforme paper
  (`var/paper_exchange.sqlite`) :
  - une intention d'entrée jamais transmise est **abandonnée** (jamais renvoyée) ;
  - un stop ou un take-profit non transmis est renvoyé avec le **même identifiant client**
    (aucun doublon possible) ;
  - toute divergence inexpliquée (position ou ordre inconnu, position sans stop) déclenche
    une alerte CRIT `reconcile` et une **pause des entrées**.
- **Manuel en cas de divergence :** `python -m bot status`, examiner les tables `orders` et
  `positions` (`sqlite3 var/state.sqlite`). Une fois la cause comprise :
  `python -m bot resume --confirm JE_CONFIRME_LA_REPRISE` puis
  `sudo systemctl restart btc-scalp-bot`.
- Une configuration refusée (code de sortie 2 : mode ≠ paper, section interdite) n'est pas
  relancée.
- Si le service redémarre en boucle (20 fois en 10 min), systemd l'arrête. Consulter
  `journalctl -u btc-scalp-bot -n 200`.

### 4.3 Coupure du VPS (redémarrage, panne de l'hébergeur)
Le service démarre seul au boot (`enabled`). Le même rapprochement qu'en 4.2 s'applique.
Les trades rejoués par Kraken depuis avant l'arrêt ne sont pas réappliqués à la
plateforme paper. Les enregistrements sont écrits toutes les 5 s : au plus 5 s de
données sont perdues.

### 4.4 Données anormales
Les cas détectés sont : prix ≤ 0, prix hors tick, saut de plus de 200 bp entre deux
trades, trou dans les numéros de séquence des trades, retard médian supérieur à 2 s, horloge
décalée de plus de 1 s, marché suspendu ou en post-only, règles de l'instrument
différentes des hypothèses.
- **Automatique :** pause des entrées (15 min, ou tant que la condition dure) et alerte.
- **Carnet croisé ou trou de séquence du carnet :** le carnet ne sert qu'à la surveillance
  (spread, comparaison des coûts). Le robot se réabonne au flux `book` sur la même
  connexion (au plus une fois par minute) ; **aucune pause des entrées**, aucun trou dans
  les trades. Compteurs `book_seq_gaps` et `book_resync_throttled` : voir les logs.
- **Manuel :** vérifier chrony (`chronyc tracking`) si l'alerte porte sur l'horloge.
  Vérifier les annonces Kraken si les règles de l'instrument ont changé : dans ce cas, ne
  rien modifier soi-même, prévenir Hugo, car les hypothèses de coûts sont à revoir.

### 4.5 Coupe-circuit déclenché (perte de 150 USDT ou de 15 % depuis le sommet)
- **Automatique :** position fermée, arrêt définitif des entrées, alerte CRIT
  `kill_switch`. L'arrêt **persiste après un redémarrage** (état `risk_state` en SQLite).
- **Manuel :** analyser les trades (`positions`, `decisions`). La levée exige **une
  décision explicite d'Hugo** : elle ne passe ni par Telegram ni par `bot resume`. Elle
  se fait dans le code avec `RiskManager.manual_reset_kill("HUGO_AUTORISE_REDEMARRAGE")`
  sur l'état sauvegardé, robot arrêté.

### 4.6 Limites journalière ou hebdomadaire (−3 % / −6 %)
Ces limites sont automatiques : arrêt jusqu'au lendemain UTC (ou jusqu'au lundi), puis
reprise à 50 % de taille et retour à 100 % après une période complète sans nouvelle
limite. Aucune action n'est nécessaire.

### 4.7 Disque plein
Une alerte `disk_low` part sous 2 Go libres et une alerte `recorder_errors` en cas
d'échec d'écriture. Il faut archiver `var/data/` hors du VPS (par exemple
`rsync -a vps:/opt/btc_scalp/var/data/ ./kraken_data/`) avant de supprimer quoi que ce
soit, car la consigne est de tout conserver.

## 5. Arrêt d'urgence

```bash
sudo btc-scalp-emergency-stop
```
Ce script pose l'arrêt persistant, arrête le service, annule les entrées paper (les stops
sont conservés) et affiche l'état. Depuis le téléphone, `/stop` sur Telegram produit le
même effet sur les entrées (fermeture au marché de la position paper). `/stop` est
**persistant** : après un redémarrage, le robot reste arrêté.

## 6. Reprise après /stop ou arrêt d'urgence

Sur le serveur uniquement (jamais par Telegram) :
```bash
cd /opt/btc_scalp
sudo -u btcbot .venv/bin/python -m bot status        # comprendre pourquoi on s'est arrêté
sudo -u btcbot .venv/bin/python -m bot resume --confirm JE_CONFIRME_LA_REPRISE
sudo systemctl start btc-scalp-bot                   # ou restart
```
`resume` ne lève **pas** le coupe-circuit (voir 4.5).

## 7. Mise à jour du code

```bash
cd ~/btc_scalp && git pull           # ou scp de la nouvelle version
sudo -u btcbot /opt/btc_scalp/.venv/bin/python -m bot stop     # pas de nouvelle entrée
sudo systemctl stop btc-scalp-bot
sudo bash bot/deploy/install.sh      # var/ (état, données) est préservé
sudo -u btcbot /opt/btc_scalp/.venv/bin/python -m bot resume --confirm JE_CONFIRME_LA_REPRISE
sudo systemctl start btc-scalp-bot
```

## 8. Fichiers

| Chemin | Contenu |
|---|---|
| `/opt/btc_scalp/bot/config.toml` | mode (paper), flux, stratégie, stockage, Telegram |
| `/opt/btc_scalp/config/default.toml` | risque, coûts, sorties, barres dont la grâce de clôture `[bars].close_grace_ms` (source unique, versionnée : elle entre dans la latence d'entrée du backtest) |
| `/etc/btc_scalp/telegram.env` | secrets Telegram (hors dépôt) |
| `var/state.sqlite` | décisions, ordres, positions, compteurs de risque, /stop |
| `var/paper_exchange.sqlite` | plateforme simulée (ordres et exécutions paper) |
| `var/data/kraken/PF_XBTUSD/AAAA-MM-JJ/*.jsonl.gz` | enregistrements horaires bruts (trades, book, ticker, heartbeat, events) |
| `var/log/bot.log*` | journaux (rotation quotidienne, compressés, conservés) |
