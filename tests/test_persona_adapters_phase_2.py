"""Per-persona adapters — Phase 2: per-cohort fine-tune step (default-off).

Phase 1 (PR #28) laid the schema + classify-on-insert + backfill +
observability foundation. Phase 2 adds the training surface:

1. **`export_feedback_jsonl.py --persona <sender_type>`** — filters
   the export to one cohort; bypasses the `used_in_finetune=0` filter
   because per-persona training re-trains from scratch each run.
2. **`finetune_lora.py --persona <sender_type>`** — routes the
   adapter to `<models>/adapters/personas/<sender_type>/`; skips
   marking pairs as `used_in_finetune=1` (the global still needs
   them); writes `persona: <sender_type>` into meta.json.
3. **Nightly step `step_finetune_personas`** — for each cohort
   above `finetune.min_pairs_per_persona` (default 30), runs the
   export → finetune subprocess pipeline. Skipped silently when no
   cohort qualifies (the common case until enough feedback
   accumulates per cohort).
4. **Stats observability** — `/stats/data` gains
   `persona_adapters: {persona: {trained, mtime, pairs_used}}` so
   the user can see which personas are ready for Phase-3 routed
   generation without poking the filesystem.

Zero generation behavior change in Phase 2 either — Phase 3 will
flip the routing flag.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance_with_persona_pairs(monkeypatch, tmp_path, _reset_settings):
    """A YOUOS_DATA_DIR with feedback_pairs containing classified rows."""
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    db = tmp_path / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL,
            generated_draft TEXT NOT NULL,
            edited_reply TEXT NOT NULL,
            feedback_note TEXT,
            rating INTEGER,
            used_in_finetune INTEGER DEFAULT 0,
            edit_distance_pct REAL,
            reply_pair_id INTEGER,
            sender_type TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # Seed: 5 internal, 2 personal, 1 unknown — only `internal` will be
    # above the threshold=3 used in tests below. Distinguishable text per
    # cohort so the persona filter is observable in the exported JSONL
    # without depending on internal oversampling counts.
    rows: list = []
    for i in range(5):
        rows.append((f"internal-inbound-{i}", "draft", f"INTERNAL-reply-{i} long enough", 4, "internal"))
    for i in range(2):
        rows.append((f"personal-inbound-{i}", "draft", f"PERSONAL-reply-{i} long enough", 4, "personal"))
    rows.append(("unk-inbound", "draft", "UNKNOWN-reply long enough", 4, None))
    conn.executemany(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating, sender_type) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return tmp_path


# ── 1. export --persona filters to one cohort ─────────────────────────────

def test_export_persona_filters_to_cohort(monkeypatch, instance_with_persona_pairs, tmp_path):
    """`--persona internal` must export only internal-cohort rows. Tests
    the per-cohort SQL filter that Phase 2 added to the export."""
    out = tmp_path / "internal.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_feedback_jsonl.py",
            "--persona", "internal",
            "--output", str(out),
            "--min-rating", "1",  # avoid filtering on rating
            "--min-edit-pct", "0.0",
            "--no-dedup",
        ],
    )
    from scripts.export_feedback_jsonl import main as export_main

    export_main()

    assert out.exists()
    body = out.read_text(encoding="utf-8")
    # Every line in the export must come from the internal cohort —
    # personal/unknown rows must NOT appear regardless of oversampling.
    # Tagging the reply text per cohort makes this a substring check, so
    # the assertion is robust to internal duplication / curriculum
    # warmup / valid-split behavior in the exporter.
    assert "INTERNAL-reply" in body
    assert "PERSONAL-reply" not in body, "persona filter leaked personal cohort"
    assert "UNKNOWN-reply" not in body, "persona filter leaked unknown cohort"


def test_export_persona_bypasses_used_in_finetune_filter(
    monkeypatch, instance_with_persona_pairs, tmp_path,
):
    """The global incremental loop marks pairs as `used_in_finetune=1`
    after training. Per-persona training must NOT honor that flag —
    otherwise the first persona to run would steal pairs from later
    personas and from the global."""
    db = instance_with_persona_pairs / "var" / "youos.db"
    # Mark every row as already-used by the global.
    sqlite3.connect(db).execute("UPDATE feedback_pairs SET used_in_finetune = 1").connection.commit()

    out = tmp_path / "internal.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_feedback_jsonl.py",
            "--persona", "internal",
            "--output", str(out),
            "--min-rating", "1",
            "--min-edit-pct", "0.0",
            "--no-dedup",
        ],
    )
    from scripts.export_feedback_jsonl import main as export_main

    export_main()
    body = out.read_text(encoding="utf-8")
    # All internal pairs still exported despite every row being marked
    # used_in_finetune=1 — persona bypass works.
    assert "INTERNAL-reply" in body


def test_export_without_persona_still_honors_used_in_finetune(
    monkeypatch, instance_with_persona_pairs, tmp_path,
):
    """Back-compat: the global export path is unchanged — still skips
    already-used rows when not in persona mode."""
    db = instance_with_persona_pairs / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE feedback_pairs SET used_in_finetune = 1 WHERE sender_type = 'internal'")
    conn.commit()
    conn.close()

    out = tmp_path / "global.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_feedback_jsonl.py",
            "--output", str(out),
            "--min-rating", "1",
            "--min-edit-pct", "0.0",
            "--no-dedup",
        ],
    )
    from scripts.export_feedback_jsonl import main as export_main

    export_main()
    body = out.read_text(encoding="utf-8")
    # Internal cohort marked used → excluded from global export.
    # Personal + unknown still present (they weren't marked used).
    assert "INTERNAL-reply" not in body, "global export ignored used_in_finetune=1"
    assert "PERSONAL-reply" in body
    assert "UNKNOWN-reply" in body


# ── 2. finetune --persona routes to the right adapter dir ────────────────

def test_finetune_persona_main_routes_adapter_dir(monkeypatch, tmp_path, _reset_settings):
    """When `--persona X` is set and `--adapter-dir` isn't, the adapter
    dir gets redirected to `<models>/adapters/personas/X/`. Stub out
    `run_training` so we don't actually train — we're testing the
    routing override."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()

    import scripts.finetune_lora as ft_mod

    captured: dict = {}

    def _capture(args):
        captured["adapter_dir"] = args.adapter_dir
        captured["persona"] = args.persona

    monkeypatch.setattr(ft_mod, "run_training", _capture)
    monkeypatch.setattr("sys.argv", ["finetune_lora.py", "--persona", "internal"])

    ft_mod.main()
    assert "personas" in captured["adapter_dir"]
    assert "internal" in captured["adapter_dir"]
    assert "latest" not in captured["adapter_dir"]  # NOT the global dir
    assert captured["persona"] == "internal"


