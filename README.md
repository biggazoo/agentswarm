# VPS-AgentSwarm

Self-hosted multi-agent coding orchestrator for a single VPS.
No cloud GPUs. No Kubernetes. No $5,500 Modal bill.
Just your server, your API keys, and a swarm of coding agents.

## What It Does

Given a project specification, VPS-AgentSwarm:

1. Decomposes the project into granular tasks via a planner

## Process Management

- Workers tracked by PID with signal-based cleanup
- SIGTERM triggers graceful shutdown (10s grace → SIGKILL)
- Systemd service template included: agentswarm.service
- Zero orphaned processes on shutdown (verified under load)

## License

MIT

---
Built by Gary @ Kinetics.link
