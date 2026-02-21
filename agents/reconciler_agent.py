#!/usr/bin/env python3
"""
Reconciler Agent - Background thread that checks build health
"""
import json
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional

import requests

import config
from orchestrator import get_queue


class ReconcilerAgent:
    """Reconciler that runs in background thread"""
    
    def __init__(self):
        self.model = config.RECONCILER_MODEL
        self.apiToken = config.MINIMAX_API_KEY
        self.base_url = config.MINIMAX_BASE_URL
        self.workspace = Path(config.WORKSPACE_DIR)
        self.running = False
        
    def read_prompt(self) -> str:
        """Load reconciler system prompt"""
        with open(f"{Path(__file__).parent.parent}/prompts/reconciler.txt") as f:
            return f.read()
    
    def call_api(self, system: str, user: str) -> str:
        """Call MiniMax API"""
        headers = {
            "Authorization": f"{'Bearer'} {self.apiToken}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "max_tokens": 2000,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        }
        
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )
        
        if response.status_code != 200:
            raise Exception(f"API error: {response.status_code}")
        
        data = response.json()
        return data["choices"][0]["message"]["content"]
    
    def check_syntax(self) -> dict:
        """Check Python syntax across workspace"""
        try:
            result = subprocess.run(
                ["python3", "-m", "compileall", str(self.workspace), "-q"],
                capture_output=True,
                text=True,
                timeout=60
            )
            return {
                "ok": result.returncode == 0,
                "output": result.stderr
            }
        except Exception as e:
            return {"ok": False, "output": str(e)}
    
    def check_stalled_workers(self, queue) -> list:
        """Find tasks running too long"""
        import sqlite3
        from datetime import datetime, timedelta
        
        stalled = []
        timeout_secs = config.WORKER_TIMEOUT_SECONDS
        
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT task_id, title, started_at, assigned_worker
            FROM tasks
            WHERE status = 'running'
        ''')
        
        running = cursor.fetchall()
        conn.close()
        
        for task_id, title, started_at, worker_id in running:
            if started_at:
                try:
                    started = datetime.fromisoformat(started_at)
                    elapsed = (datetime.now() - started).total_seconds()
                    if elapsed > timeout_secs:
                        stalled.append({
                            'task_id': task_id,
                            'title': title,
                            'worker_id': worker_id,
                            'elapsed': elapsed
                        })
                except:
                    pass
        
        return stalled
    
    def handle_fix_needed(self, queue) -> int:
        """Handle tasks marked as fix_needed"""
        import sqlite3
        
        fixed = 0
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT task_id, title, error
            FROM tasks
            WHERE status = 'fix_needed'
        ''')
        
        fix_tasks = cursor.fetchall()
        conn.close()
        
        for task_id, title, error in fix_tasks:
            # Create a fix task
            fix_title = f"Fix: {title}"
            fix_desc = f"Fix the issue: {error}"
            
            new_task_id = queue.add_task(
                title=fix_title,
                description=fix_desc,
                priority=1
            )
            
            # Mark original as done (replaced by fix)
            queue.complete_task(task_id, f"Replaced by {new_task_id}", "replaced")
            
            queue.log_event("reconciler", task_id, "fix_created", f"Created {new_task_id} to fix")
            fixed += 1
        
        return fixed
    
    def analyze_and_fix(self, error_msg: str) -> Optional[dict]:
        """Use MiniMax to analyze error and generate fix"""
        system_prompt = self.read_prompt()
        
        user_prompt = f"""Error to fix:

{error_msg}

Analyze this error and generate a fix task. Output ONLY JSON:

{{
  "fix_task": {{
    "title": "Short fix title",
    "description": "Specific instructions to fix the error"
  }}
}}

Or if no fix needed:
{{"status": "healthy"}}"""

        try:
            response = self.call_api(system_prompt, user_prompt)
            json_match = response.find('{')
            if json_match != -1:
                return json.loads(response[json_match:])
        except:
            pass
        
        return None
    
    def run_once(self) -> dict:
        """Run one reconciliation check"""
        queue = get_queue()
        results = {
            "syntax_check": None,
            "stalled": [],
            "fix_needed": 0,
            "fixes_created": 0
        }
        
        # Check syntax
        print("🔍 Checking syntax...")
        syntax = self.check_syntax()
        results["syntax_check"] = syntax
        
        if not syntax["ok"]:
            print(f"❌ Syntax errors: {syntax['output'][:200]}")
            # Create fix task
            queue.add_task(
                title="Fix Python syntax errors",
                description=f"Fix syntax errors: {syntax['output'][:500]}",
                priority=1
            )
            queue.log_event("reconciler", "syntax", "error", syntax['output'][:200])
        else:
            print("✅ Syntax OK")
        
        # Check stalled workers
        stalled = self.check_stalled_workers(queue)
        results["stalled"] = stalled
        
        for s in stalled:
            print(f"⚠️ Stalled worker: {s['worker_id']} on {s['title']} ({s['elapsed']:.0f}s)")
            queue.log_event("reconciler", s['task_id'], "stalled", f"Worker {s['worker_id']} stalled")
            # Reset task to pending
            queue.fail_task(s['task_id'], f"Stalled after {s['elapsed']:.0f}s")
        
        # Handle fix_needed tasks
        fixes = self.handle_fix_needed(queue)
        results["fix_needed"] = fixes
        results["fixes_created"] = fixes
        
        if fixes > 0:
            print(f"✅ Created {fixes} fix tasks")
        
        return results
    
    def background_loop(self, interval: int = 120):
        """Run reconciler in background thread"""
        self.running = True
        print(f"🔄 Reconciler starting (interval: {interval}s)")
        
        while self.running:
            try:
                self.run_once()
            except Exception as e:
                print(f"❌ Reconciler error: {e}")
            
            time.sleep(interval)
    
    def start_background(self, interval: int = 120):
        """Start reconciler in background thread"""
        thread = threading.Thread(target=self.background_loop, args=(interval,))
        thread.daemon = True
        thread.start()
        return thread
    
    def stop(self):
        """Stop reconciler"""
        self.running = False


# Singleton
_reconciler = None

def get_reconciler() -> ReconcilerAgent:
    global _reconciler
    if _reconciler is None:
        _reconciler = ReconcilerAgent()
    return _reconciler
