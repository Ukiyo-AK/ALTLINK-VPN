from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

def test_backend_mode_enables_proxy_headers(monkeypatch):
    settings = SimpleNamespace(backend_host="0.0.0.0", backend_port=8000)
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=fake_run))
    main_module = importlib.import_module("altlink.main")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["altlink", "backend"])

    main_module.main()

    kwargs = captured["kwargs"]
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8000
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "*"