def test_finetune_persona_respects_explicit_adapter_dir(monkeypatch, tmp_path, _reset_settings):
    """If the user passes `--adapter-dir` explicitly, persona routing
    must not silently override it — they know what they're doing."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()

    import scripts.finetune_lora as ft_mod

    captured: dict = {}
    monkeypatch.setattr(ft_mod, "run_training", lambda a: captured.update(adapter_dir=a.adapter_dir))
    explicit = str(tmp_path / "my-custom-adapter")
    monkeypatch.setattr(
        "sys.argv",
        ["finetune_lora.py", "--persona", "internal", "--adapter-dir", explicit],
    )

    ft_mod.main()
    assert captured["adapter_dir"] == explicit


# ── 3. finetune --persona skips the used_in_finetune marking ─────────────

def test_finetune_persona_does_not_mark_pairs_used(monkeypatch, tmp_path, _reset_settings):
    """The global training marks rows used_in_finetune=1 to drive the
    incremental loop. Per-persona must NOT do this — otherwise the
    global adapter never sees those rows again, and other personas
    that share them would also miss them."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()

    # Seed a DB with some unused pairs.
    db = tmp_path / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY, used_in_finetune INTEGER DEFAULT 0,
            inbound_text TEXT, generated_draft TEXT, edited_reply TEXT
        );
        INSERT INTO feedback_pairs (used_in_finetune) VALUES (0), (0), (0);
        """
    )
    conn.commit()
    conn.close()

    # Stub the subprocess training call (we're testing the post-train
    # bookkeeping, not actually running mlx_lm).
    import scripts.finetune_lora as ft_mod

    class _FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ft_mod.subprocess, "run", lambda *a, **kw: _FakeResult())
    # The stubbed subprocess writes no real adapter, so stub the atomic
    # promotion to succeed too — this test exercises the post-train DB
    # bookkeeping, not adapter validation (b171: a failed promotion now
    # exits nonzero, which would otherwise mask the bookkeeping assertion).
    monkeypatch.setattr(ft_mod, "_promote_adapter", lambda *a, **kw: True)

    # Run with --persona and confirm used_in_finetune stays 0.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "train.jsonl").write_text('{"messages": []}\n' * 5)

    monkeypatch.setattr(
        "sys.argv",
        [
            "finetune_lora.py",
            "--persona", "internal",
            "--data-dir", str(data_dir),
            "--db", str(db),
            "--no-auto",  # skip auto-scaling complexity
            "--iters", "10",
        ],
    )
    ft_mod.main()

    # Crucially: all rows still used_in_finetune=0.
    rows = sqlite3.connect(db).execute("SELECT used_in_finetune FROM feedback_pairs").fetchall()
    assert all(r[0] == 0 for r in rows), f"persona training marked pairs used: {rows}"


# ── 4. Nightly step: threshold-gated per-cohort training ─────────────────

def test_persona_cohorts_above_threshold_returns_only_qualifying(instance_with_persona_pairs):
    """Direct helper test: with threshold=3, only `internal` (5 pairs)
    qualifies. `personal` (2) and `unknown` (1) are excluded."""
    from scripts.nightly_pipeline import _persona_cohorts_above_threshold

    db = instance_with_persona_pairs / "var" / "youos.db"
    eligible = _persona_cohorts_above_threshold(db, threshold=3)
    assert eligible == {"internal": 5}


def test_persona_cohorts_excludes_unknown_always(instance_with_persona_pairs):
    """Even with threshold=1 (every cohort would qualify by count),
    `unknown` is excluded — we don't train an adapter for un-classified
    pairs because the prompt-side persona modes don't have an `unknown`
    style anchor to pair with."""
    from scripts.nightly_pipeline import _persona_cohorts_above_threshold

    db = instance_with_persona_pairs / "var" / "youos.db"
    eligible = _persona_cohorts_above_threshold(db, threshold=1)
    assert "unknown" not in eligible
    assert eligible == {"internal": 5, "personal": 2}


def test_persona_cohorts_tolerates_missing_column(monkeypatch, tmp_path, _reset_settings):
    """Pre-migration DB (no sender_type column): returns empty dict
    rather than crashing the nightly's persona step."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()
    db = tmp_path / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE feedback_pairs (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    from scripts.nightly_pipeline import _persona_cohorts_above_threshold

    assert _persona_cohorts_above_threshold(db, threshold=30) == {}


def test_load_min_pairs_per_persona_defaults_to_30(monkeypatch):
    """Threshold default matches the existing finetune-milestone threshold."""
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **kw: {})
    from scripts.nightly_pipeline import _load_min_pairs_per_persona

    assert _load_min_pairs_per_persona() == 30


