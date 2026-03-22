---
name: guardian-status
description: Query Pipeline Guardian monitoring data — pipeline list, trace history, step statistics, quality trends, and drift detection. Use when the user asks about pipeline status, health, monitoring, quality metrics, or drift.
---

# Guardian Status

Query pipeline monitoring data from the trace database.

## Available queries

Determine what the user wants, then run the appropriate query:

### List all pipelines

```bash
cd /Users/lizhuojun/Desktop/APP/traceguard
uv run python -c "
from guardian.store.reader import TraceReader
import json
reader = TraceReader('sqlite:///traces.db')
print(json.dumps(reader.list_pipelines(), indent=2))
"
```

### Step statistics (pass rate, avg score, action breakdown)

```bash
uv run python -c "
from guardian.store.reader import TraceReader
import json
reader = TraceReader('sqlite:///traces.db')
print(json.dumps(reader.get_step_stats('<PIPELINE>', '<STEP>', days=7), indent=2))
"
```

### Recent traces

```bash
uv run python -c "
from guardian.store.reader import TraceReader
import json
reader = TraceReader('sqlite:///traces.db')
print(json.dumps(reader.query_traces(pipeline_name='<PIPELINE>', days=7, limit=20), indent=2))
"
```

### Drift detection

```bash
uv run python -c "
from guardian.optimizer.drift_detector import detect_drift
from guardian.store.reader import TraceReader
import json, dataclasses
reader = TraceReader('sqlite:///traces.db')
report = detect_drift(reader, '<PIPELINE>')
print(json.dumps(dataclasses.asdict(report), indent=2, default=str))
"
```

### LLM environment status

```bash
uv run python -c "
from guardian.env import probe_llm_environment, LLMMode
import asyncio
ep = asyncio.run(probe_llm_environment())
print(f'Mode: {ep.mode.value}, Provider: {ep.provider}, Model: {ep.model}')
"
```

## Present results

- Show pass rates and average scores prominently
- Highlight any drift signals (degrading trend, score drops)
- If failure rate > 30%, suggest running `/guardian-suggest`
- For "is everything OK?" questions, check drift across all pipelines
