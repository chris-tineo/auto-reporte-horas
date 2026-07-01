"""Cliente del round-trip de decisiones con Claudia (repo aparte).

Cuando el bot no puede resolver algo (ej. una semana sin datos), crea una
"decisión pendiente" en Claudia (`POST /api/decisions`), que el usuario responde
desde la PWA; la siguiente corrida lee las respondidas (`GET /api/decisions/answered`)
y las aplica, marcándolas (`POST /api/decisions/{id}/applied`).

Auth: mismo `X-Api-Key` que notify. La URL base se deriva de CLAUDIA_NOTIFY_URL
(`.../api/notify` → `.../api`). Best-effort: si Claudia no está, degrada a
comportarse como antes (semana sin datos → se omite).
"""
import logging
import os

import requests

log = logging.getLogger("decisions")


def _base() -> tuple[str | None, str | None]:
    url = os.getenv("CLAUDIA_NOTIFY_URL")
    key = os.getenv("CLAUDIA_API_KEY")
    if not (url and key):
        return None, None
    return url.rsplit("/", 1)[0], key  # .../api/notify -> .../api


def ask(key: str, question: str, options: list[str],
        context: str | None = None, timeout: float = 10) -> tuple[dict | None, bool]:
    """Crea (o recupera, idempotente por `key`) una decisión pendiente.
    Devuelve (decisión, created) — created=True solo si es nueva (HTTP 201)."""
    base, api_key = _base()
    if not base:
        return None, False
    try:
        r = requests.post(f"{base}/decisions",
                          json={"key": key, "question": question,
                                "options": list(options), "context": context},
                          headers={"X-Api-Key": api_key}, timeout=timeout)
        r.raise_for_status()
        return r.json(), r.status_code == 201
    except requests.RequestException as e:
        log.warning(f"ask() falló (best-effort): {e}")
        return None, False


def answered(timeout: float = 10) -> list[dict]:
    """Decisiones respondidas por el usuario y aún no aplicadas."""
    base, api_key = _base()
    if not base:
        return []
    try:
        r = requests.get(f"{base}/decisions/answered",
                         headers={"X-Api-Key": api_key}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning(f"answered() falló (best-effort): {e}")
        return []


def mark_applied(decision_id: str, timeout: float = 10) -> bool:
    base, api_key = _base()
    if not base:
        return False
    try:
        r = requests.post(f"{base}/decisions/{decision_id}/applied",
                          headers={"X-Api-Key": api_key}, timeout=timeout)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning(f"mark_applied() falló (best-effort): {e}")
        return False
