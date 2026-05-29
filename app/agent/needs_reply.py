"""Needs-reply classifier.

Combines hard rules (skip newsletters / noreply / empty) with a lightweight
score (sender history, action verbs, question marks) and the cold-outreach
detector. The goal isn't perfect precision — it's "filter the obvious noise
so the agent doesn't draft for every newsletter, and surface what actually
wants a reply."

Cold outreach gets *flagged* but not auto-skipped: the user may want a
polite-decline draft (and the generation pipeline's DECLINE_NUDGE handles
the tone).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.agent.inbox_fetch import InboxMessage

# --- Hard-skip patterns (sender NEVER wants a personal reply) --------------

# Bounces / mailer-daemon — actual mail-server replies. Hard-skip.
MAILER_DAEMON_PAT = re.compile(
    r"\b(mailer[-_.]?daemon|bounces?)\b",
    re.IGNORECASE,
)

# Automation domains that *are* systems (not human-tended). Hard-skip.
# Each addition came from a real-inbox false-positive: GitHub/CI in b29,
# meeting-bot services (Fireflies/Otter/Loom/Calendly/Doodle) in b30.
AUTOMATION_DOMAIN_PAT = re.compile(
    r"@(?:"
    r"notifications?\.|.*\.bounces\.|amazonses\.com|mailgun\.org|sendgrid\.net|"
    r"mailchimp\.com|github\.com|gitlab\.com|bitbucket\.org|"
    r"[\w-]+\.atlassian\.net|[\w-]+\.circleci\.com|[\w-]+\.travis-ci\.(?:com|org)|"
    r"fireflies\.ai|otter\.ai|loom\.com|calendly\.com|doodle\.com|fathom\.video|"
    r"krisp\.ai|grain\.com"
    r")",
    re.IGNORECASE,
)

# Subject patterns specific to service/CI/notification mail. Hard-skip.
# `[Org/Repo]` prefixes (GitHub, GitLab) and `<X> failed/succeeded` runs.
SERVICE_SUBJECT_PAT = re.compile(
    r"^\s*\[[\w./-]+/[\w./-]+\]|"
    r"\b(?:Build|Run|Pipeline|CI|PR)\s+(?:failed|succeeded|completed|cancelled|started)\b",
    re.IGNORECASE,
)

# Transactional template indicator. Matched in *either* subject or body —
# fires when a message reads as a confirmation/receipt template (booking
# confirmations, order receipts, appointment confirmations, etc.). Soft
# penalty rather than hard skip so a real human follow-up that *quotes* one
# of these phrases can still surface if other signals fire (a "could we
# reschedule the booking?" reply ends with a question, lifting the score).
#
# Caught in b50 QA on a real BaherOS inbox: an "Ali Barber Shop Booking
# Confirmation" hit score 0.60 (base 0.5 + imperative verb 0.10) and got
# auto-drafted. Even though replying is technically fine, the agent shouldn't
# spend its budget on transactional acknowledgements.
TRANSACTIONAL_TEMPLATE_PAT = re.compile(
    r"\b(?:"
    # Common confirmation/receipt subject lines.
    r"booking confirmation|order confirmation|appointment confirmation|"
    r"reservation confirmation|receipt for|payment (?:received|confirmation)|"
    r"delivery scheduled|order (?:placed|received|shipped)|"
    # Common body openings, e.g. "Your appointment is confirmed".
    r"your\s+(?:appointment|booking|order|reservation|payment|purchase|delivery|"
    r"subscription|trip|flight|hotel)\s+"
    r"(?:is\s+(?:confirmed|booked|scheduled|ready)|"
    r"has\s+been\s+(?:confirmed|received|placed|scheduled|shipped|processed))"
    r")\b",
    re.IGNORECASE,
)

# --- Soft-penalty patterns (might still want a personal reply) -------------

# `noreply@` / `donotreply@` — was hard-skip, now a soft penalty because
# transactional notifications (demo-form alerts, password resets, lead
# notifications) come from these addresses but carry real content. Marketing
# `noreply@` is caught separately by the List-Unsubscribe hard-rule.
NOREPLY_LOCAL_PAT = re.compile(
    r"\b(no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply)\b",
    re.IGNORECASE,
)

# Operational mailbox indicators in the local part — *anywhere* in the local
# part, not just at the start. b29 anchored at `^` and missed Google's
# `workspace-noreply@` / `calendar-notification@` patterns where the
# operational keyword sits *after* a prefix word. Substring match so any of
# these embedded keywords trips the penalty. Soft penalty (not hard skip) so
# a human-tended `support@vendor.com` conversation can still surface if other
# signals are strong.
NON_HUMAN_MAILBOX_PAT = re.compile(
    # noreply/donotreply intentionally left out — NOREPLY_LOCAL_PAT handles
    # those separately so we don't apply two −0.20 penalties to the same
    # `noreply@`-style address.
    r"(?:^|[\w-])(?:"
    r"notifications?|notify|alerts?|automated|billing|support|help|info|hello|"
    r"admin|team|service|webmaster|postmaster|abuse"
    r")(?:[\w-]*)@",
    re.IGNORECASE,
)

# Action verbs in the imperative — strong "the sender wants something from
# you" signal. Conservative list to avoid false positives.
ACTION_VERB_PAT = re.compile(
    r"\b(?:please|could you|can you|would you|let me know|send|share|review|"
    r"approve|confirm|check|update|fix|investigate|reply|respond|forward)\b",
    re.IGNORECASE,
)


@dataclass
class NeedsReplyVerdict:
    needs_reply: bool
    score: float                    # 0.0 – 1.0
    reasons: list[str] = field(default_factory=list)
    cold_outreach: bool = False
    # True when ``needs_reply=False`` but the message wasn't hard-skipped and
    # the score is in the borderline band (≥ 0.3) — i.e. "the agent isn't
    # confident enough to auto-draft, but the message shouldn't be silently
    # buried either." β surfaces these collapsed under "Review skipped."
    surface_for_review: bool = False
    # True when the sender matches ``agent.vip_senders`` — gets a strong score
    # boost and should sort to the top of the queue.
    vip: bool = False


# --- Sender history (count of prior reply pairs to a sender) ---------------


class SenderHistory:
    """Counts how many reply pairs the user has with a given inbound author.

    The signal is "have I corresponded with this exact email before" — same
    spirit as ``sender_email_boost`` in retrieval (b26), used here as a
    lightweight needs-reply hint rather than a re-ranking weight.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._cache: dict[str, int] = {}

    def count_for(self, sender_email: str | None) -> int:
        if not sender_email:
            return 0
        key = sender_email.lower()
        if key in self._cache:
            return self._cache[key]
        try:
            from app.db.bootstrap import connect

            conn = connect(self._db_path)
            try:
                row = conn.execute(
                    # Match either "Name <email>" or bare "email" forms.
                    "SELECT COUNT(*) FROM reply_pairs "
                    "WHERE LOWER(inbound_author) LIKE ? OR LOWER(inbound_author) LIKE ?",
                    (f"%<{key}>%", f"%{key}%"),
                ).fetchone()
                n = int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            n = 0
        self._cache[key] = n
        return n

    @classmethod
    def from_database_url(cls, database_url: str) -> "SenderHistory":
        # urlparse turns ``sqlite:///var/youos.db`` into the absolute
        # ``/var/youos.db``, breaking the relative-path default Settings
        # emits. Use removeprefix to mirror app/db/bootstrap.py + the
        # rest of the codebase.
        prefix = "sqlite:///"
        if not database_url.startswith(prefix):
            raise ValueError(f"Only sqlite:/// URLs are supported (got {database_url!r})")
        return cls(database_url.removeprefix(prefix))


