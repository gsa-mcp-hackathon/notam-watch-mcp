# Technical Evaluation Details

[Back to Notam Watch](README.md)

## MLflow Agent Evaluation

The [nine-question evaluation suite](agent-app/evaluation.py) runs through the
real agent and MCP tools. Its cases cover:

- Identifying the most important notices for a planned flight.
- Filtering notices using route and altitude evidence.
- Prioritizing arrival runway and approach impacts.
- Finding flights entering temporary or special-use airspace.
- Separating confirmed altitude conflicts from altitude-unknown cases.
- Identifying route deviations without claiming unsupported causation.
- Combining multiple MCP tools into a prioritized flight briefing.
- Ranking airport outages by operational impact.
- Translating coded NOTAM text without changing its facts.

MLflow records tool selection, tool-call budget, tool errors, response latency,
complete responses, and trace identifiers. Answer and mission quality can be
scored when the LLM judges are enabled and available. Reviewers can inspect
individual answers in the dashboard's **Evaluate** view.

### Recorded Results

| November 20, 2026 evaluation | Result |
| --- | --- |
| Questions completed | 9 of 9 |
| Selected the expected MCP capability | 9 of 9 |
| Stayed within the defined tool-call budget | 9 of 9 |
| Failed evaluation cases | 0 |

These results were saved in MLflow in the local Docker continuity environment.
They measure how the assistant used its tools, not answer correctness or
operational readiness. The recorded run did not include a model-only comparison.

## Automated Checks

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
```

The repository includes tests for tool contracts, query behavior, SQL rendering,
schedule and altitude logic, dashboard normalization, and ingestion behavior.
