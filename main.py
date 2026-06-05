from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, func, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import httpx
import os
import json
import math

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

# ─── Database setup ────────────────────────────────────────────────────────────

db_url = os.environ.get("DATABASE_URL", "sqlite:///conversations.db").replace("postgres://", "postgresql://", 1)
engine = create_engine(db_url)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    session_id = Column(String, index=True)
    role = Column(String)
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    cards = Column(Text, nullable=True)

class Usage(Base):
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cost = Column(Float)
    created_at = Column(DateTime, default=datetime.now)

class Memory(Base):
    __tablename__ = "memory"
    id = Column(Integer, primary_key=True)
    category = Column(String, index=True)
    key = Column(String)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.now)

Base.metadata.create_all(engine)

with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE messages ADD COLUMN session_id VARCHAR"))
        conn.commit()
    except:
        pass
    try:
        conn.execute(text("ALTER TABLE messages ADD COLUMN cards TEXT"))
        conn.commit()
    except:
        pass
    try:
        conn.execute(text("CREATE TABLE IF NOT EXISTS memory (id SERIAL PRIMARY KEY, category VARCHAR, key VARCHAR, value TEXT, updated_at TIMESTAMP)"))
        conn.commit()
    except:
        pass
# ─── Tool definitions (the schema Claude sees) ─────────────────────────────────
#
# These tell Claude what tools exist, what they do, and what parameters to pass.
# Think of each one as a function signature that Claude reads before deciding
# whether to call it.

TOOLS = [
    {
        # Web search — stays from before, for destination research & hotel hunting
        "type": "web_search_20250305",
        "name": "web_search"
    },
    {
        "name": "search_trains",
        "description": (
            "Search for train options between two cities. Returns realistic SNCF pricing, "
            "journey duration, and number of connections. Always call this when the user asks "
            "about getting somewhere by train, or when comparing transport options. "
            "Origin is always Angers unless the user specifies otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Departure city, e.g. 'Angers'"
                },
                "destination": {
                    "type": "string",
                    "description": "Arrival city, e.g. 'Bordeaux'"
                },
                "passengers": {
                    "type": "integer",
                    "description": "Number of passengers (default 5 for the full family)"
                }
            },
            "required": ["origin", "destination"]
        }
    },
    {
        "name": "search_driving",
        "description": (
            "Calculate the driving option for a trip using the family's Volkswagen Touran "
            "2.0L TDI (2017). Returns estimated distance, driving time, fuel cost, toll cost, "
            "and total cost. Use this whenever the user asks about driving, or when comparing "
            "all transport options side by side."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Departure city, e.g. 'Angers'"
                },
                "destination": {
                    "type": "string",
                    "description": "Destination city, e.g. 'Barcelone'"
                }
            },
            "required": ["origin", "destination"]
        }
    },
    {
        "name": "calculate_carbon",
        "description": (
            "Calculate and compare CO₂ emissions for train, car (Touran TDI), and plane "
            "for a given journey. Always call this when presenting transport options — "
            "showing carbon impact is non-negotiable for this concierge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Departure city"
                },
                "destination": {
                    "type": "string",
                    "description": "Destination city"
                },
                "passengers": {
                    "type": "integer",
                    "description": "Number of passengers (affects car total, not per-person)"
                }
            },
            "required": ["origin", "destination"]
        }
    },
    {
        "name": "read_memories",
        "description": (
            "Read all conversations before actually answering to the user so you always start with a full contex of what the user has already written before, where it went, their preferences, budgets, the trips they've been to, etc..."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "write_memories",
        "description": (
            "create new memories when there are new entries that were not already in your written memories, like new places, new destination, new trips, new preferences, updated kids ages, etc..."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "destination, trip, accomodation type, transportation, family updates, budget, etc..."
                },
                "key": {
                    "type": "string",
                    "description": "city, country, car brand, kid's age, etc... "
                },
                "value": {
                    "type": "string",
                    "description": "Rome, Italy, Kia, 9, etc..."
                }
            },
            "required": ["category", "key", "value"]
        }
    }
]

