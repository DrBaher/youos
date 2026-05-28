"""Agent-first triage: fetch unread → filter → draft.

Phase 1 (this module): in-app loop. Phase 2 will push drafts to real Gmail
Drafts via OAuth. Phase 3 will allow rules-based auto-send. Never auto-sends
in Phase 1 — always drafts for review.
"""
