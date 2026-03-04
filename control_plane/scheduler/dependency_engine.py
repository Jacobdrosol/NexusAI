from typing import Dict

from shared.models import Task


class DependencyEngine:
    """Determines whether blocked tasks are ready to run."""

    @staticmethod
    def is_ready(task: Task, tasks_by_id: Dict[str, Task]) -> bool:
        for dep_id in task.depends_on:
            dep = tasks_by_id.get(dep_id)
            if dep is None or dep.status != "completed":
                return False
        return True
