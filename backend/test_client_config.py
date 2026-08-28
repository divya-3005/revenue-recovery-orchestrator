import os
from unittest.mock import patch
import pytest

def test_inngest_client_production_mode():
    # When ENVIRONMENT is not production, is_production should be False
    with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
        from app.inngest_client import is_prod
        assert is_prod is False

    # When ENVIRONMENT is production, is_production should be True and it should require keys
    with patch.dict(os.environ, {
        "ENVIRONMENT": "production",
        "INNGEST_SIGNING_KEY": "test_signing_key_hex",
        "INNGEST_EVENT_KEY": "test_event_key"
    }, clear=True):
        import importlib
        import app.inngest_client
        importlib.reload(app.inngest_client)
        assert app.inngest_client.is_prod is True
