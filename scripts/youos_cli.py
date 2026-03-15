#!/usr/bin/env python3
"""YouOS CLI — your personal AI email copilot."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import typer

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

app = typer.Typer(
    name="youos",
    help="YouOS — your personal AI email copilot. Learns your writing style and drafts replies.",
    no_args_is_help=True,
)


@app.command()
def setup():
    """Run the interactive setup wizard."""
    subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "setup_wizard.py")])


@app.command()
def status():
    """Show corpus size, model status, last run."""
    from app.core.config import (
        get_display_name,
        get_server_port,
        get_tailscale_hostname,
        get_user_emails,
        get_user_name,
        load_config,
    )
    from app.core.settings import get_settings
    from app.db.bootstrap import resolve_sqlite_path

    config = load_config()
    settings = get_settings()
    name = get_user_name(config)
    emails = get_user_emails(config)
    port = get_server_port(config)
    ts_hostname = get_tailscale_hostname(config)

    print()
    print(f"{get_display_name(config)} Status")
    print("\u2501" * 34)

    print(f"User:        {name} ({', '.join(emails) or 'not configured'})")

    # Server status
    try:
        result = subprocess.run(
            ["pgrep", "-f", "uvicorn.*app.main:app"],
            capture_output=True, text=True, timeout=5,
        )
        server_running = result.returncode == 0
    except Exception:
        server_running = False
    server_icon = "\u2705" if server_running else "\u274c"
    print(f"Server:      {server_icon} {'running' if server_running else 'stopped'} on port {port}")

    # Tailscale
    if ts_hostname:
        print(f"Tailscale:   \u2705 https://{ts_hostname}.ts.net")
    else:
        print("Tailscale:   not configured")

    print()

    db_path = resolve_sqlite_path(settings.database_url)
    if not db_path.exists():
        print(f"Database:    not found ({db_path})")
        print("Run 'youos setup' to initialize.")
        print("\u2501" * 34)
        return

    conn = sqlite3.connect(db_path)
    try:
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        pairs = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
        feedback = conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]

        try:
            reviewed_today = conn.execute(
                "SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')"
            ).fetchone()[0]
        except Exception:
            reviewed_today = 0

        print(f"Corpus:      {docs:,} docs | {pairs:,} reply pairs")
        print(f"Feedback:    {feedback} pairs ({reviewed_today} today)")
    except Exception:
        print("Database exists but tables may not be initialized.")

    conn.close()

    # Model info
    model_used = config.get("model", {}).get("base", "Qwen/Qwen2.5-1.5B-Instruct")
    adapter_path = ROOT_DIR / "models" / "adapters" / "latest" / "adapters.safetensors"
    if adapter_path.exists():
        mtime = os.path.getmtime(adapter_path)
        dt = datetime.fromtimestamp(mtime)
        print(f"Model:       {model_used} (trained {dt.strftime('%Y-%m-%d %H:%M')})")
    else:
        print(f"Model:       {model_used} (not fine-tuned yet)")

    # Last nightly run
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1", "--format=%ar"],
            capture_output=True, text=True, timeout=5, cwd=ROOT_DIR,
        )
        if result.returncode == 0 and result.stdout.strip():
            print(f"\nLast run:    {result.stdout.strip()}")
    except Exception:
        pass

    # Benchmark results
    try:
        conn = sqlite3.connect(db_path)
        total = conn.execute("SELECT COUNT(*) FROM benchmark_cases").fetchone()[0]
        passed = conn.execute(
            "SELECT COUNT(*) FROM eval_runs WHERE status = 'pass'"
        ).fetchone()[0]
        conn.close()
        if total > 0:
            print(f"Benchmark:   {passed}/{total} pass")
    except Exception:
        pass

    print("\u2501" * 34)


@app.command()
def ui():
    """Open the web UI in your browser."""
    from app.core.config import get_server_port
    port = get_server_port()
    url = f"http://localhost:{port}/feedback"
    print(f"Opening {url}")
    webbrowser.open(url)


@app.command()
def draft(
    message: str = typer.Argument(..., help="The inbound email text to draft a reply to"),
    sender: str = typer.Option(None, help="Sender email address"),
    mode: str = typer.Option(None, help="Override mode: work or personal"),
):
    """Draft a reply to an email."""
    from app.core.settings import get_settings
    from app.generation.service import DraftRequest, generate_draft

    settings = get_settings()
    request = DraftRequest(
        inbound_message=message,
        mode=mode,
        sender=sender,
    )
    response = generate_draft(
        request,
        database_url=settings.database_url,
        configs_dir=settings.configs_dir,
    )
    print(response.draft)


@app.command()
def improve():
    """Run the nightly pipeline manually (ingest, feedback, finetune, autoresearch)."""
    subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "nightly_pipeline.py")])


@app.command()
def note(
    email: str = typer.Argument(..., help="Sender email address"),
    text: str = typer.Argument(..., help="Relationship note"),
):
    """Add a sender relationship note."""
    from app.core.settings import get_settings
    from app.db.bootstrap import resolve_sqlite_path

    settings = get_settings()
    db_path = resolve_sqlite_path(settings.database_url)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE sender_profiles SET relationship_note = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE email = ?",
            (text, email.lower()),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO sender_profiles (email, relationship_note) VALUES (?, ?)",
                (email.lower(), text),
            )
        conn.commit()
        print(f"Note saved for {email.lower()}")
    finally:
        conn.close()


@app.command()
def stats():
    """Print stats summary."""
    from app.core.settings import get_settings
    from app.db.bootstrap import resolve_sqlite_path

    settings = get_settings()
    db_path = resolve_sqlite_path(settings.database_url)
    if not db_path.exists():
        print("No database found. Run 'youos setup' first.")
        return

    conn = sqlite3.connect(db_path)
    try:
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        pairs = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
        feedback = conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]

        print("YouOS Stats")
        print("=" * 30)
        print(f"  Documents:      {docs:,}")
        print(f"  Reply pairs:    {pairs:,}")
        print(f"  Feedback pairs: {feedback:,}")

        try:
            reviewed_today = conn.execute(
                "SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')"
            ).fetchone()[0]
            print(f"  Reviewed today: {reviewed_today}")
        except Exception:
            pass

        try:
            profiles = conn.execute("SELECT COUNT(*) FROM sender_profiles").fetchone()[0]
            print(f"  Sender profiles: {profiles}")
        except Exception:
            pass
    finally:
        conn.close()


@app.command()
def ingest():
    """Run email ingestion manually."""
    subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "ingest_gmail_threads.py"), "--live"])


@app.command()
def finetune():
    """Run LoRA fine-tuning manually."""
    subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "export_feedback_jsonl.py")])
    subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "finetune_lora.py")])


@app.command(name="eval")
def run_eval():
    """Run benchmark evaluation."""
    subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "run_eval.py")])


@app.command()
def serve():
    """Start the YouOS web server."""
    from app.core.config import get_server_host, get_server_port
    port = get_server_port()
    host = get_server_host()
    subprocess.run([
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", host, "--port", str(port),
    ], cwd=str(ROOT_DIR))


@app.command()
def teardown(
    all_data: bool = typer.Option(False, "--all", help="Delete everything without prompting"),
):
    """Remove all YouOS user data (corpus, model, database)."""
    from scripts.teardown import teardown as do_teardown
    do_teardown(delete_all=all_data)


model_app = typer.Typer(help="Manage the local model.")
app.add_typer(model_app, name="model")


@model_app.command(name="set")
def model_set(
    model_name: str = typer.Argument(help="HuggingFace model name, e.g. Qwen/Qwen2.5-3B-Instruct"),
):
    """Set the base model for fine-tuning and generation."""
    import yaml
    config_path = ROOT_DIR / "youos_config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    config.setdefault("model", {})["base"] = model_name
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    typer.echo(f"✅ Model set to {model_name}")
    typer.echo("   Run `youos finetune` to train a new adapter on this model.")


@model_app.command(name="show")
def model_show():
    """Show the currently configured model."""
    import yaml
    config_path = ROOT_DIR / "youos_config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    base = config.get("model", {}).get("base", "Qwen/Qwen2.5-1.5B-Instruct")
    adapter = ROOT_DIR / "models" / "adapters" / "latest" / "adapters.safetensors"
    typer.echo(f"Base model:  {base}")
    typer.echo(f"Adapter:     {'✅ trained' if adapter.exists() else '❌ not trained yet'}")


if __name__ == "__main__":
    app()
