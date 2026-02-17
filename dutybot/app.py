import json
import os
import sqlite3
import time
import uuid
from datetime import datetime

import requests
from flask import Flask, g, jsonify, render_template, request

app = Flask(__name__)

DATABASE = os.environ.get("DATABASE_PATH", "/app/data/dutybot.db")
LLAMA_URL = os.environ.get("LLAMA_URL", "http://llama-server:8080")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
MAX_CONTEXT_MESSAGES = int(os.environ.get("MAX_CONTEXT_MESSAGES", "20"))

SYSTEM_PROMPT = """You are DutyBot, a UK Police Duty Assistant. You help police officers with \
operational guidance, definitions of offences, points to prove, and general \
policing knowledge based on UK law.

IMPORTANT CONSTRAINTS:
- You are for TRAINING AND EDUCATIONAL PURPOSES ONLY — never for live operational use
- Always encourage officers to verify guidance against local force policy and official sources
- Be professional, precise, and cite legislation where possible
- If unsure, say so clearly — never fabricate legal definitions
- When legislation lookup results are provided, use them to ground your answer"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New Conversation',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, timestamp);
        CREATE TABLE IF NOT EXISTS memory (
            id TEXT PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()


def now_iso():
    return datetime.utcnow().isoformat() + "Z"


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


# -- Conversations --

@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    db = get_db()
    rows = db.execute(
        "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/conversations", methods=["POST"])
def create_conversation():
    db = get_db()
    conv_id = str(uuid.uuid4())
    ts = now_iso()
    title = (request.json or {}).get("title", "New Conversation")
    db.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conv_id, title, ts, ts),
    )
    db.commit()
    return jsonify({"id": conv_id, "title": title, "created_at": ts, "updated_at": ts}), 201


@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
def delete_conversation(conv_id):
    db = get_db()
    db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    db.commit()
    return jsonify({"deleted": True})


@app.route("/api/conversations/<conv_id>/messages", methods=["GET"])
def get_messages(conv_id):
    db = get_db()
    rows = db.execute(
        "SELECT id, role, content, timestamp FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC",
        (conv_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# -- Chat --

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    conv_id = data.get("conversation_id")
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    db = get_db()

    # Create conversation if needed
    if not conv_id:
        conv_id = str(uuid.uuid4())
        ts = now_iso()
        title = user_message[:60] + ("..." if len(user_message) > 60 else "")
        db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, title, ts, ts),
        )
        db.commit()

    # Save user message
    msg_id = str(uuid.uuid4())
    ts = now_iso()
    db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (msg_id, conv_id, "user", user_message, ts),
    )
    db.commit()

    # Gather conversation history
    history_rows = db.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC",
        (conv_id,),
    ).fetchall()

    # Build messages for llama.cpp
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add memory context (filter out suspiciously long values)
    memories = db.execute("SELECT key, value FROM memory").fetchall()
    if memories:
        valid = [(m['key'], m['value']) for m in memories if len(m['value']) < 80]
        if valid:
            mem_text = "\n".join(f"- {k}: {v}" for k, v in valid)
            messages[0]["content"] += f"\n\nThings you remember about this user:\n{mem_text}"

    # Add conversation history (limited)
    for row in history_rows[-MAX_CONTEXT_MESSAGES:]:
        messages.append({"role": row["role"], "content": row["content"]})

    # Call llama.cpp
    try:
        resp = requests.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "model": "dutybot",
                "messages": messages,
                "max_tokens": 512,
                "temperature": 0.3,
                "stop": ["<|im_end|>", "<|im_start|>"],
                "frequency_penalty": 0.6,
                "presence_penalty": 0.3,
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
        assistant_content = result["choices"][0]["message"]["content"]
    except Exception as e:
        assistant_content = f"I'm sorry, I'm having trouble connecting to the inference server. Error: {str(e)}"

    # Save assistant message
    assistant_msg_id = str(uuid.uuid4())
    ts = now_iso()
    db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (assistant_msg_id, conv_id, "assistant", assistant_content, ts),
    )
    db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conv_id)
    )
    db.commit()

    # Background verification — search legislation.gov.uk to confirm answer
    verification = None
    try:
        verification = verify_answer(user_message)
    except Exception:
        pass

    # Extract memories (best-effort) — only when message likely contains personal info
    trigger_phrases = ["i am", "i'm", "i work", "my rank", "my force", "my team", "my unit", "my station"]
    if any(phrase in user_message.lower() for phrase in trigger_phrases):
        try:
            extract_memory(db, user_message, assistant_content)
        except Exception as e:
            app.logger.error(f"Memory extraction call failed: {e}", exc_info=True)

    return jsonify({
        "conversation_id": conv_id,
        "message": {
            "id": assistant_msg_id,
            "role": "assistant",
            "content": assistant_content,
            "timestamp": ts,
        },
        "verification": verification,
    })


def verify_answer(query):
    """Search legislation.gov.uk via SearXNG to verify the model's answer."""
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={
                "q": f"site:legislation.gov.uk {query}",
                "format": "json",
                "engines": "google,bing,duckduckgo",
                "categories": "general",
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            return None

        sources = []
        for r in results[:5]:
            sources.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            })

        return {
            "status": "found",
            "count": len(sources),
            "sources": sources,
        }
    except Exception as e:
        app.logger.warning(f"Verification search failed: {e}")
        return None


