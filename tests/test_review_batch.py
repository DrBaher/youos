"""Tests for configurable review batch size."""

from app.core.config import get_review_batch_size


def test_default_batch_size():
    assert get_review_batch_size({}) == 10


def test_custom_batch_size():
    assert get_review_batch_size({"review": {"batch_size": 20}}) == 20


def test_batch_size_clamp_min():
    assert get_review_batch_size({"review": {"batch_size": 1}}) == 5


def test_batch_size_clamp_max():
    assert get_review_batch_size({"review": {"batch_size": 100}}) == 50


def test_batch_size_missing_section():
    assert get_review_batch_size({"user": {"name": "Test"}}) == 10
