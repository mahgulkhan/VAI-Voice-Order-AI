import json, os, re, tempfile, uuid
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse

# --- config -----------------------------------------------------------------
MODEL = "gemini-3.8-flash"
MODELS = [MODEL, "gemini-3.7-flash", "gemini-3.5-flash-lite"]

def gen(**kwargs):
    for i, model in enumerate(MODELS):
        try:
            return client.models.generate_content(model=model, **kwargs)
        except Exception as e:
            s = str(e)
            print(f"[gen] {model} failed: {s[:120]}")
            skippable = "503" in s or "429" in s or "404" in s
            if not skippable or i == len(MODELS) - 1:
                raise

ORDERS_FILE = Path("orders.jsonl")

MENU = """
HOT DEALS (all PKR):
Smart Deal: 2 Small Pizzas, 1 Small Pasta, 1 Drink 500ml - 1750
Regular Deal: 1 Reg. Special Pizza, 1 Drink 500ml - 1350
Value Deal: 1 Large Traditional Pizza, 1 Drink 1.5L - 1790
Special Deal: 1 Large Special Pizza, 1 Drink 1.5L - 1890
Super Deal: 1 Large Traditional Pizza, 1 Pasta, 1 Drink 1.5L - 2690
Double Up Deal: 2 Large Special Pizzas, 1 Drink 1.5L - 3690
Square Deal: 1 Square Pizza, 1 Drink 1.5L - 2490
Chaska Deal: 1 Small Pasta with 6 Wings, 1 Drink 500ml - 1090

HOTTER DEALS:
Mega Deal: 3 Large Special Pizzas, 2 Drinks 1.5L - 5790
Royal Deal: 2 Large Special Pizzas, 12 Pcs Wings, 1 Drink 1.5L - 4590
Family Ties: 1 Large Square Pizza, 1 Medium Square Pizza, 12 Pcs Wings, 1 Small Pasta, 2 Drinks 1L - 5590
Birthday Bash: 3 Square Pizzas, 12 Pcs Wings, 2 Drinks 1.5L - 7990

PIZZA (customer must choose size and flavor):
White's Specials - Small 600 / Medium 1380 / Large 1850
  Flavors: White's Special, Super Supreme, Bonfire, Click On, Pizza Delight, Supreme Le'bistro, Picklish, Cheese Burst
Traditional - Small 580 / Medium 1290 / Large 1720
  Flavors: Chicken Tikka, Chicken Fajita, Fajita Sicilian, Hot N Spicy, Cheese Lover, Veggie Lover, Euro, Chicken Supreme
Stuffed Crust (Round or Square) - Medium 1530 / Large 2350
  Flavors: Kebab Stuffer, Cheese Stuffer, Chicken & Cheese

PASTA (Half 580 / Full 970): Chef Special, Flaming, Crunchy
CALZONE (1150): Magnetic, Sizzling

EXTRAS:
Chicken Topping - Small 110 / Medium 170 / Large 220
Cheese Topping - Small 140 / Medium 170 / Large 220
Sauce (Dip / Mayo Mustard / Thousand Island) - 90
Thin Crust (extra) - 70

FRIES & SANDWICHES:
Majestic Crunch 500, Wings of the Throne 680, Crispy Crown 450, Crown Bites 650,
King's Stack 990, Regular Fries 250, Castle Crunch Pasta 990

STARTERS:
Cheese Stuffer Sticks (4) 590, Kabab Stuffer Sticks (4) 610, Cheezy Sticks (6) 400,
Mexican Sandwich 870, Buffalo Wings (6) 500, Oven Baked Wings (6) 460, Oven Baked Wings (12) 890,
Malai Boti Platter (4) 1250, White Special Platter (4) 1050,
Peri Peri Rolls (4) 740, Behari Rolls (4) 740, Tempeeli Rolls (4) 740, Indian Rolls (4) 740

DRINKS (sold separately):
Drink 500ml - 100
Drink 1L - 180
Drink 1.5L - 250
Flavors: Pepsi, 7Up, Mirinda
"""

SYSTEM = f"""You are a phone order-taker for a restaurant in Pakistan.
Menu (PKR):
{MENU}
Rules:
- Reply in the language the customer uses (Urdu, English, or Roman Urdu). Urdu replies in Urdu script.
- Keep replies SHORT (1-2 sentences), they are spoken aloud.
- Ask one follow-up at a time: missing quantities, anything unclear, dine-in/pickup/delivery, and a name.
- Only accept items on the menu.
- When the customer has confirmed the final order, finish.
- For pizzas always confirm size and flavor; for pasta confirm half/full and type. Deals have fixed contents and price. Add the correct price for every item and compute the total.

ALWAYS respond with ONLY a JSON object, no other text:
{{"reply": "<what to say>", "done": false, "order": null}}
When the order is confirmed:
{{"reply": "<thank-you + total>", "done": true,
  "order": {{"customer_name": "", "type": "", "items": [{{"name": "", "qty": 1, "price": 0}}], "total": 0}}}}
"""

# --- setup ------------------------------------------------------------------
app = FastAPI()
client = genai.Client()
CALLS: dict[str, list] = {}  # session_id -> message history


def ask_claude(history):
    contents = [
        types.Content(role="user" if m["role"] == "user" else "model",
                      parts=[types.Part(text=m["content"])])
        for m in history
    ]
    resp = gen(
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM, response_mime_type="application/json"),
    )
    raw = resp.text
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(match.group(0))
    except Exception:
        data = {"reply": raw.strip(), "done": False, "order": None}
    history.append({"role": "assistant", "content": raw})
    return data


def transcribe(upload_bytes: bytes) -> str:
    resp = gen(
        contents=[
            types.Part.from_bytes(data=upload_bytes, mime_type="audio/webm"),
            "Transcribe this audio exactly as spoken (Urdu, English or Roman Urdu). Return only the transcript.",
        ],
    )
    return (resp.text or "").strip()


def save_order(order: dict):
    order["id"] = uuid.uuid4().hex[:8]
    order["time"] = datetime.now().isoformat(timespec="seconds")
    with ORDERS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(order, ensure_ascii=False) + "\n")
    return order


# --- routes -----------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse("index.html")


BUSY = "Sorry, the AI is busy right now. Please say that again."


@app.post("/start")
def start():
    sid = uuid.uuid4().hex
    CALLS[sid] = [{"role": "user", "content": "(customer just called, greet them and ask for their order)"}]
    try:
        data = ask_claude(CALLS[sid])
    except Exception:
        return {"session_id": sid, "reply": BUSY}
    return {"session_id": sid, "reply": data["reply"]}


@app.post("/turn")
async def turn(session_id: str = Form(...), audio: UploadFile = File(None), text: str = Form(None)):
    history = CALLS[session_id]
    try:
        user_text = text or transcribe(await audio.read())
    except Exception:
        return {"user_text": "", "reply": BUSY, "done": False, "order": None}
    if not user_text:
        return {"user_text": "", "reply": None, "done": False, "order": None}
    history.append({"role": "user", "content": user_text})
    try:
        data = ask_claude(history)
    except Exception:
        history.pop()  # drop the unanswered message so they can retry
        return {"user_text": user_text, "reply": BUSY, "done": False, "order": None}
    order = save_order(data["order"]) if data.get("done") and data.get("order") else None
    if order:
        CALLS.pop(session_id, None)
    return {"user_text": user_text, "reply": data["reply"], "done": bool(order), "order": order}


@app.get("/orders")
def orders():
    if not ORDERS_FILE.exists():
        return []
    return [json.loads(l) for l in ORDERS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()][::-1]