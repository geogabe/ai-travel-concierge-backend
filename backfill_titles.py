"""
One-off script — generates AI titles for all sessions that don't have one yet.
Run from the mon-backend folder with venv activated:
    python3 backfill_titles.py
"""

import os, time, json
import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

db_url = os.environ.get("DATABASE_URL", "sqlite:///conversations.db").replace("postgres://", "postgresql://", 1)
engine = create_engine(db_url)

def generate_title_sync(first_message: str) -> str:
    headers = {
        "x-api-key": os.environ.get("ANTHROPIC_API_KEY"),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 30,
        "system": (
            "You generate short, descriptive titles for travel planning conversations. "
            "Reply with only the title — no quotes, no punctuation at the end, no explanation. "
            "Maximum 5 words. Examples: 'Weekend à Porto en famille', 'Train Paris–Berlin juillet', "
            "'Vacances vélo Bretagne'."
        ),
        "messages": [{"role": "user", "content": first_message}]
    }
    with httpx.Client(timeout=10) as client:
        r = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        data = r.json()
        return data["content"][0]["text"].strip()


def backfill():
    with engine.connect() as conn:
        # Get first user message per session
        all_sessions = conn.execute(text("""
            SELECT session_id, MIN(content) as first_message
            FROM messages
            WHERE role = 'user'
            GROUP BY session_id
        """)).fetchall()

        # Get sessions that already have a title
        titled = {row[0] for row in conn.execute(text(
            "SELECT session_id FROM sessions WHERE title IS NOT NULL"
        )).fetchall()}

        to_process = [s for s in all_sessions if s[0] not in titled]
        print(f"Found {len(all_sessions)} sessions total, {len(to_process)} need titles.\n")

        for i, s in enumerate(to_process):
            session_id, first_message = s[0], s[1]
            try:
                title = generate_title_sync(first_message[:200])

                # Check if row exists
                existing = conn.execute(text(
                    "SELECT session_id FROM sessions WHERE session_id = :sid"
                ), {"sid": session_id}).fetchone()

                if existing:
                    conn.execute(text(
                        "UPDATE sessions SET title = :title WHERE session_id = :sid"
                    ), {"title": title, "sid": session_id})
                else:
                    conn.execute(text(
                        "INSERT INTO sessions (session_id, title) VALUES (:sid, :title)"
                    ), {"sid": session_id, "title": title})

                conn.commit()
                print(f"[{i+1}/{len(to_process)}] {session_id[:8]}… → '{title}'")

                if i < len(to_process) - 1:
                    time.sleep(2)

            except Exception as e:
                conn.rollback()
                print(f"[{i+1}/{len(to_process)}] {session_id[:8] if session_id else '?'}… FAILED: {e}")

    print("\nDone. Reload the app to see updated titles.")


if __name__ == "__main__":
    backfill()