def test_load_min_pairs_per_persona_reads_config(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"finetune": {"min_pairs_per_persona": 50}},
    )
    from scripts.nightly_pipeline import _load_min_pairs_per_persona

    assert _load_min_pairs_per_persona() == 50


def test_load_min_pairs_per_persona_ignores_bad_value(monkeypatch):
    """A fat-fingered string or negative integer falls back to default
    rather than crashing or accepting nonsense."""
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"finetune": {"min_pairs_per_persona": "lots"}},
    )
    from scripts.nightly_pipeline import _load_min_pairs_per_persona

    assert _load_min_pairs_per_persona() == 30

    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"finetune": {"min_pairs_per_persona": -5}},
    )
    assert _load_min_pairs_per_persona() == 30


def test_step_finetune_personas_skips_silently_when_no_cohort_qualifies(
    monkeypatch, instance_with_persona_pairs,
):
    """The common case until the user has accumulated enough feedback per
    sender_type — skip without crashing the nightly, return a clear
    structure so the pipeline log can show the skip reason."""
    # Force threshold above any cohort size.
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"finetune": {"min_pairs_per_persona": 1000}},
    )

    from scripts.nightly_pipeline import step_finetune_personas

    result = step_finetune_personas(verbose=False)
    assert result == {"ok": True, "skipped": True, "trained": [], "threshold": 1000}


