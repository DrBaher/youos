"""Create a throwaway YouOS demo instance seeded with SYNTHETIC data for
landing-page screenshots. No real inbox content is used."""
import os
import shutil
import json
from pathlib import Path

DEMO = Path("/tmp/youos_demo")
if DEMO.exists():
    shutil.rmtree(DEMO)
(DEMO / "var").mkdir(parents=True)
(DEMO / "configs").mkdir(parents=True)
(DEMO / "docs").mkdir(parents=True)
shutil.copy("~/YouOS/docs/schema.sql", DEMO / "docs" / "schema.sql")

os.environ["YOUOS_DATA_DIR"] = str(DEMO)

# --- config: synthetic identity, local model, no PIN ---
import yaml  # noqa: E402
repo_cfg = yaml.safe_load(Path("~/YouOS/youos_config.yaml").read_text())
repo_cfg.setdefault("user", {})
repo_cfg["user"].update({
    "name": "Alex",
    "display_name": "Alex Rivera",
    "emails": ["alex@example.com"],
    "timezone": "America/New_York",
})
repo_cfg.setdefault("identity", {})["display_name"] = "AlexOS"
repo_cfg["ingestion"]["accounts"] = ["alex@example.com"]
repo_cfg["server"]["pin"] = ""
# Warm LoRA model server on a dedicated port (8088 is often a global base-only
# server) so the Draft tab streams from the fine-tuned model and the badge reads
# "✍️ your fine-tuned model". Requires an adapter copied into
# models/adapters/latest (see CAPTURE.md step 2).
repo_cfg.setdefault("model", {})["server"] = {"enabled": True, "port": 8099}
# copy persona/retrieval/prompts configs so generation has its knobs
for f in ("persona.yaml", "retrieval.yaml", "prompts.yaml", "autoresearch.yaml"):
    src = Path("~/YouOS/configs") / f
    if src.exists():
        shutil.copy(src, DEMO / "configs" / f)
Path(DEMO / "youos_config.yaml").write_text(yaml.safe_dump(repo_cfg, sort_keys=False))

# --- bootstrap schema (base + agent tables) ---
from app.db.bootstrap import bootstrap_database, ensure_agent_schema  # noqa: E402
db_path = bootstrap_database()
db_url = f"sqlite:///{db_path}"
ensure_agent_schema(db_url)
print("DB:", db_path)

import sqlite3  # noqa: E402
conn = sqlite3.connect(db_path)

# --- synthetic reply pairs (the "real past replies" corpus) ---
PAIRS = [
    ("Hey, can we move our Thursday sync to Friday morning instead?",
     "Friday morning works great — let's lock in 9:30. I'll move the invite and add the updated agenda.",
     "priya@northwind.io"),
    ("Quick one: did the contract redlines go back to legal yet?",
     "They went over last night. Legal said to expect their pass by Wednesday — I'll forward the moment it lands.",
     "marcus@northwind.io"),
    ("Are you still good to give feedback on the launch deck today?",
     "Yep — just read it through. Strong narrative. Two notes on the pricing slide; I'll drop comments in the doc this afternoon.",
     "dana@brightlabs.co"),
    ("We're seeing intermittent 500s on the staging API since the deploy. Thoughts?",
     "Looks like the new connection-pool limit is too tight under load. Bumping it back and adding a retry — should clear within the hour. Watching the dashboards now.",
     "sam@brightlabs.co"),
    ("Would love 20 min to pick your brain on the partnership idea.",
     "Happy to. I'm open Tuesday after 2 or Thursday before noon — send whatever fits and I'll confirm.",
     "lena@meridian.partners"),
    ("Can you approve the Q3 marketing spend before EOD?",
     "Approved. One flag: the events line is running ~12% hot vs plan, so let's revisit that in next week's review.",
     "tom@northwind.io"),
    ("Sorry to bug you — any update on the hiring loop for the backend role?",
     "No bug at all. Two strong finalists; I'm scheduling the final panels for next week and will share scorecards right after.",
     "priya@northwind.io"),
    ("Customer is asking for a firm migration date. What can I tell them?",
     "Tell them the 18th, with a 2-day buffer either side. We're feature-complete and just finishing the data-replication tests.",
     "marcus@northwind.io"),
]
for i, (inb, rep, author) in enumerate(PAIRS, 1):
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, "
        "inbound_author, reply_author, quality_score, paired_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("synthetic", f"synth_{i:03d}", inb, rep, author, "alex@example.com",
         0.9, f"2026-05-{10+i:02d}T09:00:00"),
    )

