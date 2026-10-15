"""Robot PAPER Kraken Derivatives EU (PF_XBTUSD).

Problème résolu : faire tourner en continu, sur un petit VPS, la même chaîne
barres -> facteurs -> décision -> veto du risque -> exécution simulée que la recherche,
sur le flux public Kraken en temps réel, en enregistrant les données de marché.

Seul le mode "paper" existe : aucun ordre réel ne peut être envoyé par ce paquet.
"""
__version__ = "0.1.0"
