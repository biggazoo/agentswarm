#!/usr/bin/env python3
"""
AgentSwarm Lite - Main Orchestrator Entry Point
"""
import argparse
import io
import os
import sys
import tarfile
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from multiprocessing import Process
import threading

import config
from orchestrator import get_queue


def setup_workspace():
    """Setup workspace directory and git repo"""
    workspace = Path(config.WORKSPACE_DIR)
    workspace.mkdir(parents=True, exist_ok=True)
    
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=workspace, check=True, capture_output=True)
        print(f"✅ Initialized git repo in {workspace}")
    
    return workspace


def write_spec(workspace: Path, spec: str):
    """Write SPEC.md to workspace"""
    spec_path = workspace / "SPEC.md"
    spec_path.write_text(spec)


def run_planner(spec: str) -> dict:
    """Run the planner agent"""
    from agents.planner import PlannerAgent
    planner = PlannerAgent()
    return planner.run("project", spec)


def worker_process(worker_id: str):
    """Worker subprocess entry point"""
    from agents.worker import WorkerAgent
    worker = WorkerAgent(worker_id)
    worker.run()


def spawn_worker(worker_id: str) -> Process:
    """Spawn a worker process"""
    proc = Process(target=worker_process, args=(worker_id,))
    proc.start()
    return proc


def start_dashboard():
    """Start dashboard in background"""
    proc = subprocess.Popen(
        [sys.executable, "dashboard.py"],
        cwd=Path(__file__).parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return proc


def package_final_delivery(project_name: str, run_id: str, start_time: float,
                           end_time: float, stats: dict, total_workers: int) -> str:
    """Package the entire workspace as the final project delivery archive."""
    workspace = Path(config.WORKSPACE_DIR)
    outputs_dir = Path(config.OUTPUTS_DIR)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = project_name[:30].replace(" ", "_").replace("/", "_")
    archive_name = f"{safe_name}-final-{timestamp}.tar.gz"
    archive_path = outputs_dir / archive_name

    duration = int(end_time - start_time)
    start_iso = datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()

    manifest = (
        f"run_id: {run_id}\n"
        f"project: {project_name}\n"
        f"task_count: {stats['total']}\n"
        f"tasks_done: {stats['done']}\n"
        f"tasks_failed: {stats['failed']}\n"
        f"workers_used: {total_workers}\n"
        f"start_time: {start_iso}\n"
        f"end_time: {end_iso}\n"
        f"duration_seconds: {duration}\n"
    )

    with tarfile.open(archive_path, "w:gz") as tar:
        # Embed manifest.txt at archive root
        manifest_bytes = manifest.encode()
        info = tarfile.TarInfo(name="manifest.txt")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

        # Add all workspace files (skip .git internals)
        for p in sorted(workspace.rglob("*")):
            if p.is_file() and ".git" not in p.parts:
                tar.add(p, arcname=str(p.relative_to(workspace)))

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"PROJECT COMPLETE — output: {archive_path} ({size_mb:.2f}MB)")
    return str(archive_path)


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py \"<project description>\"")
        print("   or: python main.py --spec SPEC.md")
        sys.exit(1)
    
    # Get spec from args
    if "--spec" in sys.argv:
        idx = sys.argv.index("--spec")
        spec_path = sys.argv[idx + 1]
        spec = Path(spec_path).read_text()
    else:
        spec = " ".join(sys.argv[1:])
    
    project_name = spec[:30].replace(" ", "_")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    
    print(f"🚀 AgentSwarm Lite starting...")
    print(f"📋 Project: {spec}")
    print()
    
    # Setup
    workspace = setup_workspace()
    queue = get_queue()
    queue.clear()
    
    # Write spec (locked)
    write_spec(workspace, spec)
    print(f"📝 Wrote SPEC.md")
    
    # Phase 1: Planning
    print("\n" + "="*50)
    print("PHASE 1: PLANNING")
    print("="*50 + "\n")
    
    plan_result = run_planner(spec)
    if plan_result.get('status') == 'error':
        print(f"❌ Planning failed: {plan_result.get('error')}")
        sys.exit(1)
    
    stats = queue.get_stats()
    print(f"✅ {stats['total']} tasks created\n")
    
    # Start dashboard
    print("📊 Starting dashboard...")
    dashboard_proc = start_dashboard()
    time.sleep(2)
    
    # Start reconciler in background
    from agents.reconciler_agent import get_reconciler
    reconciler = get_reconciler()
    reconciler_thread = reconciler.start_background(config.RECONCILER_INTERVAL)
    
    # Phase 2: Execution
    print("\n" + "="*50)
    print("PHASE 2: EXECUTION")
    print("="*50 + "\n")
    
    running_workers = {}
    total_workers_spawned = 0
    start_time = time.time()
    
    try:
        while True:
            stats = queue.get_stats()
            
            # Check if done
            if stats['pending'] == 0 and stats['running'] == 0:
                break
            
            # Spawn workers if needed
            slots = config.MAX_WORKERS - len(running_workers)
            if slots > 0 and stats['pending'] > 0:
                for i in range(min(slots, stats['pending'])):
                    worker_id = f"worker-{len(running_workers) + i + 1}"
                    proc = spawn_worker(worker_id)
                    running_workers[proc.pid] = (worker_id, proc)
                    total_workers_spawned += 1
                    print(f"🚀 Spawned {worker_id}")
            
            # Check for finished workers
            finished = []
            for pid, (worker_id, proc) in running_workers.items():
                if not proc.is_alive():
                    finished.append(pid)
            
            for pid in finished:
                del running_workers[pid]
            
            # Print status
            elapsed = int(time.time() - start_time)
            print(f"⏱️ {elapsed}s | Workers: {len(running_workers)} | Done: {stats['done']} | Pending: {stats['pending']}")
            
            time.sleep(5)
    
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted")
    
    # Final reconciler run
    print("\n" + "="*50)
    print("FINAL: Reconciliation")
    print("="*50 + "\n")
    
    reconciler.run_once()
    
    # Stop reconciler
    reconciler.stop()
    
    # Final stats
    stats = queue.get_stats()
    elapsed = int(time.time() - start_time)
    
    print("\n" + "="*50)
    print("COMPLETE")
    print("="*50)
    print(f"Runtime: {elapsed}s")
    print(f"Tasks: {stats['done']} done, {stats['failed']} failed, {stats['total']} total")
    print("="*50 + "\n")
    
    # Package final delivery archive
    end_time = time.time()
    package_final_delivery(project_name, run_id, start_time, end_time, stats, total_workers_spawned)

    # Stop dashboard
    dashboard_proc.terminate()

    if stats['failed'] > 0:
        print(f"⚠️ {stats['failed']} tasks failed")
        return 1

    print("✅ AgentSwarm Lite complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
