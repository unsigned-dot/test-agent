# btc_scalp — recherche étape 1 (robot BTC perpétuel)

Pipeline de **recherche** pour le robot de scalping BTC perpétuel : exécution prévue sur Kraken
Derivatives EU (PF_XBTUSD), recherche sur les données publiques Binance USDⓈ-M. Le cahier des
charges v2 (14/10/2026) fait foi.

Étape 1 seulement : **un** modèle de gradient boosting (un classifieur long, un short) qui estime la
probabilité qu'un trade soit rentable après coûts, et qui ne trade pas sous le seuil. Aucune stratégie
n'est validée. Aucun résultat sur BTC n'a encore été produit : les seuls chiffres obtenus jusqu'ici
viennent de données **synthétiques** et ne disent rien de BTC.

## Installation (Windows, PowerShell, Python ≥ 3.11)

```powershell
cd C:\chemin\vers\btc_scalp
py -3.11 -m venv .venv            # ou py -3.13
.\.venv\Scripts\Activate.ps1      # si bloqué : Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Dépendances : numpy, pandas, scipy, scikit-learn, matplotlib (rien d'autre ; TOML lu par `tomllib`).

## Commandes, dans l'ordre

```powershell
# 0. Contrôle du code sur données synthétiques (≈ 5 min ; --quick sans les sensibilités)
python scripts/run_pipeline.py synthetic-demo

# 1. Données publiques Binance (24 mois, vérification CHECKSUM SHA-256), puis contrôles qualité
python scripts/run_pipeline.py download
python scripts/run_pipeline.py validate-data

# 2. Recherche : entraînement sur dev (60 %), choix de la cible et du seuil sur val (20 %)
python scripts/run_pipeline.py research --run-id r1

# 3. Walk-forward purgé sur dev + val (le test reste fermé)
python scripts/run_pipeline.py walkforward --run-id r1

# 4. Sensibilités sur val : profils binance / kraken, latence d'entrée 250 / 600 / 1000 ms, slippage, frais, seuil voisin
python scripts/run_pipeline.py sensitivity --run-id r1 --period val

# 5. Test final verrouillé (20 % final) : UNE configuration, sans retouche ensuite
python scripts/run_pipeline.py final-test --run-id r1
python scripts/run_pipeline.py sensitivity --run-id r1 --period test