# ─── Tool data: city distances from Angers ─────────────────────────────────────
#
# Real approximate road distances in km from Angers.
# When a destination isn't in this table, we fall back to a straight-line
# estimate using lat/lon (see haversine() below).
# Toll costs are rough but realistic for French autoroutes.

CITY_DATA = {
    # city_name_lowercase: (road_km, toll_euros_one_way, train_duration_min, train_price_pp, direct_train)
    "paris":        (295,  18,  95,  35, True),
    "bordeaux":     (370,  28, 115,  42, True),
    "lyon":         (530,  38, 180,  55, True),
    "marseille":    (820,  58, 230,  75, True),
    "nantes":       (85,    4,  25,  12, True),
    "rennes":       (130,   6,  75,  22, True),
    "toulouse":     (620,  48, 210,  68, True),
    "nice":         (1020, 72, 310,  95, False),
    "strasbourg":   (740,  52, 260,  82, False),
    "lille":        (530,  38, 190,  58, False),
    "brest":        (310,  10, 170,  45, False),
    "la rochelle":  (180,  10,  75,  28, True),
    "biarritz":     (520,  40, 195,  65, True),
    "bayonne":      (510,  39, 190,  63, True),
    "san sebastian":(560,  42, 215,  70, False),
    "barcelone":    (1050, 68, 420, 110, False),
    "barcelona":    (1050, 68, 420, 110, False),
    "madrid":       (1400, 85, 600, 140, False),
    "amsterdam":    (820,  52, 360,  95, False),
    "bruxelles":    (590,  38, 270,  75, False),
    "brussels":     (590,  38, 270,  75, False),
    "londres":      (640,  55, 210,  89, False),
    "london":       (640,  55, 210,  89, False),
    "rome":         (1820, 95, 720, 180, False),
    "florence":     (1560, 85, 620, 160, False),
    "venise":       (1470, 82, 600, 155, False),
    "venice":       (1470, 82, 600, 155, False),
    "lisbonne":     (1530, 72, 720, 170, False),
    "lisbon":       (1530, 72, 720, 170, False),
    "seville":      (1480, 82, 660, 165, False),
    "le mans":      (100,   5,  40,  18, True),
    "tours":        (100,   5,  45,  20, True),
    "poitiers":     (180,  12,  65,  25, True),
    "saint-malo":   (205,   8, 120,  32, False),
    "mont saint-michel": (195, 8, 115, 30, False),
    "dordogne":     (330,  22, 180,  52, False),
    "périgueux":    (340,  24, 190,  54, False),
    "béziers":      (690,  50, 240,  78, True),
    "montpellier":  (730,  52, 250,  80, True),
    "avignon":      (760,  54, 260,  82, True),
    "aix-en-provence": (820, 58, 280, 85, True),
    "carcassonne":  (660,  48, 235,  72, True),
    "perpignan":    (820,  58, 290,  88, True),
    "grenoble":     (680,  48, 260,  82, False),
    "annecy":       (720,  52, 290,  88, False),
    "chamonix":     (790,  56, 330,  95, False),
    "dijon":        (530,  36, 200,  60, True),
    "besançon":     (600,  42, 230,  70, False),
    "colmar":       (720,  50, 270,  82, False),
}

# ─── Haversine: straight-line distance between two lat/lon points ──────────────
#
# Used as a fallback when a city isn't in CITY_DATA.
# We multiply by 1.3 to convert straight-line to realistic road distance.

CITY_COORDS = {
    "paris": (48.85, 2.35), "bordeaux": (44.84, -0.58), "lyon": (45.75, 4.85),
    "marseille": (43.30, 5.37), "nantes": (47.22, -1.55), "angers": (47.47, -0.55),
    "barcelone": (41.39, 2.15), "barcelona": (41.39, 2.15), "madrid": (40.42, -3.70),
    "amsterdam": (52.37, 4.90), "rome": (41.90, 12.50), "london": (51.51, -0.13),
    "lisbonne": (38.72, -9.14), "lisbon": (38.72, -9.14),
}

