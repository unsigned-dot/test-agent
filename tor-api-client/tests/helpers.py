"""Utilitaires partagés par les tests."""

from __future__ import annotations

import requests


def make_response(status: int = 200, body: bytes = b"{}", headers: dict | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body
    response._content_consumed = True
    response.headers.update(headers or {})
    response.url = "https://api.example.com/"
    return response
