"""Docker-healthcheck: dashboard bereikbaar én bot verbonden (of nog niet ingesteld)."""
import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=5) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
