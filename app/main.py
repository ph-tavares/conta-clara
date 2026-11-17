"""Conta Clara - API do MVP (leituras do medidor de luz, estimativa e conferencia da conta)."""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

# shortcut: SQLite num unico arquivo (a assinatura Azure Starter nao oferece banco gerenciado); trocar por banco gerenciado se houver mais de 1 instancia
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/contaclara.db")
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
METRICS_TOKEN = os.environ["METRICS_TOKEN"]
APP_VERSION = os.getenv("APP_VERSION", "dev")
SEED_DEMO = os.getenv("SEED_DEMO", "true").lower() == "true"
# Valores de REFERENCIA (configuraveis). No produto completo vem da camada Ouro do componente de Dados (ANEEL).
TARIFA_KWH = float(os.getenv("TARIFA_KWH", "0.92"))  # R$/kWh com impostos, residencial B1 (referencia)
BANDEIRA = os.getenv("BANDEIRA", "amarela")
ADICIONAL_BANDEIRA = {"verde": 0.0, "amarela": 1.885, "vermelha1": 4.463, "vermelha2": 7.877}  # R$ por 100 kWh
LIMITE_DIVERGENCIA = float(os.getenv("LIMITE_DIVERGENCIA", "0.15"))  # 15% entre o cobrado e o medido
CODIGO_DEMO = "demonstracao"


class NovaLeitura(BaseModel):
    data: date
    kwh: int = Field(ge=0, le=999_999, description="numero que aparece no display do medidor")


class Conferencia(BaseModel):
    kwh_cobrado: int = Field(ge=0, le=100_000)
    dias: int = Field(default=30, ge=1, le=60)


SCHEMA = """
CREATE TABLE IF NOT EXISTS casas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  codigo_hash TEXT NOT NULL UNIQUE,
  criado_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  fonte TEXT NOT NULL DEFAULT 'cidadao'
);
CREATE TABLE IF NOT EXISTS leituras (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  casa_id INTEGER NOT NULL REFERENCES casas(id),
  data TEXT NOT NULL,
  kwh INTEGER NOT NULL,
  criado_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (casa_id, data)
);
CREATE TABLE IF NOT EXISTS conferencias (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  casa_id INTEGER NOT NULL REFERENCES casas(id),
  criado_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  kwh_cobrado INTEGER NOT NULL,
  kwh_esperado REAL NOT NULL,
  divergencia REAL NOT NULL,
  resultado TEXT NOT NULL
);
"""


def hash_codigo(codigo: str) -> str:
    """O banco guarda so o hash do codigo da casa: um vazamento do arquivo nao da acesso a nenhuma casa."""
    return hashlib.sha256(codigo.encode()).hexdigest()


@contextmanager
def conectar():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15, isolation_level=None)
    conn.row_factory = lambda cur, row: {col[0]: v for col, v in zip(cur.description, row)}
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def semear_demo(conn):
    """Casa de DEMONSTRACAO com leituras SINTETICAS (fonte='sintetico'), para o avaliador usar sem cadastro."""
    cur = conn.execute("INSERT INTO casas (codigo_hash, fonte) VALUES (?, 'sintetico')", (hash_codigo(CODIGO_DEMO),))
    hoje = date.today()
    for dias_atras, kwh in ((35, 48210), (28, 48251), (21, 48290), (14, 48333), (7, 48371)):
        conn.execute("INSERT INTO leituras (casa_id, data, kwh) VALUES (?, ?, ?)",
                     (cur.lastrowid, (hoje - timedelta(days=dias_atras)).isoformat(), kwh))


@asynccontextmanager
async def lifespan(_app):
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    with conectar() as conn:
        conn.executescript(SCHEMA)
        sem_demo = conn.execute("SELECT 1 FROM casas WHERE codigo_hash = ?", (hash_codigo(CODIGO_DEMO),)).fetchone() is None
        if SEED_DEMO and sem_demo:
            semear_demo(conn)
    yield


