from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, func, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import httpx
import os
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://ai-travel-concierge-beryl.vercel.app",
        "https://ai-travel-concierge-dyum7l5e0.vercel.app"
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Engine first
db_url = os.environ.get("DATABASE_URL", "sqlite:///conversations.db").replace("postgres://", "postgresql://", 1)
engine = create_engine(db_url)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

# 2. Models
class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    session_id = Column(String, index=True)
    role = Column(String)
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

class Usage(Base):
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cost = Column(Float)
    created_at = Column(DateTime, default=datetime.now)

# 3. Create tables
Base.metadata.create_all(engine)

# 4. ALTER TABLE — safe, ignores if column exists
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE messages ADD COLUMN session_id VARCHAR"))
        conn.commit()
    except:
        pass

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def home():
    return {"message": "Hello from Python"}

@app.get("/sessions")
def get_sessions():
    db = SessionLocal()
    sessions = db.query(
        Message.session_id,
        func.min(Message.created_at).label("started_at"),
        func.min(Message.content).label("first_message")
    ).filter(
        Message.role == "user"
    ).group_by(Message.session_id).order_by(
        func.min(Message.created_at).desc()
    ).all()
    db.close()
    return [
        {
            "session_id": s.session_id,
            "started_at": str(s.started_at),
            "title": s.first_message[:40] + "..." if len(s.first_message) > 40 else s.first_message
        }
        for s in sessions
    ]

@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    db = SessionLocal()
    messages = db.query(Message).filter(
        Message.session_id == session_id
    ).order_by(Message.created_at).all()
    db.close()
    return [{"role": m.role, "content": m.content} for m in messages]

@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    db = SessionLocal()
    db.query(Message).filter(Message.session_id == session_id).delete()
    db.commit()
    db.close()
    return {"deleted": session_id}

@app.get("/conversations")
def get_conversations():
    db = SessionLocal()
    messages = db.query(Message).order_by(Message.created_at).all()
    db.close()
    return [
        {"id": m.id, "role": m.role, "content": m.content, "created_at": str(m.created_at)}
        for m in messages
    ]

@app.get("/usage")
def get_usage():
    db = SessionLocal()
    records = db.query(Usage).all()
    total_cost = sum(r.cost for r in records)
    remaining = 4.98 - total_cost
    db.close()
    return {
        "total_spent": round(total_cost, 6),
        "remaining": round(remaining, 6),
        "messages_count": len(records)
    }

# ─── Prompts & tools ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an eco-conscious travel advisor. You help discerning travellers plan, book, and re-book trips with precision and warmth.

## Your personality
- Warm but efficient. Never sycophantic. No "Great question!"
- Proactive — anticipate needs before the user asks
- Confident — make clear recommendations, don't hedge
- Honest — if you don't know something, say so directly

## Priorities
- Train over plane wherever the journey is under 6 hours
- Local accommodation (gîtes, family-run hotels) over chains
- Destinations reachable from Angers without flying
- Outdoor, natural landscapes over tourist traps
- Shoulder season travel to avoid overtourism
You always mention the carbon impact of transport options.
You never suggest a flight if a scenic train exists.

## About Geoffroy
- Travels with partner + 3 kids (ages 11, 9 and 7 in 2026)
- Budget: ~€3,000 for a family trip for 2 weeks
- Prefers direct trains or flights, hates layovers or connections with kids
- Based near Angers — closest airports: Nantes, Paris CDG
- Loved: Thailand in 2026, Marrakech 2022, lived in London for 11 years from 2010 until 2021
- Avoid: beach-only resorts, all-inclusive hotels
- Constraint: school holidays (French calendar, zone B).
- Always flag overtourism risks for peak August destinations
- Cinque Terre, Santorini, Dubrovnik, Mykonos = flag as overcrowded in July/August

## How you work
- Always confirm key details before booking anything irreversible
- When presenting options, show maximum 3. Never overwhelm.
- Structure complex information visually — use bullet points, clear headers
- Remember everything the user has told you in this conversation