def haversine_km(city1: str, city2: str) -> float:
    """Straight-line distance in km between two cities, multiplied by 1.3 for road."""
    c1 = CITY_COORDS.get(city1.lower(), (47.47, -0.55))  # default: Angers
    c2 = CITY_COORDS.get(city2.lower(), (48.85, 2.35))   # default: Paris
    R = 6371
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a)) * 1.3

# ─── Volkswagen Touran constants ───────────────────────────────────────────────
#
# 2.0 TDI 150cv 2017, loaded with family + luggage.
# 6.8L/100km is realistic for motorway with 5 people.
# Diesel CO2: 2.65 kg per litre burned.

TOURAN_L_PER_100KM = 6.8
DIESEL_PRICE_EUR = 1.99          # approximate pump price in France, 2026
DIESEL_CO2_KG_PER_L = 2.65
TOURAN_CO2_KG_PER_KM = (TOURAN_L_PER_100KM / 100) * DIESEL_CO2_KG_PER_L  # ≈ 0.180 kg/km

# CO2 reference values (kg per passenger per km)
TRAIN_CO2_KG_PER_PKM = 0.004    # French TGV — almost entirely nuclear electricity
PLANE_CO2_KG_PER_PKM = 0.255    # Economy, including radiative forcing factor

# ─── Tool execution functions ──────────────────────────────────────────────────
#
# These are the actual Python functions that run when Claude calls a tool.
# They return a plain string — Claude reads this string and uses it to answer.

def execute_search_trains(origin: str, destination: str, passengers: int = 5) -> str:
    key = destination.lower().strip()
    origin_key = origin.lower().strip()

    if key in CITY_DATA:
        road_km, toll, duration_min, price_pp, direct = CITY_DATA[key]
    else:
        # Fallback: estimate from haversine
        road_km = haversine_km(origin_key, key)
        duration_min = int(road_km * 0.35)   # rough: ~170 km/h avg TGV
        price_pp = max(15, int(road_km * 0.08))
        direct = road_km < 400
        toll = 0

    hours = duration_min // 60
    minutes = duration_min % 60
    duration_str = f"{hours}h{minutes:02d}" if hours else f"{minutes}min"
    total = price_pp * passengers
    connection = "direct" if direct else "1 connection (Paris or Tours)"

    return json.dumps({
        "tool": "search_trains",
        "origin": origin,
        "destination": destination,
        "passengers": passengers,
        "duration": duration_str,
        "price_per_person_eur": price_pp,
        "total_family_eur": total,
        "connection": connection,
        "note": "Prices are indicative SNCF Ouigo/TGV estimates. Book on oui.sncf for live fares.",
        "source": "SNCF mock data — real API coming in Phase 3"
    }, ensure_ascii=False)


def execute_search_driving(origin: str, destination: str) -> str:
    key = destination.lower().strip()
    origin_key = origin.lower().strip()

    if key in CITY_DATA:
        road_km, toll_eur, _, _, _ = CITY_DATA[key]
    else:
        road_km = haversine_km(origin_key, key)
        # Rough toll estimate: French autoroutes ~0.08 €/km
        toll_eur = int(road_km * 0.08)

    # Fuel calculation
    litres = (road_km / 100) * TOURAN_L_PER_100KM
    fuel_cost = round(litres * DIESEL_PRICE_EUR, 2)
    total_cost = round(fuel_cost + toll_eur, 2)

    # Driving time: 110 km/h average on French motorways, +30min for stops
    drive_minutes = int((road_km / 110) * 60) + 30
    drive_hours = drive_minutes // 60
    drive_mins = drive_minutes % 60
    duration_str = f"{drive_hours}h{drive_mins:02d}"

    co2_total = round(road_km * TOURAN_CO2_KG_PER_KM, 1)

    return json.dumps({
        "tool": "search_driving",
        "vehicle": "VW Touran 2.0 TDI 2017",
        "origin": origin,
        "destination": destination,
        "distance_km": road_km,
        "driving_time": duration_str,
        "fuel_litres": round(litres, 1),
        "fuel_cost_eur": fuel_cost,
        "toll_cost_eur": toll_eur,
        "total_cost_eur": total_cost,
        "co2_kg_total": co2_total,
        "note": "Tolls are estimates. Check sanef.com or ViaMichelin for exact amounts.",
    }, ensure_ascii=False)


