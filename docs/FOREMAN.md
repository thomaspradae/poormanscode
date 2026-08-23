# Foreman and feature backlog

PMC features are reviewed dependency graphs whose nodes are ordinary PMC jobs.
Workers never spawn other workers, and the Foreman never accepts code.

```bash
pmc feature-create /path/to/repo "Playable prototype" "Build the first prototype"
pmc feature-plan PMCF-000001 --candidate groq-oss20-bash
pmc feature-approve PMCF-000001
pmc board PMCF-000001
```

The proposal is stored as `PLANNED` and dispatches nothing until approval. A
human-authored plan can be loaded with `--file plan.json` instead.

```json
{
  "tasks": [
    {
      "id": "bootstrap",
      "request": "Create the project skeleton",
      "acceptance": ["The project opens successfully"],
      "depends_on": [],
      "task_type": "FEATURE",
      "priority": 1
    }
  ]
}
```

`pmc daemon` selects only roots or tasks whose dependencies have been
human-accepted. Multiple daemons may process independent ready tasks concurrently;
job and resource leases prevent duplicate execution. Verification alone does not
unlock dependents: prerequisites must reach `ACCEPTED`.

Automatic cross-worktree integration remains a separate safety milestone. Accepted
commits remain attributable, but PMC does not yet merge a feature DAG automatically.
