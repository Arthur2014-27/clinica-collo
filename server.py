from dotenv import load_dotenv
from pathlib import Path
import os

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime, timezone, timedelta
from bson import ObjectId
import logging, bcrypt, jwt

client = AsyncIOMotorClient(os.environ['MONGO_URL'])
db = client[os.environ.get('DB_NAME', 'clinica_collo')]

app = FastAPI()
api_router = APIRouter(prefix="/api")

JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGORITHM = "HS256"
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
AI_MODEL = os.environ.get('AI_MODEL', 'claude-sonnet-5-5')

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clinica")

def hash_password(p): return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
def verify_password(p, h): return bcrypt.checkpw(p.encode(), h.encode())
def now(): return datetime.now(timezone.utc).isoformat()

def create_access_token(uid, email, role):
    payload = {"sub": uid, "email": email, "role": role,
               "exp": datetime.now(timezone.utc) + timedelta(days=7), "type": "access"}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "): token = auth[7:]
    if not token: raise HTTPException(401, "Não autenticado")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
    except jwt.ExpiredSignatureError: raise HTTPException(401, "Sessão expirada")
    except Exception: raise HTTPException(401, "Token inválido")
    if not user: raise HTTPException(401, "Usuário não encontrado")
    user["id"] = str(user.pop("_id")); user.pop("password_hash", None)
    return user

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

def serialize(doc): doc = dict(doc); doc["id"] = str(doc.pop("_id")); return doc
def public_user(u): return {"id": u["id"], "name": u["name"], "email": u["email"],
                            "phone": u.get("phone", ""), "role": u["role"]}

# ---- Auth ----
@api_router.post("/auth/register")
async def register(data: RegisterInput):
    email = data.email.lower()
    if len(data.password) < 6: raise HTTPException(400, "Senha muito curta (mínimo 6)")
    if await db.users.find_one({"email": email}): raise HTTPException(400, "Email já cadastrado")
    doc = {"name": data.name, "email": email, "phone": data.phone,
           "password_hash": hash_password(data.password), "role": "patient", "created_at": now()}
    res = await db.users.insert_one(doc); uid = str(res.inserted_id)
    return {"token": create_access_token(uid, email, "patient"),
            "user": {"id": uid, "name": data.name, "email": email, "phone": data.phone, "role": "patient"}}

@api_router.post("/auth/login")
async def login(data: LoginInput):
    email = data.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(401, "Email ou senha incorretos")
    uid = str(user["_id"])
    return {"token": create_access_token(uid, email, user["role"]),
            "user": {"id": uid, "name": user["name"], "email": email,
                     "phone": user.get("phone", ""), "role": user["role"]}}

@api_router.post("/auth/logout")
async def logout(): return {"ok": True}

@api_router.get("/auth/me")
async def me(user=Depends(get_current_user)): return public_user(user)

# ---- IA (Claude) ----
AI_SYSTEM = (
    "Você é a atendente virtual da recepção da Clínica de Psicologia Carolina Collo. "
    "Fale sempre em português do Brasil, com tom acolhedor e profissional. "
    "Ajude a tirar dúvidas e a agendar a primeira consulta coletando nome, telefone e horário. "
    "NÃO ofereça diagnósticos. Não invente preços/endereço/horários; diga que a recepção confirmará. "
    "Seja breve e gentil."
)

async def ask_llm(prompt: str, session_id: str):
    # Prioridade: chave da Anthropic; senão, chave universal da Emergent.
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

async def generate_ai_reply(patient_id):
    history = await db.messages.find({"patient_id": patient_id}).sort("created_at", 1).to_list(50)
    convo = "\n".join(f"{'Paciente' if m['sender']=='patient' else 'Recepção'}: {m['text']}" for m in history)
    try:
        reply = await ask_llm(f"Histórico:\n{convo}\n\nResponda a última mensagem.", f"recep-{patient_id}")
    except Exception as e:
        log.error("Falha na IA: %s", e); return None
    if not reply: return None
    doc = {"patient_id": patient_id, "sender": "ai", "text": reply.strip(),
           "read_by_reception": True, "created_at": now()}
    res = await db.messages.insert_one(doc); doc["_id"] = res.inserted_id
    await db.conversations.update_one({"patient_id": patient_id},
        {"$set": {"last_message": doc["text"], "last_at": doc["created_at"]}})
    return serialize(doc)

# ---- Chat ----
@api_router.post("/chat/messages")
async def patient_send(data: MessageInput, user=Depends(get_current_user)):
    pid = user["id"]
    doc = {"patient_id": pid, "sender": "patient", "text": data.text,
           "read_by_reception": False, "created_at": now()}
    await db.messages.insert_one(doc)
    await db.conversations.update_one({"patient_id": pid},
        {"$set": {"patient_id": pid, "patient_name": user["name"], "phone": user.get("phone", ""),
                  "email": user["email"], "last_message": data.text, "last_at": doc["created_at"]},
         "$setOnInsert": {"ai_enabled": False}}, upsert=True)
    conv = await db.conversations.find_one({"patient_id": pid})
    if conv and conv.get("ai_enabled"): await generate_ai_reply(pid)
    return {"ok": True}

