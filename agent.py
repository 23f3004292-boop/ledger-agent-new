
import os
import json
import uuid
import hashlib
import sqlite3
from pathlib import Path
from threading import RLock

from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()

app = Flask(__name__)

BASE = Path(__file__).resolve().parent
DB = BASE / "tasks.db"
CARD = BASE / "agent_card.json"
TOKEN = os.environ.get("AGENT_BEARER_TOKEN", "")
LOCK = RLock()

INPUT_TYPE = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_TYPE = "application/vnd.ga5.invoice-action-proposals+json"
RECEIPT_TYPE = "application/vnd.ga5.invoice-action-receipts+json"


def connect():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            owner TEXT NOT NULL,
            task_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            body TEXT NOT NULL,
            created REAL NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (owner, task_id),
            UNIQUE (owner, message_id)
        )
    """)
    return con


def fail(message, status):
    return jsonify({"error": message}), status


def owner_id():
    return hashlib.sha256(TOKEN.encode()).hexdigest()


def read_task(task_id, owner):
    with connect() as con:
        row = con.execute(
            "SELECT body FROM tasks WHERE owner=? AND task_id=?",
            (owner, task_id)
        ).fetchone()
    return json.loads(row["body"]) if row else None


def save_task(task, owner, message_id, body_hash):
    with connect() as con:
        con.execute(
            """INSERT INTO tasks(owner, task_id, message_id, body_hash, body)
               VALUES (?, ?, ?, ?, ?)""",
            (
                owner, task["id"], message_id, body_hash,
                json.dumps(task, ensure_ascii=False)
            )
        )


@app.before_request
def authenticate():
    if request.path == "/.well-known/agent-card.json":
        return None

    if not TOKEN:
        return fail("Server token is not configured", 503)

    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return fail("Unauthorized", 401)

    if request.headers.get("A2A-Version") != "1.0":
        return fail("A2A-Version must be 1.0", 400)


@app.get("/.well-known/agent-card.json")
def agent_card():
    if not CARD.exists():
        return fail("Agent Card file is missing", 500)
    return jsonify(json.loads(CARD.read_text(encoding="utf-8")))


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/a2a/tasks")
def list_tasks():
    with connect() as con:
        rows = con.execute(
            "SELECT body FROM tasks WHERE owner=? ORDER BY created DESC",
            (owner_id(),)
        ).fetchall()
    return jsonify({"tasks": [json.loads(r["body"]) for r in rows]})


@app.get("/a2a/tasks/<task_id>")
def get_task(task_id):
    task = read_task(task_id, owner_id())
    if not task:
        return fail("Task not found", 404)
    return jsonify(task)


@app.post("/a2a/tasks/<task_id>:cancel")
def cancel_task(task_id):
    with LOCK:
        task = read_task(task_id, owner_id())
        if not task:
            return fail("Task not found", 404)

        if task.get("status", {}).get("state") in (
            "TASK_STATE_COMPLETED", "TASK_STATE_CANCELED",
            "TASK_STATE_REJECTED", "TASK_STATE_FAILED"
        ):
            return jsonify(task)

        task["status"] = {
            "state": "TASK_STATE_CANCELED",
            "message": {"role": "agent", "parts": [
                {"kind": "text", "text": "Task canceled."}
            ]}
        }
        with connect() as con:
            con.execute(
                "UPDATE tasks SET body=? WHERE owner=? AND task_id=?",
                (json.dumps(task), owner_id(), task_id)
            )
        return jsonify(task)


def parts_of(message):
    return message.get("parts", []) if isinstance(message, dict) else []


def get_payload(message):
    for part in parts_of(message):
        if not isinstance(part, dict):
            continue
        data = part.get("data")
        if isinstance(data, dict):
            return data
    return {}


def evidence_refs(claim):
    refs = []
    for key in ("evidence", "evidenceRefs", "evidence_refs", "evidenceReferences"):
        value = claim.get(key)
        if isinstance(value, list):
            refs.extend(str(x) for x in value)
        elif isinstance(value, str):
            refs.append(value)
    return list(dict.fromkeys(refs))


def build_proposals(payload):
    claims = payload.get("invoices", payload.get("claims", []))
    if not isinstance(claims, list):
        claims = []

    proposals = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue

        invoice_id = str(
            claim.get("invoiceId", claim.get("invoice_id",
            claim.get("id", f"invoice-{index + 1}")))
        )
        refs = evidence_refs(claim)

        # Default to review unless the input provides clear evidence.
        action = "REVIEW"
        reason = "Insufficient structured evidence for an automated decision."

        status = str(claim.get("status", "")).lower()
        if status in ("duplicate", "duplicate_invoice"):
            action = "REJECT"
            reason = "The invoice is marked as a duplicate in the supplied data."
        elif status in ("approved", "verified") and refs:
            action = "APPROVE"
            reason = "The invoice is marked verified and has supplied evidence references."

        proposals.append({
            "invoiceId": invoice_id,
            "action": action,
            "reason": reason,
            "evidenceRefs": refs
        })

    return proposals


@app.post("/a2a/message:send")
def send_message():
    if request.mimetype != "application/a2a+json":
        return fail("Content-Type must be application/a2a+json", 415)

    envelope = request.get_json(silent=True)
    if not isinstance(envelope, dict):
        return fail("Invalid JSON envelope", 400)

    message = envelope.get("message", envelope)
    if not isinstance(message, dict):
        return fail("Invalid message", 400)

    message_id = message.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        return fail("Missing messageId", 400)

    canonical = json.dumps(message, sort_keys=True, separators=(",", ":"))
    body_hash = hashlib.sha256(canonical.encode()).hexdigest()
    owner = owner_id()

    with LOCK:
        with connect() as con:
            existing = con.execute(
                "SELECT body_hash, body FROM tasks WHERE owner=? AND message_id=?",
                (owner, message_id)
            ).fetchone()

        if existing:
            if existing["body_hash"] != body_hash:
                return fail("IDEMPOTENCY_CONFLICT", 409)
            return jsonify({"task": json.loads(existing["body"])})

        payload = get_payload(message)
        task_id = str(uuid.uuid4())
        proposals = build_proposals(payload)

        task = {
            "id": task_id,
            "contextId": str(message.get("contextId", uuid.uuid4())),
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "message": {
                    "role": "agent",
                    "parts": [{
                        "kind": "text",
                        "text": "Review the proposed invoice actions and submit accepted or rejected decisions."
                    }]
                }
            },
            "artifacts": [{
                "artifactId": str(uuid.uuid4()),
                "name": "invoice-action-proposals",
                "parts": [{
                    "kind": "data",
                    "data": {"proposals": proposals}
                }],
                "metadata": {"mediaType": PROPOSAL_TYPE}
            }],
            "history": [message],
            "metadata": {"receiptNonces": {}}
        }

        try:
            save_task(task, owner, message_id, body_hash)
        except sqlite3.IntegrityError:
            return fail("IDEMPOTENCY_CONFLICT", 409)

    return jsonify({"task": task})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))
