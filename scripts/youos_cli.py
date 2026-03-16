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
        get_ingestion_accounts,
        get_last_ingest_at,
        get_server_port,
        get_tailscale_hostname,
        get_user_emails,
        get_user_name,
        is_ollama_enabled,
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
            capture_output=True,
            text=True,
            timeout=5,
        )
        server_running = result.returncode == 0
    except Exception:
        server_running = False
    server_icon = "\u2705" if server_running else "\u274c"
    print(f"Server:      {server_icon} {'running' if server_running else 'stopped'} on port {port}")

    # Ollama status
    ollama_cfg = config.get("model", {}).get("ollama", {})
    if is_ollama_enabled(config):
        ollama_model = ollama_cfg.get("model", "mistral")
        print(f"Ollama:      \u2705 enabled ({ollama_model})")
    else:
        print("Ollama:      \u274c not configured")

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
            reviewed_today = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')").fetchone()[0]
        except Exception:
            reviewed_today = 0

        print(f"Corpus:      {docs:,} docs | {pairs:,} reply pairs")
        print(f"Feedback:    {feedback} pairs ({reviewed_today} today)")

        # Embedding coverage
        try:
            embedded = conn.execute("SELECT COUNT(*) FROM documents WHERE embedding IS NOT NULL").fetchone()[0]
            pct = (embedded / docs * 100) if docs > 0 else 0
            print(f"Embeddings:  {embedded:,}/{docs:,} ({pct:.0f}%)")
        except Exception:
            pass
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

    # Last ingestion dates
    accounts = get_ingestion_accounts(config)
    ingest_parts = []
    for acct in accounts:
        last = get_last_ingest_at(acct, config)
        if last:
            ingest_parts.append(f"{last[:10]} ({acct})")
    if ingest_parts:
        print(f"Last ingest: {', '.join(ingest_parts)}")

    # Last nightly run
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1", "--format=%ar"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ROOT_DIR,
        )
        if result.returncode == 0 and result.stdout.strip():
            print(f"\nLast run:    {result.stdout.strip()}")
    except Exception:
        pass

    # Benchmark results
    try:
        conn = sqlite3.connect(db_path)
        total = conn.execute("SELECT COUNT(*) FROM benchmark_cases").fetchone()[0]
        passed = conn.execute("SELECT COUNT(*) FROM eval_runs WHERE status = 'pass'").fetchone()[0]
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
def improve(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print Rich progress for each step"),
):
    """Run the nightly pipeline manually (ingest, feedback, finetune, autoresearch)."""
    cmd = [sys.executable, str(ROOT_DIR / "scripts" / "nightly_pipeline.py")]
    if verbose:
        cmd.append("--verbose")
    subprocess.run(cmd)


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
            "UPDATE sender_profiles SET relationship_note = ?, updated_at = CURRENT_TIMESTAMP WHERE email = ?",
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
            reviewed_today = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')").fetchone()[0]
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
def ingest(
    whatsapp: str = typer.Option(None, "--whatsapp", help="Path to a WhatsApp chat export .txt file"),
):
    """Run email ingestion manually."""
    if whatsapp:
        from pathlib import Path as P

        from app.ingestion.whatsapp import ingest_whatsapp_export

        result = ingest_whatsapp_export(P(whatsapp))
        print(f"[{result.status}] {result.detail}")
        return
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
    subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=str(ROOT_DIR),
    )


@app.command()
def teardown(
    all_data: bool = typer.Option(False, "--all", help="Delete everything without prompting"),
):
    """Remove all YouOS user data (corpus, model, database)."""
    from scripts.teardown import teardown as do_teardown

    do_teardown(delete_all=all_data)


@app.command()
def doctor():
    """Check system requirements and project health."""
    import shutil
    import socket

    from rich.console import Console

    from app.core.config import get_user_emails

    console = Console()
    all_required_pass = True

    def check(name: str, passed: bool, required: bool = True) -> None:
        nonlocal all_required_pass
        icon = "\u2713" if passed else "\u2717"
        style = "green" if passed else ("red" if required else "yellow")
        level = "required" if required else "warning"
        console.print(f"  [{style}]{icon}[/{style}] {name} ({level})")
        if required and not passed:
            all_required_pass = False

    console.print("[bold]YouOS Doctor[/bold]\n")

    # Python >= 3.11
    check("Python >= 3.11", sys.version_info >= (3, 11))

    # gog CLI installed
    check("gog CLI installed", shutil.which("gog") is not None)

    # mlx_lm importable
    try:
        import importlib

        importlib.import_module("mlx_lm")
        mlx_ok = True
    except ImportError:
        mlx_ok = False
    check("mlx_lm importable", mlx_ok)

    # youos_config.yaml exists
    config_path = ROOT_DIR / "youos_config.yaml"
    check("youos_config.yaml exists", config_path.exists())

    # user.emails set
    try:
        emails = get_user_emails()
        emails_set = len(emails) > 0
    except Exception:
        emails_set = False
    check("user.emails set in config", emails_set)

    # var/youos.db exists (warning)
    db_path = ROOT_DIR / "var" / "youos.db"
    check("var/youos.db exists", db_path.exists(), required=False)

    # >= 3GB disk free (warning)
    try:
        stat = os.statvfs(ROOT_DIR)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        check(f">= 3GB disk free ({free_gb:.1f}GB available)", free_gb >= 3.0, required=False)
    except Exception:
        check(">= 3GB disk free", False, required=False)

    # models/ dir has content (warning)
    models_dir = ROOT_DIR / "models"
    has_models = models_dir.exists() and any(models_dir.iterdir())
    check("models/ dir has content", has_models, required=False)

    # Port 8901 free (warning)
    try:
        from app.core.config import get_server_port

        port = get_server_port()
    except Exception:
        port = 8901
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.close()
        port_free = True
    except OSError:
        port_free = False
    check(f"Port {port} free", port_free, required=False)

    console.print()
    if all_required_pass:
        console.print("[bold green]All required checks passed.[/bold green]")
        raise SystemExit(0)
    else:
        console.print("[bold red]Some required checks failed.[/bold red]")
        raise SystemExit(1)


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


ollama_app = typer.Typer(help="Manage Ollama integration.")
model_app.add_typer(ollama_app, name="ollama")


@ollama_app.command(name="enable")
def ollama_enable():
    """Enable Ollama as a generation backend."""
    import yaml

    config_path = ROOT_DIR / "youos_config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    config.setdefault("model", {}).setdefault("ollama", {})["enabled"] = True
    config["model"]["fallback"] = "ollama"
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    typer.echo("Ollama enabled as generation fallback.")


@ollama_app.command(name="disable")
def ollama_disable():
    """Disable Ollama as a generation backend."""
    import yaml

    config_path = ROOT_DIR / "youos_config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    config.setdefault("model", {}).setdefault("ollama", {})["enabled"] = False
    if config.get("model", {}).get("fallback") == "ollama":
        config["model"]["fallback"] = "claude"
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    typer.echo("Ollama disabled. Fallback set to claude.")


if __name__ == "__main__":
    app()
