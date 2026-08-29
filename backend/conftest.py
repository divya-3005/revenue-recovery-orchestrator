"""
Global test configuration.

Sets dummy API keys so provider constructors succeed in test environments.
Real API calls are always mocked at the ask_structured() level.
"""

import os
import pytest


@pytest.fixture(autouse=True)
def set_test_env_vars(monkeypatch):
    """Ensure provider constructors don't fail due to missing API keys in tests."""
    monkeypatch.setenv("GEMINI_API_KEY", "test_dummy_key")
    monkeypatch.setenv("GROQ_API_KEY", "test_dummy_key")
