"""Fail image builds when shared publishing modules/dependencies are missing.

Only import classes. This does not start an app, browser, or platform request and
does not need a database or instance key. Also usable as a container smoke check.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys


SHARED_ENTRIES = (
    "get_publish_account_capability_service_class",
    "get_xianyu_publisher_class",
    "get_xianyu_direct_publisher_class",
    "get_xianyu_personal_publisher_class",
)


def forbid_external_connections():
    """Import checks cannot contact a database, platform, or DNS resolver."""
    def check(event, _args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            raise RuntimeError("Runtime module verification must not make external connections")
    sys.addaudithook(check)


def verify_runtime_modules():
    root = Path(__file__).resolve().parent
    current_app = root / "service/app"
    shared_app = root / "backend-web/app"

    import app
    import app.core.config
    import app.services
    import common

    original_modules = {name: sys.modules[name] for name in ("app", "app.services", "app.core.config")}
    if not Path(common.__file__).resolve().is_relative_to(root / "common"):
        raise RuntimeError("Common modules were loaded outside the application image")
    if not Path(app.__file__).resolve().is_relative_to(current_app):
        raise RuntimeError("The current service app package must come from /app/service")

    from common.services import xianyu_publish_service

    loaded = {}
    for getter in SHARED_ENTRIES:
        cls = getattr(xianyu_publish_service, getter)()
        if not Path(inspect.getfile(cls)).resolve().is_relative_to(shared_app):
            raise RuntimeError(f"Shared entry {getter} was not loaded from packaged backend-web source")
        loaded[getter] = cls.__name__

    for name, original in original_modules.items():
        if sys.modules[name] is not original:
            raise RuntimeError(f"Shared imports replaced the current service module {name}")
    if not Path(app.services.__path__[0]).resolve().is_relative_to(current_app):
        raise RuntimeError("Shared imports changed the current service search path priority")
    if not Path(app.core.config.__file__).resolve().is_relative_to(current_app):
        raise RuntimeError("Shared imports replaced the current service configuration")
    return loaded


if __name__ == "__main__":
    forbid_external_connections()
    print(json.dumps(verify_runtime_modules(), ensure_ascii=False))
