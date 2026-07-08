import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, g, jsonify, render_template, request

app = Flask(__name__)

DATABASE = os.environ.get("DATABASE_PATH", "/app/data/dutybot.db")
LLAMA_URL = os.environ.get("LLAMA_URL", "http://llama-server:8080")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


MAX_CONTEXT_MESSAGES = _int_env("MAX_CONTEXT_MESSAGES", 20)
CTX_SIZE = _int_env("CTX_SIZE", 4096)

# Inference parameters. Frequency penalty is kept low on purpose: legal answers
# must repeat exact statute names and section numbers, and penalising that
# repetition pushes the model into paraphrase drift (a hallucination source).
LLM_MAX_TOKENS = 768
LLM_TEMPERATURE = 0.2
LLM_FREQUENCY_PENALTY = 0.1
LLM_PRESENCE_PENALTY = 0.0

# Context budgeting (approximate — llama.cpp does the real tokenisation)
APPROX_CHARS_PER_TOKEN = 4
CONTEXT_SAFETY_MARGIN_TOKENS = 128

# Memory hygiene: only these keys ever reach the system prompt, and only this many
MEMORY_ALLOWED_KEYS = {"rank", "force", "specialization", "specialisation", "team", "unit", "station", "role"}
MEMORY_MAX_INJECTED = 12
MEMORY_MAX_VALUE_CHARS = 80

LEGAL_KEYWORDS = [
    "section", "offence", "offense", "arrest", "assault", "gbh", "abh",
    "theft", "burglary", "robbery", "pace", "act", "law", "crime",
    "criminal", "powers", "evidence", "caution", "charge", "custody",
    "bail", "warrant", "search", "stop", "force", "weapon", "drug",
    "drugs", "fraud", "damage", "public order", "harassment", "stalking",
    "domestic", "murder", "manslaughter", "definition", "points to prove",
    "stolen", "steal", "knife", "firearm", "trespass", "criminal damage",
    "affray", "kidnap", "kidnapping", "blackmail", "voyeurism", "riot",
    "violent disorder", "wounding", "grievous bodily harm", "actual bodily harm",
    "possession", "going equipped", "handling", "offensive weapon",
    "breach of the peace", "sentence", "sentencing", "twoc",
]
# Word-boundary matching (so "act" no longer fires on "contact"), plus statute
# shorthand like "s.18" / "s 47".
LEGAL_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in LEGAL_KEYWORDS) + r")\b"
    r"|\bs\.?\s?\d+[a-z]?\b",
    re.IGNORECASE,
)

LEGISLATION_DOMAIN = "legislation.gov.uk"
# URL path segments that address a bounded fragment of an act — fetching a whole
# act would blow the context window, so only these get full-text retrieval.
LEGISLATION_FRAGMENT_SEGMENTS = ("/section/", "/regulation/", "/article/", "/rule/")
GROUNDING_MAX_CHARS = 1200
GROUNDING_MAX_SOURCES = 5
GROUNDING_FULL_TEXT_SOURCES = 2

SYSTEM_PROMPT = """You are DutyBot, a UK Police Duty Assistant. You help police officers with \
operational guidance, definitions of offences, points to prove, and general \
policing knowledge based on UK law.

IMPORTANT CONSTRAINTS:
- You are for TRAINING AND EDUCATIONAL PURPOSES ONLY — never for live operational use
- Always encourage officers to verify guidance against local force policy and official sources
- Be professional, precise, and cite legislation where possible
- If unsure, say so clearly — never fabricate legal definitions
- When legislation lookup results are provided, treat them as the authoritative basis for \
statutory wording: quote act names and section numbers exactly as they appear there
- If no lookup results are provided, or they do not cover the question, say so and clearly \
label statutory details as unverified general knowledge
- UK legislation changes over time — prefer provided lookup results over your memory"""


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
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    components = {}
    for name, url in (("llama", f"{LLAMA_URL}/health"), ("searxng", f"{SEARXNG_URL}/healthz")):
        try:
            resp = requests.get(url, timeout=2)
            components[name] = "ok" if resp.status_code == 200 else f"http {resp.status_code}"
        except Exception:
            components[name] = "unreachable"
    status = "ok" if all(v == "ok" for v in components.values()) else "degraded"
    return jsonify({"status": status, "components": components})


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