def test_step_finetune_personas_invokes_subprocess_for_qualifying_cohorts(
    monkeypatch, instance_with_persona_pairs,
):
    """When a cohort crosses the threshold, the step launches the export
    and finetune subprocesses for that cohort. Stub `_run_step` so we
    observe what got invoked without actually training."""
    import scripts.nightly_pipeline as np_mod

    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"finetune": {"min_pairs_per_persona": 3}},
    )

    invocations: list[list[str]] = []

    def _capture(_name, cmd, **_kw):
        invocations.append(cmd)
        return True

    monkeypatch.setattr(np_mod, "_run_step", _capture)

    result = np_mod.step_finetune_personas(verbose=False)
    assert result["ok"] is True
    assert result["skipped"] is False
    assert result["trained"] == ["internal"]
    # Two invocations per persona: export + finetune.
    assert len(invocations) == 2
    assert any("export_feedback_jsonl.py" in arg for arg in invocations[0])
    assert any("--persona" in arg for arg in invocations[0])
    assert any("finetune_lora.py" in arg for arg in invocations[1])


def test_step_finetune_personas_reports_failures(monkeypatch, instance_with_persona_pairs):
    """If a cohort's export or finetune subprocess fails, the step
    surfaces the persona name in `failed:` so the pipeline log records
    which cohorts didn't update."""
    import scripts.nightly_pipeline as np_mod

    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"finetune": {"min_pairs_per_persona": 1}},
    )
    monkeypatch.setattr(np_mod, "_run_step", lambda *a, **kw: False)

    result = np_mod.step_finetune_personas(verbose=False)
    assert result["ok"] is False
    assert set(result["failed"]) == {"internal", "personal"}
    assert result["trained"] == []


# ── 5. Stats observability: persona_adapters ─────────────────────────────

def test_persona_adapter_status_reports_untrained_when_dir_missing(
    monkeypatch, tmp_path, _reset_settings,
):
    """No adapter dir → trained=False, mtime=None, pairs_used=None.
    The Phase-3 routing flag will fall through to the global for any
    persona with trained=False."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.stats import get_persona_adapter_status

    status = get_persona_adapter_status()
    for persona in ("internal", "external_client", "personal", "automated"):
        assert status[persona]["trained"] is False
        assert status[persona]["mtime"] is None
        assert status[persona]["pairs_used"] is None


def test_persona_adapter_status_reports_trained_when_safetensors_present(
    monkeypatch, tmp_path, _reset_settings,
):
    """An adapters.safetensors file → trained=True; meta.json fields
    surface through when present."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.settings import get_persona_adapter_path

    adapter_dir = get_persona_adapter_path("internal")
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "adapters.safetensors").write_bytes(b"weights")
    (adapter_dir / "meta.json").write_text(
        json.dumps({"trained_at": "2026-05-25T01:00:00+00:00", "pairs_used": 42}),
        encoding="utf-8",
    )

    from app.core.stats import get_persona_adapter_status

    status = get_persona_adapter_status()
    assert status["internal"]["trained"] is True
    assert status["internal"]["mtime"] == "2026-05-25T01:00:00+00:00"
    assert status["internal"]["pairs_used"] == 42
    # Other personas still untrained.
    assert status["personal"]["trained"] is False


def test_persona_adapter_status_falls_back_to_fs_mtime_without_meta(
    monkeypatch, tmp_path, _reset_settings,
):
    """An adapter with no meta.json (legacy / partial train) still gets
    a useful mtime via stat — don't lose the "trained at" signal just
    because metadata wasn't written."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.settings import get_persona_adapter_path

    adapter_dir = get_persona_adapter_path("personal")
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "adapters.safetensors").write_bytes(b"weights")
    # no meta.json

    from app.core.stats import get_persona_adapter_status

    status = get_persona_adapter_status()
    assert status["personal"]["trained"] is True
    assert status["personal"]["mtime"] is not None  # fs fallback worked
    assert status["personal"]["pairs_used"] is None


def test_persona_adapter_status_does_not_include_unknown(
    monkeypatch, tmp_path, _reset_settings,
):
    """`unknown` isn't in the persona-routing taxonomy (no style anchor)
    so we don't surface a status for it either — keeps the dashboard
    focused on the cohorts that actually matter."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.stats import get_persona_adapter_status

    status = get_persona_adapter_status()
    assert "unknown" not in status


# Quiet the unused-import lint.
_ = Path