# 6. Rapport
python scripts/run_pipeline.py report --run-id r1
```

Options globales : `--config chemin.toml`, `--runs-dir dossier`. `research` accepte aussi
`--synthetic null|signal --days N --rate R --seed S` pour un run synthétique pas à pas.

Chaque run écrit dans `runs/<id>/` : `run.log`, `config.toml` (copie) et `config_hash.txt`, `split.json`,
`val_results.csv`, `selection.json`, `model/model.pkl` + `model/model.json` (métadonnées),
`walkforward*.{json,csv}`, `sensitivity_*.json`, `final_test_lock.json`, `final_test*.{json,csv}`,
`equity_test.png` (équité réalisée nette, brute et mark-to-market), `pnl_by_hour_test.png`,
`pnl_by_regime_test.png`, `report.md`.

## Protocole (section 7)

- **Cible sans fuite** : label à la barre k = « le prix traverse +X % dans le sens du trade avant
  −stop, dans l'horizon H », calculé uniquement avec les trades strictement postérieurs à
  `ts_k + latence`. Elle ne sert jamais de feature (vérifié par les tests).
- Grille courte fixée à l'avance : (X = 12 bp, stop 25 bp, H = 60 s) et (35 bp, 25 bp, 300 s).
  Les sorties du trade simulé reprennent celles de la cible. Configurable avec `[ml].label_grid`.
- **Modèle** : `HistGradientBoostingClassifier` avec des hyperparamètres fixés à l'avance
  (`scalp/model.py: HYPERPARAMS`) et une graine fixe.
- **Décision** : entrer si `proba ≥ seuil` ET `p·(TP − coûts) − (1−p)·(stop + coûts) > 0`. Coûts =
  frais + slippage + 1 tick de spread du profil de plateforme. Le risque garde ensuite son veto
  (`scalp/risk.py`).
- **Découpage** : 60 / 20 / 20 en jours de trading entiers (week-ends exclus), chronologique, sans
  recouvrement. Le seuil et la cible se choisissent **uniquement sur val**.
- **Walk-forward** avec purge (≥ horizon + latence + attente du fill) et embargo.
- **Verrou** : `final_test_lock.json` (empreinte de la config et des paramètres) est écrit **avant**
  la lecture du test. Une 2e évaluation avec une autre configuration est refusée, sauf avec
  `--force`. Un `--force` est journalisé dans le verrou, le log et le rapport. Sur données réelles,
  le registre global `runs/final_test_registry.jsonl` refuse aussi une période de test qui
  chevauche une période déjà évaluée avec une autre empreinte (nouveau `--run-id`, verrou supprimé).
- **Latence d'entrée = celle du robot** : le robot ne clôt une barre qu'à sa fin + `[bars].close_grace_ms`
  (300 ms), puis l'ordre met `[costs].latency_ms` (300 ms : retard du flux, calcul, réseau) à arriver.
  Le backtest, la cible et la purge utilisent la même somme (`scalp.config.entry_latency_ms`, 600 ms :
  hypothèse prudente pour un VPS européen). La grâce n'existe qu'à un endroit (`config/default.toml`,
  lu aussi par le robot). Pose du TP et sortie à durée max : `latency_ms` seule, comme le robot.
- **Facteurs du modèle = facteurs du robot** : uniquement `FEATURE_COLS_ML` (20 facteurs calculés depuis le
  seul flux de trades, donc à l'identique par `bot/bars.py` ; test de parité live/batch). `sec_to_funding`
  suit l'intervalle de funding de la plateforme d'exécution (1 h sur Kraken), en recherche comme en live.
  Les facteurs issus des fichiers Binance metrics / funding / spot (`oi_chg_*`, `last_funding`,
  `basis_bps`…) ne sont **jamais** donnés au modèle (refus explicite) ; ils restent calculés pour
  d'éventuelles règles de référence, non destinées au live.
- **Funding** : convention interne `ts` = FIN de la période couverte. Binance : `calc_time` = instant de
  règlement (fin, parfois quelques ms après l'heure pile). Kraken (`historicalfundingrates`) :
  `timestamp` = DÉBUT de la période horaire, ramené à la fin par `loaders.kraken_funding`.
- **Facteurs externes (référence seulement)** : une ligne `metrics` (create_time T) n'est utilisée qu'à T + 360 s
  (`[data].metrics_lag_seconds` : T peut être le début de la période de 5 min ; instant de
  publication non documenté) ; funding : dernier taux réglé (calc_time ≤ instant de décision).
- **Régimes** : terciles de `vol_regime` (vol 1 h / médiane 24 h) fixés sur **dev** uniquement.
- **Essais** : chaque configuration évaluée sur val compte comme un essai pour le DSR. Pour les
  données réelles, le registre `runs/trials_registry.jsonl` est cumulé sur tous les runs.
- **Comparaisons** sur les mêmes jours avec le même moteur : références R1/R2, benchmarks naïfs,
  entrées aléatoires (et entrées aléatoires appariées au nombre de trades du modèle, 20 graines),
  ne rien faire, buy-and-hold.
- **Incertitude** : IC 95 % de l'espérance par bootstrap de **blocs de 3 jours** consécutifs
  (`[bootstrap].block_days`). IC et DSR sont « non calculables » (critères non remplis) sous
  `[bootstrap].min_days` = 20 jours : avec 2 jours, le bootstrap ne voit que la variabilité
  intra-journalière et donne un intervalle faussement étroit. Le DSR est aussi non calculable si la
  variance des Sharpe des essais est inconnue (sinon aucune déflation).
- **Verdict go/no-go** : les 7 critères chiffrés « Backtest → démo », plus un 8e ajouté par l'audit
  (≥ `[split].min_test_days` = 60 jours de trading dans le test final : ≈ 12 semaines, ≥ 20 blocs
  de 3 jours, plusieurs régimes ; 24 mois donnent ≈ 104 jours de test). **Tous** sont évalués avec
  le profil Kraken à la latence d'entrée de base de la config (600 ms, grâce de barre incluse ; modèle,
  benchmarks naïfs et entrées aléatoires), quel que soit `[venue].execution`. Le critère 7 (signal confirmé sur données Kraken) n'est **jamais** rempli par
  ce pipeline, donc le verdict reste NO-GO tant que la revalidation Kraken n'est pas faite.

## Structure

| Fichier | Rôle |
|---|---|
| `config/default.toml` | Tous les paramètres (coûts par plateforme, risque, sorties, split, ML) |
| `scalp/config.py` | Lecture TOML, profil de plateforme, empreinte de configuration |
| `scalp/data/download.py` | Téléchargement data.binance.vision + CHECKSUM |
| `scalp/data/loaders.py` | Lecture aggTrades / metrics / funding, cache .npz par jour |
| `scalp/data/validate.py` | Contrôles qualité |
| `scalp/data/synthetic.py` | Marche aléatoire nulle / signal implanté (tests uniquement) |
| `scalp/features.py` | Barres de 5 s et facteurs causaux |
| `scalp/execution.py` | Backtest événementiel sur les trades (latence, taker/maker, stop, funding) |
| `scalp/risk.py` | Mandat de risque (veto, limites jour/semaine, coupe-circuit, sizing) |
| `scalp/metrics.py` | Métriques, bootstrap par jours, PSR/DSR, mark-to-market |
| `scalp/signals.py` | Règles de référence et benchmarks naïfs |
| `scalp/model.py` | Cible, modèle long/short, décision, sauvegarde/chargement |
| `scalp/validation.py` | Split, purge, walk-forward, verrou, essais, évaluation, orchestration |
| `scalp/report.py` | `report.md` et graphiques |
| `scripts/run_pipeline.py` | CLI |
| `tests/` | Tests unittest (`python -m unittest discover -s tests`) |

Le robot live (`bot/`) est hors du périmètre de l'étape 1. Pour charger le modèle :
`TradeModel.load("runs/r1/model/model.pkl", expected_config_hash=...)`, puis `predict_proba(df)` et
`decide(df, ExecParams.from_cfg(cfg, model.spec.exits_override()))` sur les facteurs de
`features.add_features`. Le fichier pickle ne doit venir que de vos propres runs.

## Mémoire

Les vraies données sont traitées **mois par mois** : un mois de trades, plus un jour de chauffe et un
jour de marge, à la fois. Le jeu d'entraînement ne garde qu'une barre sur 6 (`[ml].train_stride`,
une décision toutes les 30 s) : ≈ 2 M échantillons × 20 facteurs float32 ≈ 200 Mo sur 24 mois.

Mesures (tracemalloc, flux SYNTHÉTIQUE de 12 M trades) : trades en mémoire 21 o/trade
(ts int64, px float64, qty float32, bm bool) ; pic transitoire de `make_bars` ≈ 34 o/trade ;
facteurs, cible et `simulate` < 8 o/trade. Pour un bloc mensuel de 33 jours à 2–3 M trades/jour
(66–99 M trades) : ≈ 4–6 Go de pic, ce qui tient dans 16 Go. Extrapolation, non mesurée sur les
vraies données. (Avant l'audit : identifiants gardés au chargement, concaténation avec copie
complète et deux index int64 recalculés à chaque `simulate` → pic estimé à 9–10 Go.)

## Disque

Le cache `data/cache/um/aggTrades/<jour>.npz` est **compressé** et ne garde que ts/px/qty/bm
(les identifiants ne servent qu'au contrôle qualité, fait sur les zips bruts par `validate-data`).
Mesuré sur un jour SYNTHÉTIQUE de 1,7 M trades : 5,4 o/trade (45 o/trade avant : non compressé avec
identifiants), écriture 2,7 s, lecture 0,2 s. Estimation pour 24 mois à 1,5–3 M trades/jour :
≈ 6–12 Go de cache (≈ 65 Go avant), à ajouter aux zips bruts (ordre de grandeur 20–50 Go, non
mesuré). La compressibilité des vrais fichiers peut différer.

## Limites (honnêteté)

- **Rien n'a tourné sur de vraies données** : l'environnement de développement n'avait pas d'accès
  réseau. `download`, `validate-data` et le chemin `BinanceSource` (mois par mois, metrics,
  funding) n'ont **jamais été exécutés** de bout en bout.
- Pas de carnet historique : le spread est inféré (1 tick) et la file d'attente est supposée
  pessimiste (dernier de la file). Le slippage et l'impact Kraken sont des hypothèses non mesurées.
- Le gain attendu traite un label 0 comme une perte au stop (conservateur). Le funding est ignoré
  dans la décision mais compté dans le backtest.
- IC et DSR exigent au moins 20 jours avec trades. La démo synthétique (14 jours, dont 2 de test)
  les déclare donc « non calculables » : elle contrôle le **code**, pas une significativité. Son
  contrôle « signal implanté » porte sur l'AUC val et le signe de l'espérance brute (test et
  walk-forward). Les AUC sur moins de 20 jours de val sont signalées comme bruit dans le rapport.
- Le walk-forward réutilise la cible et le seuil choisis sur val, et ses plis couvrent val : il est
  informatif, pas hors échantillon pour ce choix.
- `spot_leadlag` télécharge le spot mais l'écart spot/perp (`basis_bps`) n'est **pas** branché sur
  `BinanceSource` (facteur absent). Le loader lit pourtant correctement le spot (8 colonnes, µs).
- Le choix du seuil sur val est biaisé à la hausse : seul le test final verrouillé compte.
- Le critère 7 (données Kraken) n'est pas implémenté.
- Aucune performance passée ne garantit une performance future.
