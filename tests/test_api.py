import importlib
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LOJA_106_TOKEN", "token-teste")

    import app as app_module

    importlib.reload(app_module)

    with TestClient(app_module.app) as test_client:
        yield test_client


def auth_headers():
    return {"Authorization": "Bearer token-teste"}


def evento_payload(**overrides):
    payload = {
        "timestamp": "2026-06-10T14:32:08",
        "pdv": "01",
        "cupom": "221548",
        "imagem": "/var/lib/pdv-visual-auditor/snap.jpg",
        "produto": "Cafe Marata 250g",
        "valor_unitario": 14.99,
        "quantidade": 1,
        "modo": "produto",
        "resultado": {
            "resultado": "NAO_CONFERE",
            "confianca": 94,
            "comparacao_pdv": "Produto registrado nao foi visto no scanner.",
            "possivel_divergencia": "Possivel passagem sem leitura.",
            "acao_recomendada": "revisar cupom",
        },
    }
    payload.update(overrides)
    return payload


def test_evento_sem_token_e_rejeitado(client):
    resp = client.post("/api/v1/events", json=evento_payload())
    assert resp.status_code == 401


def test_evento_com_token_invalido_e_rejeitado(client):
    resp = client.post(
        "/api/v1/events", json=evento_payload(), headers={"Authorization": "Bearer errado"}
    )
    assert resp.status_code == 401


def test_criar_evento_e_listar_alerta(client):
    resp = client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    assert resp.status_code == 200

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106"})
    assert resp.status_code == 200
    alerts = resp.json()
    assert len(alerts) == 1

    alerta = alerts[0]
    assert alerta["severity"] == "critical"
    assert alerta["pdv"] == "PDV 01"
    assert alerta["receipt"] == "221548"
    assert alerta["event"] == "Produto incompatível"
    assert alerta["subtitle"] == "Divergência identificada na análise visual"
    assert alerta["product"] == "Cafe Marata 250g"
    assert alerta["confidence"] == 94
    assert alerta["state"] == "pending"
    assert alerta["stateText"] == "Em revisão"
    assert alerta["qty"] == "1 unidade"
    assert alerta["value"] == "R$ 14,99"
    assert alerta["result"] == "Não confere"
    assert alerta["time"] == "14:32:08"


def test_evento_duplicado_e_ignorado(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106"})
    assert len(resp.json()) == 1


def test_evento_confere_fica_resolvido(client):
    payload = evento_payload(
        resultado={
            "resultado": "CONFERE",
            "confianca": 96,
            "comparacao_pdv": "Produto visivel e compativel.",
            "possivel_divergencia": "",
            "acao_recomendada": "liberar",
        }
    )
    client.post("/api/v1/events", json=payload, headers=auth_headers())

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106"})
    alerta = resp.json()[0]
    assert alerta["severity"] == "ok"
    assert alerta["state"] == "resolved"
    assert alerta["stateText"] == "Salvo"
    assert alerta["result"] == "Confere"


def test_filtro_critical_e_resolved(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    client.post(
        "/api/v1/events",
        json=evento_payload(
            timestamp="2026-06-10T14:40:00",
            resultado={
                "resultado": "CONFERE",
                "confianca": 99,
                "comparacao_pdv": "ok",
                "possivel_divergencia": "",
                "acao_recomendada": "liberar",
            },
        ),
        headers=auth_headers(),
    )

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "filter": "critical"})
    assert len(resp.json()) == 1
    assert resp.json()[0]["severity"] == "critical"

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "filter": "resolved"})
    assert len(resp.json()) == 1
    assert resp.json()[0]["result"] == "Confere"

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "filter": "review"})
    assert len(resp.json()) == 1
    assert resp.json()[0]["result"] == "Não confere"


def test_loja_inexistente_retorna_404(client):
    resp = client.get("/api/v1/alerts", params={"loja": "loja-inexistente"})
    assert resp.status_code == 404


def test_health_post_e_get(client):
    payload = [
        {"pdv": "01", "bridge": "online", "imhdx": "online", "audit": "online"},
        {"pdv": "02", "bridge": "online", "imhdx": "warning", "audit": "offline"},
    ]
    resp = client.post("/api/v1/health", json=payload, headers=auth_headers())
    assert resp.status_code == 200

    resp = client.get("/api/v1/health", params={"loja": "loja-106"})
    assert resp.status_code == 200
    health = resp.json()
    assert len(health) == 2
    assert health[0] == {"pdv": "01", "bridge": "online", "imhdx": "online", "audit": "online"}


def test_health_atualiza_em_vez_de_duplicar(client):
    payload = [{"pdv": "01", "bridge": "online", "imhdx": "online", "audit": "online"}]
    client.post("/api/v1/health", json=payload, headers=auth_headers())

    payload2 = [{"pdv": "01", "bridge": "warning", "imhdx": "online", "audit": "online"}]
    client.post("/api/v1/health", json=payload2, headers=auth_headers())

    resp = client.get("/api/v1/health", params={"loja": "loja-106"})
    health = resp.json()
    assert len(health) == 1
    assert health[0]["bridge"] == "warning"


def test_decision_save_e_ignore(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}).json()[0]["id"]

    resp = client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "save"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"

    resp = client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "ignore"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"

    alerta = client.get("/api/v1/alerts", params={"loja": "loja-106"}).json()[0]
    assert alerta["state"] == "resolved"
    assert alerta["stateText"] == "Ignorado"


def test_decision_invalida(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}).json()[0]["id"]

    resp = client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "delete"})
    assert resp.status_code == 400


def test_decision_alerta_inexistente(client):
    resp = client.post("/api/v1/alerts/999/decision", json={"action": "save"})
    assert resp.status_code == 404
