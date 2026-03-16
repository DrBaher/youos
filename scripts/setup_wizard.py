#!/usr/bin/env python3
"""YouOS Setup Wizard — interactive first-time configuration."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "youos_config.yaml"

# Add root to path for imports
sys.path.insert(0, str(ROOT_DIR))


def _print_banner():
    print()
    print("+" + "=" * 41 + "+")
    print("|           Welcome to YouOS              |")
    print("|  Your personal AI email copilot         |")
    print("+" + "=" * 41 + "+")
    print()
    print("YouOS learns how YOU write email - from your own sent history.")
    print("It runs entirely on your Mac. Your data never leaves your machine.")
    print()
    print("This setup takes about 15 minutes (mostly waiting for ingestion).")
    print("Let's get started.")
    print()


def _check_dependencies() -> bool:
    """Check and display status for each dependency. Returns True if all critical deps present."""
    print("Checking dependencies...")
    print()
    all_ok = True

    # Python version
    py_ver = sys.version_info
    if py_ver >= (3, 11):
        print(f"  Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}  OK")
    else:
        print(f"  Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}  FAIL (need 3.11+)")
        all_ok = False

    # gog CLI
    if shutil.which("gog"):
        print("  gog CLI                OK")
    else:
        print("  gog CLI                MISSING")
        print("    Install: pip install gog-cli  OR  see OpenClaw docs for gog setup")
        all_ok = False

    # mlx (optional but recommended)
    import importlib.util as _ilu

    if _ilu.find_spec("mlx") is not None:
        print("  mlx (Apple Silicon ML) OK")
    else:
        print("  mlx (Apple Silicon ML) MISSING (optional - needed for local model)")
        print("    Install: pip install mlx mlx-lm")

    # git
    if shutil.which("git"):
        print("  git                    OK")
    else:
        print("  git                    MISSING")
        all_ok = False

    # RAM check
    try:
        # Use sysctl on macOS
        result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ram_bytes = int(result.stdout.strip())
            ram_gb = ram_bytes / (1024**3)
            if ram_gb >= 16:
                print(f"  RAM: {ram_gb:.0f} GB         OK")
            elif ram_gb >= 8:
                print(f"  RAM: {ram_gb:.0f} GB         WARN (16GB recommended)")
            else:
                print(f"  RAM: {ram_gb:.0f} GB         WARN (<8GB, may be slow)")
    except Exception:
        print("  RAM: unknown")

    # Disk check
    try:
        stat = os.statvfs(str(ROOT_DIR))
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        if free_gb >= 10:
            print(f"  Disk: {free_gb:.0f} GB free    OK")
        else:
            print(f"  Disk: {free_gb:.0f} GB free    WARN (<10GB, recommend more)")
    except Exception:
        print("  Disk: unknown")

    print()
    return all_ok


def _get_user_identity() -> dict:
    """Prompt for user name, emails, and names."""
    print("--- User Identity ---")
    print()

    name = input("What's your name? (used to personalise the UI)\n> ").strip()
    if not name:
        name = "User"

    print()
    print("What email accounts should YouOS learn from?")
    print("Enter your Gmail addresses one per line. Empty line when done.")
    emails = []
    while True:
        email = input("> ").strip()
        if not email:
            break
        if "@" in email:
            emails.append(email)
        else:
            print(f"  '{email}' doesn't look like an email, skipping")

    print()
    print("What names do you go by in email? (for signature detection)")
    names = [name]  # include primary name by default
    while True:
        n = input("> ").strip()
        if not n:
            break
        if n not in names:
            names.append(n)

    print()
    print("What domains are internal to your organisation? (e.g. company.com, subsidiary.com) [optional, press Enter to skip]")
    domains_input = input("> ").strip()
    internal_domains = [d.strip().lower() for d in domains_input.split(",") if d.strip()] if domains_input else []

    return {"name": name, "emails": emails, "names": names, "internal_domains": internal_domains}


def _verify_accounts(emails: list[str]) -> list[str]:
    """Verify each email account has gog access. Returns list of verified accounts."""
    print()
    print("--- Account Verification ---")
    verified = []
    for email in emails:
        try:
            result = subprocess.run(
                ["gog", "gmail", "search", "in:sent", "--account", email, "--limit", "1", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                print(f"  {email}  OK")
                verified.append(email)
            else:
                print(f"  {email}  FAIL")
                print(f"    Run: gog auth --account {email}")
        except FileNotFoundError:
            print(f"  {email}  FAIL (gog not found)")
        except subprocess.TimeoutExpired:
            print(f"  {email}  TIMEOUT")

    if not verified:
        print()
        print("No accounts verified. You can still proceed and configure accounts later.")

    return verified


def _run_ingestion(config: dict) -> dict:
    """Run ingestion for configured accounts. Returns stats."""
    emails = config.get("user", {}).get("emails", [])
    if not emails:
        print("No email accounts configured. Skipping ingestion.")
        return {"threads": 0, "reply_pairs": 0}

    months = config.get("ingestion", {}).get("initial_months", 12)

    print()
    print("--- Corpus Ingestion ---")
    print(f"  Accounts: {', '.join(emails)}")
    print(f"  Range: last {months} months of sent mail")
    print("  Estimated time: 10-30 minutes depending on volume")
    print()

    proceed = input("Start ingestion? [Y/n] ").strip().lower()
    if proceed == "n":
        print("Skipping ingestion. Run 'youos ingest' later.")
        return {"threads": 0, "reply_pairs": 0}

    total_threads = 0
    total_pairs = 0

    for email in emails:
        print(f"\nIngesting {email}...")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT_DIR / "scripts" / "ingest_gmail_threads.py"),
                    "--live",
                    "--account",
                    email,
                    "--query",
                    "in:sent",
                    "--max-threads",
                    "0",
                ],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            print(result.stdout)
            if result.returncode == 0:
                # Parse counts from output
                for line in result.stdout.splitlines():
                    if "threads" in line.lower() and "reply" in line.lower():
                        import re

                        thread_match = re.search(r"(\d+)\s*threads?", line)
                        pair_match = re.search(r"(\d+)\s*reply.?pairs?", line)
                        if thread_match:
                            total_threads += int(thread_match.group(1))
                        if pair_match:
                            total_pairs += int(pair_match.group(1))
            else:
                print(f"  WARN: Ingestion for {email} had issues")
                if result.stderr:
                    print(f"  {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT: Ingestion for {email} timed out after 30 minutes")
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print(f"\nCorpus built: {total_threads} threads | {total_pairs} reply pairs")
    return {"threads": total_threads, "reply_pairs": total_pairs}


def _run_persona_analysis(config: dict) -> dict | None:
    """Run persona analysis with interactive preference setting."""
    print()
    print("--- Persona Analysis ---")
    print("Analysing your writing style...")

    try:
        from app.core.settings import get_settings
        from app.db.bootstrap import resolve_sqlite_path
        from scripts.analyze_persona import analyze

        settings = get_settings()
        db_path = resolve_sqlite_path(settings.database_url)

        if not db_path.exists():
            print("  No database found. Skipping persona analysis.")
            return None

        findings = analyze(db_path)
        if findings.get("error"):
            print(f"  {findings['error']}")
            return None

        print()
        total = findings.get("total_pairs", 0)
        rl = findings.get("reply_length", {})
        greetings = findings.get("greeting_patterns", {})
        closers = findings.get("closing_patterns", {})

        top_greeting = ""
        greeting_pct = 0
        if greetings:
            top_greeting = max(greetings, key=greetings.get)
            greeting_pct = round(greetings[top_greeting] / total * 100) if total else 0

        top_closing = ""
        if closers:
            top_closing = max(closers, key=closers.get)

        # Determine work email ratio from sender classification
        work_pct = 0
        try:
            conn = sqlite3.connect(db_path)
            total_pairs = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
            personal_count = conn.execute(
                "SELECT COUNT(*) FROM reply_pairs WHERE inbound_author LIKE '%gmail.com%' "
                "OR inbound_author LIKE '%yahoo.com%' OR inbound_author LIKE '%hotmail.com%'"
            ).fetchone()[0]
            conn.close()
            if total_pairs > 0:
                work_pct = round((1 - personal_count / total_pairs) * 100)
        except Exception:
            pass

        print(f"Here's what we found from your {total} replies:")
        print()
        print(f"  Average reply length:    {rl.get('avg_words', 'N/A')} words")
        if top_greeting:
            print(f'  Most common greeting:    "{top_greeting}" ({greeting_pct}%)')
        if top_closing:
            print(f'  Most common closing:     "{top_closing}"')
        print(f"  Work email ratio:        {work_pct}%")
        print()

        # Interactive greeting preference
        print("Let's set your preferences:")
        print()
        print("Preferred greeting style:")
        print("  1. Hi [name],          (casual-professional)")
        print("  2. Dear [name],        (formal)")
        print("  3. Hey [name],         (casual)")
        print("  4. No greeting         (direct)")
        if top_greeting:
            print(f'  5. Keep what we found  ["{top_greeting}"]')
        choice = input("> ").strip()
        greeting_map = {
            "1": "Hi {name},",
            "2": "Dear {name},",
            "3": "Hey {name},",
            "4": "",
        }
        if choice == "5" and top_greeting:
            chosen_greeting = top_greeting
        elif choice in greeting_map:
            chosen_greeting = greeting_map[choice]
        else:
            chosen_greeting = greeting_map.get("1", "Hi {name},")

        print()
        user_name = config.get("user", {}).get("name", "User")
        default_formal = f"Best,\\n{user_name}"
        print("Preferred formal closing (e.g. partnership emails):")
        formal_input = input(f"> [{default_formal}] ").strip()
        chosen_formal = formal_input if formal_input else f"Best,\n{user_name}"

        print()
        default_informal = f"Cheers,\\n{user_name}"
        print("Preferred informal closing (e.g. supplier emails):")
        informal_input = input(f"> [{default_informal}] ").strip()
        chosen_informal = informal_input if informal_input else f"Cheers,\n{user_name}"

        print()
        print("Any phrases you never want to use? (comma-separated, or press Enter to skip)")
        print('e.g. "Hope this email finds you well, I wanted to reach out, Please don\'t hesitate"')
        banned_input = input("> ").strip()
        banned_phrases = [p.strip() for p in banned_input.split(",") if p.strip()] if banned_input else []

        # Save to configs/persona.yaml
        import yaml as _yaml

        persona_path = ROOT_DIR / "configs" / "persona.yaml"
        persona_config = _yaml.safe_load(persona_path.read_text(encoding="utf-8")) if persona_path.exists() else {}
        persona_config["name"] = user_name
        persona_config.setdefault("style", {})["avg_reply_words"] = rl.get("avg_words", 40)
        persona_config.setdefault("greeting_patterns", {})["default"] = chosen_greeting
        persona_config.setdefault("closing_patterns", {})["formal"] = chosen_formal.replace("\\n", "\n")
        persona_config["closing_patterns"]["informal"] = chosen_informal.replace("\\n", "\n")
        if banned_phrases:
            constraints = persona_config.get("style", {}).get("constraints", [])
            for phrase in banned_phrases:
                constraints.append(f'never use: "{phrase}"')
            persona_config["style"]["constraints"] = constraints

        persona_path.write_text(
            _yaml.dump(persona_config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
        print(f"\nPersona saved to {persona_path}")

        # Also update youos_config.yaml persona section
        config.setdefault("persona", {})
        config["persona"]["avg_reply_words"] = rl.get("avg_words", 40)
        config["persona"]["greeting_style"] = chosen_greeting
        config["persona"]["closing_formal"] = chosen_formal.replace("\\n", "\n")
        config["persona"]["closing_informal"] = chosen_informal.replace("\\n", "\n")
        config["persona"]["custom_constraints"] = banned_phrases

        import yaml as _yaml2

        CONFIG_PATH.write_text(
            _yaml2.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )

        # Save analysis JSON
        output_path = ROOT_DIR / "configs" / "persona_analysis.json"
        output_path.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")

        return findings
    except Exception as exc:
        print(f"  Persona analysis failed: {exc}")
        return None


def _generate_benchmarks() -> bool:
    """Generate benchmark cases from corpus using generate_benchmarks module."""
    print()
    print("--- Benchmark Generation ---")
    print("Generating personalised benchmark cases from your corpus...")

    try:
        from app.core.settings import get_settings
        from app.db.bootstrap import resolve_sqlite_path
        from scripts.generate_benchmarks import (
            generate_cases,
            seed_to_db,
            update_refresh_count,
            write_fixtures,
        )

        settings = get_settings()
        db_path = resolve_sqlite_path(settings.database_url)

        if not db_path.exists():
            print("  No database found. Skipping benchmarks.")
            return False

        cases = generate_cases(db_path, count=15)
        if not cases:
            print("  Not enough reply pairs for benchmarks.")
            return False

        write_fixtures(cases)
        inserted = seed_to_db(cases, db_path)

        # Update refresh count
        conn = sqlite3.connect(db_path)
        try:
            pair_count = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
        finally:
            conn.close()
        update_refresh_count(pair_count)

        print(f"  Generated {inserted} benchmark cases from your corpus")
        return True
    except Exception as exc:
        print(f"  Benchmark generation failed: {exc}")
        return False


def _offer_finetune(config: dict) -> None:
    """Offer initial fine-tuning if enough data."""
    print()
    try:
        from app.core.settings import get_settings
        from app.db.bootstrap import resolve_sqlite_path

        settings = get_settings()
        db_path = resolve_sqlite_path(settings.database_url)

        if not db_path.exists():
            return

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
        finally:
            conn.close()

        if count < 50:
            print(f"Only {count} reply pairs — not enough for fine-tuning yet.")
            print("Review some emails in the web UI first, then run 'youos finetune'.")
            return

        print(f"You have {count} reply pairs — enough to do an initial fine-tune.")
        print("This trains a local Qwen model on your writing style (~3 minutes).")
        print()
        proceed = input("Run initial fine-tune? [Y/n] ").strip().lower()
        if proceed == "n":
            return

        result = subprocess.run(
            [sys.executable, str(ROOT_DIR / "scripts" / "finetune_lora.py")],
            timeout=3600,
        )
        if result.returncode == 0:
            print("  Fine-tuning complete!")
        else:
            print("  Fine-tuning had issues. Run 'youos finetune' later.")
    except Exception as exc:
        print(f"  Fine-tuning skipped: {exc}")


def _setup_pin(config: dict) -> dict:
    """Optionally set a PIN for web UI access."""
    print()
    print("--- Access PIN ---")
    print("Set an access PIN? (recommended if sharing Tailscale with others)")
    print("Leave blank to skip (your Tailscale network is the auth layer).")
    pin = input("PIN: ").strip()

    if pin:
        from app.core.auth import get_pin_hash

        config.setdefault("server", {})["pin"] = get_pin_hash(pin)
        print("  PIN set. You'll need this to access the web UI.")
    else:
        config.setdefault("server", {})["pin"] = ""
        print("  No PIN set. Access is open on your network.")

    import yaml as _yaml

    CONFIG_PATH.write_text(
        _yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    return config


def _start_server(config: dict) -> None:
    """Start the YouOS server."""
    port = config.get("server", {}).get("port", 8765)
    host = config.get("server", {}).get("host", "0.0.0.0")

    print()
    print("--- Server ---")
    print(f"Starting YouOS server on {host}:{port}...")
    print()
    print(f"  Web UI:      http://localhost:{port}/feedback")
    print(f"  Stats:       http://localhost:{port}/stats")
    print(f"  Bookmarklet: http://localhost:{port}/bookmarklet")
    print()
    print("  Next step: open the Review Queue and review 10 emails.")
    print("  After 10 reviews, YouOS will fine-tune to your exact style.")
    print()
    print("  Run 'youos status' to check the system anytime.")
    print("  Run 'youos ui' to open the web UI.")


def _offer_nightly_pipeline(config: dict) -> None:
    """Offer to set up nightly pipeline."""
    print()
    print("Set up nightly self-improvement? (runs at 1 AM, takes ~90 min)")
    print("This ingests new emails, captures feedback, and auto-tunes the system.")
    print()
    proceed = input("Enable nightly pipeline? [Y/n] ").strip().lower()
    if proceed == "n":
        return

    schedule = config.get("autoresearch", {}).get("schedule", "0 1 * * *")
    print(f"  Nightly pipeline scheduled at: {schedule}")
    print("  To change: edit autoresearch.schedule in youos_config.yaml")

    # Try to set up cron
    try:
        if shutil.which("openclaw"):
            subprocess.run(
                [
                    "openclaw",
                    "cron",
                    "add",
                    "--name",
                    "youos:nightly",
                    "--schedule",
                    schedule,
                    "--command",
                    f"{sys.executable} {ROOT_DIR / 'scripts' / 'nightly_pipeline.py'}",
                ],
                timeout=10,
            )
            print("  Registered with OpenClaw scheduler.")
        else:
            print("  OpenClaw not found. Add to your system crontab manually:")
            print(f"  {schedule} cd {ROOT_DIR} && {sys.executable} scripts/nightly_pipeline.py")
    except Exception:
        print(f"  Add to crontab: {schedule} cd {ROOT_DIR} && {sys.executable} scripts/nightly_pipeline.py")


def main() -> None:
    _print_banner()

    # Pre-flight: run doctor checks
    from app.core.doctor import run_doctor_checks

    passed, failures = run_doctor_checks()
    if not passed:
        print("System check failed:")
        for msg in failures:
            print(f"  \u2717 {msg}")
        print("\nFix the above issues first.")
        sys.exit(1)

    input("Press Enter to continue...")
    print()

    # Step 1: Dependency check
    deps_ok = _check_dependencies()
    if not deps_ok:
        print("Some critical dependencies are missing. Please install them and re-run setup.")
        proceed = input("Continue anyway? [y/N] ").strip().lower()
        if proceed != "y":
            sys.exit(1)

    # Step 2: User identity
    identity = _get_user_identity()

    # Step 3: Verify accounts
    _verify_accounts(identity["emails"])

    # Step 4: Build config
    import yaml

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    config.setdefault("user", {})
    config["user"]["name"] = identity["name"]
    config["user"]["display_name"] = f"{identity['name']}OS" if identity["name"] != "User" else "YouOS"
    config["user"]["emails"] = identity["emails"]
    config["user"]["names"] = identity["names"]
    if identity.get("internal_domains"):
        config["user"]["internal_domains"] = identity["internal_domains"]
    config.setdefault("ingestion", {})
    config["ingestion"]["accounts"] = identity["emails"]
    config.setdefault("review", {})["batch_size"] = 10

    # Save config
    CONFIG_PATH.write_text(
        yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    print(f"\nConfiguration saved to {CONFIG_PATH}")

    # Step 5: Bootstrap database
    print()
    print("Initializing database...")
    try:
        subprocess.run(
            [sys.executable, str(ROOT_DIR / "scripts" / "bootstrap_db.py")],
            timeout=30,
        )
    except Exception as exc:
        print(f"  Database init failed: {exc}")

    # Step 6: Ingestion
    _run_ingestion(config)

    # Step 7: Persona analysis
    _run_persona_analysis(config)

    # Step 8: Benchmarks
    _generate_benchmarks()

    # Step 9: Fine-tune offer
    _offer_finetune(config)

    # Step 10: PIN auth
    config = _setup_pin(config)

    # Step 11: Server info
    _start_server(config)

    # Step 12: Nightly pipeline
    _offer_nightly_pipeline(config)

    print()
    print("Setup complete!")
    print()


if __name__ == "__main__":
    main()
