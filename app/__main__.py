"""Start het dashboard (en daarmee de bot) met uvicorn."""
import os
import sys

import uvicorn

from . import paths


def _check_writable() -> None:
    for d in (paths.DATA_DIR, paths.KEY_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".schrijftest"
            probe.write_text("ok")
            probe.unlink()
        except OSError:
            print(f"FOUT: kan niet schrijven in {d}. Gebruik de named volumes uit docker-compose.yml "
                  f"(of geef de map eigenaar uid 10001).", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    _check_writable()
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=False, server_header=False, log_config=None)
