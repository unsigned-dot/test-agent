"""Exemple d'utilisation de TorApiClient.

Lancer : python examples/basic_usage.py
"""

from tor_api_client import (
    RateLimitedError,
    RetriesExhaustedError,
    TorApiClient,
    TorApiClientError,
    setup_logging,
)


def main() -> None:
    setup_logging("INFO")

    with TorApiClient() as client:  # configuration lue depuis .env / l'environnement
        try:
            response = client.get("https://httpbin.org/get", params={"demo": "1"})
            print("GET :", response.status_code, response.json().get("origin"))

            response = client.post("https://httpbin.org/post", json={"hello": "world"})
            print("POST :", response.status_code, response.json().get("json"))

            # Une 404 n'est PAS une erreur réseau : la réponse est renvoyée telle quelle.
            response = client.get("https://httpbin.org/status/404")
            print("404 :", response.status_code)
        except RateLimitedError as exc:
            print(f"Service limité, réessayer dans {exc.retry_after:.0f} s")
        except RetriesExhaustedError as exc:
            print(f"Abandon après {exc.attempts} tentatives : {exc}")
        except TorApiClientError as exc:
            print(f"Erreur : {exc}")


if __name__ == "__main__":
    main()
