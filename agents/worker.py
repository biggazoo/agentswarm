#!/usr/bin/env python3
"""
Worker Agent - Executes a single coding task
"""
import io
import json
import subprocess
import sys
import os
import fcntl
import tarfile
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Optional
import requests
import traceback

import config
from orchestrator import get_queue


class WorkerAgent:
    """Worker that executes a single task"""
    
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self.workspace = Path(config.WORKSPACE_DIR)
        self.model = config.MINIMAX_MODEL
        self.api_key = config.MINIMAX_API_KEY
        self.base_url = config.MINIMAX_BASE_URL
        
    def read_prompt(self) -> str:
        """Load worker system prompt"""
        with open(f"{Path(__file__).parent.parent}/prompts/worker.txt") as f:
            return f.read()
    
    def get_workspace_tree(self, max_depth: int = 2) -> str:
        """Get workspace file tree"""
        tree = []
        for p in self.workspace.rglob('*'):
            if p.is_file():
                depth = len(p.relative_to(self.workspace).parts)
                if depth <= max_depth:
                    tree.append(str(p.relative_to(self.workspace)))
        return '\n'.join(tree) if tree else "(empty)"
    
    def read_spec(self) -> str:
        """Read project specification"""
        spec_path = self.workspace / "SPEC.md"
        if spec_path.exists():
            return spec_path.read_text()
        return ""
    
    def read_features(self) -> str:
        """Read features list"""
        features_path = self.workspace / "FEATURES.json"
        if features_path.exists():
            return features_path.read_text()
        return "[]"
    
    def git_lock(self):
        """Acquire git lock"""
        lock_file = self.workspace / ".git" / "agentswarm.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        return open(lock_file, 'w')
    
    def init_git_repo(self):
        """Initialize git repo if needed"""
        lock = self.git_lock()
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not (self.workspace / ".git").exists():
                subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
                subprocess.run(["git", "checkout", "-b", "main"], cwd=self.workspace, check=True, capture_output=True)
                subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=self.workspace, check=True, capture_output=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
    
    def setup_branch(self, branch_name: str):
        """Create and switch to task branch with locking"""
        lock = self.git_lock()
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            subprocess.run(["git", "checkout", "main"], cwd=self.workspace, capture_output=True)
            # Delete branch if it already exists (retry scenario)
            subprocess.run(["git", "branch", "-D", branch_name], cwd=self.workspace, capture_output=True)
            subprocess.run(["git", "checkout", "-b", branch_name], cwd=self.workspace, check=True, capture_output=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
    
    def _rate_limit_state_paths(self):
        state_dir = Path(config.LOGS_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "api_rate_limit_state.json", state_dir / "api_rate_limit_state.lock"

    def _throttle_for_rate_limit(self):
        """Global cross-worker rate limiter using a shared file + flock.

        Reserves one API slot per call and enforces:
        - hard RPM cap (config.API_RATE_LIMIT_RPM)
        - proactive throttling when near limit
        - 2-3s stagger between worker API calls
        """
        rpm = max(1, int(getattr(config, "API_RATE_LIMIT_RPM", 20)))
        warn_threshold = max(1, int(rpm * 0.8))
        state_path, lock_path = self._rate_limit_state_paths()

        while True:
            wait_seconds = 0.0
            approaching = False

            with open(lock_path, "w") as lockf:
                fcntl.flock(lockf, fcntl.LOCK_EX)

                now = time.time()
                timestamps = []
                if state_path.exists():
                    try:
                        payload = json.loads(state_path.read_text())
                        timestamps = payload.get("timestamps", [])
                    except Exception:
                        timestamps = []

                timestamps = [t for t in timestamps if now - float(t) < 60.0]

                if len(timestamps) >= rpm:
                    oldest = min(timestamps)
                    wait_seconds = max(0.5, 60.0 - (now - oldest))
                else:
                    if len(timestamps) >= warn_threshold:
                        approaching = True
                    timestamps.append(now)
                    state_path.write_text(json.dumps({"timestamps": timestamps}), encoding="utf-8")

                fcntl.flock(lockf, fcntl.LOCK_UN)

            if wait_seconds > 0:
                print(f"[{self.worker_id}] Rate limit hit — waiting {wait_seconds:.1f}s before API call")
                time.sleep(min(wait_seconds, 5.0))
                continue

            if approaching:
                print(f"[{self.worker_id}] Rate limit approaching — throttling worker {self.worker_id}")

            # Always stagger calls 2-3s to avoid burst spikes.
            time.sleep(random.uniform(2.0, 3.0))
            return

    def call_api(self, system: str, user: str) -> str:
        """Call MiniMax API"""
        import re

        self._throttle_for_rate_limit()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "max_tokens": 4000,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        }

        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=300
        )

        if response.status_code != 200:
            raise Exception(f"API error: {response.status_code} - {response.text}")

        data = response.json()
        content = data["choices"][0]["message"]["content"]

        # Strip thinking tags that MiniMax includes
        return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    
    def syntax_check(self, file_path: Path) -> bool:
        """Check syntax of a file"""
        try:
            if file_path.suffix == '.py':
                subprocess.run(["python3", "-m", "py_compile", str(file_path)], 
                             cwd=self.workspace, check=True, capture_output=True)
            elif file_path.suffix in ['.js', '.ts', '.jsx', '.tsx']:
                subprocess.run(["node", "--check", str(file_path)], 
                             cwd=self.workspace, check=True, capture_output=True)
            elif file_path.suffix == '.sh':
                subprocess.run(["bash", "-n", str(file_path)], 
                             cwd=self.workspace, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"Syntax error in {file_path}: {e.stderr.decode() if e.stderr else str(e)}")
            return False
        except Exception as e:
            print(f"Could not syntax check {file_path}: {e}")
            return True
    
    def write_files(self, files: list) -> list:
        """Write files to workspace"""
        written = []
        for f in files:
            path = f.get('path', '')
            if not path:
                continue
            
            file_path = self.workspace / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(f.get('content', ''))
            written.append(path)
            print(f"[{self.worker_id}] 📝 Wrote {path}")
            
            if not self.syntax_check(file_path):
                raise Exception(f"Syntax check failed for {path}")
        
        return written
    
    def commit_and_merge(self, task_id: str, title: str) -> bool:
        """Commit changes and merge to main with locking"""
        branch_name = f"agent-{task_id[:8]}"
        commit_msg = f"task: {title}"
        
        lock = self.git_lock()
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            
            subprocess.run(["git", "add", "-A"], cwd=self.workspace, capture_output=True)
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=self.workspace, capture_output=True)
            
            subprocess.run(["git", "checkout", "main"], cwd=self.workspace, capture_output=True)
            
            merge_result = subprocess.run(
                ["git", "merge", "--no-ff", branch_name],
                cwd=self.workspace,
                capture_output=True,
                text=True
            )
            
            if merge_result.returncode != 0:
                subprocess.run(["git", "merge", "--abort"], cwd=self.workspace, capture_output=True)
                subprocess.run(["git", "checkout", branch_name], cwd=self.workspace, capture_output=True)
                return False
            
            return True
            
        except Exception as e:
            print(f"Git error: {e}")
            return False
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
    
    def parse_response(self, response: str) -> dict:
        """Parse JSON response from agent"""
        # Remove thinking tags that MiniMax adds
        import re
        cleaned = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
        cleaned = cleaned.strip()
        
        # Strip markdown code fences if present
        fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', cleaned)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        try:
            json_match = cleaned.find('[')
            brace_match = cleaned.find('{')
            if json_match == -1 or (brace_match != -1 and brace_match < json_match):
                json_match = brace_match

            if json_match != -1:
                result = json.loads(cleaned[json_match:])
                if isinstance(result, list):
                    result = result[0] if result else {}
                return result
        except json.JSONDecodeError:
            pass

        return {"files": [], "summary": cleaned[:500], "tokens_estimate": 500}
    
    def package_output(self, task_id: str, written_files: list) -> Optional[str]:
        """Package task output files into a .tar.gz archive.

        Returns the archive path on success, None on failure.
        """
        try:
            outputs_dir = Path(config.OUTPUTS_DIR)
            outputs_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            archive_name = f"{task_id[:8]}-{self.worker_id}-{timestamp}.tar.gz"
            archive_path = outputs_dir / archive_name

            with tarfile.open(archive_path, "w:gz") as tar:
                for rel_path in written_files:
                    # Read from git's object store (main branch) to avoid
                    # filesystem race conditions from concurrent branch switches
                    result = subprocess.run(
                        ["git", "show", f"main:{rel_path}"],
                        cwd=self.workspace,
                        capture_output=True
                    )
                    if result.returncode == 0:
                        content = result.stdout
                        info = tarfile.TarInfo(name=rel_path)
                        info.size = len(content)
                        info.mode = 0o644
                        tar.addfile(info, io.BytesIO(content))

            print(f"[{self.worker_id}] Worker {self.worker_id} output packaged: {archive_path}")
            return str(archive_path)
        except Exception as e:
            print(f"[{self.worker_id}] ⚠️ Failed to package output: {e}")
            return None

    def execute_task(self, task: dict) -> dict:
        """Execute a single task"""
        task_id = task['task_id']
        title = task['title']
        description = task['description']
        branch_name = task.get('branch_name', f"agent-{task_id[:8]}")
        
        queue = get_queue()
        
        try:
            print(f"[{self.worker_id}] Starting: {title}")
            queue.log_event(self.worker_id, task_id, "started", f"Task: {title}")
            
            # Setup
            self.init_git_repo()
            self.setup_branch(branch_name)
            
            spec = self.read_spec()
            features = self.read_features()
            tree = self.get_workspace_tree()
            system_prompt = self.read_prompt()
            
            user_prompt = f"""Task: {title}

Description: {description}

Project Specification:
{spec}

Current File Tree:
{tree}

All Tasks (FEATURES.json):
{features}

Execute this task. Write complete, working code. No placeholders or TODOs.

Output your response as a JSON object:
{{
  "files": [
    {{"path": "relative/path/to/file.py", "content": "full file content here"}},
    ...
  ],
  "summary": "one sentence of what was done",
  "tokens_estimate": 500
}}

Only create files relevant to your task."""
            
            response = self.call_api(system_prompt, user_prompt)
            result = self.parse_response(response)
            
            files = result.get('files', [])
            written = []
            if files:
                written = self.write_files(files)
                print(f"[{self.worker_id}] Wrote {len(written)} files")
            
            merged = self.commit_and_merge(task_id, title)
            
            if merged:
                print(f"[{self.worker_id}] ✅ Completed: {title}")
                archive_path = self.package_output(task_id, written)
                queue.log_event(self.worker_id, task_id, "done", result.get('summary', ''))
                return {"status": "success", "files": written, "summary": result.get('summary', ''), "archive": archive_path}
            else:
                queue.log_event(self.worker_id, task_id, "conflict", "Merge conflict")
                queue.mark_fix_needed(task_id, "Merge conflict")
                return {"status": "conflict", "error": "Merge conflict"}
            
        except Exception as e:
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            print(f"[{self.worker_id}] ❌ Failed: {title} - {e}")
            queue.log_event(self.worker_id, task_id, "error", str(e)[:200])
            queue.fail_task(task_id, str(e)[:500])
            return {"status": "failed", "error": str(e)}
    
    def run(self):
        """Main worker loop"""
        queue = get_queue()
        
        while True:
            task = queue.claim_task(self.worker_id)
            if not task:
                break
            
            result = self.execute_task(task)
            
            if result.get("status") == "success":
                queue.complete_task(task['task_id'], json.dumps(result))
        
        print(f"[{self.worker_id}] No more tasks, exiting")


if __name__ == "__main__":
    worker_id = sys.argv[1] if len(sys.argv) > 1 else f"worker-{os.getpid()}"
    
    try:
        worker = WorkerAgent(worker_id)
        worker.run()
    except Exception as e:
        # Ensure we don't leave orphan running tasks
        print(f"[{worker_id}] Fatal error: {e}")
        queue = get_queue()
        # Try to reset any running tasks for this worker
        try:
            queue.log_event(worker_id, "", "fatal", str(e)[:200])
        except:
            pass
        sys.exit(1)
