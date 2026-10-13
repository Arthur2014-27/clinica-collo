from dotenv import load_dotenv
from pathlib import Path
import os

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit
import logging, bcrypt, jwt, asyncpg, httpx

DATABASE_URL = os.environ['DATABASE_URL']
JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGORITHM = "HS256"
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
GROQ_MODELS = [m.strip() for m in os.environ.get('GROQ_MODELS', 'openai/gpt-oss-120b,openai/gpt-oss-20b').split(',') if m.strip()]
FALLBACK_REPLY = ('Olá! Recebemos sua mensagem. Para agendar sua consulta, nos envie seu nome completo, '
                  'telefone e o melhor horário. A recepção confirmará os detalhes em breve.')
AI_MODEL = os.environ.get('AI_MODEL', 'claude-sonnet-5-5')

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clinica")

app = FastAPI()
api_router = APIRouter(prefix="/api")
pool = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT DEFAULT '',
  password_hash TEXT NOT NULL, role TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS messages(
  id SERIAL PRIMARY KEY, patient_id INT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL,
  read_by_reception BOOLEAN DEFAULT false, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS messages_patient_idx ON messages(patient_id, id);
CREATE TABLE IF NOT EXISTS conversations(
  patient_id INT PRIMARY KEY, patient_name TEXT DEFAULT '', phone TEXT DEFAULT '', email TEXT DEFAULT '',
  last_message TEXT DEFAULT '', last_at TIMESTAMPTZ DEFAULT now(), ai_enabled BOOLEAN DEFAULT false);
CREATE TABLE IF NOT EXISTS appointments(
  id SERIAL PRIMARY KEY, patient_name TEXT NOT NULL, phone TEXT NOT NULL, date TEXT NOT NULL, time TEXT NOT NULL,
  modality TEXT DEFAULT 'Presencial', notes TEXT DEFAULT '', patient_id TEXT, created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(date, time));
"""

def hash_password(p): return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
def verify_password(p, h): return bcrypt.checkpw(p.encode(), h.encode())
def iso(v): return v.isoformat() if isinstance(v, datetime) else v
def to_int(v):
    try: return int(v)
    except Exception: raise HTTPException(400, "ID inválido")

def create_access_token(uid, email, role):
    payload = {"sub": str(uid), "email": email, "role": role,
               "exp": datetime.now(timezone.utc) + timedelta(days=7), "type": "access"}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "): raise HTTPException(401, "Não autenticado")
    try:
        payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
        u = await pool.fetchrow("SELECT id,name,email,phone,role FROM users WHERE id=$1", int(payload["sub"]))
    except jwt.ExpiredSignatureError: raise HTTPException(401, "Sessão expirada")
    except Exception: raise HTTPException(401, "Token inválido")
    if not u: raise HTTPException(401, "Usuário não encontrado")
    return {"id": str(u["id"]), "name": u["name"], "email": u["email"], "phone": u["phone"] or "", "role": u["role"]}

async def require_reception(user=Depends(get_current_user)):
    if user.get("role") != "reception": raise HTTPException(403, "Acesso restrito à recepção")
    return user

class RegisterInput(BaseModel):
    name: str; email: EmailStr; phone: str; password: str
class LoginInput(BaseModel):
    email: EmailStr; password: str
class MessageInput(BaseModel): text: str
class AIToggleInput(BaseModel): enabled: bool
class AppointmentInput(BaseModel):
    patient_name: str; phone: str; date: str; time: str
    modality: str = "Presencial"; notes: Optional[str] = ""; patient_id: Optional[str] = None

def msg_out(r): return {"id": str(r["id"]), "sender": r["sender"], "text": r["text"], "created_at": iso(r["created_at"])}
def appt_out(r):
    return {"id": str(r["id"]), "patient_name": r["patient_name"], "phone": r["phone"], "date": r["date"],
            "time": r["time"], "modality": r["modality"], "notes": r["notes"] or "", "patient_id": r["patient_id"]}

# ---- Auth ----
@api_router.post("/auth/register")
async def register(data: RegisterInput):
    email = data.email.lower()
    if len(data.password) < 6: raise HTTPException(400, "Senha muito curta (mínimo 6)")
    if await pool.fetchrow("SELECT 1 FROM users WHERE email=$1", email): raise HTTPException(400, "Email já cadastrado")
    uid = await pool.fetchval("INSERT INTO users(name,email,phone,password_hash,role) VALUES($1,$2,$3,$4,'patient') RETURNING id",
                              data.name, email, data.phone, hash_password(data.password))
    return {"token": create_access_token(uid, email, "patient"),
            "user": {"id": str(uid), "name": data.name, "email": email, "phone": data.phone, "role": "patient"}}

@api_router.post("/auth/login")
async def login(data: LoginInput):
    email = data.email.lower()
    u = await pool.fetchrow("SELECT * FROM users WHERE email=$1", email)
    if not u or not verify_password(data.password, u["password_hash"]):
        raise HTTPException(401, "Email ou senha incorretos")
    return {"token": create_access_token(u["id"], email, u["role"]),
            "user": {"id": str(u["id"]), "name": u["name"], "email": email, "phone": u["phone"] or "", "role": u["role"]}}

@api_router.post("/auth/logout")
async def logout(): return {"ok": True}

@api_router.get("/auth/me")
async def me(user=Depends(get_current_user)): return user

# ---- IA ----
AI_SYSTEM = (
    "Você é a atendente virtual da recepção da Clínica de Psicologia Carolina Collo. "
    "Fale sempre em português do Brasil, com tom acolhedor e profissional. "
    "Ajude a tirar dúvidas e a agendar a primeira consulta coletando nome, telefone e horário. "
    "NÃO ofereça diagnósticos. Não invente preços/endereço/horários; diga que a recepção confirmará. "
    "Seja breve e gentil."
)

async def ask_groq(prompt: str):
    # Cada modelo tem limite próprio no Groq: se um estourar, tenta o próximo.
    last = None
    async with httpx.AsyncClient(timeout=30) as http:
        for model in GROQ_MODELS:
            try:
                r = await http.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": model, "max_tokens": 800, "temperature": 0.5,
                          "messages": [{"role": "system", "content": AI_SYSTEM},
                                       {"role": "user", "content": prompt}]})
                if r.status_code != 200:
                    last = f"{model}: HTTP {r.status_code} {r.text[:200]}"; log.warning("Groq falhou: %s", last); continue
                text = (r.json()["choices"][0]["message"].get("content") or "").strip()
                if text: return text
                last = f"{model}: resposta vazia"
            except Exception as e:
                last = f"{model}: {e}"; log.warning("Groq erro: %s", last)
    raise RuntimeError(last or "Groq sem modelos configurados")

async def ask_llm(prompt: str, session_id: str):
    if GROQ_API_KEY: return await ask_groq(prompt)
    if ANTHROPIC_API_KEY:
        from anthropic import AsyncAnthropic
        c = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        r = await c.messages.create(model=AI_MODEL, max_tokens=500, system=AI_SYSTEM,
                                    messages=[{"role": "user", "content": prompt}])
        return r.content[0].text
    if EMERGENT_LLM_KEY:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(api_key=EMERGENT_LLM_KEY, session_id=session_id,
                       system_message=AI_SYSTEM).with_model("anthropic", AI_MODEL)
        return await chat.send_message(UserMessage(text=prompt))
    return None

async def generate_ai_reply(pid: int):
    rows = await pool.fetch("SELECT sender,text FROM (SELECT * FROM messages WHERE patient_id=$1 ORDER BY id DESC LIMIT 50) t ORDER BY id", pid)
    convo = "\n".join(f"{'Paciente' if r['sender']=='patient' else 'Recepção'}: {r['text']}" for r in rows)
    try:
        reply = await ask_llm(f"Histórico:\n{convo}\n\nResponda a última mensagem.", f"recep-{pid}")
    except Exception as e:
        log.error("Falha na IA: %s", e); reply = FALLBACK_REPLY
    if not reply: return None
    reply = reply.strip()
    r = await pool.fetchrow("INSERT INTO messages(patient_id,sender,text,read_by_reception) VALUES($1,'ai',$2,true) RETURNING *", pid, reply)
    await pool.execute("UPDATE conversations SET last_message=$2, last_at=now() WHERE patient_id=$1", pid, reply)
    return msg_out(r)

# ---- Chat ----
@api_router.post("/chat/messages")
async def patient_send(data: MessageInput, user=Depends(get_current_user)):
    pid = int(user["id"])
    await pool.execute("INSERT INTO messages(patient_id,sender,text,read_by_reception) VALUES($1,'patient',$2,false)", pid, data.text)
    ai = await pool.fetchval("""
        INSERT INTO conversations(patient_id,patient_name,phone,email,last_message,last_at) VALUES($1,$2,$3,$4,$5,now())
        ON CONFLICT (patient_id) DO UPDATE SET patient_name=$2, phone=$3, email=$4, last_message=$5, last_at=now()
        RETURNING ai_enabled""", pid, user["name"], user["phone"], user["email"], data.text)
    if ai: await generate_ai_reply(pid)
    return {"ok": True}

@api_router.get("/chat/messages")
async def patient_messages(user=Depends(get_current_user)):
    rows = await pool.fetch("SELECT * FROM messages WHERE patient_id=$1 ORDER BY id LIMIT 500", int(user["id"]))
    return [msg_out(r) for r in rows]

@api_router.get("/chat/conversations")
async def list_conversations(_=Depends(require_reception)):
    rows = await pool.fetch("""
        SELECT c.*, (SELECT count(*) FROM messages m WHERE m.patient_id=c.patient_id
                     AND m.sender='patient' AND NOT m.read_by_reception) AS unread
        FROM conversations c ORDER BY c.last_at DESC LIMIT 500""")
    return [{"id": str(r["patient_id"]), "patient_id": str(r["patient_id"]), "patient_name": r["patient_name"],
             "phone": r["phone"], "email": r["email"], "last_message": r["last_message"],
             "last_at": iso(r["last_at"]), "ai_enabled": r["ai_enabled"], "unread": r["unread"]} for r in rows]

@api_router.get("/chat/conversations/{patient_id}/messages")
async def conv_messages(patient_id: str, _=Depends(require_reception)):
    pid = to_int(patient_id)
    await pool.execute("UPDATE messages SET read_by_reception=true WHERE patient_id=$1 AND sender='patient'", pid)
    rows = await pool.fetch("SELECT * FROM messages WHERE patient_id=$1 ORDER BY id LIMIT 500", pid)
    return [msg_out(r) for r in rows]

@api_router.post("/chat/conversations/{patient_id}/messages")
async def reception_send(patient_id: str, data: MessageInput, _=Depends(require_reception)):
    pid = to_int(patient_id)
    r = await pool.fetchrow("INSERT INTO messages(patient_id,sender,text,read_by_reception) VALUES($1,'reception',$2,true) RETURNING *", pid, data.text)
    await pool.execute("UPDATE conversations SET last_message=$2, last_at=now() WHERE patient_id=$1", pid, data.text)
    return msg_out(r)

@api_router.post("/chat/conversations/{patient_id}/ai-toggle")
async def ai_toggle(patient_id: str, data: AIToggleInput, _=Depends(require_reception)):
    pid = to_int(patient_id)
    await pool.execute("""INSERT INTO conversations(patient_id,ai_enabled) VALUES($1,$2)
                          ON CONFLICT (patient_id) DO UPDATE SET ai_enabled=$2""", pid, data.enabled)
    reply = None
    if data.enabled:
        last = await pool.fetchval("SELECT sender FROM messages WHERE patient_id=$1 ORDER BY id DESC LIMIT 1", pid)
        if last == "patient": reply = await generate_ai_reply(pid)
    return {"ai_enabled": data.enabled, "reply": reply}

# ---- Agenda ----
@api_router.get("/appointments")
async def get_appointments(_=Depends(require_reception)):
    rows = await pool.fetch("SELECT * FROM appointments ORDER BY date, time LIMIT 1000")
    return [appt_out(r) for r in rows]

@api_router.post("/appointments")
async def create_appointment(data: AppointmentInput, _=Depends(require_reception)):
    try:
        r = await pool.fetchrow("""INSERT INTO appointments(patient_name,phone,date,time,modality,notes,patient_id)
                                   VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
                                data.patient_name, data.phone, data.date, data.time, data.modality, data.notes or "", data.patient_id)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Já existe uma consulta neste horário")
    return appt_out(r)