app = FastAPI(title="Conta Clara API", version=APP_VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# --- 4 sinais de ouro: latencia, trafego, erros (label status) e saturacao (em andamento + process_*) ---
REQS = Counter("http_requests_total", "Requisicoes HTTP", ["method", "route", "status"])
LAT = Histogram("http_request_duration_seconds", "Latencia HTTP", ["method", "route"],
                buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
INPROG = Gauge("http_requests_in_progress", "Requisicoes em andamento")
CONFERENCIAS = Counter("contaclara_conferencias_total", "Conferencias de conta por resultado", ["resultado"])

CSP = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


@app.middleware("http")
async def observar(request: Request, call_next):
    INPROG.inc()
    inicio = time.perf_counter()
    status = 500
    try:
        resposta = await call_next(request)
        status = resposta.status_code
    finally:
        INPROG.dec()
        rota = getattr(request.scope.get("route"), "path", "nao_mapeada")
        LAT.labels(request.method, rota).observe(time.perf_counter() - inicio)
        REQS.labels(request.method, rota, str(status)).inc()
    resposta.headers.update({
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    })
    return resposta


def exigir_token(esperado: str):
    def verificar(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), f"Bearer {esperado}".encode()):
            raise HTTPException(status_code=401, detail="nao autorizado")
    return verificar


def casa_atual(x_codigo_casa: str = Header(default="")) -> int:
    # Codigo vai no cabecalho, nunca na URL: URLs ficam nos logs HTTP da plataforma.
    if not x_codigo_casa or len(x_codigo_casa) > 64:
        raise HTTPException(status_code=401, detail="informe o codigo da casa")
    with conectar() as conn:
        casa = conn.execute("SELECT id FROM casas WHERE codigo_hash = ?", (hash_codigo(x_codigo_casa),)).fetchone()
    if not casa:
        raise HTTPException(status_code=401, detail="codigo da casa invalido")
    return casa["id"]


def estimar_reais(kwh: float) -> float:
    return round(kwh * TARIFA_KWH + kwh / 100 * ADICIONAL_BANDEIRA.get(BANDEIRA, 0.0), 2)


def resumo(leituras: list[dict]) -> dict | None:
    """Consumo medio diario entre a primeira e a ultima leitura; precisa de 2 leituras em dias diferentes."""
    if len(leituras) < 2:
        return None
    primeira, ultima = leituras[0], leituras[-1]
    dias = (date.fromisoformat(ultima["data"]) - date.fromisoformat(primeira["data"])).days
    diario = (ultima["kwh"] - primeira["kwh"]) / dias
    return {"consumo_diario_kwh": round(diario, 2), "projecao_30d_kwh": round(diario * 30, 1),
            "estimativa_30d_reais": estimar_reais(diario * 30)}


def ler_leituras(conn, casa_id: int) -> list[dict]:
    return conn.execute("SELECT data, kwh FROM leituras WHERE casa_id = ? ORDER BY data", (casa_id,)).fetchall()


def texto_contestacao(c: Conferencia, esperado: float, divergencia: float) -> str:
    return (
        "À [nome da distribuidora],\n\n"
        "Solicito a revisão da fatura referente a [mês/ano], instalação nº [número da instalação].\n\n"
        f"A fatura cobra {c.kwh_cobrado} kWh para {c.dias} dias. Pelas leituras que fiz no meu medidor, "
        f"o consumo esperado para o período é de cerca de {esperado:.0f} kWh, uma diferença de "
        f"{divergencia * 100:+.0f}%.\n\n"
        "Peço a verificação da leitura (e a informação de se a fatura foi calculada por média ou estimativa) "
        "e, se confirmado o erro, a correção da fatura e a devolução de eventual valor pago a mais, "
        "conforme as regras de faturamento da Resolução Normativa ANEEL nº 1.000/2021.\n\n"
        "Anexo fotos do medidor com data.\n\n[seu nome]"
    )


@app.get("/health")
def health():
    try:
        with conectar() as conn:
            conn.execute("SELECT 1")
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="banco indisponivel")
    return {"status": "ok", "version": APP_VERSION}


@app.get("/metrics", dependencies=[Depends(exigir_token(METRICS_TOKEN))])
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/config")
def config():
    return {"version": APP_VERSION, "tarifa_kwh": TARIFA_KWH, "bandeira": BANDEIRA,
            "adicional_bandeira_100kwh": ADICIONAL_BANDEIRA.get(BANDEIRA, 0.0),
            "limite_divergencia": LIMITE_DIVERGENCIA,
            "aviso": "Valores de referência para o piloto; confira a tarifa da sua distribuidora."}


