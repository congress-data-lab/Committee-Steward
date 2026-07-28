import os
import psycopg2
from pathlib import Path

def get_connection():
    _load_env()
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        raise ValueError("NEON_DATABASE_URL environment variable is not set")
    return psycopg2.connect(url)


def _load_env():
    """Load .env into os.environ if not already set."""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if key and key not in os.environ:
                        os.environ[key] = val
