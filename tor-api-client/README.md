# tor-api-client

Client API Python dont **tout le trafic passe par Tor** (SOCKS5h, DNS compris),
avec un mécanisme de **résilience réseau** : retry borné, backoff, respect de
`Retry-After`, et demande d'un **nouveau circuit Tor (NEWNYM)** via le ControlPort
authentifié quand une erreur réseau le justifie.

> **Usage légitime uniquement.** Ce projet améliore la résilience d'appels API
> légitimes face aux aléas du réseau Tor (circuit lent, nœud de sortie
> défaillant…). Il ne change **jamais** de circuit sur un 429 ou une erreur
> 4xx et n'est pas conçu pour contourner des limitations de débit, CAPTCHA,
> bannissements ou contrôles d'accès.

```
Application Python ──► SOCKS5h 127.0.0.1:9050 ──► réseau Tor ──► API distante
Application Python ──► ControlPort 127.0.0.1:9051 ──► authentification (cookie) ──► NEWNYM
```

## Sommaire

- [Hypothèses sur la distribution](#hypothèses-sur-la-distribution)
- [Structure](#structure)
- [Installation pas à pas](#installation-pas-à-pas)
- [Utilisation](#utilisation)
- [Politique d'erreurs et de retry](#politique-derreurs-et-de-retry)
- [Configuration (.env)](#configuration-env)
- [Sécurité](#sécurité)
- [Dépannage](#dépannage)

## Hypothèses sur la distribution

- Testé pour **Debian 12 (bookworm)**, **Ubuntu 22.04** et **Ubuntu 24.04**.
  Python **≥ 3.10** requis (Debian 11 fournit 3.9 : non supporté).
- Paquet `tor` des dépôts de la distribution (ou du dépôt officiel
  deb.torproject.org, même organisation des fichiers).
- Spécificités Debian/Ubuntu utilisées dans ce document :
  - le service réel est `tor@default.service` (`tor.service` n'est qu'un
    service « parapluie ») ;
  - Tor tourne sous l'utilisateur **`debian-tor`** ;
  - `/usr/share/tor/tor-service-defaults-torrc` place le cookie dans
    **`/run/tor/control.authcookie`** avec `CookieAuthFileGroupReadable 1` ;
  - Debian 12 et Ubuntu 23.04+ interdisent `pip install` hors virtualenv
    (PEP 668) : le virtualenv est obligatoire.

## Structure

```
tor-api-client/
├── README.md
├── pyproject.toml            # paquet, dépendances, commande `tor-api-client`
├── requirements.txt          # dépendances d'exécution
├── requirements-dev.txt      # + pytest
├── .env.example              # modèle de configuration (copier en .env)
├── .gitignore                # exclut .env
├── config/
│   └── torrc.example         # lignes à ajouter dans /etc/tor/torrc
├── examples/
│   └── basic_usage.py
├── scripts/
│   └── check_tor.py          # test d'intégration manuel sur un vrai Tor
├── src/tor_api_client/
│   ├── __init__.py           # API publique
│   ├── __main__.py           # CLI : python -m tor_api_client / tor-api-client
│   ├── client.py             # TorApiClient, classification d'erreurs, retry
│   ├── tor.py                # rotate_tor_circuit, check_tor_connection, get_tor_ip...
│   ├── config.py             # TorConfig (env / .env) + validation
│   ├── exceptions.py         # hiérarchie d'exceptions
│   └── logging_config.py     # setup_logging, sanitize_url
└── tests/                    # tests unitaires (mocks, aucun Tor requis)
    ├── conftest.py
    ├── helpers.py
    ├── test_client.py
    ├── test_config.py
    └── test_tor.py
```

## Installation pas à pas

Toutes les commandes sont pour Debian/Ubuntu, à lancer depuis le dossier `tor-api-client/`.

### 1. Installer Python

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
python3 --version   # doit afficher 3.10 ou plus
```

### 2. Installer Tor

```bash
sudo apt install -y tor
tor --version
```

### 3. Configurer `/etc/tor/torrc`

Sauvegarde, puis ajout des lignes de [`config/torrc.example`](config/torrc.example) :

```bash
sudo cp /etc/tor/torrc /etc/tor/torrc.bak.$(date +%F)
sudo tee -a /etc/tor/torrc > /dev/null <<'EOF'

## --- tor-api-client ---
SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
CookieAuthFileGroupReadable 1
CookieAuthFile /run/tor/control.authcookie
EOF

# Vérification de la syntaxe (doit finir par "Configuration was valid")
sudo -u debian-tor tor --verify-config -f /etc/tor/torrc
```

Points importants :

- `ControlPort 127.0.0.1:9051` : écoute **uniquement en local**. N'utilisez
  jamais `0.0.0.0` ni une IP publique.
- Ne désactivez pas l'authentification : le client **refuse** un ControlPort
  sans authentification.
- Définir `SocksPort` dans `/etc/tor/torrc` remplace les `SocksPort` du fichier
  de valeurs par défaut Debian (dont le socket Unix `/run/tor/socks`). Ajoutez
  `SocksPort unix:/run/tor/socks WorldWritable` si un autre logiciel s'en sert.

### 4. Démarrer (ou redémarrer) Tor

```bash
sudo systemctl enable --now tor
sudo systemctl restart tor
systemctl status tor@default --no-pager
journalctl -u tor@default -e --no-pager | grep -i bootstrapped   # attendre "Bootstrapped 100% (done)"
```

### 5. Vérifier les ports 9050 et 9051

```bash
sudo ss -lntp | grep -E ':(9050|9051)\b'
```

Résultat attendu : deux lignes `LISTEN` sur **`127.0.0.1:9050`** et
**`127.0.0.1:9051`** (jamais `0.0.0.0` ni `*`).

```bash
# Test rapide du proxy SOCKS (DNS via Tor grâce à socks5h)
curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip
# => {"IsTor":true,"IP":"..."}
```

### 6. Permissions du cookie

Le cookie appartient à `debian-tor:debian-tor` en mode `0640`. L'utilisateur
qui exécute Python doit appartenir au groupe **`debian-tor`** pour le lire :

```bash
ls -l /run/tor/control.authcookie        # -rw-r----- 1 debian-tor debian-tor 32 ...
sudo usermod -aG debian-tor "$USER"
```

Le nouveau groupe n'est pris en compte **qu'après reconnexion** (fermer la
session, ou `ssh` à nouveau). Pour le shell courant uniquement : `newgrp debian-tor`.

```bash
id -nG | tr ' ' '\n' | grep -x debian-tor           # doit afficher debian-tor
head -c 32 /run/tor/control.authcookie > /dev/null && echo "cookie lisible"
```

Ne faites **pas** de `chmod o+r` sur le cookie : quiconque le lit peut
contrôler Tor. Pour un compte de service (systemd, cron), ajoutez ce compte au
groupe : `sudo usermod -aG debian-tor <compte>` (ou `SupplementaryGroups=debian-tor`
dans l'unité systemd).

> Alternative au cookie : mot de passe haché. `tor --hash-password 'phrase-longue'`,
> placer `HashedControlPassword 16:...` dans torrc, puis `TOR_CONTROL_PASSWORD=...`
> dans `.env` (jamais dans le code). Le cookie reste recommandé.

### 7. Créer le virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 8. Installer les dépendances

```bash
pip install -e ".[dev]"
# ou, sans installer le paquet : pip install -r requirements-dev.txt
```

Dépendances : `requests[socks]` (≥ 2.32, inclut PySocks pour `socks5h://`),
`stem` (≥ 1.8.2, ControlPort), `python-dotenv` (≥ 1.0), `pytest` (dev).

### 9. Configurer le `.env`

```bash
cp .env.example .env
chmod 600 .env
# éditer si besoin : nano .env
```

Avec la configuration Tor ci-dessus, les valeurs par défaut conviennent.

### 10. Lancer le programme

```bash
# Test d'intégration complet sur le vrai Tor (SOCKS, ControlPort, IsTor, IP, NEWNYM)
python scripts/check_tor.py

# CLI
tor-api-client check
tor-api-client ip
tor-api-client rotate
tor-api-client get https://httpbin.org/get
tor-api-client post https://httpbin.org/post --json '{"hello": "world"}'
python -m tor_api_client get https://httpbin.org/get   # équivalent

# Exemple Python
python examples/basic_usage.py
```

### 11. Lancer les tests

```bash
pytest
```

Les tests unitaires utilisent des mocks : ils n'ont besoin ni de Tor ni d'Internet.
`scripts/check_tor.py` sert de test d'intégration manuel sur une vraie installation.

## Utilisation

```python
from tor_api_client import TorApiClient, RetriesExhaustedError, setup_logging

setup_logging("INFO")

with TorApiClient() as client:          # lit .env / l'environnement
    response = client.get("https://example.com")
    print(response.status_code)

    response = client.post("https://example.com/api", json={"hello": "world"})

    # 4xx : renvoyée telle quelle, sans retry ni changement de circuit
    response = client.get("https://example.com/introuvable")
    if response.status_code == 404:
        ...
```

Fonctions utilitaires :

```python
from tor_api_client import (
    TorConfig, check_tor_connection, get_tor_ip, rotate_tor_circuit, wait_for_tor_ready,
)

config = TorConfig.from_env()
assert check_tor_connection(config)     # True si TOR_CHECK_URL répond IsTor=true
print(get_tor_ip(config))               # IP vue par IP_CHECK_URL
rotate_tor_circuit(config)              # NEWNYM + attente TOR_NEWNYM_WAIT
wait_for_tor_ready(config)              # attend un circuit établi
print(get_tor_ip(config))               # peut être identique : NEWNYM ne garantit pas une nouvelle IP
```

Paramètres de `request()` / `get()` / `post()`… : ceux de
`requests.Session.request` (`params`, `json`, `data`, `headers`, `timeout`…),
plus `allow_unsafe_retry=True` pour autoriser à rejouer un POST/PATCH dont la
requête a pu atteindre le serveur. `proxies` est interdit (tout passe par Tor).

## Politique d'erreurs et de retry

| Situation | Exception | Retry | NEWNYM | Attente |
|---|---|---|---|---|
| Timeout de connexion / lecture | `RequestTimeoutError` | oui | **oui** | NEWNYM + Tor prêt |
| Erreur SOCKS de Tor (0x01, 0x02, 0x03, 0x05, 0x06…) | `TorProxyError` | oui | **oui** | NEWNYM + Tor prêt |
| Hôte injoignable / DNS (SOCKS 0x04, 0xF0-0xF7) | `DNSResolutionError` | oui | **oui** | NEWNYM + Tor prêt |
| Connexion interrompue (reset…) | `ConnectionFailedError` | oui* | **oui** | NEWNYM + Tor prêt |
| Port SOCKS injoignable (Tor arrêté) | `TorProxyUnavailableError` | oui | non | backoff exponentiel |
| Erreur TLS / certificat | `TLSError` | **non** | non | — |
| HTTP 429 | `RateLimitedError` si `Retry-After` > `RETRY_AFTER_MAX` | oui | **jamais** | `Retry-After` ou backoff |
| HTTP 500, 502, 503, 504 | `RetriesExhaustedError` à la fin | oui* | non (option `ROTATE_ON_5XX`) | `Retry-After` ou backoff |
| HTTP 400, 401, 403, 404, autres 4xx | aucune : réponse renvoyée | non | non | — |

\* Pour POST/PATCH, uniquement si `allow_unsafe_retry=True` ou
`RETRY_NON_IDEMPOTENT=true` (sinon risque de doublon côté serveur). Les échecs
de connexion (requête jamais envoyée) sont toujours rejouables.

Déroulé d'un retry avec changement de circuit :

1. log de l'erreur (type, méthode, URL nettoyée) ;
2. `rotate_tor_circuit()` : connexion + authentification au ControlPort,
   respect de l'intervalle minimal entre NEWNYM (`TOR_NEWNYM_MIN_INTERVAL`,
   partagé par tout le processus), envoi de `Signal.NEWNYM` ;
3. attente de `TOR_NEWNYM_WAIT` secondes ;
4. fermeture des connexions HTTP persistantes (elles restent sinon sur l'ancien circuit) ;
5. `wait_for_tor_ready()` : bootstrap à 100 % et circuit établi ;
6. nouvelle tentative ;
7. abandon (`RetriesExhaustedError`) après `1 + MAX_RETRIES` tentatives.

Si le ControlPort est injoignable ou Tor pas prêt, le client se replie sur un
backoff simple : le nombre total de tentatives reste borné.

## Configuration (.env)

| Variable | Défaut | Rôle |
|---|---|---|
| `TOR_SOCKS_HOST` / `TOR_SOCKS_PORT` | `127.0.0.1` / `9050` | proxy SOCKS (loopback obligatoire) |
| `TOR_CONTROL_HOST` / `TOR_CONTROL_PORT` | `127.0.0.1` / `9051` | ControlPort (loopback obligatoire) |
| `TOR_CONTROL_PASSWORD` | vide | vide = cookie ; sinon mot de passe `HashedControlPassword` |
| `REQUEST_TIMEOUT` | `15` | délai de lecture (s) |
| `CONNECT_TIMEOUT` | = `REQUEST_TIMEOUT` | délai de connexion (s) |
| `MAX_RETRIES` | `3` | nouvelles tentatives (0 à 10) |
| `TOR_NEWNYM_WAIT` | `10` | attente après NEWNYM (s) |
| `TOR_NEWNYM_MIN_INTERVAL` | `10` | intervalle minimal entre deux NEWNYM (s) |
| `TOR_READY_TIMEOUT` | `30` | attente max d'un circuit établi (s) |
| `BACKOFF_BASE` / `BACKOFF_MAX` | `1` / `30` | backoff exponentiel `base × 2^(n-1)` + gigue, plafonné |
| `RETRY_AFTER_MAX` | `120` | au-delà, abandon plutôt qu'attente |
| `ROTATE_ON_5XX` | `false` | NEWNYM aussi sur 5xx |
| `RETRY_NON_IDEMPOTENT` | `false` | rejouer POST/PATCH potentiellement reçus |
| `REQUIRE_TOR` | `false` | fail-closed : confirmer que le trafic passe par Tor avant d'émettre, sinon refuser (anti-fuite) |
| `TOR_CHECK_URL` | `https://check.torproject.org/api/ip` | doit renvoyer `{"IsTor": ...}` |
| `IP_CHECK_URL` | `https://check.torproject.org/api/ip` | JSON (`IP`, `ip`, `origin`, `query`) ou texte brut |
| `ALLOWED_HOSTS` | vide | liste blanche d'hôtes (sous-domaines inclus) |
| `USER_AGENT` | vide | User-Agent optionnel |
| `LOG_LEVEL` | `INFO` | niveau de log (CLI / scripts) |

Les variables déjà présentes dans l'environnement sont prioritaires sur le `.env`.

## Vie privée : ce que ce client protège, et ce qu'il ne protège pas

Ce client sécurise le **transport** d'appels API : tout passe par Tor, la
résolution DNS comprise. C'est utile pour ne pas exposer votre IP à l'API
appelée. Mais **un script Python via Tor ne vous rend pas anonyme pour naviguer**,
et plusieurs protections essentielles se situent *hors* de ce code.

**Ce que le client garantit (couche transport) :**

- Trafic et DNS via Tor, grâce à `socks5h://` (le client **refuse** de démarrer
  si la session n'utilise pas `socks5h` : pas de résolution DNS en local).
- `trust_env = False` et argument `proxies` interdit : les variables
  `HTTP(S)_PROXY` / `NO_PROXY` ne peuvent pas faire sortir le trafic hors de Tor.
- `REQUIRE_TOR=true` (*fail-closed*) : avant la première requête, le client
  confirme via `TOR_CHECK_URL` que le trafic sort bien par Tor et **refuse
  d'émettre** sinon. À activer si une fuite en clair serait inacceptable.
- Logs sans URL complète, identifiants ni corps.

**Ce que le client NE protège PAS (à traiter en dehors) :**

- **Navigation web anonyme → utilisez le [Tor Browser](https://www.torproject.org/).**
  C'est le seul moyen sérieux d'éviter le *fingerprinting* : votre navigateur
  (polices, taille d'écran, canvas, extensions…) vous identifie bien au-delà de
  l'IP. Un client HTTP maison n'uniformise rien de tout cela.
- **Fuites WebRTC** : propres au navigateur, sans objet pour un client `requests`,
  mais à neutraliser si vous naviguez par ailleurs.
- **Cloisonnement du système** : pour un vrai modèle de menace, faites passer
  *tout* le trafic de la machine par Tor et isolez les applications avec un
  système dédié — **[Tails](https://tails.net/)** (amnésique, live USB),
  **[Whonix](https://www.whonix.org/)** (gateway Tor + workstation isolée) ou
  **[Qubes OS](https://www.qubes-os.org/)**. Seul ce niveau empêche qu'une appli
  mal configurée contourne Tor.
- **Corrélation de trafic** : Tor ne protège pas contre un adversaire capable
  d'observer à la fois l'entrée et la sortie du réseau.
- **Ce que vous envoyez** : identifiants, cookies, données personnelles dans le
  corps ou les en-têtes restent visibles du service appelé, Tor ou pas.

Tor est lent par conception (trois sauts chiffrés) : ne cherchez pas à « accélérer »
en multipliant circuits et concurrence, cela dégrade l'anonymat et surcharge un
réseau tenu par des bénévoles.

## Sécurité

- SOCKS et ControlPort **uniquement en loopback** : refus de toute autre adresse dans la configuration.
- **Authentification obligatoire** : un ControlPort sans authentification est refusé.
- Aucun secret dans le code ; `.env` exclu de Git ; le mot de passe n'apparaît pas dans `repr(TorConfig)`.
- `socks5h://` : la résolution DNS est faite par Tor (pas de fuite DNS).
- `session.trust_env = False` : les variables `HTTP(S)_PROXY` / `NO_PROXY` ne peuvent pas faire sortir le trafic hors de Tor ; l'argument `proxies` est interdit.
- Timeouts systématiques ; retries et NEWNYM bornés ; intervalle minimal entre NEWNYM.
- Logs sans query string, identifiants, en-têtes ni corps (`sanitize_url`).
- Validation d'URL : http/https uniquement, hôte requis, pas d'adresse locale/privée, liste blanche optionnelle. Avertissement en cas de HTTP en clair (le nœud de sortie peut lire le trafic).
- Erreurs TLS jamais retentées silencieusement.
- Les messages des exceptions sont sûrs, mais l'exception `requests` d'origine
  (chaînée via `__cause__`) peut contenir l'URL complète : évitez d'afficher des
  tracebacks complets dans des logs partagés.

## Dépannage

**`Port SOCKS de Tor injoignable` / `[Errno 111] Connection refused`**
Tor ne tourne pas ou n'écoute pas sur 9050.
```bash
systemctl status tor@default --no-pager
journalctl -u tor@default -e --no-pager
sudo ss -lntp | grep 9050
```

**`ControlPort injoignable sur 127.0.0.1:9051`**
La ligne `ControlPort 127.0.0.1:9051` manque ou Tor n'a pas été redémarré.
```bash
grep -nE '^(ControlPort|CookieAuth)' /etc/tor/torrc
sudo -u debian-tor tor --verify-config -f /etc/tor/torrc && sudo systemctl restart tor
```

**`Cookie Tor illisible` / `UnreadableCookieFile` / `Permission denied`**
L'utilisateur n'est pas (encore) dans le groupe `debian-tor`.
```bash
id -nG                                     # debian-tor doit apparaître
sudo usermod -aG debian-tor "$USER"        # puis se reconnecter
ls -ld /run/tor; ls -l /run/tor/control.authcookie
```
Vérifiez aussi `CookieAuthFileGroupReadable 1`. Sous un autre service (Docker,
systemd avec `ProtectSystem`, etc.), le fichier `/run/tor/control.authcookie`
doit être visible depuis le processus Python.

**`Le ControlPort exige un mot de passe`**
torrc contient `HashedControlPassword` : renseignez `TOR_CONTROL_PASSWORD` dans
`.env`, ou passez à `CookieAuthentication 1`.

**`Mot de passe du ControlPort refusé`**
Le mot de passe ne correspond pas au hash ; régénérez-le avec `tor --hash-password`.

**`Le ControlPort n'exige aucune authentification : configuration refusée`**
Ajoutez `CookieAuthentication 1` dans torrc et redémarrez Tor.

**`Tor n'est pas prêt après 30 s` / timeouts répétés**
Tor n'a pas fini son bootstrap ou le réseau bloque Tor.
```bash
journalctl -u tor@default --no-pager | grep -i bootstrapped | tail -3
```
Si bloqué sous 100 % : pare-feu, réseau filtrant (utiliser des bridges), horloge
système fausse (`timedatectl`).

**`Support SOCKS absent : installez 'requests[socks]'`**
```bash
pip install "requests[socks]"
```

**`Rate limiting NEWNYM request: delaying by N second(s)` dans les logs de Tor**
Normal : Tor espace les NEWNYM. Augmentez `TOR_NEWNYM_MIN_INTERVAL` si vous le voyez souvent.

**Même IP après NEWNYM**
Normal : NEWNYM crée de nouveaux circuits mais peut réutiliser le même nœud de sortie.

**`check` renvoie ÉCHEC alors que Tor fonctionne**
`TOR_CHECK_URL` doit renvoyer un JSON avec `IsTor` (format de
`check.torproject.org/api/ip`). Un service qui ne renvoie que l'IP convient à
`IP_CHECK_URL`, pas à `TOR_CHECK_URL`.

**`pip install` : `externally-managed-environment`**
Vous n'êtes pas dans le virtualenv : `source .venv/bin/activate`.

**`ConfigurationError: ... doivent être joignables uniquement en local`**
`TOR_SOCKS_HOST` / `TOR_CONTROL_HOST` doivent valoir `127.0.0.1`, `::1` ou `localhost`.