def execute_calculate_carbon(origin: str, destination: str, passengers: int = 5) -> str:
    key = destination.lower().strip()
    origin_key = origin.lower().strip()

    if key in CITY_DATA:
        road_km = CITY_DATA[key][0]
    else:
        road_km = haversine_km(origin_key, key)

    # Straight-line distance for plane (shorter than road)
    straight_km = road_km / 1.3

    train_co2_pp = round(straight_km * TRAIN_CO2_KG_PER_PKM, 2)
    train_co2_total = round(train_co2_pp * passengers, 2)

    car_co2_total = round(road_km * TOURAN_CO2_KG_PER_KM, 1)
    car_co2_pp = round(car_co2_total / passengers, 2)

    plane_co2_pp = round(straight_km * PLANE_CO2_KG_PER_PKM, 1)
    plane_co2_total = round(plane_co2_pp * passengers, 1)

    # How many times worse than train?
    car_vs_train = round(car_co2_total / max(train_co2_total, 0.1), 1)
    plane_vs_train = round(plane_co2_total / max(train_co2_total, 0.1), 1)

    return json.dumps({
        "tool": "calculate_carbon",
        "origin": origin,
        "destination": destination,
        "distance_km_road": road_km,
        "passengers": passengers,
        "train": {
            "co2_kg_per_person": train_co2_pp,
            "co2_kg_total": train_co2_total,
            "note": "French TGV runs on ~95% nuclear electricity"
        },
        "car_touran_tdi": {
            "co2_kg_per_person": car_co2_pp,
            "co2_kg_total": car_co2_total,
            "times_worse_than_train": car_vs_train
        },
        "plane": {
            "co2_kg_per_person": plane_co2_pp,
            "co2_kg_total": plane_co2_total,
            "times_worse_than_train": plane_vs_train,
            "note": "Includes radiative forcing factor ×2"
        },
        "verdict": f"Train is {plane_vs_train}× cleaner than flying and {car_vs_train}× cleaner than driving for this trip."
    }, ensure_ascii=False)


# ─── Tool dispatcher ───────────────────────────────────────────────────────────
#
# Claude returns a tool_use block with a name and input dict.
# This function routes it to the right Python function above.
# web_search is handled by Anthropic server-side — we return "" for those.

def execute_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "search_trains":
        return execute_search_trains(
            origin=tool_input.get("origin", "Angers"),
            destination=tool_input.get("destination", ""),
            passengers=tool_input.get("passengers", 5)
        )
    elif tool_name == "search_driving":
        return execute_search_driving(
            origin=tool_input.get("origin", "Angers"),
            destination=tool_input.get("destination", "")
        )
    elif tool_name == "calculate_carbon":
        return execute_calculate_carbon(
            origin=tool_input.get("origin", "Angers"),
            destination=tool_input.get("destination", ""),
            passengers=tool_input.get("passengers", 5)
        )
    elif tool_name == "web_search":
        return ""  # Anthropic runs this server-side on the next call
                           
    elif tool_name == "read_memories":
        return execute_read_memories()

    elif tool_name == "write_memory":
        return execute_write_memory(
            category=tool_input.get("category", ""),
            key=tool_input.get("key", ""),
            value=tool_input.get("value", "")
        )
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"}
        )
   
    


# ─── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an eco-conscious travel advisor for a French family. You help plan trips that are restorative, low-impact, and beautiful — without compromising on experience.

## How you think before answering (agentic planning)
Before composing any response about getting somewhere or planning a trip:
1. Always call read_memories first before anything else — every single conversation, no exceptions
2. Decide what information you actually need (prices? carbon? driving time?)
3. Call the relevant tools to get real data — never invent prices or journey times
4. Read the results carefully
5. If the response involves transport options, output a <cards> JSON block before your narrative text, 
   following the transport card schema provided by the system.
6. Then write your response, grounded in what the tools returned
7. Call write_memories if you think that you learned something new in the full context of your user.
    and tell them explicitely with, for example "I'll remember that you prefer gîtes"

