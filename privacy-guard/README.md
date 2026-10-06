# privacy-guard

Outil Python qui **automatise l'audit et le durcissement de sécurité** d'une
machine Debian/Ubuntu. Il vérifie les mesures qui comptent le plus pour un
particulier et peut appliquer, de façon contrôlée, les corrections sûres.

> **Philosophie de sûreté**
> - **Audit en lecture seule par défaut** : `privacy-guard audit` ne modifie rien.
> - Les corrections ne s'appliquent qu'avec la sous-commande **`apply`**, qui
>   **affiche chaque commande et demande confirmation** (défaut : non).
> - L'outil ne **réduit jamais** une protection et ne fait rien de destructif.
>   Il ne propose de correction automatique que lorsqu'elle est **idempotente et
>   sans risque**. Les actions délicates (chiffrement du disque, SSH) sont
>   signalées mais **laissées à votre main**, avec la marche à suivre.

## Ce qui est vérifié

| Vérification | Gravité | Correction auto |
|---|---|---|
| Chiffrement du disque (LUKS) | haute | non (se fait à l'installation) |
| Pare-feu ufw (installé, actif, entrant refusé) | haute | oui |
| Mises à jour de sécurité automatiques (unattended-upgrades) | haute | oui |
| Durcissement SSH (mot de passe/root désactivés) | haute | non (risque de verrouillage) |
| DNS chiffré (DNS-over-TLS via systemd-resolved) | moyenne | oui |
| Secure Boot (UEFI) | basse | non (réglage firmware) |
| Comptes sans mot de passe | haute | non (action manuelle ciblée) |

Les vérifications non applicables (commande ou service absent) sont **ignorées
(SKIP)**, jamais comptées comme des échecs. L'outil ne touche ni au navigateur,
ni au fingerprinting, ni au cloisonnement OS : ces aspects relèvent du Tor
Browser et de systèmes dédiés (Tails, Whonix, Qubes), pas d'un script.

## Installation

```bash
cd privacy-guard
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Aucune dépendance externe : l'outil n'utilise que la bibliothèque standard.

## Utilisation

```bash
# 1. Auditer (lecture seule). Code de sortie 1 s'il reste au moins un échec.
privacy-guard audit

# 2. Sortie JSON (pour l'intégrer à un tableau de bord ou un cron)
privacy-guard audit --json

# 3. Appliquer les corrections sûres, une par une, avec confirmation
privacy-guard apply

# 4. Sans confirmation (automatisation ; à réserver à un environnement maîtrisé)
privacy-guard apply --yes
```

`python -m privacy_guard audit` fonctionne aussi.

### Exemple de sortie

```
[FAIL] Pare-feu (ufw) (high)
       ufw n'est pas installé.
       → Installer et activer ufw avec une politique par défaut « refuser l'entrant ».
       Commandes de remédiation (--apply) :
         $ sudo apt-get update
         $ sudo apt-get install -y ufw
         $ sudo ufw default deny incoming
         $ sudo ufw default allow outgoing
         $ sudo ufw --force enable

Résumé : 1 OK, 3 échec(s), 0 avertissement(s), 3 ignoré(s), 0 erreur(s).
```

## Automatiser l'audit (cron)

Pour recevoir un état régulier sans rien modifier :

```bash
# Audit quotidien, journalisé ; n'applique aucune correction.
0 9 * * * /chemin/.venv/bin/privacy-guard audit >> ~/privacy-guard.log 2>&1
```

Le code de sortie `1` quand un échec subsiste permet de déclencher une alerte.
**N'automatisez pas `apply --yes`** sans avoir vérifié l'audit au moins une fois
à la main : certaines corrections demandent sudo et modifient le système.

## Pourquoi certaines corrections ne sont pas automatisées

- **Chiffrement du disque** : LUKS se met en place à l'installation. L'activer
  après coup implique de réinstaller ou de manipuler les partitions — trop
  risqué pour un script. L'outil le détecte et explique la marche à suivre.
- **SSH (désactiver les mots de passe)** : le faire sans clé SSH valide en place
  vous **verrouille dehors**. L'outil signale le problème et rappelle de
  configurer `ssh-copy-id` d'abord.
- **Secure Boot** : réglage du firmware UEFI, hors de portée d'un script.
- **Comptes sans mot de passe** : la bonne action (définir un mot de passe ou
  verrouiller le compte) dépend du compte ; à faire à la main.

## Architecture

```
privacy-guard/
├── pyproject.toml
├── README.md
├── src/privacy_guard/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py          # sous-commandes audit / apply
│   ├── models.py       # Status, Severity, Finding
│   ├── system.py       # CommandRunner (injectable) + détection OS
│   ├── checks.py       # une fonction par vérification
│   ├── runner.py       # orchestration, isolation des exceptions
│   ├── report.py       # rendu texte / JSON
│   └── apply.py        # application contrôlée des corrections
└── tests/              # tests unitaires (runner mocké, aucun accès système réel)
```

Toute interaction système passe par `CommandRunner`, ce qui rend chaque
vérification testable sans toucher à la machine.

## Tests

```bash
pytest
```

Les tests injectent un faux runner : ils ne lisent ni ne modifient le système
réel, et couvrent chaque vérification (présent / absent / cas limites), le
rendu, et la logique d'`apply` (confirmation, exécution, arrêt sur erreur).

## Limites

`privacy-guard` couvre l'hygiène système de base. Il **ne remplace pas** le Tor
Browser pour la navigation anonyme, ni un système cloisonné (Tails/Whonix/Qubes)
pour un modèle de menace élevé, ni la bonne hygiène d'identité (ne pas mélanger
comptes nominatifs et activité privée). C'est un socle, pas une solution totale.
