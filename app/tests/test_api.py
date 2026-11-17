"""Testes de integracao: API completa sobre um banco SQLite temporario."""
import os
import sqlite3
import tempfile
from datetime import date, timedelta

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "teste.db"))
os.environ.setdefault("ADMIN_TOKEN", "token-admin-teste")
os.environ.setdefault("METRICS_TOKEN", "token-metricas-teste")

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

ADMIN = {"Authorization": f"Bearer {os.environ['ADMIN_TOKEN']}"}
HOJE = date.today()


def dia(n: int) -> str:
    return (HOJE - timedelta(days=n)).isoformat()


def nova_casa(c) -> dict:
    r = c.post("/api/casas")
    assert r.status_code == 201
    return {"X-Codigo-Casa": r.json()["codigo"]}


def test_fluxo_cidadao_conta_correta_e_divergente():
    with TestClient(app) as c:
        assert c.get("/health").json()["status"] == "ok"
        casa = nova_casa(c)
        assert c.post("/api/leituras", json={"data": dia(10), "kwh": 1000}, headers=casa).status_code == 201
        assert c.get("/api/leituras", headers=casa).json()["resumo"] is None  # 1 leitura so: sem estimativa
        assert c.post("/api/leituras", json={"data": dia(0), "kwh": 1050}, headers=casa).status_code == 201
        resumo = c.get("/api/leituras", headers=casa).json()["resumo"]
        assert resumo["consumo_diario_kwh"] == 5.0 and resumo["projecao_30d_kwh"] == 150.0
        assert resumo["estimativa_30d_reais"] > 0

        ok = c.post("/api/conferir", json={"kwh_cobrado": 155, "dias": 30}, headers=casa).json()
        assert ok["resultado"] == "ok" and ok["contestacao"] is None
        ruim = c.post("/api/conferir", json={"kwh_cobrado": 240, "dias": 30}, headers=casa).json()
        assert ruim["resultado"] == "divergente" and "1.000/2021" in ruim["contestacao"]

        painel = c.get("/api/admin/painel", headers=ADMIN).json()
        assert painel["conferencias_divergentes"] >= 1 and painel["casas"] >= 1


def test_validacoes_do_medidor():
    with TestClient(app) as c:
        casa = nova_casa(c)
        assert c.post("/api/leituras", json={"data": dia(5), "kwh": 500}, headers=casa).status_code == 201
        # medidor nao volta: numero menor num dia posterior e recusado
        assert c.post("/api/leituras", json={"data": dia(1), "kwh": 400}, headers=casa).status_code == 422
        assert c.post("/api/leituras", json={"data": dia(5), "kwh": 510}, headers=casa).status_code == 409
        assert c.post("/api/leituras", json={"data": (HOJE + timedelta(days=1)).isoformat(), "kwh": 600},
                      headers=casa).status_code == 422
        assert c.post("/api/leituras", json={"data": dia(1), "kwh": -1}, headers=casa).status_code == 422
        assert c.post("/api/conferir", json={"kwh_cobrado": 100}, headers=casa).status_code == 422  # 1 leitura so


def test_isolamento_e_autorizacao():
    with TestClient(app) as c:
        a, b = nova_casa(c), nova_casa(c)
        c.post("/api/leituras", json={"data": dia(3), "kwh": 77}, headers=a)
        assert c.get("/api/leituras", headers=b).json()["leituras"] == []  # uma casa nao ve a outra
        assert c.get("/api/leituras").status_code == 401
        assert c.get("/api/leituras", headers={"X-Codigo-Casa": "inexistente"}).status_code == 401
        assert c.get("/api/admin/painel").status_code == 401
        assert c.get("/api/admin/painel", headers={"Authorization": "Bearer errado"}).status_code == 401


def test_banco_guarda_so_hash_do_codigo():
    with TestClient(app) as c:
        codigo = nova_casa(c)["X-Codigo-Casa"]
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        hashes = [h for (h,) in conn.execute("SELECT codigo_hash FROM casas")]
    assert codigo not in hashes and all(len(h) == 64 for h in hashes)


def test_casa_de_demonstracao():
    with TestClient(app) as c:
        demo = c.get("/api/leituras", headers={"X-Codigo-Casa": "demonstracao"}).json()
        assert len(demo["leituras"]) == 5 and demo["resumo"]["consumo_diario_kwh"] > 0


def test_metricas_protegidas_e_sinais_de_ouro():
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        c.get("/api/config")
        corpo = c.get("/metrics", headers={"Authorization": f"Bearer {os.environ['METRICS_TOKEN']}"}).text
        for m in ("http_requests_total", "http_request_duration_seconds_bucket", "http_requests_in_progress"):
            assert m in corpo
        assert 'route="/api/config"' in corpo


def test_cabecalhos_de_seguranca():
    with TestClient(app) as c:
        h = c.get("/").headers
        assert "default-src 'self'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff"