You have four tools available:
- web_search — for destination research, local accommodation, activities, recent travel tips
- search_trains — for SNCF train options and pricing from Angers
- search_driving — for driving time, fuel cost, and toll cost in the family Touran
- calculate_carbon — for CO₂ comparison across all transport modes

When a user asks about getting somewhere, always call search_trains AND search_driving AND calculate_carbon before answering. Never present transport options without carbon data.

## Your personality
- Warm but efficient. Never sycophantic. No "Great question!" or "Absolutely!"
- Proactive — anticipate what the family will need to know
- Confident — make a clear recommendation, then show alternatives
- Honest — if data is estimated or mocked, say so briefly

## About this family
- Geoffroy, partner, and 3 kids (ages 11, 9, and 7 in 2026)
- Budget: ~€3,000 for a 2-week family trip
- Based in Angers — closest train hub is Angers Saint-Laud
- Hates connections and layovers with kids — direct routes strongly preferred
- Loved: Thailand 2026, Marrakech 2022, lived in London 2010–2021
- Avoid: beach-only resorts, all-inclusive hotels, overtouristed spots in peak season
- School holidays: French calendar, zone B
- Car: Volkswagen Touran 2.0L TDI 2017 (seats 7, diesel)

## Travel philosophy
- Train over plane whenever journey is under 6–7 hours
- Local accommodation: gîtes, chambres d'hôtes, family-run hotels, homexchange
- Shoulder season travel to avoid overtourism and high prices
- Nature and landscape over tourist attractions
- Always show CO₂ impact — it's non-negotiable, but make it understandable showing comparison with real life examples.

## How you respond
- Lead with your recommendation, follow with alternatives
- Maximum 3 options — curate, never dump
- For each transport option: duration, total family cost, CO₂ total
- Ask one clarifying question at a time, never multiple at once
- End every response with a clear next action

## What you never do
- Never invent train prices or driving distances — use the tools
- Never recommend a flight if a scenic train exists under 6h
- Never give more than 3 options
- Never book or confirm anything without explicit user approval
- Flag Cinque Terre, Santorini, Dubrovnik, Mykonos as overcrowded in July/August
- Never claim to search booking platforms (Homexchange, Airbnb, Booking.com) — 
  you cannot access listings that require login. Instead, give the direct URL 
  and tell the user exactly what to search for.
- Never say the tools didn't return data if they did — trust the tool output and present it directly

## Output schemas (technical — do not mention to user)
When transport tools have fired, prepend your response with:
<cards>{"type":"transport","origin":"...","destination":"...","passengers":5,"options":[
  {"id":"train","mode":"train","icon":"🚆","label":"Train TGV","sublabel":"...","badge":"recommended","badgeLabel":"Recommandé","duration":"3h00","cost":275,"co2":8,"co2Max":200},
  {"id":"car","mode":"car","icon":"🚗","label":"Voiture","sublabel":"Touran TDI","badge":"economic","badgeLabel":"Économique","duration":"5h19","cost":110,"co2":96,"co2Max":200}
]}</cards>

