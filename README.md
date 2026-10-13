# Clínica Carolina Collo

Um único serviço: FastAPI (API em /api) + frontend (static/index.html) + MongoDB.

## Rodar local
1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` e preencha (MongoDB, JWT_SECRET, senhas, chave da IA)
4. `uvicorn server:app --reload` e abra http://localhost:8000

## Hospedar (Render + MongoDB Atlas, grátis)
1. Atlas: crie um cluster M0, um usuário e libere o IP 0.0.0.0/0. Copie a string de conexão para MONGO_URL.
2. Suba esta pasta para um repositório no GitHub (o .env NÃO vai, já está no .gitignore).
3. Render: New > Web Service > escolha o repositório > Runtime "Docker".
4. Em Environment, cadastre as variáveis do .env.example.
5. Deploy. O Render dá um link https://... pronto.
