import os
from unittest.mock import patch

from src.config.settings import Settings


def test_settings_defaults():
    # Isolate from local .env file and env vars to test pure defaults
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("AI_LCM_", "AI_DEPLOY_"))}
    with patch.dict(os.environ, clean_env, clear=True):
        fresh = Settings(_env_file=None)
    assert fresh.aws_region == "us-west-2"
    assert fresh.dynamodb_table == "ai-deploy-table"
    assert fresh.port == 8000
