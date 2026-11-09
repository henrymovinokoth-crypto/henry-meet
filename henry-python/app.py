"""Henry - free video meetings, paid upgrades via M-Pesa.
Python (FastAPI) server: serves the site, relays WebRTC signalling over WebSockets,
enforces plan limits, and handles M-Pesa payments (Daraja STK push or manual approval)."""
import os, re, json, time, hmac, base64, hashlib, secrets, asyncio
from contextlib import asynccontextmanager
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)   # set a fixed one in production!
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
PAY_NAME = os.getenv("PAY_NAME", "Henry Okoth")
PAY_NUMBER = os.getenv("PAY_NUMBER", "0181288331")
PLANS = {"Plus": {"price": 250, "days": 30}, "Pro": {"price": 700, "days": 30}}
LIMITS = {"Free": 10, "Plus": 12, "Pro": 16}        # people per meeting (video quality is lowered automatically in bigger calls)
FREE_MINUTES = 40
# Daraja (optional). Needs a Paybill or Till from Safaricom - a personal number cannot receive STK pushes.
MP = {k: os.getenv(k, "") for k in ("MPESA_KEY", "MPESA_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_TILL", "PUBLIC_URL")}
MP_ENV = os.getenv("MPESA_ENV", "sandbox")
MP_BASE = "https://api.safaricom.co.ke" if MP_ENV == "production" else "https://sandbox.safaricom.co.ke"
STK_ON = all(MP[k] for k in ("MPESA_KEY", "MPESA_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "PUBLIC_URL"))
CB_SECRET = hashlib.sha256((SECRET_KEY + "cb").encode()).hexdigest()[:24]

# ---------- licence tokens (signed, stateless: survive restarts) ----------
def b64(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
def unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def sign(body): return hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
def make_token(plan, days, name=""):
    body = b64(json.dumps({"p": plan, "e": int(time.time() + days * 86400), "n": name[:40]}).encode())
    return body + "." + sign(body)
def verify_token(tok):
    try:
        body, sig = str(tok).split(".")
        if not hmac.compare_digest(sig, sign(body)): return None
        d = json.loads(unb64(body))
        return d if d["e"] > time.time() and d["p"] in PLANS else None
    except Exception:
        return None

# ---------- in-memory state ----------
PAYMENTS = {}          # CheckoutRequestID -> {status, plan, phone, token}
CLAIMS = {}            # claim id -> {name, phone, code, plan, status, token, at}
USED_CODES = set()
HITS = {}
ROOMS = {}

def rate(ip, limit=6, per=60):
    now = time.time(); h = [t for t in HITS.get(ip, []) if now - t < per]
    if len(h) >= limit: raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
    h.append(now); HITS[ip] = h

def clean(s, n):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(s or ""))[:n].strip()

def norm_phone(p):
    p = re.sub(r"[\s+-]", "", str(p or ""))
    if re.fullmatch(r"0[17]\d{8}", p): p = "254" + p[1:]
    if not re.fullmatch(r"254[17]\d{8}", p): raise HTTPException(400, "Enter a valid Kenyan phone number, e.g. 0712345678")
    return p

class Room:
    def __init__(self, code):
        self.code, self.clients, self.host, self.locked = code, {}, None, False
        self.plan, self.created, self.warned = "Free", time.time(), False
    @property
    def ends(self): return None if self.plan != "Free" else self.created + FREE_MINUTES * 60
    def info(self): return {"t": "room", "host": self.host, "plan": self.plan, "ends": self.ends, "locked": self.locked, "limit": LIMITS[self.plan]}

async def send(c, msg):
    try: await c["ws"].send_text(json.dumps(msg))
    except Exception: pass

async def broadcast(r, msg, skip=None):
    await asyncio.gather(*[send(c, msg) for i, c in list(r.clients.items()) if i != skip])

async def ticker():
    while True:
        await asyncio.sleep(10)
        now = time.time()
        for r in list(ROOMS.values()):
            if r.ends and now >= r.ends:
                await broadcast(r, {"t": "timeup"})
                for c in list(r.clients.values()):
                    try: await c["ws"].close()
                    except Exception: pass
            elif r.ends and r.ends - now < 300 and not r.warned:
                r.warned = True
                await broadcast(r, {"t": "warn", "m": "5 minutes left on the free plan. Upgrade for unlimited time."})

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(ticker()); yield; task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------- pages ----------
@app.get("/")
def home(): return FileResponse("static/index.html")
@app.get("/r/{code}")
def room_page(code: str): return FileResponse("static/index.html")
@app.get("/admin")
def admin_page(): return FileResponse("static/admin.html")
@app.get("/health")
def health(): return {"ok": True}

@app.get("/api/config")
def config():
    ice = [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]
    if os.getenv("TURN_URL"):
        ice.append({"urls": os.getenv("TURN_URL").split(","), "username": os.getenv("TURN_USER", ""), "credential": os.getenv("TURN_PASS", "")})
    return {"stk": STK_ON, "ice": ice, "plans": PLANS, "limits": LIMITS, "free_minutes": FREE_MINUTES, "pay": {"name": PAY_NAME, "number": PAY_NUMBER}}

@app.get("/api/me")
def me(token: str = ""):
    d = verify_token(token)
    return {"plan": d["p"], "until": d["e"], "name": d["n"]} if d else {"plan": "Free"}

# ---------- M-Pesa: automatic STK push (needs Paybill/Till) ----------
def mpesa_access():
    r = requests.get(MP_BASE + "/oauth/v1/generate?grant_type=client_credentials", auth=(MP["MPESA_KEY"], MP["MPESA_SECRET"]), timeout=20)
    r.raise_for_status(); return r.json()["access_token"]

@app.post("/api/pay")
def pay(body: dict, request: Request):
    if not STK_ON: raise HTTPException(503, "Automatic payment is not set up yet. Use Send money.")
    rate(request.client.host if request.client else "x", 4)
    plan = body.get("plan")
    if plan not in PLANS: raise HTTPException(400, "Unknown plan")
    phone = norm_phone(body.get("phone"))
    ts = time.strftime("%Y%m%d%H%M%S")
    pw = base64.b64encode((MP["MPESA_SHORTCODE"] + MP["MPESA_PASSKEY"] + ts).encode()).decode()
    till = bool(MP["MPESA_TILL"])
    payload = {"BusinessShortCode": MP["MPESA_SHORTCODE"], "Password": pw, "Timestamp": ts,
               "TransactionType": "CustomerBuyGoodsOnline" if till else "CustomerPayBillOnline",
               "Amount": PLANS[plan]["price"], "PartyA": phone, "PartyB": MP["MPESA_TILL"] or MP["MPESA_SHORTCODE"],
               "PhoneNumber": phone, "CallBackURL": MP["PUBLIC_URL"].rstrip("/") + "/mpesa/callback/" + CB_SECRET,
               "AccountReference": "Henry", "TransactionDesc": "Henry " + plan}
    try:
        r = requests.post(MP_BASE + "/mpesa/stkpush/v1/processrequest", json=payload, headers={"Authorization": "Bearer " + mpesa_access()}, timeout=25).json()
    except Exception:
        raise HTTPException(502, "Could not reach M-Pesa. Try again.")
    if r.get("ResponseCode") != "0": raise HTTPException(400, r.get("errorMessage") or r.get("ResponseDescription") or "M-Pesa refused the request")
    cid = r["CheckoutRequestID"]; PAYMENTS[cid] = {"status": "pending", "plan": plan, "phone": phone}
    return {"id": cid}

@app.post("/mpesa/callback/{secret}")
async def callback(secret: str, request: Request):
    if not hmac.compare_digest(secret, CB_SECRET): raise HTTPException(403)
    try:
        cb = (await request.json())["Body"]["stkCallback"]; p = PAYMENTS.get(cb["CheckoutRequestID"])
        if p and cb["ResultCode"] == 0:
            items = {i["Name"]: i.get("Value") for i in cb["CallbackMetadata"]["Item"]}
            receipt = str(items.get("MpesaReceiptNumber"))
            if receipt not in USED_CODES and float(items.get("Amount", 0)) >= PLANS[p["plan"]]["price"]:
                USED_CODES.add(receipt); p.update(status="paid", token=make_token(p["plan"], PLANS[p["plan"]]["days"]))
            else: p["status"] = "failed"
        elif p: p["status"] = "failed"
    except Exception: pass
    return {"ResultCode": 0, "ResultDesc": "Accepted"}

@app.get("/api/pay/{cid}")
def pay_status(cid: str):
    p = PAYMENTS.get(cid)
    if not p: raise HTTPException(404)
    return {"status": p["status"], "token": p.get("token")}

# ---------- M-Pesa: manual send-money + admin approval ----------
@app.post("/api/claim")
def claim(body: dict, request: Request):
    rate(request.client.host if request.client else "x", 5)
    plan = body.get("plan"); code = clean(body.get("code"), 10).upper()
    if plan not in PLANS: raise HTTPException(400, "Unknown plan")
    if not re.fullmatch(r"[A-Z0-9]{10}", code): raise HTTPException(400, "The M-Pesa code has 10 letters and numbers.")
    cid = secrets.token_urlsafe(12)
    CLAIMS[cid] = {"name": clean(body.get("name"), 40) or "Guest", "phone": norm_phone(body.get("phone")), "code": code, "plan": plan, "status": "pending", "at": int(time.time())}
    return {"id": cid}

@app.get("/api/claim/{cid}")
def claim_status(cid: str):
    c = CLAIMS.get(cid)
    if not c: raise HTTPException(404)
    return {"status": c["status"], "token": c.get("token")}

def need_admin(request: Request):
    pw = request.headers.get("x-admin", "")
    if not ADMIN_PASSWORD or not hmac.compare_digest(pw, ADMIN_PASSWORD): raise HTTPException(401, "Wrong admin password")

@app.get("/api/admin/claims")
def admin_claims(request: Request):
    need_admin(request)
    counts = {}
    for c in CLAIMS.values(): counts[c["code"]] = counts.get(c["code"], 0) + 1
    return [dict(c, id=i, dup=counts[c["code"]] > 1 or c["code"] in USED_CODES) for i, c in sorted(CLAIMS.items(), key=lambda x: -x[1]["at"]) if c["status"] == "pending"]

@app.post("/api/admin/decide")
def admin_decide(body: dict, request: Request):
    need_admin(request); c = CLAIMS.get(body.get("id"))
    if not c: raise HTTPException(404)
    if body.get("approve"):
        USED_CODES.add(c["code"]); c.update(status="approved", token=make_token(c["plan"], PLANS[c["plan"]]["days"], c["name"]))
    else: c["status"] = "rejected"
    return {"ok": True}

@app.post("/api/admin/issue")
def admin_issue(body: dict, request: Request):
    need_admin(request)
    if body.get("plan") not in PLANS: raise HTTPException(400, "Unknown plan")
    return {"token": make_token(body["plan"], int(body.get("days") or 30), clean(body.get("name"), 40))}

# ---------- meeting signalling ----------
EMOJI = {"👏", "😂", "🎉", "✋", "❤️", "👍"}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    r = cid = None
    try:
        first = json.loads(await ws.receive_text())
        code = re.sub(r"[^a-z0-9-]", "", str(first.get("room", "")).lower())[:40]
        if first.get("t") != "join" or len(code) < 3:
            await ws.send_text(json.dumps({"t": "error", "m": "Invalid meeting code"})); return await ws.close()
        d = verify_token(first.get("token")); plan = d["p"] if d else "Free"
        r = ROOMS.get(code) or ROOMS.setdefault(code, Room(code))
        if r.clients and r.locked:
            await ws.send_text(json.dumps({"t": "error", "m": "This meeting is locked by the host"})); return await ws.close()
        if len(r.clients) >= LIMITS[r.plan]:
            await ws.send_text(json.dumps({"t": "error", "m": f"This meeting is full ({LIMITS[r.plan]} people on this plan)"})); return await ws.close()
        cid = secrets.token_hex(4)
        me = {"ws": ws, "name": clean(first.get("name"), 30) or "Guest", "plan": plan, "hand": False, "mic": True, "cam": True}
        if not r.clients: r.host, r.plan, r.created, r.warned = cid, plan, time.time(), False
        peers = [{"id": i, "name": c["name"], "plan": c["plan"], "hand": c["hand"], "mic": c["mic"], "cam": c["cam"]} for i, c in r.clients.items()]
        r.clients[cid] = me
        await send(me, {"t": "welcome", "id": cid, "peers": peers, "plan": plan, "room": r.info()})
        await broadcast(r, {"t": "joined", "peer": {"id": cid, "name": me["name"], "plan": plan, "hand": False, "mic": True, "cam": True}}, skip=cid)
        while True:
            raw = await ws.receive_text()
            if len(raw) > 40000: continue
            m = json.loads(raw); t = m.get("t")
            if t == "sig":
                to = r.clients.get(m.get("to"))
                if to: await send(to, {"t": "sig", "from": cid, "d": m.get("d")})
            elif t == "chat":
                txt = clean(m.get("m"), 500)
                if txt: await broadcast(r, {"t": "chat", "from": cid, "name": me["name"], "m": txt})
            elif t == "react" and m.get("e") in EMOJI:
                await broadcast(r, {"t": "react", "from": cid, "name": me["name"], "e": m["e"]})
            elif t == "hand":
                me["hand"] = bool(m.get("on")); await broadcast(r, {"t": "hand", "from": cid, "on": me["hand"], "name": me["name"]})
            elif t == "state":
                me["mic"], me["cam"] = bool(m.get("mic")), bool(m.get("cam"))
                await broadcast(r, {"t": "state", "from": cid, "mic": me["mic"], "cam": me["cam"]}, skip=cid)
            elif t == "cap" and plan != "Free":
                txt = clean(m.get("m"), 300)
                if txt: await broadcast(r, {"t": "cap", "from": cid, "name": me["name"], "m": txt})
            elif cid == r.host and t == "kick" and m.get("id") in r.clients and m.get("id") != cid:
                v = r.clients[m["id"]]; await send(v, {"t": "kicked"})
                try: await v["ws"].close()
                except Exception: pass
            elif cid == r.host and t == "lock":
                r.locked = bool(m.get("on")); await broadcast(r, r.info())
            elif cid == r.host and t == "muteall":
                await broadcast(r, {"t": "muteall"}, skip=cid)
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        if r and cid and cid in r.clients:
            r.clients.pop(cid, None)
            if not r.clients: ROOMS.pop(r.code, None)
            else:
                if r.host == cid:
                    r.host = next(iter(r.clients)); r.plan = r.clients[r.host]["plan"]; r.created = r.created
                    await broadcast(r, r.info())
                await broadcast(r, {"t": "left", "id": cid})
