#!/usr/bin/env python3
"""
Planner Agent - Breaks project into tasks using MiniMax API
"""
import json
import re
import sys
from pathlib import Path

import requests

import config
from orchestrator import get_queue


class PlannerAgent:
    """Planner agent that decomposes projects into tasks"""
    
    def __init__(self):
        self.model = config.PLANNER_MODEL
        self.api_key = config.MINIMAX_API_KEY
        self.base_url = config.MINIMAX_BASE_URL
        
    def read_prompt(self) -> str:
        """Load planner system prompt"""
        with open(f"{Path(__file__).parent.parent}/prompts/planner.txt") as f:
            return f.read()
    
    def call_api(self, system: str, user: str) -> str:
        """Call MiniMax API"""
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
            timeout=120
        )
        
        if response.status_code != 200:
            raise Exception(f"API error: {response.status_code} - {response.text}")
        
        data = response.json()
        return data["choices"][0]["message"]["content"]
    
    def plan(self, spec_content: str) -> list:
        """Generate task list from specification"""
        system_prompt = self.read_prompt()
        
        user_prompt = f"""Project Specification:

{spec_content}

Generate a task list that implements this project. Output ONLY a JSON array of tasks, no other text.

Required format:
[
  {{
    "title": "Create project file structure",
    "description": "Create all directories and empty placeholder files as defined in SPEC.md. Do not write any logic yet.",
    "priority": 1,
    "depends_on": []
  }},
  ...
]

Rules:
- Maximum 20 tasks
- Each task completable in under 5 minutes
- Priority 1 = first (structure, config)
- Priority 5 = middle (features)
- Priority 9 = last (testing, integration)"""

        response = self.call_api(system_prompt, user_prompt)
        
        # Extract JSON from response
        try:
            json_match = re.search(r'\[[\s\S]*\]', response)
            if json_match:
                tasks = json.loads(json_match.group())
            else:
                tasks = json.loads(response)
            
            return tasks
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse planner response: {e}\n\nResponse:\n{response[:1000]}")
    
    def run(self, project_name: str, spec_content: str) -> dict:
        """Run planner and populate task queue"""
        print(f"📋 Planning project: {project_name}")
        
        # Write SPEC.md to workspace (locked)
        workspace = Path(config.WORKSPACE_DIR)
        workspace.mkdir(parents=True, exist_ok=True)
        spec_path = workspace / "SPEC.md"
        spec_path.write_text(spec_content)
        print(f"📝 Wrote SPEC.md to workspace")
        
        try:
            tasks = self.plan(spec_content)
            
            # Write FEATURES.json
            features_path = workspace / "FEATURES.json"
            features_path.write_text(json.dumps(tasks, indent=2))
            print(f"📝 Wrote FEATURES.json to workspace")
            
            # Add tasks to queue
            queue = get_queue()
            queue.add_tasks_batch(tasks)
            
            print(f"✅ Generated {len(tasks)} tasks")
            return {
                'status': 'success',
                'task_count': len(tasks),
                'tasks': tasks
            }
            
        except Exception as e:
            print(f"❌ Planning failed: {e}")
            return {
                'status': 'error',
                'error': str(e)
            }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python planner.py <spec_content>")
        sys.exit(1)
    
    spec = sys.argv[1]
    planner = PlannerAgent()
    result = planner.run("project", spec)
    print(json.dumps(result, indent=2))
