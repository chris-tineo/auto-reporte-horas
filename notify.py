#!/usr/bin/env python3
"""Cliente de notificaciones del bot hacia Claudia (push vía FCM).

Claudia (repo aparte, chris-tineo/claudia) expone `POST /api/notify` autenticado
por API key (header `X-Api-Key`) que reusa su canal de push existente. Es una
frontera HTTP de una sola vía: el bot avisa, Claudia entrega el push al
teléfono/PWA. Best-effort: si falta config o Claudia está caída, NO rompe la
corrida.

Config en `.env`:
  CLAUDIA_NOTIFY_URL   p.ej. https://<host>.<tailnet>.ts.net/api/notify
  CLAUDIA_API_KEY      shared secret acordado con Claudia

NOTA: el endpoint en Claudia se implementa en una sesión aparte. Hasta entonces,
si las vars no están seteadas este cliente es un no-op silencioso; si lo están
pero Claudia no responde, loguea un warning y sigue.

Smoke-test manual (requiere Claudia arriba):  python notify.py
"""
import logging
import os

import requests

log = logging.getLogger("notify")

_LEVELS = ("info", "warn", "error", "question")


def notify(title: str, body: str, level: str = "info",
           data: dict | None = None, timeout: float = 10) -> bool:
    """Envía una notificación a Claudia. Devuelve True si se entregó (HTTP 2xx).

    Nunca lanza: ante cualquier problema (sin config, red caída, error HTTP)
    loguea y devuelve False, para que una corrida jamás falle por una notificación.
    """
    url = os.getenv("CLAUDIA_NOTIFY_URL")
    key = os.getenv("CLAUDIA_API_KEY")
    if not (url and key):
        log.debug("notify: CLAUDIA_NOTIFY_URL/CLAUDIA_API_KEY sin configurar; no-op.")
        return False
    payload = {
        "title": title,
        "body": body,
        "level": level if level in _LEVELS else "info",
    }
    if data:
        payload["data"] = data
    try:
        r = requests.post(url, json=payload, headers={"X-Api-Key": key}, timeout=timeout)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning(f"notify: no se pudo entregar (best-effort): {e}")
        return False


if __name__ == "__main__":
    from pathlib import Path
    from dotenv import load_dotenv
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    load_dotenv(Path(__file__).parent / ".env")
    ok = notify("🔔 Test", "Notificación de prueba desde el bot de horas.", "info")
    print("Enviada ✓" if ok else
          "No enviada — revisa CLAUDIA_NOTIFY_URL/CLAUDIA_API_KEY o que Claudia esté arriba.")
