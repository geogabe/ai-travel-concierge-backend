from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import httpx
import os

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

engine = create_engine("sqlite:///conversations.db")
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
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

Base.metadata.create_all(engine)

@app.get("/")
def home():
    return {"message": "Hello from Python"}

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

SYSTEM_PROMPT = """You are a luxury travel concierge. You help discerning travellers plan, book, and re-book trips with precision and warmth.

## Your personality
- Warm but efficient. Never sycophantic. No "Great question!"
- Proactive — anticipate needs before the user asks
- Confident — make clear recommendations, don't hedge
- Honest — if you don't know something, say so directly

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
✓ "I found two strong options. The Air France flight fits your budget and has the better departure time — here's why."
✗ "Great! I'd be happy to help you find flights! Here are 8 options I found!"""

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search"
}

@app.post("/chat")
async def chat(body: dict):
    db = SessionLocal()

    user_msg = Message(role="user", content=body["messages"][-1]["content"])
    db.add(user_msg)
    db.commit()

    headers = {
        "x-api-key": os.environ.get("ANTHROPIC_API_KEY"),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    messages = body["messages"]

    async with httpx.AsyncClient() as client:
        response = await client.post(
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

        data = response.json()

        if data.get("stop_reason") == "tool_use":
            messages = messages + [{"role": "assistant", "content": data["content"]}]

            tool_results = []
            for block in data["content"]:
                if block.get("type") == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": ""
                    })

            messages = messages + [{"role": "user", "content": tool_results}]

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

    reply = next(
        (block["text"] for block in data["content"] if block.get("type") == "text"),
        ""
    )

    assistant_msg = Message(role="assistant", content=reply)
    db.add(assistant_msg)
    db.commit()

    usage = Usage(
        input_tokens=data["usage"]["input_tokens"],
        output_tokens=data["usage"]["output_tokens"],
        cost=(data["usage"]["input_tokens"] + data["usage"]["output_tokens"]) / 1000 * 0.001
    )
    db.add(usage)
    db.commit()
    db.close()

    return {"content": [{"type": "text", "text": reply}], "usage": data["usage"]}