@api_router.get("/chat/messages")
async def patient_messages(user=Depends(get_current_user)):
    msgs = await db.messages.find({"patient_id": user["id"]}).sort("created_at", 1).to_list(500)
    return [serialize(m) for m in msgs]

@api_router.get("/chat/conversations")
async def list_conversations(_=Depends(require_reception)):
    convs = await db.conversations.find().sort("last_at", -1).to_list(500)
    out = []
    for c in convs:
        unread = await db.messages.count_documents(
            {"patient_id": c["patient_id"], "sender": "patient", "read_by_reception": False})
        c = serialize(c); c["unread"] = unread; out.append(c)
    return out

@api_router.get("/chat/conversations/{patient_id}/messages")
async def conv_messages(patient_id: str, _=Depends(require_reception)):
    await db.messages.update_many({"patient_id": patient_id, "sender": "patient"},
        {"$set": {"read_by_reception": True}})
    msgs = await db.messages.find({"patient_id": patient_id}).sort("created_at", 1).to_list(500)
    return [serialize(m) for m in msgs]

@api_router.post("/chat/conversations/{patient_id}/messages")
async def reception_send(patient_id: str, data: MessageInput, _=Depends(require_reception)):
    doc = {"patient_id": patient_id, "sender": "reception", "text": data.text,
           "read_by_reception": True, "created_at": now()}
    res = await db.messages.insert_one(doc)
    await db.conversations.update_one({"patient_id": patient_id},
        {"$set": {"last_message": data.text, "last_at": doc["created_at"]}})
    doc["_id"] = res.inserted_id; return serialize(doc)

@api_router.post("/chat/conversations/{patient_id}/ai-toggle")
async def ai_toggle(patient_id: str, data: AIToggleInput, _=Depends(require_reception)):
    await db.conversations.update_one({"patient_id": patient_id},
        {"$set": {"ai_enabled": data.enabled}}, upsert=True)
    reply = None
    if data.enabled:
        last = await db.messages.find({"patient_id": patient_id}).sort("created_at", -1).to_list(1)
        if last and last[0]["sender"] == "patient": reply = await generate_ai_reply(patient_id)
    return {"ai_enabled": data.enabled, "reply": reply}

# ---- Agenda ----
@api_router.get("/appointments")
async def get_appointments(_=Depends(require_reception)):
    items = await db.appointments.find().sort([("date", 1), ("time", 1)]).to_list(1000)
    return [serialize(a) for a in items]

@api_router.post("/appointments")
async def create_appointment(data: AppointmentInput, _=Depends(require_reception)):
    if await db.appointments.find_one({"date": data.date, "time": data.time}):
        raise HTTPException(409, "Já existe uma consulta neste horário")
    doc = data.model_dump(); doc["created_at"] = now()
    res = await db.appointments.insert_one(doc); doc["_id"] = res.inserted_id
    return serialize(doc)

@api_router.delete("/appointments/{appt_id}")
async def delete_appointment(appt_id: str, _=Depends(require_reception)):
    try: oid = ObjectId(appt_id)
    except Exception: raise HTTPException(400, "ID inválido")
    await db.appointments.delete_one({"_id": oid}); return {"ok": True}

@api_router.get("/health")
async def health(): return {"ok": True}

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.messages.create_index([("patient_id", 1), ("created_at", 1)])
    for ek, pk in [("RECEPTION_EMAIL", "RECEPTION_PASSWORD"), ("OWNER_EMAIL", "OWNER_PASSWORD")]:
        email = os.environ.get(ek, "").lower(); pw = os.environ.get(pk)
        if not email or not pw: continue
        existing = await db.users.find_one({"email": email})
        if existing is None:
            await db.users.insert_one({"name": "Recepção Carolina Collo", "email": email, "phone": "",
                "password_hash": hash_password(pw), "role": "reception", "created_at": now()})
        elif existing.get("role") != "reception" or not verify_password(pw, existing["password_hash"]):
            await db.users.update_one({"email": email},
                {"$set": {"role": "reception", "password_hash": hash_password(pw)}})

@app.on_event("shutdown")
async def shutdown(): client.close()

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_credentials=True,
    allow_origins=[o for o in [os.environ.get("FRONTEND_URL")] if o] or ["http://localhost:8000"],
    allow_methods=["*"], allow_headers=["*"])
# Frontend servido pelo próprio backend (mesma origem, sem CORS). Deve ficar por último.
app.mount("/", StaticFiles(directory=ROOT_DIR / "static", html=True), name="static")
