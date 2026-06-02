"""Hermetic tests for fairer golden scoring (b181).

These feed SYNTHETIC drafts to score_case / run_golden_eval — no model, no GPU.
They pin the b181 fairness contract:
  * a good, on-topic, correct-mode reply that is slightly over a tight cap now
    scores PASS/WARN, not a hard FAIL;
  * a keyword expressed via a synonym or an inflected form is credited;
  * empty / wrong-language / off-topic / egregiously-long replies STILL FAIL;
  * the graded composite reflects partial credit while the headline pass_rate
    stays passed/total.
"""

from __future__ import annotations

import yaml

from scripts.run_golden_eval import _keyword_hits, _stem, run_golden_eval, score_case

# ---------------------------------------------------------------------------
# Length: graded penalty, not a cliff
# ---------------------------------------------------------------------------


def test_slightly_long_good_reply_is_not_hard_fail():
    """A good, on-topic, correct-mode reply a few words over a tight cap should
    PASS (small length deduction), not the old hard FAIL."""
    case = {
        "id": "brief",
        "expected_keywords": ["when", "reschedule", "works"],
        "expected_mode": "work",
        "max_words": 20,
    }
    # 23 words — 15% over the 20-word cap, all keywords present, right mode.
    draft = (
        "No problem at all, we can reschedule the meeting. "
        "When works best for you later this week, and I'll make it happen?"
    )
    r = score_case(case, draft, "work")
    assert r["word_count"] > case["max_words"]
    assert r["status"] in {"pass", "warn"}
    assert r["status"] != "fail"
    # Length factor is a partial deduction, not zero.
    assert 0.5 <= r["length_factor"] < 1.0


def test_egregiously_long_reply_still_hard_fails():
    """Past 2x the cap is a degenerate blowup — still a hard FAIL."""
    case = {
        "id": "brief",
        "expected_keywords": ["when", "reschedule", "works"],
        "expected_mode": "work",
        "max_words": 20,
    }
    draft = "When can we reschedule, what works for you " * 12  # ~84 words >> 40
    r = score_case(case, draft, "work")
    assert r["word_count"] > case["max_words"] * 2
    assert r["status"] == "fail"
    assert r["length_factor"] == 0.0
    assert r["case_score"] == 0.0


def test_length_factor_full_credit_at_or_under_cap():
    case = {"id": "x", "expected_keywords": ["hello"], "expected_mode": "work", "max_words": 10}
    r = score_case(case, "hello there friend", "work")
    assert r["length_factor"] == 1.0


# ---------------------------------------------------------------------------
# Keywords: synonym / inflection tolerant
# ---------------------------------------------------------------------------


def test_inflected_keyword_is_credited():
    """`review` should be credited by `reviewed`; `connect` by `connecting`."""
    case = {
        "id": "prop",
        "expected_keywords": ["proposal", "review", "follow"],
        "expected_mode": "work",
        "max_words": 60,
    }
    # "document" (synonym of proposal), "reviewed" (inflection), "following".
    draft = "Following up on the document I sent — have you reviewed it yet?"
    r = score_case(case, draft, "work")
    assert r["keyword_hit_rate"] == 1.0
    assert r["status"] == "pass"


def test_synonym_keyword_is_credited():
    """`available`->`free`, `call`->`meeting`, `time`->`slot` all credited."""
    case = {
        "id": "sched",
        "expected_keywords": ["available", "time", "call", "week"],
        "expected_mode": "work",
        "max_words": 60,
    }
    draft = "I'm free Thursday; does that slot work for a meeting this week?"
    r = score_case(case, draft, "work")
    assert r["keyword_hit_rate"] == 1.0
    assert r["status"] == "pass"


def test_keyword_hits_helper_directly():
    stems = {_stem(t) for t in "i reviewed the introduction and were connecting".split()}
    assert _keyword_hits("review", stems, "i reviewed the introduction")
    assert _keyword_hits("intro", stems, "i reviewed the introduction")
    assert _keyword_hits("connect", stems, "were connecting")
    # An unrelated keyword is NOT credited.
    assert not _keyword_hits("invoice", stems, "i reviewed the introduction")


def test_offtopic_reply_gets_no_keyword_credit():
    """The synonym layer must not credit an unrelated reply — keywords stay
    meaningful, not auto-satisfied."""
    case = {
        "id": "sched",
        "expected_keywords": ["available", "time", "call", "week"],
        "expected_mode": "work",
        "max_words": 60,
    }
    draft = "The pizza we ordered downtown yesterday was absolutely delicious."
    r = score_case(case, draft, "work")
    assert r["keyword_hit_rate"] == 0.0


# ---------------------------------------------------------------------------
# Real badness still FAILs
# ---------------------------------------------------------------------------


def test_empty_reply_fails():
    case = {"id": "x", "expected_keywords": ["a"], "expected_mode": "work", "max_words": 50}
    r = score_case(case, "   ", "work")
    assert r["status"] == "fail"
    assert r["empty"] is True
    assert r["case_score"] == 0.0


def test_offtopic_reply_fails_even_with_mode_match():
    """Mode match alone must not rescue an off-topic reply to 'warn'."""
    case = {
        "id": "sched",
        "expected_keywords": ["available", "time", "call", "week"],
        "expected_mode": "work",
        "max_words": 60,
    }
    draft = "The pizza we ordered downtown yesterday was absolutely delicious."
    r = score_case(case, draft, "work")  # mode matches
    assert r["mode_match"] is True
    assert r["keyword_hit_rate"] == 0.0
    assert r["status"] == "fail"