def clean_response(text):
    """Strip training data artifacts, partial ChatML tokens, and leaked system context."""
    # Truncate at any leaked system/memory markers
    for marker in [
        "Things you remember about this user:",
        "[INTERNAL CONTEXT",
        "Example system role:",
        "Example user role:",
        "The system role is used to",
        "The user role should describe",
    ]:
        if marker in text:
            text = text[:text.index(marker)].rstrip()

    # Truncate at leaked training-doc sub-headings (e.g. "### 3.2 Defining the System Role").
    # Single-level numbered headings ("### 3. Intent") are legitimate answer structure.
    leak = re.search(r"\n#{1,6}\s*\d+\.\d+\s", text)
    if leak:
        text = text[:leak.start()].rstrip()

    # Strip partial ChatML tokens (e.g. "<|", "<|im_", "<|im_end")
    text = re.sub(r"<\|[^>]*$", "", text).rstrip()

    # Strip trailing markdown code fences that were never opened
    text = re.sub(r"\n```\s*$", "", text).rstrip()

    if not text.strip():
        return "I wasn't able to produce a usable answer to that. Please try rephrasing the question."
    return text


def approx_token_count(text):
    return len(text) // APPROX_CHARS_PER_TOKEN + 4


def fit_messages_to_budget(messages, budget_tokens):
    """Drop oldest history turns (never the system prompt or the current user turn)
    until the approximate prompt size fits the model's context window. Without this,
    long conversations silently overflow ctx-size and llama.cpp truncates or errors."""
    while len(messages) > 2 and sum(approx_token_count(m["content"]) for m in messages) > budget_tokens:
        messages.pop(1)
    return messages


# Capitalised words (plus legal connectors, e.g. "Offences Against the Person
# Act 1861") ending in "Act <year>" — anchored so it can't span whole sentences
ACT_CITATION_RE = re.compile(
    r"\b[A-Z][A-Za-z']*\s+(?:(?:[A-Z][A-Za-z']*|of|the|and|against)\s+)*Act\s+\d{4}"
)


def check_citations(answer, sources):
    """Best-effort check: flag act names cited in the answer that never appear in
    the retrieved grounding text, so the UI can warn about unverified citations."""
    corpus = " ".join(
        f"{s.get('title', '')} {s.get('snippet', '')} {s.get('grounding', '')}"
        for s in sources
    ).lower()
    def variants(act):
        # Sentence-leading capitals ("See the Offences...") can extend the match,
        # so try every suffix starting at a capitalised word (min 3 words).
        words = act.split()
        for i, word in enumerate(words):
            if word[:1].isupper() and len(words) - i >= 3:
                yield " ".join(words[i:]).lower()

    cited = sorted(set(ACT_CITATION_RE.findall(answer)))
    unmatched = [
        act for act in cited
        if not any(v in corpus for v in variants(act))
    ]
    return {"cited": cited, "unmatched": unmatched}


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

    # Build messages for llama.cpp; the date matters for a legal assistant —
    # legislation gets amended, and the model must know "now" vs its training data
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    messages = [{"role": "system", "content": SYSTEM_PROMPT + f"\n\nCurrent date: {today}."}]

    # Add memory context (allow-listed keys only — memory values are model-generated
    # from user text, so anything else is a prompt-injection persistence vector)
    memories = db.execute("SELECT key, value FROM memory ORDER BY updated_at DESC").fetchall()
    if memories:
        valid = [
            (m["key"], m["value"]) for m in memories
            if m["key"].strip().lower() in MEMORY_ALLOWED_KEYS
            and len(m["value"]) < MEMORY_MAX_VALUE_CHARS
        ][:MEMORY_MAX_INJECTED]
        if valid:
            mem_text = "\n".join(f"- {k}: {v}" for k, v in valid)
            messages[0]["content"] += (
                f"\n\n[INTERNAL CONTEXT — DO NOT include this in your response] "
                f"User details:\n{mem_text}"
            )

    # Add conversation history (limited); the current user turn is always included
    if MAX_CONTEXT_MESSAGES > 0:
        recent_rows = history_rows[-MAX_CONTEXT_MESSAGES:]
    else:
        recent_rows = history_rows[-1:]
    for row in recent_rows:
        messages.append({"role": row["role"], "content": row["content"]})

    # Search legislation.gov.uk BEFORE generating response (RAG)
    verification = None
    should_verify = len(user_message.split()) > 2 and bool(LEGAL_KEYWORD_RE.search(user_message))
    if should_verify:
        verification = verify_answer(user_message)

    # Inject grounding text into the last user message. Prefer the actual section
    # text fetched from legislation.gov.uk over the (often boilerplate) search snippet.
    if verification and verification.get("status") == "found":
        grounding_lines = "\n".join(
            f"- {s['title']} ({s['url']}): {s.get('grounding') or s['snippet']}"
            for s in verification["sources"]
        )
        messages[-1]["content"] += (
            "\n\n[Legislation lookup results from legislation.gov.uk — treat these extracts as the "
            "authoritative basis for any statutory wording. If they do not answer the question, say "
            "the lookup was inconclusive rather than guessing.]\n" + grounding_lines
        )

    # Keep the prompt inside the context window — drop oldest history first
    messages = fit_messages_to_budget(
        messages, CTX_SIZE - LLM_MAX_TOKENS - CONTEXT_SAFETY_MARGIN_TOKENS
    )

    # Call llama.cpp
    try:
        resp = requests.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "model": "dutybot",
                "messages": messages,
                "max_tokens": LLM_MAX_TOKENS,
                "temperature": LLM_TEMPERATURE,
                "stop": ["<|im_end|>", "<|im_start|>"],
                "frequency_penalty": LLM_FREQUENCY_PENALTY,
                "presence_penalty": LLM_PRESENCE_PENALTY,
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
        choice = result["choices"][0]
        assistant_content = clean_response(choice["message"]["content"])
        if choice.get("finish_reason") == "length":
            # A silently truncated legal answer is misinformation — say so
            assistant_content += "\n\n*(Answer hit the length limit — ask me to continue for the rest.)*"
    except Exception as e:
        # Don't persist a fake assistant turn — it would pollute the model context
        # of every later message in this conversation.
        app.logger.error(f"LLM request failed: {e}", exc_info=True)
        return jsonify({
            "error": "The inference server is unavailable or returned an invalid response. Please try again.",
            "conversation_id": conv_id,
        }), 502

    # Flag any acts the model cited that never appeared in the retrieved sources
    if verification and verification.get("status") == "found":
        verification["citation_check"] = check_citations(assistant_content, verification["sources"])

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


