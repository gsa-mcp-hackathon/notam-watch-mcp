# NFS Airspace Watch (Databricks App)

An operational aviation workspace backed by the `mcp-nfs` MCP server. It pairs
a live map and priority NOTAM queue with an analyst assistant that filters the
workspace using the same MCP tools selected for each answer. It is deployed as
the Databricks App `nfs-aviation-agent`.

- `agent.py`: OpenAI Agents SDK agent.
  - Model: a Databricks Foundation Model endpoint through the Responses API.
    gpt-6 models reject function tools on `/chat/completions`.
  - Tools: discovered from `mcp-nfs` over streamable HTTP, authenticated as
    the app's service principal.
  - Tracing: MLflow traces every request.
- `prompts.py`: the system prompt (tool-call budget, tool routing, how to
  answer).
- `dashboard.py`: calls MCP tools and normalizes their evidence into flights,
  NOTAMs, route lines, priority counts, freshness, and partial-result errors.
- `app.py`: FastAPI server.
  - `GET /api/dashboard/briefing`: one MCP tool call for the startup TFR snapshot.
  - `GET /api/dashboard`: cached national TFR and route-deviation watch.
  - `POST /api/dashboard/view`: replays up to three MCP tool calls selected by
    the assistant and returns a question-specific map view.
  - `/api/health`, `/api/chat`, and `/api/eval` (start evaluation with POST,
    then poll with GET).
  - The static analyst workspace in `static/`.
- `evaluation.py`: a 9-question mission-focused evaluation set and its scorers:
  tool-call count and budget, expected tool, tool errors, latency, answer
  quality, and mission quality for risk prioritization and noise reduction.
  - `EVAL_MODE=rest` (default): logs through MLflow REST calls only, so it
    works without access to MLflow cloud storage.
  - `EVAL_MODE=genai`: uses `mlflow.genai.evaluate`; it needs the app to
    reach that storage.

## Map behavior

- Flight markers use live positions returned by the MCP tools.
- NOTAM markers are representative published or computed reference points;
  they are not rendered as complete hazard boundaries.
- MCP filtering still uses the full hazard geometry stored by the server.
- On page load, a single MCP call checks for flights approaching temporary flight
  restrictions in the next 60 minutes. The same snapshot populates the map and a
  startup briefing in chat that identifies the MCP tool used.
- The dashboard does not poll. Use **Refresh** or **Reset national watch** to
  fetch the full national watch, including route deviations.
- All results remain advisory and require verification against authoritative
  FAA sources and operational procedures.

## App resources

Add these on the app (**Edit** > **App resources**). `app.yaml` reads them with
`valueFrom`.

| Resource name | Type | Permission | Used for |
| --- | --- | --- | --- |
| `app` | App `mcp-nfs` | Can use | Calling the MCP server |
| `experiment` | MLflow experiment | Can edit | Traces and evaluation runs (`MLFLOW_EXPERIMENT_ID`) |
| `model` | Serving endpoint, e.g. `databricks-gpt-6-1-sol` | Can query | The agent model (`AGENT_MODEL`) |

Replace the `MCP_SERVER_URL` placeholder in `app.yaml` with your deployed
`mcp-nfs` app's `/mcp` endpoint.

## Deploy

Deploy the repository-root `mcp-nfs` server first so its tool responses include
the representative NOTAM coordinates and route geometry used by the map. Then
deploy this agent app. Ask “Show the filed route for live flight DAL636” using a
currently tracked callsign. When the flight has a mapped plan, the map shows
its filed route as a dashed line and its latest reported position as a separate
marker. If there is no mapped plan, the app does not draw a substitute line.

Deploy from this folder, either from this Git folder in the workspace or with
the app's Git source set to this repository and the source path `agent-app`.

```bash
databricks apps deploy nfs-aviation-agent \
  --source-code-path /Workspace/Users/<user>/mcp-nfs-repo/agent-app
```

Replace `<user>`, including the angle brackets, with your workspace user name.

The `mcp-nfs` server itself is deployed from the repository root (`app.yaml`
there). This folder has its own `app.yaml` and `requirements.txt` and does not
affect it.
