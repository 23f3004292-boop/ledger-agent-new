import json, re, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

BASE = "https://exam.sanand.workers.dev/questionData?email=23f3004292%40ds.study.iitm.ac.in&quizSign=b5TBgqyJnxqHBsuJf0y4KxYoZYPevbhal9kITxhTJm77ea2zEPGEwD23bof6z0m6y6UGbHZMj3CAkjkibDCm4jnUBCOIxrtY0XySIFNx5z4PM1lhOBxhkw2nrPOCFE7EyWy%2Bwv0ycRAuZf81HuF1J82u0x0HoAY%2F3TYUESr7hZPe0b2VGE%2Fhf3ubm%2Fj%2FkXPRyG6KJw6y23lhsvSSnSOMj%2FcrMpEx3N1oP%2Bl7uW%2BczCa9t1lC%2FAtg1QBWUIeynPf8z6Y%2FIc4Cs4%2BGoZSJdQUr0PaFzd5W3S8eB9yqH3hbayIw5K43bmetjcq2rXBFAAoSVd9sD2rOZATgqd3ImILt8g%3D%3D&questionId=q-ledger-agent-server"

IST = timezone(timedelta(hours=5, minutes=30))

# ---- load and clean the data ONCE at startup ----
rates = requests.get(BASE + "&path=%2Frates", timeout=30).json()["usd_per_unit"]
text = requests.get(BASE + "&path=%2Fexport", timeout=60).text

latest = {}  # order id -> row with the latest updated_at
for line in text.splitlines():
    if not line.strip():
        continue
    o = json.loads(line)
    o["_upd"] = datetime.fromisoformat(o["updated_at"])
    if o["id"] not in latest or o["_upd"] > latest[o["id"]]["_upd"]:
        latest[o["id"]] = o

rows = []
for o in latest.values():
    d = datetime.fromisoformat(o["created_at"]).astimezone(IST)  # business date in IST
    rows.append({
        "customer": o["customer"], "region": o["region"],
        "product": o["product"], "qty": o["qty"], "status": o["status"],
        "usd": o["amount"] * rates[o["currency"]],  # amount converted to USD
        "date": d.strftime("%Y-%m-%d"), "month": d.strftime("%Y-%m"), "year": d.year,
    })
print("orders loaded:", len(rows))

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
REGIONS = ["Central", "East", "North", "South", "West"]
PRODUCTS = sorted({r["product"] for r in rows}, key=len, reverse=True)

def answer(q):
    ql = q.lower()
    ql = ql.replace("at least", "")  # "at least one" is not a ranking word

    # 1. which orders count: refund questions use refunded, otherwise paid
    if "refund" in ql: status = "refunded"
    elif "void" in ql or "cancel" in ql: status = "void"
    else: status = "paid"
    sel = [r for r in rows if r["status"] == status]

    # 2. filters: region, product, customer
    for reg in REGIONS:
        if re.search(r"\b" + reg.lower() + r"\b", ql):
            sel = [r for r in sel if r["region"] == reg]
    for p in PRODUCTS:
        if p.lower() in ql:
            sel = [r for r in sel if r["product"] == p]
    m = re.search(r"\bc\d{3,5}\b", ql)
    if m:
        sel = [r for r in sel if r["customer"] == m.group().upper()]

    # 3. filters: date (exact day, month, quarter, or year)
    year = re.search(r"\b(20\d\d)\b", ql)
    year = int(year.group(1)) if year else 2026
    iso = re.search(r"\d{4}-\d{2}-\d{2}", ql)
    quarter = re.search(r"\bq([1-4])\b", ql)
    month = next((i + 1 for i, name in enumerate(MONTHS)
                  if re.search(r"\b" + name + r"\b", ql)), None)
    if iso:
        sel = [r for r in sel if r["date"] == iso.group()]
    elif month:
        sel = [r for r in sel if r["month"] == f"{year}-{month:02d}"]
    elif quarter:
        qs = int(quarter.group(1))
        sel = [r for r in sel if r["year"] == year and (int(r["month"][5:]) - 1) // 3 + 1 == qs]
    elif re.search(r"\b20\d\d\b", ql):
        sel = [r for r in sel if r["year"] == year]

    # 4. what to compute
    g = re.search(r"\b(product|region|customer)s?\b", ql)
    ranking = re.search(r"\b(which|top|best|highest|most|largest|biggest|lowest|least|fewest|smallest|worst)\b", ql)
    by_count = re.search(r"\b(units|quantity|qty|orders|how many)\b", ql)

    # "how many different customers ..." -> count distinct customers (checked first)
    if re.search(r"(how many|number of|count of|unique|distinct|different) .*customers", ql):
        return len({r["customer"] for r in sel})

    if g and ranking:  # "which product had the most revenue ..."
        key = g.group(1)
        totals = {}
        for r in sel:
            add = r["qty"] if re.search(r"units|quantity|qty", ql) else (1 if by_count else r["usd"])
            totals[r[key]] = totals.get(r[key], 0) + add
        if not totals:
            return None
        pick = min if re.search(r"lowest|least|fewest|smallest|worst", ql) else max
        return pick(totals, key=totals.get)
    if re.search(r"average|avg|mean", ql):
        return round(sum(r["usd"] for r in sel) / len(sel), 2) if sel else 0
    if re.search(r"units|quantity|qty|items sold", ql):
        return sum(r["qty"] for r in sel)
    if re.search(r"how many|number of|count", ql):
        return len(sel)
    return round(sum(r["usd"] for r in sel), 2)  # default: total USD

@app.get("/")
def home():
    return jsonify({"status": "ok", "orders": len(rows)})

@app.post("/answer")
def ans():
    q = request.get_json(force=True)["question"]
    print("Q:", q, flush=True)
    try:
        a = answer(q)
    except Exception as e:
        print("  error:", e, flush=True)
        a = None
    print("  ->", a, flush=True)
    return jsonify({"answer": a})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
# ================= MCP server (Streamable HTTP, JSON responses) =================
import hashlib

EMAIL = "23f3004292@ds.study.iitm.ac.in"  # normalized: trimmed + lowercase

def mcp_handle(m):
    method, mid = m.get("method"), m.get("id")
    if mid is None:  # notifications (e.g. notifications/initialized) get no reply
        return None
    if method == "initialize":
        ver = (m.get("params") or {}).get("protocolVersion", "2025-03-26")
        result = {"protocolVersion": ver, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "challenge-server", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{
            "name": "solve_challenge",
            "description": "Returns the first 16 hex chars of SHA-256(challenge:email).",
            "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        # the challenge comes from the HTTP header, NOT the JSON body
        ch = request.headers.get("X-Exam-Challenge", "")
        text = hashlib.sha256(f"{ch}:{EMAIL}".encode()).hexdigest()[:16]
        result = {"content": [{"type": "text", "text": text}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}

@app.route("/mcp", methods=["POST"], strict_slashes=False)
def mcp():
    body = request.get_json(force=True, silent=True)
    if isinstance(body, list):  # JSON-RPC batch
        out = [r for r in (mcp_handle(m) for m in body) if r]
    else:
        out = mcp_handle(body or {})
    return (jsonify(out), 200) if out else ("", 202)