"""Backend-aware doctor + setup-wizard dependency checks (PR 4).

The doctor used to require the OpenClaw `gog` CLI unconditionally, which failed
a `gws`/`native` user who doesn't have `gog` at all. These pin that the Google
backend dependency is now keyed on `ingestion.google_backend`.
"""

from __future__ import annotations

from app.core import doctor


def _set_backend(monkeypatch, backend: str) -> None:
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda *a, **k: backend)


# --- _google_backend_status ------------------------------------------------


def test_gog_backend_ok(monkeypatch):
    _set_backend(monkeypatch, "gog")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/gog" if name == "gog" else None)
    assert doctor._google_backend_status() == ("gog", True, "gog CLI installed")


def test_gog_backend_missing(monkeypatch):
    _set_backend(monkeypatch, "gog")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    backend, ok, detail = doctor._google_backend_status()
    assert backend == "gog" and ok is False and "gog CLI not found" in detail


def test_gws_backend_ok(monkeypatch):
    _set_backend(monkeypatch, "gws")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/gws" if name == "gws" else None)
    backend, ok, _ = doctor._google_backend_status()
    assert backend == "gws" and ok is True


def test_gws_backend_missing(monkeypatch):
    _set_backend(monkeypatch, "gws")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    backend, ok, detail = doctor._google_backend_status()
    assert backend == "gws" and ok is False and "gws CLI not found" in detail


def test_native_backend_ok(monkeypatch):
    _set_backend(monkeypatch, "native")
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    backend, ok, _ = doctor._google_backend_status()
    assert backend == "native" and ok is True


def test_native_backend_missing_extra(monkeypatch):
    _set_backend(monkeypatch, "native")
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)
    backend, ok, detail = doctor._google_backend_status()
    assert backend == "native" and ok is False and "youos[google]" in detail


# --- run_doctor_checks integration -----------------------------------------


def test_run_doctor_surfaces_gws_failure(monkeypatch):
    _set_backend(monkeypatch, "gws")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # gws absent
    _, failures = doctor.run_doctor_checks()
    assert any("gws CLI not found" in f for f in failures)


def test_run_doctor_native_user_not_failed_for_missing_gog(monkeypatch):
    # The whole point: a native user with the extra installed must not be
    # failed just because gog/gws aren't on PATH.
    _set_backend(monkeypatch, "native")
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    _, failures = doctor.run_doctor_checks()
    assert not any("gog CLI not found" in f for f in failures)
    assert not any("gws CLI not found" in f for f in failures)


# --- setup wizard wiring ---------------------------------------------------


def test_wizard_dependency_check_passes_for_ok_backend(monkeypatch):
    import scripts.setup_wizard as sw

    monkeypatch.setattr("app.core.doctor._google_backend_status", lambda: ("native", True, "Google API libraries importable"))
    monkeypatch.setattr(sw.shutil, "which", lambda name: f"/usr/bin/{name}")  # git etc. present
    assert sw._check_dependencies() is True


def test_wizard_dependency_check_fails_for_missing_backend(monkeypatch):
    import scripts.setup_wizard as sw

    monkeypatch.setattr("app.core.doctor._google_backend_status", lambda: ("gws", False, "gws CLI not found in PATH"))
    monkeypatch.setattr(sw.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert sw._check_dependencies() is False
