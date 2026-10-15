# Role
You are a Principal Software Architect and Systems Engineer with deep expertise in Python, networking, proxy design, and developer tooling.

---

# Project Overview & Tech Stack
- **Project Name:** OmniRoute Smart Proxy (omni-mac)
- **Path:** `/Users/olegvarikov/Desktop/omni-mac`
- **Languages:** Python 3 (standard library only: `http.server`, `urllib`, `threading`, `concurrent.futures`, `json`, `socket`), Bash/Zsh scripts
- **Target OS:** macOS (Darwin), Linux, Windows cross-compatible
- **Purpose:** Local transparent failover proxy and orchestrator connecting Claude Code (`claude`) and OpenClaw CLI to OmniRoute model providers. It performs health-checks on AI models, transparently routes requests, manages failover on rate limits (429) or upstream errors (5xx), and displays live token statistics in terminal Window 1 while developer interacts in Window 2.

---

# Directory Tree & Key Context
```
/Users/olegvarikov/Desktop/omni-mac/
├── omni_claude.py          # Main Python orchestrator & proxy for Claude Code (ModelPoolManager, ProxyRequestHandler)
├── omni_openclaw.py        # Orchestrator & proxy for OpenClaw
├── active_pool.json        # Persisted state: active smart/light models, fallback pools, stats
├── README.md               # User documentation & instructions
├── run_claude.sh           # Auto-generated runner setting ANTHROPIC_BASE_URL & auth tokens
├── start_claude.sh         # Entry launcher for Window 1
├── start_claude.command    # Double-clickable macOS launcher
├── status.sh               # Quick CLI status check
└── switch.sh               # Script for quick model switching
```

### Key Components in `omni_claude.py`:
- `ModelPoolManager`: Manages thread-safe active model selection, smart/light pools, failure counters, and token usage statistics (`total_input_tokens`, `total_output_tokens`, `failovers_count`).
- `ProxyRequestHandler(BaseHTTPRequestHandler)`: Intercepts `/v1/messages` and `/v1/complete`, substitutes models, proxies payload via `urllib.request`, captures streaming SSE tokens, and implements automatic retry/failover if upstream fails.
- Hotkey Listener: Background thread reading console stdin (`c` - change model, `s` - status, `r` - reload, `q` - quit).

---

# Task / Problem Statement
**Objective:** Enhance `omni_claude.py` with an internal Healthcheck & Observability subsystem and File Watcher Hot-Reload:
1. **HTTP Status & Metrics Endpoint:** Expose `GET /_omni/status` (JSON with active models, uptime, token totals, pool health) and `GET /_omni/healthz` (200 OK lightweight ping) directly from the proxy server without interfering with Claude Code API traffic.
2. **Automatic Hot-Reload via File Watcher:** Monitor `active_pool.json` modification timestamp in a background thread so that external updates (e.g. from `switch.sh` or manual edits) immediately update in-memory `ModelPoolManager` without requiring a full proxy restart.
3. **Structured Diagnostics & Graceful Degradation:** Ensure zero-downtime, thread safety, and zero external pip dependencies (pure Python standard library).

---

# Required Output Structure
Please provide your response strictly adhering to the following structure:

### 1. High-Level Architecture & Design Decisions
- Architectural approach and data flow diagram/overview.
- Threading & concurrency model (ensuring lock safety between file watcher, request handlers, and hotkey loop).
- Route routing & conflict avoidance (how `/_omni/*` is separated from `/v1/*` proxy calls).
- Trade-offs analyzed (e.g., polling interval vs event-driven watcher, memory overhead).

### 2. Step-by-Step Action Plan
Present a clear, sequential implementation plan using markdown checkboxes (`- [ ]`).

### 3. Detailed File Specifications
For each step, specify:
- Exact file path to create or modify.
- Function/Class signatures, data structures, and algorithms.
- Edge cases and error handling (corrupted JSON, file lock contention, concurrent requests during reload).

### 4. Verification & Testing Criteria
- Concrete verification commands (e.g., `curl -i http://127.0.0.1:9099/_omni/status`).
- Testing procedure for hot-reload without breaking active streaming connections.
- Acceptance criteria for completion.