# --- synthetic review-queue rows (the autonomous-agent flow) ---
QUEUE = [
    ("priya@northwind.io", "Priya Shah", "Re: Roadmap review — need your call on scope",
     "Hi Alex — before Friday's roadmap review I need your call on whether we cut the "
     "reporting module from v2 to hit the date. Can you weigh in?",
     0.91, 0.78, "draft",
     "Good question. Let's keep reporting in v2 but ship it behind a flag — that protects the "
     "date without dropping the feature. I'll bring the rollout plan to Friday's review.",
     ["asked a direct question", "decision needed before a deadline"]),
    ("dana@brightlabs.co", "Dana Lin", "Launch deck — final pricing slide",
     "Can you give the pricing slide one more look before we send to the board tonight?",
     0.84, 0.66, "draft",
     "Just looked — pricing reads clean. One tweak: lead with the annual plan so the savings "
     "anchor lands first. Otherwise good to send.",
     ["explicit request for review", "time-sensitive (tonight)"]),
    ("lena@meridian.partners", "Lena Ortiz", "Partnership follow-up",
     "Great chatting earlier! Sending over the one-pager — let me know if a pilot in Q3 could work.",
     0.72, 0.40, "draft",
     "Thanks Lena — enjoyed it too. The one-pager looks compelling; a Q3 pilot is realistic on "
     "our side. Let me loop in our product lead and propose a scope by end of week.",
     ["warm follow-up", "proposes next step"]),
    ("newsletter@devweekly.com", "Dev Weekly", "This week in backend: connection pooling deep-dive",
     "Your weekly roundup of backend engineering reads...",
     0.18, 0.05, "surface", None,
     ["likely newsletter", "low needs-reply score"]),
]
for i, (em, name, subj, body, score, urg, tier, draft, reasons) in enumerate(QUEUE, 1):
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, thread_id, account, sender, sender_email, "
        "subject, body, received_at, needs_reply_score, reasons_json, tier, urgency_score, "
        "urgency_reasons_json, draft, draft_model, status, quality_score, calibrated_score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"msg_{i:03d}", f"thr_{i:03d}", "alex@example.com", name, em, subj, body,
         f"2026-06-03T{8+i:02d}:15:00", score, json.dumps(reasons), tier, urg,
         json.dumps([]), draft, "qwen3-4b-lora" if draft else None, "pending",
         0.82 if draft else None, score),
    )

# --- synthetic facts / memory (personalization) ---
FACTS = [
    ("contact", "priya@northwind.io", "Prefers morning meetings; leads the platform team.", 0.92,
     ["contact", "scheduling"]),
    ("contact", "marcus@northwind.io", "Owns legal/contract coordination; likes firm dates with buffers.", 0.88,
     ["contact"]),
    ("project", "v2_launch", "v2 launch targets the 18th; reporting module ships behind a flag.", 0.9,
     ["project", "roadmap"]),
    ("user_pref", "sign_off", "Signs work email 'Best, Alex'; warmer 'Cheers' with close contacts.", 0.95,
     ["style"]),
    ("user_pref", "tone", "Concise and direct on internal threads; reassuring and polished with clients.", 0.9,
     ["style", "tone"]),
]
for typ, key, fact, conf, tags in FACTS:
    conn.execute(
        "INSERT OR IGNORE INTO memory (type, key, fact, confidence, tags) VALUES (?,?,?,?,?)",
        (typ, key, fact, conf, json.dumps(tags)),
    )

conn.commit()
# populate FTS for retrieval
try:
    from app.db.bootstrap import _populate_fts
    _populate_fts(conn)
    conn.commit()
except Exception as e:
    print("fts:", e)
conn.close()
print("Seeded:", len(PAIRS), "pairs,", len(QUEUE), "queue rows,", len(FACTS), "facts")
