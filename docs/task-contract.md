# Task contract

The planner returns `{"tasks": [...]}`. Each task uses these fields:

```yaml
id: task-001
workspace: C:/Projects/example
objective: Implement the described feature.
inputs: [requirements.md]       # optional
acceptance:
  - Feature behaves as specified
  - Automated tests pass
depends_on: []                  # optional task IDs
```

Workspaces are absolute. Task IDs are unique within each objective; dependencies
must exist and form an acyclic graph. Tasks cannot specify provider, model or
permission fields. The objective pins its provider configuration at submission.
The scheduler persists the full graph atomically before execution. Successful
worker output must register validators covering every acceptance index; Python
runs those validators in the task workspace before marking a task complete.

Opt-in [autonomous operations](autonomous-operations.md) use the same Task schema
with one durable `operate` task, additional worker handoffs, deterministic health
monitoring and recovery assessment before external escalation. Legacy objectives
retain the planner DAG and original failure policy.
