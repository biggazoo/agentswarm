import os
from pathlib import Path

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# API - use MiniMax for all calls
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
if not MINIMAX_API_KEY:
    raise ValueError("MINIMAX_API_KEY not set in environment or .env file")

MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5")

# Use MiniMax for all agents
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "MiniMax-M2.5")
RECONCILER_MODEL = os.environ.get("RECONCILER_MODEL", "MiniMax-M2.5")

# Swarm limits
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "15"))
MAX_TASKS = int(os.environ.get("MAX_TASKS", "100"))
RECONCILER_INTERVAL = int(os.environ.get("RECONCILER_INTERVAL", "120"))
WORKER_TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "300"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
API_RATE_LIMIT_RPM = int(os.getenv("API_RATE_LIMIT_RPM", "20"))

# Paths
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/home/gary/agentswarm/workspace")
LOGS_DIR = os.environ.get("LOGS_DIR", "/home/gary/agentswarm/logs")
DB_PATH = os.environ.get("DB_PATH", "/home/gary/agentswarm/db/tasks.db")
OUTPUTS_DIR = os.environ.get("OUTPUTS_DIR", "/home/gary/agentswarm/outputs")