@api_router.delete("/appointments/{appt_id}")
async def delete_appointment(appt_id: str, _=Depends(require_reception)):
    await pool.execute("DELETE FROM appointments WHERE id=$1", to_int(appt_id)); return {"ok": True}

@api_router.get("/health")
async def health(): return {"ok": True}

@app.on_event("startup")
async def startup():
    global pool
    p = urlsplit(DATABASE_URL)
    dsn = urlunsplit((p.scheme, p.netloc, p.path, "", ""))  # remove ?sslmode=... etc; SSL vai abaixo
    local = p.hostname in ("localhost", "127.0.0.1")
    pool = await asyncpg.create_pool(dsn, ssl=None if local else "require", min_size=1, max_size=5,
                                     statement_cache_size=0, max_inactive_connection_lifetime=30)
    await pool.execute(SCHEMA)
    for ek, pk in [("RECEPTION_EMAIL", "RECEPTION_PASSWORD"), ("OWNER_EMAIL", "OWNER_PASSWORD")]:
        email = os.environ.get(ek, "").lower(); pw = os.environ.get(pk)
        if not email or not pw: continue
        u = await pool.fetchrow("SELECT role,password_hash FROM users WHERE email=$1", email)
        if u is None:
            await pool.execute("INSERT INTO users(name,email,phone,password_hash,role) VALUES('Recepção Carolina Collo',$1,'',$2,'reception')",
                               email, hash_password(pw))
        elif u["role"] != "reception" or not verify_password(pw, u["password_hash"]):
            await pool.execute("UPDATE users SET role='reception', password_hash=$2 WHERE email=$1", email, hash_password(pw))

@app.on_event("shutdown")
async def shutdown():
    if pool: await pool.close()

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_credentials=True,
    allow_origins=[o for o in [os.environ.get("FRONTEND_URL")] if o] or ["http://localhost:8000"],
    allow_methods=["*"], allow_headers=["*"])

INDEX = ROOT_DIR / "index.html"
if not INDEX.exists(): INDEX = ROOT_DIR / "static" / "index.html"

@app.get("/", include_in_schema=False)
async def index(): return FileResponse(INDEX)
