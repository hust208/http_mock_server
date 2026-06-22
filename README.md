# HTTP Mock Server

Python 3.6.8 + Flask | CentOS 7.9

## Quick Start

```bash
pip3 install -r requirements.txt
python3 app.py
```

Admin UI: http://localhost:5000/mock-admin/
Query API: GET /api/requests/{request_id}

## Features

- Multi-method mock (GET/POST/PUT/DELETE/PATCH/OPTIONS/HEAD)
- Domain-based access (change domain only, no code changes)
- Request logging (Header/Query/Body/RequestID to SQLite)
- Query API for automation assertion
- Rule matching (URL+method+params, priority-based)
- Scene management with switching
- Delay/timeout/random exception simulation
- Proxy forwarding for unmatched requests
- Web admin backend (Bootstrap 5)
- Rule import/export (JSON)
