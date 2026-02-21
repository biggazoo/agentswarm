#!/usr/bin/env python3
"""
Dashboard - Rich terminal UI for AgentSwarm Lite
"""
import sys
import time
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich import box

import config
from orchestrator import get_queue


console = Console()


class Dashboard:
    """Rich terminal dashboard"""
    
    def __init__(self):
        self.console = console
        self.queue = get_queue()
        
    def get_stats(self) -> dict:
        """Get current stats"""
        return self.queue.get_stats()
    
    def get_recent_tasks(self, limit: int = 10) -> list:
        """Get recent tasks"""
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT task_id, title, status, created_at, completed_at, error
            FROM tasks
            ORDER BY created_at DESC
            LIMIT ?
        ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return rows
    
    def get_recent_logs(self, limit: int = 10) -> list:
        """Get recent agent logs"""
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT worker_id, task_id, event, message, timestamp
            FROM agent_log
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return rows
    
    def render_header(self) -> Panel:
        """Render header"""
        stats = self.get_stats()
        
        text = Text()
        text.append("AgentSwarm Lite ", style="bold cyan")
        text.append(f"• ", style="dim")
        text.append(f"{stats['total']} tasks ", style="white")
        text.append(f"• ", style="dim")
        text.append(f"{stats['pending']} pending ", style="yellow")
        text.append(f"• ", style="dim")
        text.append(f"{stats['running']} running ", style="blue")
        text.append(f"• ", style="dim")
        text.append(f"{stats['done']} done ", style="green")
        text.append(f"• ", style="dim")
        text.append(f"{stats['failed']} failed ", style="red")
        
        return Panel(text, border_style="cyan", padding=(0, 1))
    
    def render_tasks_table(self) -> Table:
        """Render tasks table"""
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
        table.add_column("Task ID", style="cyan", width=12)
        table.add_column("Title", style="white", max_width=35)
        table.add_column("Status", width=10)
        table.add_column("Error", style="red", max_width=25)
        
        tasks = self.get_recent_tasks(15)
        
        for task_id, title, status, created, completed, error in tasks:
            status_style = {
                'pending': 'yellow',
                'running': 'blue',
                'done': 'green',
                'failed': 'red',
                'fix_needed': 'magenta',
            }.get(status, 'white')
            
            error_display = error[:25] + "..." if error and len(error) > 25 else error or ""
            
            table.add_row(
                task_id or "-",
                title[:35] if title else "-",
                f"[{status_style}]{status}[/{status_style}]",
                error_display
            )
        
        return table
    
    def render_logs_table(self) -> Table:
        """Render agent logs"""
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold blue")
        table.add_column("Worker", style="cyan", width=10)
        table.add_column("Task", style="white", width=12)
        table.add_column("Event", width=10)
        table.add_column("Message", style="dim", max_width=40)
        
        logs = self.get_recent_logs(10)
        
        for worker_id, task_id, event, message, timestamp in logs:
            event_style = {
                'started': 'blue',
                'done': 'green',
                'error': 'red',
                'conflict': 'magenta',
            }.get(event, 'white')
            
            table.add_row(
                worker_id or "-",
                task_id[:12] if task_id else "-",
                f"[{event_style}]{event}[/{event_style}]",
                message[:40] if message else ""
            )
        
        return table
    
    def render(self) -> Layout:
        """Render full dashboard"""
        layout = Layout()
        
        layout.split_column(
            Layout(self.render_header(), size=3),
            Layout(self.render_tasks_table()),
            Layout(self.render_logs_table()),
        )
        
        return layout
    
    def run(self, interval: int = 3):
        """Run live dashboard"""
        with Live(self.render(), console=console, refresh_per_second=4) as live:
            try:
                while True:
                    time.sleep(interval)
                    live.update(self.render())
            except KeyboardInterrupt:
                pass


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=3, help="Refresh interval")
    args = parser.parse_args()
    
    dashboard = Dashboard()
    dashboard.run(args.interval)


if __name__ == "__main__":
    main()
