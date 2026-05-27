# AI Desktop Agent

An AI-powered desktop automation system that operates a virtual machine's GUI through natural language instructions. Users interact via a web interface, watching the AI control the VM in real-time.

## Overview

```
User (Browser) → Web UI → Backend (FastAPI) → AI Agent → VM (QEMU/VNC)
                              ↑                              |
                              └── Live screen stream (noVNC) ←┘
```

The user gives natural language instructions through a web chat interface. The AI agent captures screenshots from the VM, reasons about what to do using a multimodal LLM, and executes mouse/keyboard actions — all visible in real-time through an embedded noVNC viewer.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Browser (Web UI)                                   │
│  ┌───────────────────┐  ┌────────────────────────┐  │
│  │  Instruction Panel │  │  noVNC (Live VM View)  │  │
│  │  Chat / Log        │  │  Real-time streaming   │  │
│  └───────────────────┘  └────────────────────────┘  │
└──────────────┬──────────────────────┬───────────────┘
               │ WebSocket            │ WebSocket (noVNC)
               ▼                      ▼
┌─────────────────────────────────────────────────────┐
│  Backend Server (Python / FastAPI)                   │
│  ┌──────────────┐  ┌─────────────┐  ┌───────────┐  │
│  │ Chat API     │  │ Agent Loop  │  │ websockify│  │
│  │ (instructions)│  │ (AI control)│  │ (VNC relay)│  │
│  └──────────────┘  └──────┬──────┘  └─────┬─────┘  │
└──────────────────────────┬─────────────────┬────────┘
                           │ VNC Protocol    │
                           ▼                 ▼
┌─────────────────────────────────────────────────────┐
│  QEMU VM (Linux Desktop)                            │
│  VNC Server :5900                                   │
│  (Ubuntu + Xfce / lightweight DE)                   │
└─────────────────────────────────────────────────────┘
```

## Tech Stack

| Component | Technology | Role |
|-----------|-----------|------|
| Virtual Machine | QEMU/KVM | Local VM with VNC display output |
| Screen Streaming | noVNC + websockify | VNC → WebSocket, live view in browser |
| Backend | FastAPI + WebSocket | Instruction handling, agent orchestration |
| AI Agent | Python (vncdotool + LLM API) | Screenshot → reasoning → action execution |
| LLM | Claude / GPT-4o (multimodal) | Visual understanding + action planning |
| Frontend | React (or vanilla JS) | Chat panel + embedded noVNC viewer |

## Agent Loop

```python
async def agent_loop(instruction: str, vnc, llm):
    while not task_complete:
        # 1. Capture screenshot from VM
        screenshot = vnc.capture_screen()

        # 2. Send to multimodal LLM with instruction + history
        action = await llm.decide(instruction, screenshot, action_history)

        # 3. Execute action on VM (click, type, scroll, etc.)
        await vnc.execute_action(action)

        # 4. Notify frontend of progress
        await websocket.broadcast({"status": action.description, "step": step_count})

        # 5. Wait for UI to settle
        await asyncio.sleep(1)
```

## Project Structure

```
ai-desktop-agent/
├── pyproject.toml
├── README.md
├── docs/
│   └── architecture.md
├── src/
│   └── ai_desktop_agent/
│       ├── __init__.py
│       ├── main.py              # Entry point
│       ├── config.py            # Configuration
│       ├── server/
│       │   ├── __init__.py
│       │   ├── app.py           # FastAPI application
│       │   ├── routes.py        # HTTP/WebSocket routes
│       │   └── static/          # Frontend assets
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── loop.py          # Main agent loop
│       │   ├── llm.py           # LLM client (Claude/GPT-4o)
│       │   └── actions.py       # Action types and execution
│       └── vm/
│           ├── __init__.py
│           ├── manager.py       # QEMU VM lifecycle
│           ├── vnc_client.py    # VNC connection and control
│           └── screenshot.py    # Screen capture utilities
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── style.css
└── scripts/
    ├── start_vm.sh
    └── setup_vm_image.sh
```

## Key Design Decisions

### Why QEMU + VNC?
- **Isolation**: AI operates in a sandboxed VM, cannot affect host system
- **VNC protocol**: Well-established, supports both screen capture and input injection
- **noVNC**: Mature browser-based VNC client, zero-install for users
- **Local**: No cloud dependency, full control over the VM environment

### Why noVNC for Live Viewing?
- User watches AI work in real-time without installing any software
- noVNC handles WebSocket ↔ VNC translation via websockify
- Read-only mode available (prevent user interference during AI operation)

### Agent Safety
- VM isolation prevents AI from affecting the host
- Action rate limiting (prevent runaway loops)
- User can stop the agent at any time via the web UI
- All actions are logged and visible in the chat panel

## Getting Started

> 🚧 Under construction

### Prerequisites

- Python 3.12+
- QEMU/KVM
- A VM image (Ubuntu Desktop recommended)
- API key for Claude or GPT-4o

### Installation

```bash
git clone https://github.com/Milix-M/ai-desktop-agent.git
cd ai-desktop-agent
uv sync
```

### Usage

```bash
# Start the VM and web server
uv run python -m ai_desktop_agent

# Open browser to http://localhost:8080
```

## Roadmap

- [ ] Basic QEMU VM management (start/stop)
- [ ] VNC screenshot capture + action execution
- [ ] Agent loop with multimodal LLM
- [ ] FastAPI backend with WebSocket
- [ ] noVNC integration for live viewing
- [ ] Web UI (instruction panel + viewer)
- [ ] Action history and logging
- [ ] Error recovery and retry logic
- [ ] Multiple VM support
- [ ] Task templates (common workflows)

## License

MIT
