from dotenv import load_dotenv
from pathlib import Path
import os, time

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit
import json, logging, bcrypt, jwt, asyncpg, httpx
from zoneinfo import ZoneInfo

DATABASE_URL = os.environ['DATABASE_URL']
JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGORITHM = "HS256"
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
GROQ_MODELS = [m.strip() for m in os.environ.get('GROQ_MODELS', 'openai/gpt-oss-120b,openai/gpt-oss-20b').split(',') if m.strip()]
FALLBACK_REPLY = 'Olá! Recebemos sua mensagem. A recepção responderá em breve.'
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
  last_message TEXT DEFAULT '', last_at TIMESTAMPTZ DEFAULT now(), ai_enabled BOOLEAN);
ALTER TABLE conversations ALTER COLUMN ai_enabled DROP DEFAULT;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS bans(id SERIAL PRIMARY KEY, ip TEXT NOT NULL, until_at TIMESTAMPTZ NOT NULL, patient_name TEXT DEFAULT '');
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_ip TEXT;
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
    if user.get("role") not in ("reception", "admin"): raise HTTPException(403, "Acesso restrito à recepção")
    return user

async def require_admin(user=Depends(get_current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Acesso restrito ao administrador")
    return user

class RegisterInput(BaseModel):
    name: str; email: EmailStr; phone: str; password: str
class LoginInput(BaseModel):
    email: str; password: str
class MessageInput(BaseModel): text: str
class AIToggleInput(BaseModel): enabled: bool
class AppointmentInput(BaseModel):
    patient_name: str; phone: str; date: str; time: str
    modality: str = "Presencial"; notes: Optional[str] = ""; patient_id: Optional[str] = None

def msg_out(r): return {"id": str(r["id"]), "sender": r["sender"], "text": r["text"], "created_at": iso(r["created_at"])}
def appt_out(r):
    return {"id": str(r["id"]), "patient_name": r["patient_name"], "phone": r["phone"], "date": r["date"],
            "time": r["time"], "modality": r["modality"], "notes": r["notes"] or "", "patient_id": r["patient_id"]}

# ---- IP, bloqueio e dados do site ----
FAILS = {}
SITE_DEFAULT = {"site_name": "SUA_EMPRESA", "logo": "", "about": "", "services": "", "hours_text": "", "address": "", "contact": ""}
SITE = dict(SITE_DEFAULT)

def client_ip(request):
    x = request.headers.get("x-forwarded-for")
    return x.split(",")[0].strip() if x else (request.client.host if request.client else "unknown")
def check_lock(ip):
    f = FAILS.get(ip)
    if f and f[0] >= 5 and time.time() - f[1] < 900: raise HTTPException(429, "Muitas tentativas. Tente novamente em 15 minutos.")
def reg_fail(ip):
    f = FAILS.get(ip)
    FAILS[ip] = (1, time.time()) if not f or time.time() - f[1] >= 900 else (f[0] + 1, f[1])

async def load_site():
    SITE.clear(); SITE.update(SITE_DEFAULT); SITE.update(await get_setting("site", {}) or {}); return SITE

def sys_prompt():
    info = "; ".join(f"{l}: {SITE[k].replace(chr(10), '; ')}" for k, l in (("about", "Sobre"), ("services", "Atendimento"),
        ("hours_text", "Funcionamento"), ("address", "Endereço"), ("contact", "Contato")) if SITE.get(k, "").strip())
    return (AI_SYSTEM.replace("Clínica de Psicologia Carolina Collo", f"clínica de psicologia {SITE['site_name']}")
            + (f" Informações oficiais da clínica (use somente estas): {info}." if info else ""))

# ---- Auth ----
@api_router.post("/auth/register")
async def register(data: RegisterInput, request: Request):
    email = data.email.lower()
    if len(data.password) < 6: raise HTTPException(400, "Senha muito curta (mínimo 6)")
    if await pool.fetchrow("SELECT 1 FROM users WHERE email=$1", email): raise HTTPException(400, "Email já cadastrado")
    uid = await pool.fetchval("INSERT INTO users(name,email,phone,password_hash,role) VALUES($1,$2,$3,$4,'patient') RETURNING id",
                              data.name, email, data.phone, hash_password(data.password))
    await pool.execute("UPDATE users SET last_ip=$2 WHERE id=$1", uid, client_ip(request))
    return {"token": create_access_token(uid, email, "patient"),
            "user": {"id": str(uid), "name": data.name, "email": email, "phone": data.phone, "role": "patient"}}

@api_router.post("/auth/login")
async def login(data: LoginInput, request: Request):
    ip = client_ip(request); check_lock(ip)
    email = data.email.strip().lower()
    u = await pool.fetchrow("SELECT * FROM users WHERE email=$1", email)
    if not u or not verify_password(data.password, u["password_hash"]):
        reg_fail(ip); raise HTTPException(401, "Usuário ou senha incorretos")
    FAILS.pop(ip, None)
    await pool.execute("UPDATE users SET last_ip=$2 WHERE id=$1", u["id"], ip)
    return {"token": create_access_token(u["id"], email, u["role"]),
            "user": {"id": str(u["id"]), "name": u["name"], "email": email, "phone": u["phone"] or "", "role": u["role"]}}

@api_router.post("/auth/logout")
async def logout(): return {"ok": True}

@api_router.get("/auth/me")
async def me(user=Depends(get_current_user)): return user

# ---- IA (Claude) ----
AI_SYSTEM = (
    "Você é a atendente virtual da recepção da Clínica de Psicologia Carolina Collo. "
    "Fale sempre em português do Brasil, com tom acolhedor e profissional. "
    "Ajude a tirar dúvidas e a agendar a primeira consulta coletando nome, telefone e horário. "
    "NÃO ofereça diagnósticos. Nunca invente nomes de profissionais, preços, endereço, convênios ou outras informações não fornecidas; diga que a recepção confirmará. "
    "Para horários, use SOMENTE a lista de 'Horários livres' enviada na mensagem; se não houver lista, diga que a recepção confirmará. "
    "Você agenda a consulta: ofereça os dias e horários livres da lista, confirme com o paciente o dia, o horário e se prefere Presencial ou Online, e então agende no formato JSON pedido. "
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
                          "messages": [{"role": "system", "content": sys_prompt()},
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
        r = await c.messages.create(model=AI_MODEL, max_tokens=500, system=sys_prompt(),
                                    messages=[{"role": "user", "content": prompt}])
        return r.content[0].text
    if EMERGENT_LLM_KEY:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(api_key=EMERGENT_LLM_KEY, session_id=session_id,
                       system_message=sys_prompt()).with_model("anthropic", AI_MODEL)
        return await chat.send_message(UserMessage(text=prompt))
    return None

# ---- Configurações e disponibilidade da agenda ----
WEEKDAYS = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']
SP = ZoneInfo("America/Sao_Paulo")

async def get_setting(key, default=None):
    v = await pool.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return json.loads(v) if v is not None else default

async def set_setting(key, value):
    await pool.execute("INSERT INTO settings(key,value) VALUES($1,$2::jsonb) ON CONFLICT (key) DO UPDATE SET value=$2::jsonb",
                       key, json.dumps(value))

def to_min(s):
    h, m = s.split(":"); return int(h) * 60 + int(m)

async def free_slots(days_ahead=14):
    """{'AAAA-MM-DD': ['09:00', ...]} só com horários livres, ou None se a agenda não foi configurada."""
    now_l = datetime.now(SP)
    cfg = await get_setting("hours")
    if not cfg: return None
    start, end, dur = to_min(cfg["start"]), to_min(cfg["end"]), int(cfg["duration"])
    last_day = (now_l + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    rows = await pool.fetch("SELECT date,time FROM appointments WHERE date >= $1 AND date <= $2",
                            now_l.strftime("%Y-%m-%d"), last_day)
    busy = {}
    for r in rows:
        try: busy.setdefault(r["date"], []).append(to_min(r["time"]))
        except Exception: pass
    out = {}
    for i in range(days_ahead + 1):
        d = (now_l + timedelta(days=i)).date(); ds = d.strftime("%Y-%m-%d")
        if d.weekday() not in cfg["days"]: continue
        slots, t = [], start
        while t + dur <= end:
            past = i == 0 and t <= now_l.hour * 60 + now_l.minute + 60
            taken = any(abs(b - t) < dur for b in busy.get(ds, []))
            if not past and not taken: slots.append(f"{t//60:02d}:{t%60:02d}")
            t += dur
        if slots: out[ds] = slots
    return out

async def availability_text(days_ahead=14):
    now_l = datetime.now(SP)
    head = f"Hoje é {WEEKDAYS[now_l.weekday()]} {now_l.strftime('%d/%m/%Y')} (horário de Brasília)."
    fs = await free_slots(days_ahead)
    if fs is None:
        return head + " Horários livres: a agenda ainda não foi configurada; diga que a recepção confirmará os horários e não agende."
    lines = [f"{WEEKDAYS[datetime.strptime(ds, '%Y-%m-%d').weekday()]} {ds[8:]}/{ds[5:7]} ({ds}): " + ", ".join(v) for ds, v in fs.items()]
    body = "\n".join(lines) if lines else "nenhum horário livre nos próximos dias; diga que a recepção entrará em contato."
    return head + " Horários livres (próximos 14 dias; os ocupados já foram retirados):\n" + body

async def ai_is_on(conv_flag):
    return conv_flag if conv_flag is not None else bool(await get_setting("ai_all", False))

FORMAT_RULES = ('Responda SOMENTE com um JSON no formato {"reply": "texto para o paciente", "book": null}. '
    'Só preencha "book" quando o paciente já tiver escolhido claramente um dia e horário que estejam na lista de livres e já tiver dito se prefere Presencial ou Online: '
    '{"reply": "...", "book": {"date": "AAAA-MM-DD", "time": "HH:MM", "modality": "Presencial"}}. '
    'Nome e telefone já constam no cadastro: não peça. Nunca diga que agendou sem preencher "book". Para outros assuntos, deixe "book" como null.')

def parse_ai(text):
    try:
        d = json.loads(text[text.index("{"): text.rindex("}") + 1])
        return str(d.get("reply") or "").strip(), d.get("book")
    except Exception: return text.strip(), None

async def try_book(pid, book):
    """Confere se o horário ainda está livre e grava na agenda. Retorna (ok, mensagem)."""
    try:
        date, tm = str(book["date"]), str(book["time"])
        mod = "Online" if str(book.get("modality", "")).lower().startswith("on") else "Presencial"
    except Exception: return False, "Não consegui entender o horário escolhido."
    today = datetime.now(SP).strftime("%Y-%m-%d")
    if await pool.fetchval("SELECT 1 FROM appointments WHERE patient_id=$1 AND date>=$2", str(pid), today):
        return False, "Você já tem uma consulta agendada. Para alterar, fale com a recepção."
    fs = await free_slots()
    if not fs or tm not in fs.get(date, []): return False, "Esse horário não está disponível."
    u = await pool.fetchrow("SELECT name,phone FROM users WHERE id=$1", pid)
    try:
        await pool.execute("INSERT INTO appointments(patient_name,phone,date,time,modality,notes,patient_id) VALUES($1,$2,$3,$4,$5,'Agendado pela IA',$6)",
                           u["name"], u["phone"] or "", date, tm, mod, str(pid))
    except asyncpg.UniqueViolationError: return False, "Esse horário acabou de ser ocupado."
    return True, f"✅ Consulta agendada para {date[8:]}/{date[5:7]} às {tm} ({mod}). A recepção entrará em contato se precisar."

async def generate_ai_reply(pid: int):
    await load_site()
    rows = await pool.fetch("SELECT sender,text FROM (SELECT * FROM messages WHERE patient_id=$1 ORDER BY id DESC LIMIT 50) t ORDER BY id", pid)
    convo = "\n".join(f"{'Paciente' if r['sender']=='patient' else 'Recepção'}: {r['text']}" for r in rows)
    try:
        raw = await ask_llm(f"{await availability_text()}\n\nHistórico:\n{convo}\n\n{FORMAT_RULES}\n\nResponda a última mensagem.", f"recep-{pid}")
        if raw is None: return None
        reply, book = parse_ai(raw)
        if book:
            ok, msg = await try_book(pid, book)
            if ok: reply = (reply + "\n\n" if reply else "") + msg
            else:
                fs = await free_slots() or {}
                alt = ", ".join(f"{ds[8:]}/{ds[5:7]} às {t}" for ds, ts in list(fs.items())[:3] for t in ts[:2])
                reply = msg + (f" Posso sugerir: {alt}." if alt and "já tem" not in msg else "")
    except Exception as e:
        log.error("Falha na IA: %s", e); reply = FALLBACK_REPLY
    if not reply: return None
    r = await pool.fetchrow("INSERT INTO messages(patient_id,sender,text,read_by_reception) VALUES($1,'ai',$2,true) RETURNING *", pid, reply)
    await pool.execute("UPDATE conversations SET last_message=$2, last_at=now() WHERE patient_id=$1", pid, reply)
    return msg_out(r)

# ---- Chat ----
@api_router.post("/chat/messages")
async def patient_send(data: MessageInput, request: Request, user=Depends(get_current_user)):
    pid = int(user["id"])
    await pool.execute("UPDATE users SET last_ip=$2 WHERE id=$1", pid, client_ip(request))
    await pool.execute("INSERT INTO messages(patient_id,sender,text,read_by_reception) VALUES($1,'patient',$2,false)", pid, data.text)
    ai = await pool.fetchval("""
        INSERT INTO conversations(patient_id,patient_name,phone,email,last_message,last_at) VALUES($1,$2,$3,$4,$5,now())
        ON CONFLICT (patient_id) DO UPDATE SET patient_name=$2, phone=$3, email=$4, last_message=$5, last_at=now()
        RETURNING ai_enabled""", pid, user["name"], user["phone"], user["email"], data.text)
    if await ai_is_on(ai): await generate_ai_reply(pid)
    return {"ok": True}

@api_router.get("/chat/messages")
async def patient_messages(user=Depends(get_current_user)):
    rows = await pool.fetch("SELECT * FROM messages WHERE patient_id=$1 ORDER BY id LIMIT 500", int(user["id"]))
    return [msg_out(r) for r in rows]

@api_router.get("/chat/conversations")
async def list_conversations(_=Depends(require_reception)):
    ai_all = bool(await get_setting("ai_all", False))
    rows = await pool.fetch("""
        SELECT c.*, (SELECT count(*) FROM messages m WHERE m.patient_id=c.patient_id
                     AND m.sender='patient' AND NOT m.read_by_reception) AS unread
        FROM conversations c ORDER BY c.last_at DESC LIMIT 500""")
    return [{"id": str(r["patient_id"]), "patient_id": str(r["patient_id"]), "patient_name": r["patient_name"],
             "phone": r["phone"], "email": r["email"], "last_message": r["last_message"],
             "last_at": iso(r["last_at"]), "ai_enabled": ai_all if r["ai_enabled"] is None else r["ai_enabled"], "unread": r["unread"]} for r in rows]

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

@api_router.delete("/chat/conversations/{patient_id}")
async def delete_conversation(patient_id: str, _=Depends(require_reception)):
    pid = to_int(patient_id)
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute("DELETE FROM messages WHERE patient_id=$1", pid)
            await con.execute("DELETE FROM conversations WHERE patient_id=$1", pid)
    return {"ok": True}

# ---- Configurações (IA geral e horários de atendimento) ----
class HoursInput(BaseModel):
    days: list[int]; start: str; end: str; duration: int

@api_router.get("/settings/ai")
async def get_ai_setting(_=Depends(require_reception)):
    return {"ai_all": bool(await get_setting("ai_all", False))}

@api_router.put("/settings/ai")
async def put_ai_setting(data: AIToggleInput, _=Depends(require_reception)):
    await set_setting("ai_all", data.enabled)
    await pool.execute("UPDATE conversations SET ai_enabled=NULL")  # todas passam a seguir a chave geral
    return {"ai_all": data.enabled}

@api_router.get("/settings/hours")
async def get_hours(_=Depends(require_reception)):
    return await get_setting("hours", {})

@api_router.put("/settings/hours")
async def put_hours(data: HoursInput, _=Depends(require_reception)):
    try:
        ok = (to_min(data.start) < to_min(data.end) and 15 <= data.duration <= 240
              and data.days and all(0 <= d <= 6 for d in data.days))
    except Exception: ok = False
    if not ok: raise HTTPException(400, "Horários inválidos: marque ao menos um dia e confira início, fim e duração")
    cfg = {"days": sorted(set(data.days)), "start": data.start, "end": data.end, "duration": data.duration}
    await set_setting("hours", cfg); return cfg

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

# ---- Site e administração ----
SITE_KEYS = ("site_name", "logo", "about", "services", "hours_text", "address", "contact")

@api_router.get("/public/settings")
async def public_settings():
    c = await load_site(); return {k: c.get(k, "") for k in SITE_KEYS}

@api_router.get("/admin/settings")
async def admin_settings(_=Depends(require_admin)):
    c = await load_site(); return {k: c.get(k, "") for k in SITE_KEYS}

@api_router.put("/admin/settings")
async def admin_settings_put(data: dict, _=Depends(require_admin)):
    cur = dict(await load_site())
    for k in SITE_KEYS:
        if data.get(k) is not None: cur[k] = str(data[k]) if k == "logo" else str(data[k]).strip()
    if not cur["site_name"]: cur["site_name"] = "SUA_EMPRESA"
    if len(cur["site_name"]) > 60: raise HTTPException(400, "Nome muito longo (máx. 60)")
    if len(cur["about"]) > 1500 or len(cur["services"]) > 1500: raise HTTPException(400, "Texto muito longo")
    lg = cur["logo"]
    if lg and (not lg.startswith("data:image/") or len(lg) > 400000): raise HTTPException(400, "Logo inválida ou grande demais")
    await set_setting("site", {k: cur[k] for k in SITE_KEYS}); await load_site()
    return {k: cur[k] for k in SITE_KEYS}

@api_router.delete("/patients/{patient_id}")
async def delete_patient(patient_id: str, _=Depends(require_reception)):
    pid = to_int(patient_id)
    u = await pool.fetchrow("SELECT name,phone FROM users WHERE id=$1 AND role='patient'", pid)
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute("DELETE FROM messages WHERE patient_id=$1", pid)
            await con.execute("DELETE FROM conversations WHERE patient_id=$1", pid)
            if u: await con.execute("DELETE FROM appointments WHERE patient_id=$1 OR (patient_name=$2 AND phone=$3)", str(pid), u["name"], u["phone"] or "")
            await con.execute("DELETE FROM users WHERE id=$1 AND role='patient'", pid)
    return {"ok": True}

@api_router.get("/admin/patients")
async def admin_patients(_=Depends(require_admin)):
    rows = await pool.fetch("""SELECT u.id,u.name,u.email,u.phone,u.last_ip,
        (SELECT max(until_at) FROM bans b WHERE b.ip=u.last_ip AND b.until_at>now()) AS banned_until
        FROM users u WHERE u.role='patient' ORDER BY u.id DESC LIMIT 500""")
    return [{"id": str(r["id"]), "name": r["name"], "email": r["email"], "phone": r["phone"] or "",
             "last_ip": r["last_ip"], "banned_until": iso(r["banned_until"])} for r in rows]

class BanInput(BaseModel): patient_id: str

@api_router.post("/admin/bans")
async def ban_create(data: BanInput, request: Request, _=Depends(require_admin)):
    u = await pool.fetchrow("SELECT name,last_ip FROM users WHERE id=$1 AND role='patient'", to_int(data.patient_id))
    if not u: raise HTTPException(404, "Paciente não encontrado")
    if not u["last_ip"]: raise HTTPException(400, "IP do paciente desconhecido")
    if u["last_ip"] == client_ip(request): raise HTTPException(400, "Esse é o seu próprio IP")
    await pool.execute("INSERT INTO bans(ip,until_at,patient_name) VALUES($1, now() + interval '7 days', $2)", u["last_ip"], u["name"])
    return {"ok": True}

@api_router.get("/admin/bans")
async def ban_list(_=Depends(require_admin)):
    rows = await pool.fetch("SELECT * FROM bans WHERE until_at>now() ORDER BY id DESC")
    return [{"id": str(r["id"]), "ip": r["ip"], "patient_name": r["patient_name"], "until": iso(r["until_at"])} for r in rows]

@api_router.delete("/admin/bans/{ban_id}")
async def ban_delete(ban_id: str, _=Depends(require_admin)):
    await pool.execute("DELETE FROM bans WHERE id=$1", to_int(ban_id)); return {"ok": True}

@app.middleware("http")
async def ban_guard(request: Request, call_next):
    if pool is not None:
        try: banned = await pool.fetchval("SELECT 1 FROM bans WHERE ip=$1 AND until_at>now()", client_ip(request))
        except Exception: banned = None
        if banned:
            if request.url.path.startswith("/api"): return JSONResponse({"detail": "Acesso bloqueado temporariamente."}, status_code=403)
            return HTMLResponse("<h2>Acesso bloqueado temporariamente.</h2>", status_code=403)
    return await call_next(request)

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
    for ek, pk, role, de, dp in [("RECEPTION_EMAIL", "RECEPTION_PASSWORD", "reception", "", ""),
                                 ("OWNER_EMAIL", "OWNER_PASSWORD", "reception", "", ""),
                                 ("ADMIN_USER", "ADMIN_PASSWORD", "admin", "ADM_EMPRESA", "ADM")]:
        email = (os.environ.get(ek) or de).strip().lower(); pw = os.environ.get(pk) or dp
        if not email or not pw: continue
        if role == "admin" and pw == "ADM": log.warning("ADMIN_PASSWORD padrão em uso: troque no Render.")
        u = await pool.fetchrow("SELECT role,password_hash FROM users WHERE email=$1", email)
        if u is None:
            await pool.execute("INSERT INTO users(name,email,phone,password_hash,role) VALUES($1,$2,'',$3,$4)",
                               "Administrador" if role == "admin" else "Recepção", email, hash_password(pw), role)
        elif u["role"] != role or not verify_password(pw, u["password_hash"]):
            await pool.execute("UPDATE users SET role=$3, password_hash=$2 WHERE email=$1", email, hash_password(pw), role)

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