def fetch_legislation_text(url, max_chars=GROUNDING_MAX_CHARS):
    """Fetch the actual text of a legislation.gov.uk fragment via its data API.

    Search-result snippets are often boilerplate; the real section text grounds
    the model far better. Returns None when the URL isn't a bounded fragment or
    the fetch fails (caller falls back to the search snippet).
    """
    if not any(seg in url for seg in LEGISLATION_FRAGMENT_SEGMENTS):
        return None
    base = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    try:
        resp = requests.get(f"{base}/data.xml", timeout=10, stream=True)
        resp.raise_for_status()
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > 300_000:
                break
        text = b"".join(chunks).decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] or None
    except Exception as e:
        app.logger.warning(f"Legislation text fetch failed for {url}: {e}")
        return None


def verify_answer(query):
    """Look up the query on legislation.gov.uk via SearXNG and gather grounding text.

    Returns a dict whose "status" is one of:
      - "found": usable sources below (count/sources reflect exactly what gets
        injected into the prompt, so the UI never shows sources the model didn't see)
      - "none": search worked but returned nothing usable
      - "failed": the search itself errored
    """
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
        results = resp.json().get("results", [])
    except Exception as e:
        app.logger.warning(f"Verification search failed: {e}")
        return {"status": "failed", "count": 0, "sources": []}

    sources = []
    for r in results:
        url = r.get("url", "")
        host = (urlparse(url).hostname or "").lower()
        # site: filtering is engine-dependent — enforce the domain ourselves
        if host != LEGISLATION_DOMAIN and not host.endswith("." + LEGISLATION_DOMAIN):
            continue
        snippet = (r.get("content") or "").strip()
        source = {"title": r.get("title", ""), "url": url, "snippet": snippet}
        if len(sources) < GROUNDING_FULL_TEXT_SOURCES:
            grounding = fetch_legislation_text(url)
            if grounding:
                source["grounding"] = grounding
        if source.get("grounding") or snippet:
            sources.append(source)
        if len(sources) >= GROUNDING_MAX_SOURCES:
            break

    if not sources:
        return {"status": "none", "count": 0, "sources": []}
    return {"status": "found", "count": len(sources), "sources": sources}


def extract_memory(db, user_message, assistant_response):
    """Ask the model to extract memorable facts from the conversation.

    Network and parse errors propagate to the caller, which logs them —
    memory extraction is best-effort and must never fail the chat request.
    """
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
    app.logger.info(f"[MEMORY] Raw extraction: {content!r}")

    # Handle cases where the model wraps the JSON in markdown code blocks
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
        if not (key and value and isinstance(value, str)):
            continue
        # Keys are model-generated: sanitise and allow-list them before they can
        # ever reach the system prompt or the memory panel
        key = re.sub(r"[^A-Za-z0-9 _-]", "", str(key)).strip()[:40]
        if key.lower() not in MEMORY_ALLOWED_KEYS:
            continue
        if len(value) < MEMORY_MAX_VALUE_CHARS and "\n" not in value:
            db.execute(
                "INSERT INTO memory (id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (str(uuid.uuid4()), key, value.strip(), ts),
            )
    db.commit()


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
    # Dev-only entrypoint — the container runs gunicorn (see Dockerfile).
    # Debug mode exposes the Werkzeug remote-code-execution debugger, so it
    # must be opted into explicitly and never combined with 0.0.0.0 by default.
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=5000,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