def test_wrong_language_fails():
    case = {
        "id": "de",
        "expected_keywords": ["Woche", "Gespräch", "freundlichen"],
        "expected_mode": "work",
        "expected_language": "de",
        "max_words": 60,
    }
    # English reply to a German prompt — wrong language is a genuine miss.
    r = score_case(case, "Next week works for a meeting, kind regards.", "work", "en")
    assert r["language_match"] is False
    assert r["status"] == "fail"
    assert r["case_score"] == 0.0


# ---------------------------------------------------------------------------
# Composite reflects graded scores
# ---------------------------------------------------------------------------


def _golden_path(tmp_path, cases):
    p = tmp_path / "golden.yaml"
    p.write_text(yaml.dump({"cases": cases}))
    return p


def test_graded_composite_gives_partial_credit_for_near_miss(tmp_path):
    """A slightly-long good reply contributes partial (not zero) to the graded
    composite, while pass_rate stays the discrete passed/total."""
    cases = [
        {
            "id": "c1",
            "inbound": "x",
            "expected_keywords": ["reschedule", "works"],
            "expected_mode": "work",
            "max_words": 10,
        }
    ]
    gp = _golden_path(tmp_path, cases)
    # 14 words (40% over 10) but on-topic + right mode -> graded, not zero.
    draft = "Sure, we can reschedule; let me know what time works for you next week."

    def gen(prompt, *, database_url, configs_dir):
        return {"draft": draft, "detected_mode": "work"}

    summary = run_golden_eval(generate_fn=gen, golden_path=gp)
    res = summary["results"][0]
    assert res["length_factor"] < 1.0
    assert 0.0 < res["case_score"] < 1.0
    assert summary["graded_composite"] == res["case_score"]
    # Headline pass_rate stays the discrete passed/total.
    assert summary["pass_rate"] == round(summary["passed"] / summary["total"], 4)


def test_egregious_blowup_zeroes_composite_contribution(tmp_path):
    cases = [
        {
            "id": "c1",
            "inbound": "x",
            "expected_keywords": ["reschedule"],
            "expected_mode": "work",
            "max_words": 10,
        }
    ]
    gp = _golden_path(tmp_path, cases)
    draft = "reschedule " * 40  # 40 words, 4x the cap

    def gen(prompt, *, database_url, configs_dir):
        return {"draft": draft, "detected_mode": "work"}

    summary = run_golden_eval(generate_fn=gen, golden_path=gp)
    assert summary["results"][0]["status"] == "fail"
    assert summary["graded_composite"] == 0.0
    assert summary["pass_rate"] == 0.0


def test_realistic_good_drafts_now_pass_on_full_suite():
    """End-to-end on the real 10-case golden.yaml: realistic clean drafts using
    natural phrasing (the kind a clean Qwen3-4B emits) now PASS, where brittle
    exact-substring matching previously dinged them to warn. Evidence the 0.30
    plateau was partly scoring, not model capacity."""
    real = {
        "golden-schedule-meeting": (
            "Sure, I'd be glad to discuss the roadmap. I'm free Tuesday afternoon "
            "or Thursday morning — does either of those slots suit you for a meeting?",
            "work",
            None,
        ),
        "golden-decline-request": (
            "Thanks for the kind invitation, it sounds like a great event. Sadly my "
            "calendar is full that month and I won't be able to join as a speaker "
            "this time, but I'd welcome the chance another year.",
            "work",
            None,
        ),
        "golden-follow-up-proposal": (
            "Wanted to circle back on the document I shared last week. Have you been "
            "able to look it over yet? Glad to clarify anything.",
            "work",
            None,
        ),
        "golden-thank-intro": (
            "Thanks for connecting us, I really appreciate the introduction. Sarah, "
            "great to meet you — I'll reach out shortly.",
            "personal",
            None,
        ),
        "golden-ask-clarification": (
            "Happy to send it over — could you remind me what exactly you mean? "
            "Want to make sure I grab the right file.",
            "work",
            None,
        ),
        "golden-personal-mode": (
            "So good to hear from you! Drinks this weekend would be lovely — "
            "Saturday evening? Really looking forward to catching up.",
            "personal",
            None,
        ),
        "golden-multilang": (
            "Sehr geehrte Frau Schmidt, vielen Dank für Ihre Einladung zu einem "
            "Gespräch. Nächste Woche bin ich gerne verfügbar. Mit freundlichen Grüßen.",
            "work",
            "de",
        ),
        "golden-whatsapp-brief": (
            "No problem at all, we can move it. What time would work better for you "
            "later this week?",
            "work",
            None,
        ),
        "golden-internal-brevity": (
            "Sure, fire away — what do you need on the Q2 figures?",
            "work",
            None,
        ),
        "golden-personal-warmth": (
            "Hi there! So nice to hear from you. Things are going great on my end — "
            "we should grab a coffee soon!",
            "personal",
            None,
        ),
    }

    def gen(prompt, *, database_url, configs_dir):
        # Match the inbound text back to the case to pick the right synthetic draft.
        from scripts.run_golden_eval import load_golden_cases

        for c in load_golden_cases():
            if c["inbound"] == prompt:
                draft, mode, lang = real[c["id"]]
                return {"draft": draft, "detected_mode": mode, "detected_language": lang}
        raise AssertionError("unmatched prompt")

    summary = run_golden_eval(generate_fn=gen)
    # At least 8/10 of these genuinely-good drafts should now PASS (they all do
    # in practice; pin a conservative floor so the contract is robust).
    assert summary["passed"] >= 8
    assert summary["degenerate"] is False