@app.post("/api/casas", status_code=201)
def criar_casa():
    codigo = secrets.token_urlsafe(9)  # 72 bits aleatorios: e a unica "senha" da casa, sem nome nem endereco
    with conectar() as conn:
        conn.execute("INSERT INTO casas (codigo_hash) VALUES (?)", (hash_codigo(codigo),))
    return {"codigo": codigo}


@app.get("/api/leituras")
def listar_leituras(casa_id: int = Depends(casa_atual)):
    with conectar() as conn:
        leituras = ler_leituras(conn, casa_id)
    return {"leituras": leituras, "resumo": resumo(leituras)}


@app.post("/api/leituras", status_code=201)
def registrar_leitura(leitura: NovaLeitura, casa_id: int = Depends(casa_atual)):
    if leitura.data > date.today() or leitura.data < date.today() - timedelta(days=400):
        raise HTTPException(status_code=422, detail="data fora do intervalo aceito")
    dia = leitura.data.isoformat()
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")  # leitura das vizinhas + insert atomicos
        try:
            antes = conn.execute("SELECT kwh FROM leituras WHERE casa_id = ? AND data < ? ORDER BY data DESC LIMIT 1",
                                 (casa_id, dia)).fetchone()
            depois = conn.execute("SELECT kwh FROM leituras WHERE casa_id = ? AND data > ? ORDER BY data LIMIT 1",
                                  (casa_id, dia)).fetchone()
            # O medidor so avanca: numero menor que o de um dia anterior e quase sempre erro de digitacao
            if (antes and leitura.kwh < antes["kwh"]) or (depois and leitura.kwh > depois["kwh"]):
                raise HTTPException(status_code=422, detail="leitura incoerente com as leituras de outros dias: confira o numero")
            try:
                conn.execute("INSERT INTO leituras (casa_id, data, kwh) VALUES (?, ?, ?)", (casa_id, dia, leitura.kwh))
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="ja existe leitura nesse dia")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"data": dia, "kwh": leitura.kwh}


@app.post("/api/conferir")
def conferir(c: Conferencia, casa_id: int = Depends(casa_atual)):
    with conectar() as conn:
        r = resumo(ler_leituras(conn, casa_id))
        if not r:
            raise HTTPException(status_code=422, detail="registre ao menos 2 leituras em dias diferentes")
        esperado = r["consumo_diario_kwh"] * c.dias
        divergencia = (c.kwh_cobrado - esperado) / max(esperado, 1)
        resultado = "divergente" if abs(divergencia) > LIMITE_DIVERGENCIA else "ok"
        conn.execute("INSERT INTO conferencias (casa_id, kwh_cobrado, kwh_esperado, divergencia, resultado)"
                     " VALUES (?, ?, ?, ?, ?)", (casa_id, c.kwh_cobrado, esperado, divergencia, resultado))
    CONFERENCIAS.labels(resultado).inc()
    return {"resultado": resultado, "kwh_esperado": round(esperado, 1), "divergencia": round(divergencia, 3),
            "valor_cobrado_estimado_reais": estimar_reais(c.kwh_cobrado),
            "valor_esperado_reais": estimar_reais(esperado),
            "contestacao": texto_contestacao(c, esperado, divergencia) if resultado == "divergente" else None}


@app.get("/api/admin/painel", dependencies=[Depends(exigir_token(ADMIN_TOKEN))])
def painel():
    """Painel da organizacao parceira: so agregados, nenhuma casa identificavel."""
    with conectar() as conn:
        n = lambda sql: conn.execute(sql).fetchone()["n"]  # noqa: E731
        return {
            "casas": n("SELECT count(*) AS n FROM casas WHERE fonte = 'cidadao'"),
            "leituras": n("SELECT count(*) AS n FROM leituras"),
            "conferencias": n("SELECT count(*) AS n FROM conferencias"),
            "conferencias_divergentes": n("SELECT count(*) AS n FROM conferencias WHERE resultado = 'divergente'"),
            "casas_com_divergencia": n("SELECT count(DISTINCT casa_id) AS n FROM conferencias WHERE resultado = 'divergente'"),
        }


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
