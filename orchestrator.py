#!/usr/bin/env python3
"""
Orchestrator - Task queue manager and agent dispatcher
"""
import sqlite3
import uuid
import json
import threading
import time
import fcntl
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import config


class TaskQueue:
    """SQLite-backed task queue with WAL mode"""
    
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _get_conn(self):
        """Get SQLite connection with WAL mode"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    
    def _init_db(self):
        """Initialize database schema"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 5,
                retries INTEGER DEFAULT 0,
                depends_on TEXT DEFAULT '[]',
                assigned_worker TEXT,
                branch_name TEXT,
                result TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id TEXT,
                task_id TEXT,
                event TEXT,
                message TEXT,
                tokens_used INTEGER DEFAULT 0,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS run_meta (
                id INTEGER PRIMARY KEY,
                project_name TEXT,
                spec TEXT,
                total_tasks INTEGER DEFAULT 0,
                completed_tasks INTEGER DEFAULT 0,
                failed_tasks INTEGER DEFAULT 0,
                total_cost_usd REAL DEFAULT 0,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'running'
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def log_event(self, worker_id: str, task_id: str, event: str, message: str = "", tokens: int = 0):
        """Log agent event"""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO agent_log (worker_id, task_id, event, message, tokens_used)
            VALUES (?, ?, ?, ?, ?)
        ''', (worker_id, task_id, event, message, tokens))
        conn.commit()
        conn.close()
        
        # Also log to file
        log_path = Path(config.LOGS_DIR) / f"{task_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {event}: {message}\n")
    
    def add_task(self, title: str, description: str, priority: int = 5, depends_on: list = None) -> str:
        """Add a task to the queue"""
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO tasks (task_id, title, description, priority, depends_on)
            VALUES (?, ?, ?, ?, ?)
        ''', (task_id, title, description, priority, json.dumps(depends_on or [])))
        
        conn.commit()
        conn.close()
        return task_id
    
    def add_tasks_batch(self, tasks: list):
        """Add multiple tasks at once"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        for task in tasks:
            task_id = task.get('task_id') or f"task-{uuid.uuid4().hex[:8]}"
            cursor.execute('''
                INSERT OR IGNORE INTO tasks (task_id, title, description, priority, depends_on)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                task_id,
                task.get('title', ''),
                task.get('description', ''),
                task.get('priority', 5),
                json.dumps(task.get('depends_on', []))
            ))
        
        conn.commit()
        conn.close()
    
    def get_ready_tasks(self) -> list:
        """Get tasks whose dependencies are satisfied"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT task_id, title, description, priority, depends_on
            FROM tasks
            WHERE status = 'pending'
            ORDER BY priority ASC, created_at ASC
        ''')
        
        pending = cursor.fetchall()
        conn.close()
        
        # Filter by dependencies
        ready = []
        for task_id, title, desc, priority, depends_on in pending:
            deps = json.loads(depends_on) if depends_on else []
            if not deps:
                ready.append({
                    'task_id': task_id,
                    'title': title,
                    'description': desc,
                    'priority': priority
                })
                continue
            
            # Check if all dependencies are done
            conn = self._get_conn()
            cursor = conn.cursor()
            placeholders = ','.join(['?' for _ in deps])
            cursor.execute(f'''
                SELECT COUNT(*) FROM tasks
                WHERE task_id IN ({placeholders}) AND status != 'done'
            ''', deps)
            pending_deps = cursor.fetchone()[0]
            conn.close()
            
            if pending_deps == 0:
                ready.append({
                    'task_id': task_id,
                    'title': title,
                    'description': desc,
                    'priority': priority
                })
        
        return ready
    
    def claim_task(self, worker_id: str) -> Optional[dict]:
        """Atomically claim a task"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Get a ready task
        ready_tasks = self.get_ready_tasks()
        if not ready_tasks:
            conn.close()
            return None
        
        task = ready_tasks[0]
        task_id = task['task_id']
        branch_name = f"agent-{task_id[:8]}"
        
        # Atomic claim
        cursor.execute('''
            UPDATE tasks
            SET status = 'running', assigned_worker = ?, branch_name = ?,
                started_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND status = 'pending'
        ''', (worker_id, branch_name, task_id))
        
        if cursor.rowcount == 0:
            conn.close()
            return None
        
        conn.commit()
        conn.close()
        
        task['branch_name'] = branch_name
        return task
    
    def complete_task(self, task_id: str, result: str, status: str = 'done'):
        """Mark task as complete"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE tasks
            SET status = ?, result = ?, completed_at = CURRENT_TIMESTAMP
            WHERE task_id = ?
        ''', (status, result, task_id))
        
        conn.commit()
        conn.close()
    
    def fail_task(self, task_id: str, error: str):
        """Mark task as failed, increment retries"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE tasks
            SET status = 'pending', error = ?, retries = retries + 1,
                started_at = NULL
            WHERE task_id = ?
        ''', (error, task_id))
        
        conn.commit()
        conn.close()
    
    def mark_fix_needed(self, task_id: str, error: str):
        """Mark task as needs fixing"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE tasks
            SET status = 'fix_needed', error = ?, completed_at = CURRENT_TIMESTAMP
            WHERE task_id = ?
        ''', (error, task_id))
        
        conn.commit()
        conn.close()
    
    def get_stats(self) -> dict:
        """Get queue statistics"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT status, COUNT(*) FROM tasks GROUP BY status
        ''')
        status_counts = dict(cursor.fetchall())
        
        cursor.execute('SELECT COUNT(*) FROM tasks')
        total = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'pending': status_counts.get('pending', 0),
            'running': status_counts.get('running', 0),
            'done': status_counts.get('done', 0),
            'failed': status_counts.get('failed', 0),
            'fix_needed': status_counts.get('fix_needed', 0),
            'total': total
        }
    
    def clear(self):
        """Clear all tasks"""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM tasks')
        cursor.execute('DELETE FROM agent_log')
        cursor.execute('DELETE FROM run_meta')
        conn.commit()
        conn.close()


# Singleton instance
_queue: Optional[TaskQueue] = None
_lock = threading.Lock()


def get_queue() -> TaskQueue:
    """Get or create task queue singleton"""
    global _queue
    with _lock:
        if _queue is None:
            _queue = TaskQueue()
        return _queue
