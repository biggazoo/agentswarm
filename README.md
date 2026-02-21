# AgentSwarm Lite

A local multi-agent coding orchestrator that takes one project prompt and autonomously builds it using parallel AI agents. No Modal, no cloud GPUs. Runs on a Linux VPS with Python 3.11+.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set API keys
export MINIMAX_API_KEY="your-minimax-key"
export ANTHROPIC_API_KEY="your-anthropic-key"

# Run with a prompt
python main.py "build a todo app with React and Express"

# Or use a spec file
python main.py --spec SPEC.md
```

## Architecture

```
┌─────────────┐
│   Planner   │ ← Decomposes project into tasks
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────────┐
│          Task Queue (SQLite)           │
└──────┬───────────────────────┬────────┘
       │                       │
       ▼                       ▼
┌──────────────┐        ┌──────────────┐
│   Worker 1   │  ...   │   Worker N   │
│  (execute)   │        │  (execute)   │
└──────┬───────┘        └──────┬───────┘
       │                       │
       ▼                       ▼
       └───────────┬───────────┘
                   ▼
         ┌─────────────────┐
         │   Reconciler    │ ← Checks build health
         └─────────────────┘
```

## Configuration

Edit `config.py`:

- `MINIMAX_API_KEY` - API key for worker agents (cheap/fast)
- `ANTHROPIC_API_KEY` - API key for planner/reconciler
- `MAX_WORKERS` - Max parallel agents (default: 15)
- `WORKSPACE_DIR` - Where code gets written

## Usage

```bash
# Basic usage
python main.py "build a REST API with Express"

# From spec file
python main.py --spec path/to/SPEC.md

# Custom worker count
python main.py --workers 10 "build a blog"

# Run dashboard
python dashboard.py
```

## Project Structure

```
agentswarm/
├── main.py              # Entry point
├── dashboard.py         # Rich terminal UI
├── orchestrator.py      # Task queue manager
├── reconciler.py        # Build health checker
├── config.py            # Configuration
├── agents/
│   ├── planner.py       # Task decomposition
│   ├── worker.py        # Task execution
│   └── reconciler_agent.py
├── prompts/
│   ├── planner.txt      # Planner system prompt
│   ├── worker.txt       # Worker system prompt
│   └── reconciler.txt
├── workspace/           # Generated code
└── logs/               # Agent logs
```

## How It Works

1. **Planner** - Analyzes SPEC.md, decomposes into 10-100+ tasks
2. **Workers** - Execute tasks in parallel, each on their own git branch
3. **Reconciler** - Checks build health every 2 minutes, creates fix tasks if broken
4. **Dashboard** - Real-time Rich terminal UI

## Requirements

- Python 3.11+
- MiniMax API key (for workers - cheap)
- Anthropic API key (for planner/reconciler)
- Git

## Notes

- Workers use MiniMax M1 (cheap/fast for code generation)
- Planner/Reconciler use Claude Haiku (cheap for planning)
- Each task runs in its own git branch
- Reconciler checks main branch every 2 minutes
