# AI Travel Concierge — Back-end

A Python API built in 5 days as part of a full-stack technical growth sprint.
Powers the AI travel concierge front-end, handling Claude API calls securely
and persisting conversation history.

## Why this exists
Direct browser-to-Claude API calls expose your API key publicly.
This back-end acts as a secure proxy — the front-end never touches the key,
the server handles everything.

## What it does
- Proxies Claude API calls from the React front-end
- Stores every message in SQLite with timestamps
- Tracks token usage and cost per conversation
- Exposes conversation history via REST endpoints

## Stack
- **Python + FastAPI** — REST API with automatic docs at /docs
- **Anthropic Claude API** — claude-haiku-4-5 model
- **SQLite + SQLAlchemy** — lightweight persistent storage
- **httpx** — async HTTP client for Claude API calls

## Endpoints
| Method | Route | Description |
|--------|-------|-------------|
| GET | / | Health check |
| POST | /chat | Send message, get Claude reply |
| GET | /conversations | Full conversation history |
| GET | /usage | Token usage and remaining credit |

## Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Add a `.env` file with :
```
ANTHROPIC_API_KEY=your-key-here
```

## Related
- [Front-end repo](https://github.com/geogabe/ai-travel-concierge)