def extract_memory(db, user_message, assistant_response):
    """Ask the model to extract memorable facts from the conversation."""
    try:
        resp = requests.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "model": "dutybot",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a JSON extraction assistant. You ONLY output valid JSON objects. "
                            "You NEVER output explanations, definitions, or legal text. "
                            "Values must be short identifiers of 1-3 words only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Extract ONLY short factual identifiers about the user from this exchange.\n\n"
                            f"USER: {user_message}\n"
                            f"ASSISTANT: {assistant_response[:500]}\n\n"
                            "Rules:\n"
                            "- Only extract rank, force, or specialization\n"
                            "- Values must be 1-3 words (e.g. \"PC\", \"Met Police\", \"Public Order\")\n"
                            "- Do NOT include definitions, explanations, or legal text as values\n"
                            "- If the user has NOT explicitly stated their rank, force, or specialization, "
                            "return exactly: {}\n"
                            "JSON:"
                        ),
                    },
                ],
                "max_tokens": 256,
                "temperature": 0.1,
                "stop": ["<|im_end|>", "<|im_start|>"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[MEMORY] Raw extraction: {content!r}", flush=True)

        # Try to parse JSON from the response
        # Handle cases where model wraps in markdown code blocks
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        # Extract first JSON object — model may append extra text
        start = content.find("{")
        if start == -1:
            return
        decoder = json.JSONDecoder()
        facts, _ = decoder.raw_decode(content, start)
        if not isinstance(facts, dict) or not facts:
            return

        ts = now_iso()
        for key, value in facts.items():
            if key and value and isinstance(value, str) and len(value) < 80 and "\n" not in value:
                db.execute(
                    "INSERT INTO memory (id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (str(uuid.uuid4()), key.strip(), value.strip(), ts),
                )
        db.commit()
    except Exception as e:
        import traceback
        print(f"[MEMORY] Extraction failed: {e}", flush=True)
        traceback.print_exc()


# -- Memory --

@app.route("/api/memory", methods=["GET"])
def get_memory():
    db = get_db()
    rows = db.execute("SELECT key, value, updated_at FROM memory ORDER BY updated_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/memory/<key>", methods=["DELETE"])
def delete_memory(key):
    db = get_db()
    db.execute("DELETE FROM memory WHERE key = ?", (key,))
    db.commit()
    return jsonify({"deleted": True})


@app.route("/api/memory", methods=["DELETE"])
def clear_memory():
    db = get_db()
    db.execute("DELETE FROM memory")
    db.commit()
    return jsonify({"cleared": True})


# Initialize DB on startup
with app.app_context():
    os.makedirs(os.path.dirname(DATABASE) or ".", exist_ok=True)
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