Rules:
- badge: "recommended" = best overall, "economic" = cheapest, null = neither
- co2Max: set this to the highest co2 value among all options in this response
- Only emit <cards> for transport comparisons
"""

# ─── Routes (unchanged from before) ───────────────────────────────────────────


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
    return [
        {
            "role": m.role,
            "content": m.content,
            "cards": json.loads(m.cards) if m.cards else None
        }
        for m in messages
    ]

@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    db = SessionLocal()
    db.query(Message).filter(Message.session_id == session_id).delete()
    db.commit()
    db.close()
    return {"deleted": session_id}

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

def execute_read_memories() -> str:
    db = SessionLocal()
    memories = db.query(Memory).all()
    db.close()
    if not memories:
        return json.dumps({"memories": [], "note": "No memories yet."})
    return json.dumps({
        "memories": [
            {"category": m.category, "key": m.key, "value": m.value}
            for m in memories
        ]
    }, ensure_ascii=False)

def execute_write_memory(category: str, key: str, value: str) -> str:
    db = SessionLocal()
    existing = db.query(Memory).filter(Memory.key == key).first()
    if existing:
        existing.value = value
        existing.updated_at = datetime.now()
    else:
        db.add(Memory(category=category, key=key, value=value))
    db.commit()
    db.close()
    return json.dumps({"saved": True, "key": key, "value": value})


# ─── Chat endpoint — agentic loop ──────────────────────────────────────────────

@app.post("/chat")
async def chat(body: dict):
    db = SessionLocal()
    session_id = body.get("session_id", "default")

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
        "anthropic-beta": "interleaved-thinking-2025-05-14",  # needed for tool use + thinking
        "content-type": "application/json",
    }

    messages = body["messages"]
    tools_used = []   # track which tools fired, for the frontend badge

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:

            # ── First API call ──────────────────────────────────────────────
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 4096,
                    "system": SYSTEM_PROMPT,
                    "tools": TOOLS,
                    "messages": messages
                }
            )
            data = response.json()

            print("=== FIRST RESPONSE stop_reason:", data.get("stop_reason"))
            print("=== content types:", [b.get("type") for b in data.get("content", [])])

            # ── Agentic loop ────────────────────────────────────────────────
            #
            # Claude keeps calling tools until stop_reason is "end_turn".
            # Each iteration:
            #   1. Add Claude's tool_use turn to message history
            #   2. Execute each tool and collect results
            #   3. Add tool results to history as a "user" turn
            #   4. Call Claude again — it reads the results and decides what's next
            #
            # Max 5 iterations to prevent runaway loops.

            loop_count = 0
            while data.get("stop_reason") == "tool_use" and loop_count < 5:
                loop_count += 1

                # Step 1: append Claude's turn (which contains tool_use blocks)
                messages = messages + [{"role": "assistant", "content": data["content"]}]

                # Step 2: execute each tool Claude asked for
                tool_results = []
                for block in data["content"]:
                    if block.get("type") == "tool_use":
                        tool_name = block["name"]
                        tool_input = block.get("input", {})
                        tools_used.append(tool_name)

                        print(f"=== TOOL CALL: {tool_name} with {tool_input}")
                        result = execute_tool(tool_name, tool_input)
                        print(f"=== TOOL RESULT: {result[:200]}")

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": result   # ← real data now, not ""
                        })

                if not tool_results:
                    break

                # Step 3: feed results back as a user turn
                messages = messages + [{"role": "user", "content": tool_results}]

                # Step 4: call Claude again with the results
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 4096,
                        "system": SYSTEM_PROMPT,
                        "tools": TOOLS,
                        "messages": messages
                    }
                )
                data = response.json()
                print(f"=== LOOP {loop_count} stop_reason:", data.get("stop_reason"))
                print(f"=== LOOP {loop_count} content types:", [b.get("type") for b in data.get("content", [])])

            # ── Extract final text reply ────────────────────────────────────
            text_blocks = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
            reply = text_blocks[-1] if text_blocks else None

            if not reply:
                print("=== NO TEXT REPLY. Full data:", json.dumps(data))
                reply = "I wasn't able to get a result this time. Could you rephrase your question?"

            import re

            # ── Extract cards if present ─────────────────────────────────
            cards = None
            if reply and '<cards>' in reply:
                try:
                    card_json = reply.split('<cards>')[1].split('</cards>')[0].strip()
                    cards = json.loads(card_json)
                    reply = reply.split('</cards>')[-1].strip()
                except Exception as e:
                    print(f"=== CARDS PARSE ERROR: {e}")

    except httpx.TimeoutException:
        reply = "The request timed out — try again in a moment."
        data = {"usage": {"input_tokens": 0, "output_tokens": 0}}

    except Exception as e:
        print(f"=== EXCEPTION: {str(e)}")
        reply = f"Something went wrong. Error: {str(e)[:100]}"
        data = {"usage": {"input_tokens": 0, "output_tokens": 0}}

    # ── Save assistant reply ────────────────────────────────────────────────
    assistant_msg = Message(
        session_id=session_id,
        role="assistant",
        content=reply,
        cards=json.dumps(cards) if cards else None
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
        "used_web_search": "web_search" in tools_used,
        "tools_used": tools_used,
        "cards": cards,
    }