# --- Classifier -------------------------------------------------------------


def _matches_skip_list(sender_email: str | None, skip_list: list[str]) -> bool:
    """True if the sender matches any user-configured skip entry. Entries are
    either exact emails (``alice@x.com``) or ``@domain`` prefixes
    (``@bigcorp.com``) — the latter skips the whole org. Case-insensitive."""
    if not sender_email or not skip_list:
        return False
    email = sender_email.lower()
    for entry in skip_list:
        if not entry:
            continue
        if entry.startswith("@"):
            if email.endswith(entry):
                return True
        elif email == entry:
            return True
    return False


def classify(
    msg: InboxMessage,
    *,
    history: SenderHistory | None = None,
    threshold: float = 0.6,
    skip_senders: list[str] | None = None,
    vip_senders: list[str] | None = None,
) -> NeedsReplyVerdict:
    """Decide whether this inbound deserves a draft.

    Hard skips (return immediately with score 0):
      - ``List-Unsubscribe`` header (newsletter / mass mail)
      - mailer-daemon / bounces sender
      - automation domain (GitHub, GitLab, BitBucket, Atlassian, CircleCI,
        Travis, ``notifications.*``, ``mailchimp/mailgun/sendgrid/amazonses``)
      - service subject pattern (``[Org/Repo]`` prefixes, ``Build|Run|CI
        failed/succeeded``)
      - empty body

    Surviving messages get scored from base 0.5:
      +0.20 ending question, +0.10 imperative verb, +0.10 short body,
      +0.20 prior history with this exact sender, −0.20 very long digest,
      −0.20 ``noreply@`` / ``donotreply@`` (was a hard skip; softened
      because transactional ``noreply@`` carries lead/form content),
      −0.20 operational mailbox prefix (``billing|support|info|hello|
      notifications|alerts|admin|team|...@``), −0.15 cold-outreach flag.
    """
    reasons: list[str] = []

    # 1) Hard skips — sender CANNOT be replied to personally, or content is
    # obviously not user-actionable. Each returns immediately with score=0.
    # ζ: user-configured `agent.skip_senders` is checked first so a noisy
    # specific sender can be silenced without waiting for a heuristic.
    if skip_senders and _matches_skip_list(msg.sender_email, skip_senders):
        return NeedsReplyVerdict(False, 0.0, [f"skip-list match ({msg.sender_email!r})"])
    if msg.headers.get("list-unsubscribe"):
        return NeedsReplyVerdict(False, 0.0, ["list-unsubscribe (newsletter)"])
    if msg.sender and MAILER_DAEMON_PAT.search(msg.sender):
        return NeedsReplyVerdict(False, 0.0, [f"mailer-daemon/bounce ({msg.sender!r})"])
    if msg.sender and AUTOMATION_DOMAIN_PAT.search(msg.sender):
        return NeedsReplyVerdict(False, 0.0, [f"automation domain ({msg.sender!r})"])
    if msg.subject and SERVICE_SUBJECT_PAT.search(msg.subject):
        return NeedsReplyVerdict(False, 0.0, [f"service subject pattern ({msg.subject!r})"])
    if not msg.body.strip():
        return NeedsReplyVerdict(False, 0.0, ["empty body"])

    # 2) Cold-outreach detection (re-uses the b27 detector). Doesn't decide
    # needs_reply on its own — but does flag for the UI / generation nudge.
    from app.core.cold_outreach import detect_cold_outbound

    cold = detect_cold_outbound(
        subject=msg.subject,
        body=msg.body,
        sender_email=msg.sender_email,
    )

    # Score the NEW content only — strip quoted reply history and the trailing
    # signature before looking for questions / imperatives / length. On a
    # threaded reply, msg.body carries the whole quoted prior message, whose
    # text almost always contains a '?' or "please"; scoring the raw body
    # inflated trivial acknowledgements ("thanks", "will do") into drafted
    # replies. Hard-skip header/sender checks above intentionally ran on the
    # full message; only the soft content signals use the trimmed text.
    from app.core.text_utils import extract_new_content, strip_signature

    scoring_text = strip_signature(extract_new_content(msg.body))
    if not scoring_text.strip():
        # Degenerate (pure quote / signature only) — fall back to the full body
        # rather than score emptiness.
        scoring_text = msg.body

    # 3) Lightweight needs-reply score.
    score = 0.5  # start at the boundary; signals tip it one way or the other

    # Soft penalty for `noreply@` / `donotreply@` — was a hard skip, but
    # transactional notifications (demo-form alerts, password resets) come
    # from these too. Penalty rather than skip lets strong positive signals
    # rescue real leads.
    if msg.sender and NOREPLY_LOCAL_PAT.search(msg.sender):
        score -= 0.20
        reasons.append("noreply sender (transactional or marketing)")

    # Soft penalty for operational mailbox prefixes (billing/support/info/
    # notifications/alerts/etc.). Same idea: usually automation, but a
    # human-tended `support@` can still surface if other signals fire.
    if msg.sender_email and NON_HUMAN_MAILBOX_PAT.search(msg.sender_email):
        score -= 0.20
        reasons.append(f"operational mailbox ({msg.sender_email})")

    # Transactional template detection — fires on the subject (strong cue) or
    # the body's first 500 chars (where template boilerplate sits). Subject
    # match counts for slightly more because subject patterns rarely false-
    # positive on real human mail.
    transactional = False
    if msg.subject and TRANSACTIONAL_TEMPLATE_PAT.search(msg.subject):
        score -= 0.25
        reasons.append("transactional template (subject)")
        transactional = True
    elif msg.body and TRANSACTIONAL_TEMPLATE_PAT.search(msg.body[:500]):
        score -= 0.20
        reasons.append("transactional template (body)")
        transactional = True

    if "?" in scoring_text[-200:]:
        score += 0.20
        reasons.append("ends with a question")

    if ACTION_VERB_PAT.search(scoring_text):
        # Imperative verbs are ubiquitous in transactional templates
        # ("looking forward to see you", "click here to confirm"). When the
        # template detector already fired, suppress the imperative bonus —
        # the verb is template noise, not a request for action.
        if transactional:
            reasons.append("imperative verb present — suppressed (transactional)")
        else:
            score += 0.10
            reasons.append("imperative verb present")

    word_count = len(scoring_text.split())
    # Trivial acknowledgement: very short NEW content with no question and no
    # request ("thanks", "sounds good", "will do"). Common as the latest message
    # on a thread the user already handled — penalize rather than give it the
    # short-body bonus, so it surfaces for review instead of being auto-drafted.
    is_trivial_ack = (
        word_count <= 6
        and "?" not in scoring_text
        and not ACTION_VERB_PAT.search(scoring_text)
    )
    if is_trivial_ack:
        score -= 0.15
        reasons.append(f"trivial acknowledgement ({word_count} words, no request)")
    elif word_count <= 120:
        score += 0.10
        reasons.append(f"short body ({word_count} words)")
    elif word_count > 800:
        score -= 0.20
        reasons.append(f"very long body ({word_count} words) — likely digest")

    # Prior-history boost — but only for *human* senders. b30 QA: ingest had
    # captured Wise / Workspace / Calendar notifications into reply_pairs, so
    # `count_for(noreply@wise.com)` returned 6 and the +0.20 boost lifted pure
    # automation past threshold. Suppress when noreply / operational pattern
    # already fired — those prior pairs are corpus noise, not real history.
    if history is not None and msg.sender_email:
        prior = history.count_for(msg.sender_email)
        if prior > 0:
            is_transactional = bool(
                (msg.sender and NOREPLY_LOCAL_PAT.search(msg.sender)) or
                (msg.sender_email and NON_HUMAN_MAILBOX_PAT.search(msg.sender_email))
            )
            if is_transactional:
                reasons.append(f"prior history ({prior}) — suppressed (sender is automation)")
            else:
                score += 0.20
                reasons.append(f"prior history ({prior} reply pairs)")

    if cold.is_cold:
        # Cold outreach is *lower* needs-reply priority but still gets a
        # draft so the user can decline politely. Net effect: a marginal
        # case (score ~0.55) drops below threshold; a strong case (score
        # >0.7 from history + question) still surfaces.
        score -= 0.15
        reasons.append(f"cold-outreach (heuristic score {cold.score})")

    # VIP boost — a strong, late bump so a VIP's real mail clears the threshold
    # and sorts to the top, even if it carried a penalty (operational mailbox,
    # noreply). Hard-skips ran earlier and returned, so a VIP's automation /
    # newsletters are still filtered; this only lifts mail that survived to
    # scoring. +0.25 ⇒ base 0.5 alone reaches 0.75 (> default 0.6).
    is_vip = bool(vip_senders and _matches_skip_list(msg.sender_email, vip_senders))
    if is_vip:
        score += 0.25
        reasons.append("VIP sender (prioritized)")

    score = max(0.0, min(1.0, score))
    needs_reply = score >= threshold
    # Surface-for-review tier: didn't pass, but wasn't junk either — score is
    # in the borderline band and no hard-skip ran (hard skips return early
    # with score=0.0, never reach this).
    surface_for_review = (not needs_reply) and score >= 0.30
    return NeedsReplyVerdict(
        needs_reply=needs_reply,
        score=score,
        reasons=reasons,
        cold_outreach=cold.is_cold,
        surface_for_review=surface_for_review,
        vip=is_vip,
    )


def classify_many(
    messages: Iterable[InboxMessage],
    *,
    history: SenderHistory | None = None,
    threshold: float = 0.6,
    skip_senders: list[str] | None = None,
    vip_senders: list[str] | None = None,
) -> list[tuple[InboxMessage, NeedsReplyVerdict]]:
    """Vectorised helper. Returns pairs in the input order."""
    return [
        (
            m,
            classify(
                m, history=history, threshold=threshold,
                skip_senders=skip_senders, vip_senders=vip_senders,
            ),
        )
        for m in messages
    ]