## What you always do
- Lead with the recommendation, follow with alternatives
- Include price, duration, and one key insight per option
- Ask one clarifying question at a time, never multiple at once
- End every response with a clear next action

## What you never do
- Never book, pay, or confirm anything without explicit user approval
- Never recommend options outside the user's stated budget
- Never use filler phrases like "Certainly!", "Of course!", "Absolutely!"
- Never give more than 3 options — curate, don't dump

## Tone examples
✓ I found two strong options. The Air France flight fits your budget and has the better departure time — here's why.
✗ Great! I'd be happy to help you find flights! Here are 8 options I found!"""

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search"
}

# ─── Chat endpoint ─────────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(body: dict):
    db = SessionLocal()
    session_id = body.get("session_id", "default")

    # Save user message
    user_msg = Message(
        session_id=session_id,
        role="user",
        content=body["messages"][-1]["content"]
    )
    db.add(user_msg)
    db.commit()

    headers = {
        "x-api-key": os.environ.get("ANTHROPIC_API_KEY"),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    messages = body["messages"]
    used_web_search = False

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:

            # ── First API call ──────────────────────────────────────────────
            response1 = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "system": SYSTEM_PROMPT,
                    "tools": [WEB_SEARCH_TOOL],
                    "messages": messages
                }
            )
            data = response1.json()

            # ── Tool use loop — handles real search results ─────────────────
            # THE FIX: we loop (not just if) and pass REAL content back
            loop_count = 0
            while data.get("stop_reason") == "tool_use" and loop_count < 3:
                loop_count += 1
                used_web_search = True

                # Append Claude's tool_use turn to the message history
                messages = messages + [{"role": "assistant", "content": data["content"]}]

                # Build tool_result blocks — pass the REAL search results back
                tool_results = []
                for block in data["content"]:
                    if block.get("type") == "tool_use":
                        # The actual search results are nested inside block["content"]
                        # We pass them through as-is so Claude can read them
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            # Already structured — pass through
                            result_text = json.dumps(result_content)
                        elif isinstance(result_content, str):
                            result_text = result_content
                        else:
                            result_text = str(result_content)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": result_text
                        })

                messages = messages + [{"role": "user", "content": tool_results}]

                # ── Second API call with real search results ────────────────
                response2 = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 1024,
                        "system": SYSTEM_PROMPT,
                        "tools": [WEB_SEARCH_TOOL],
                        "messages": messages
                    }
                )
                data = response2.json()

            # ── Extract final text reply ────────────────────────────────────
            reply = next(
                (block["text"] for block in data.get("content", []) if block.get("type") == "text"),
                None
            )

            # If we still got nothing, give a graceful fallback
            if not reply:
                reply = "I wasn't able to complete that search. Could you rephrase your question and I'll try again?"

    except httpx.TimeoutException:
        reply = "The search timed out — Railway took too long to respond. Try again in a moment."
        data = {"usage": {"input_tokens": 0, "output_tokens": 0}}

    except Exception as e:
        reply = f"Something went wrong on my end. Error: {str(e)[:100]}"
        data = {"usage": {"input_tokens": 0, "output_tokens": 0}}

    # ── Save assistant reply ────────────────────────────────────────────────
    assistant_msg = Message(
        session_id=session_id,
        role="assistant",
        content=reply
    )
    db.add(assistant_msg)
    db.commit()

    # ── Save usage ──────────────────────────────────────────────────────────
    usage_data = data.get("usage", {})
    if usage_data.get("input_tokens"):
        usage = Usage(
            input_tokens=usage_data["input_tokens"],
            output_tokens=usage_data["output_tokens"],
            cost=(usage_data["input_tokens"] + usage_data["output_tokens"]) / 1000 * 0.001
        )
        db.add(usage)
        db.commit()

    db.close()

    return {
        "content": [{"type": "text", "text": reply}],
        "usage": usage_data,
        "used_web_search": used_web_search  # 👈 front-end can use this to show a badge
    }
