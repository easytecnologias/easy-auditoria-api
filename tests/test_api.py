import importlib
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("VIDEOS_DIR", str(tmp_path / "videos"))
    monkeypatch.setenv("LOJA_106_TOKEN", "token-teste")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin12345")
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    import app as app_module

    importlib.reload(app_module)

    with TestClient(app_module.app) as test_client:
        yield test_client


def auth_headers():
    return {"Authorization": "Bearer token-teste"}


def login_headers(client):
    resp = client.post(
        "/auth/login",
        json={"email": "admin@easy.local", "senha": "admin12345"},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


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

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client))
    assert resp.status_code == 200
    alerts = resp.json()
    assert len(alerts) == 1

    alerta = alerts[0]
    assert alerta["severity"] == "critical"
    assert alerta["pdv"] == "PDV 01"
    assert alerta["receipt"] == "221548"
    assert alerta["event"] == "Produto incompativel"
    assert alerta["subtitle"] == "Divergencia identificada na analise visual"
    assert alerta["product"] == "Cafe Marata 250g"
    assert alerta["confidence"] == 94
    assert alerta["state"] == "pending"
    assert alerta["stateText"] == "Em revisao"
    assert alerta["qty"] == "1 unidade"
    assert alerta["value"] == "R$ 14,99"
    assert alerta["result"] == "Nao confere"
    assert alerta["time"] == "14:32:08"


def test_evento_duplicado_e_ignorado(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client))
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

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client))
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

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "filter": "critical"}, headers=login_headers(client))
    assert len(resp.json()) == 1
    assert resp.json()[0]["severity"] == "critical"

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "filter": "resolved"}, headers=login_headers(client))
    assert len(resp.json()) == 1
    assert resp.json()[0]["result"] == "Confere"

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "filter": "review"}, headers=login_headers(client))
    assert len(resp.json()) == 1
    assert resp.json()[0]["result"] == "Nao confere"


def test_loja_inexistente_retorna_404(client):
    resp = client.get("/api/v1/alerts", params={"loja": "loja-inexistente"}, headers=login_headers(client))
    assert resp.status_code == 404


def test_health_post_e_get(client):
    payload = [
        {"pdv": "01", "bridge": "online", "imhdx": "online", "audit": "online"},
        {"pdv": "02", "bridge": "online", "imhdx": "warning", "audit": "offline"},
    ]
    resp = client.post("/api/v1/health", json=payload, headers=auth_headers())
    assert resp.status_code == 200

    resp = client.get("/api/v1/health", params={"loja": "loja-106"}, headers=login_headers(client))
    assert resp.status_code == 200
    health = resp.json()
    assert len(health) == 2
    assert health[0] == {"pdv": "01", "bridge": "online", "imhdx": "online", "audit": "online"}


def test_health_atualiza_em_vez_de_duplicar(client):
    payload = [{"pdv": "01", "bridge": "online", "imhdx": "online", "audit": "online"}]
    client.post("/api/v1/health", json=payload, headers=auth_headers())

    payload2 = [{"pdv": "01", "bridge": "warning", "imhdx": "online", "audit": "online"}]
    client.post("/api/v1/health", json=payload2, headers=auth_headers())

    resp = client.get("/api/v1/health", params={"loja": "loja-106"}, headers=login_headers(client))
    health = resp.json()
    assert len(health) == 1
    assert health[0]["bridge"] == "warning"


def test_decision_save_e_ignore(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    resp = client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "save"}, headers=login_headers(client))
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"

    resp = client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "ignore"}, headers=login_headers(client))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"

    alerta = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]
    assert alerta["state"] == "resolved"
    assert alerta["stateText"] == "Ignorado"


def test_decision_invalida(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    resp = client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "delete"}, headers=login_headers(client))
    assert resp.status_code == 422


def test_decision_alerta_inexistente(client):
    resp = client.post("/api/v1/alerts/999/decision", json={"action": "save"}, headers=login_headers(client))
    assert resp.status_code == 404


def test_evento_tem_image_url(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]
    assert alerta["imageUrl"] == f"/api/v1/events/{alerta['id']}/image"


def test_imagem_inexistente_retorna_404(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]
    resp = client.get(f"/api/v1/events/{alerta_id}/image", headers=login_headers(client))
    assert resp.status_code == 404


def test_upload_e_obtencao_de_imagem(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    resp = client.post(
        f"/api/v1/events/{alerta_id}/image",
        files={"file": ("foto.jpg", b"fake-jpg-bytes", "image/jpeg")},
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    resp = client.get(f"/api/v1/events/{alerta_id}/image", headers=login_headers(client))
    assert resp.status_code == 200
    assert resp.content == b"fake-jpg-bytes"


def test_upload_imagem_sem_token_e_rejeitado(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    resp = client.post(
        f"/api/v1/events/{alerta_id}/image",
        files={"file": ("foto.jpg", b"fake-jpg-bytes", "image/jpeg")},
    )
    assert resp.status_code == 401


def test_decision_ignore_apaga_imagem(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    client.post(
        f"/api/v1/events/{alerta_id}/image",
        files={"file": ("foto.jpg", b"fake-jpg-bytes", "image/jpeg")},
        headers=auth_headers(),
    )
    client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "ignore"}, headers=login_headers(client))

    resp = client.get(f"/api/v1/events/{alerta_id}/image", headers=login_headers(client))
    assert resp.status_code == 404


def test_decision_save_mantem_imagem(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    client.post(
        f"/api/v1/events/{alerta_id}/image",
        files={"file": ("foto.jpg", b"fake-jpg-bytes", "image/jpeg")},
        headers=auth_headers(),
    )
    client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "save"}, headers=login_headers(client))

    resp = client.get(f"/api/v1/events/{alerta_id}/image", headers=login_headers(client))
    assert resp.status_code == 200


def test_evento_tem_video_url(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]
    assert alerta["videoUrl"] == f"/api/v1/events/{alerta['id']}/video"


def test_video_inexistente_retorna_404(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]
    resp = client.get(f"/api/v1/events/{alerta_id}/video", headers=login_headers(client))
    assert resp.status_code == 404


def test_upload_e_obtencao_de_video(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    resp = client.post(
        f"/api/v1/events/{alerta_id}/video",
        files={"file": ("evento.mp4", b"fake-mp4-bytes", "video/mp4")},
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    resp = client.get(f"/api/v1/events/{alerta_id}/video", headers=login_headers(client))
    assert resp.status_code == 200
    assert resp.content == b"fake-mp4-bytes"


def test_upload_video_sem_token_e_rejeitado(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    resp = client.post(
        f"/api/v1/events/{alerta_id}/video",
        files={"file": ("evento.mp4", b"fake-mp4-bytes", "video/mp4")},
    )
    assert resp.status_code == 401


def test_decision_ignore_apaga_video(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    client.post(
        f"/api/v1/events/{alerta_id}/video",
        files={"file": ("evento.mp4", b"fake-mp4-bytes", "video/mp4")},
        headers=auth_headers(),
    )
    client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "ignore"}, headers=login_headers(client))

    resp = client.get(f"/api/v1/events/{alerta_id}/video", headers=login_headers(client))
    assert resp.status_code == 404


def test_vendas_sem_dados_retorna_zero(client):
    resp = client.get("/api/v1/sales", params={"loja": "loja-106"}, headers=login_headers(client))
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "cupons": 0}


def test_vendas_sem_token_e_rejeitado(client):
    resp = client.post("/api/v1/sales", json={"pdv": "01", "total": 100.0, "cupons": 5})
    assert resp.status_code == 401


def test_vendas_post_e_get(client):
    resp = client.post(
        "/api/v1/sales",
        json={"pdv": "01", "total": 1234.5, "cupons": 10},
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    resp = client.get("/api/v1/sales", params={"loja": "loja-106"}, headers=login_headers(client))
    assert resp.json() == {"total": 1234.5, "cupons": 10}


def test_vendas_atualiza_em_vez_de_somar_por_pdv(client):
    client.post("/api/v1/sales", json={"pdv": "01", "total": 100.0, "cupons": 1}, headers=auth_headers())
    client.post("/api/v1/sales", json={"pdv": "01", "total": 250.0, "cupons": 3}, headers=auth_headers())

    resp = client.get("/api/v1/sales", params={"loja": "loja-106"}, headers=login_headers(client))
    assert resp.json() == {"total": 250.0, "cupons": 3}


def test_vendas_soma_varios_pdvs(client):
    client.post("/api/v1/sales", json={"pdv": "01", "total": 100.0, "cupons": 1}, headers=auth_headers())
    client.post("/api/v1/sales", json={"pdv": "02", "total": 50.0, "cupons": 2}, headers=auth_headers())

    resp = client.get("/api/v1/sales", params={"loja": "loja-106"}, headers=login_headers(client))
    assert resp.json() == {"total": 150.0, "cupons": 3}


def test_loja_inexistente_em_vendas_retorna_404(client):
    resp = client.get("/api/v1/sales", params={"loja": "loja-inexistente"}, headers=login_headers(client))
    assert resp.status_code == 404


def test_vendas_por_data_especifica(client):
    client.post(
        "/api/v1/sales",
        json={"pdv": "01", "total": 100.0, "cupons": 5, "data": "2026-06-09"},
        headers=auth_headers(),
    )
    client.post(
        "/api/v1/sales",
        json={"pdv": "01", "total": 200.0, "cupons": 8, "data": "2026-06-10"},
        headers=auth_headers(),
    )

    resp = client.get("/api/v1/sales", params={"loja": "loja-106", "data": "2026-06-09"}, headers=login_headers(client))
    assert resp.json() == {"total": 100.0, "cupons": 5}

    resp = client.get("/api/v1/sales", params={"loja": "loja-106", "data": "2026-06-10"}, headers=login_headers(client))
    assert resp.json() == {"total": 200.0, "cupons": 8}


def test_alertas_filtra_por_data(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "data": "2026-06-10"}, headers=login_headers(client))
    assert len(resp.json()) == 1

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "data": "2026-06-09"}, headers=login_headers(client))
    assert resp.json() == []


def test_alertas_filtra_por_pdv(client):
    client.post("/api/v1/events", json=evento_payload(pdv="01"), headers=auth_headers())
    client.post(
        "/api/v1/events",
        json=evento_payload(pdv="02", timestamp="2026-06-10T15:00:00"),
        headers=auth_headers(),
    )

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "pdv": "01"}, headers=login_headers(client))
    assert len(resp.json()) == 1
    assert resp.json()[0]["pdv"] == "PDV 01"

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "pdv": ["01", "02"]}, headers=login_headers(client))
    assert len(resp.json()) == 2

    resp = client.get("/api/v1/alerts", params={"loja": "loja-106", "pdv": "03"}, headers=login_headers(client))
    assert resp.json() == []


def test_vendas_filtra_por_pdv(client):
    client.post(
        "/api/v1/sales",
        json={"pdv": "01", "total": 100.0, "cupons": 5, "data": "2026-06-10"},
        headers=auth_headers(),
    )
    client.post(
        "/api/v1/sales",
        json={"pdv": "02", "total": 50.0, "cupons": 2, "data": "2026-06-10"},
        headers=auth_headers(),
    )

    resp = client.get("/api/v1/sales", params={"loja": "loja-106", "data": "2026-06-10", "pdv": "01"}, headers=login_headers(client))
    assert resp.json() == {"total": 100.0, "cupons": 5}

    resp = client.get("/api/v1/sales", params={"loja": "loja-106", "data": "2026-06-10", "pdv": ["01", "02"]}, headers=login_headers(client))
    assert resp.json() == {"total": 150.0, "cupons": 7}


def test_decision_save_mantem_video(client):
    client.post("/api/v1/events", json=evento_payload(), headers=auth_headers())
    alerta_id = client.get("/api/v1/alerts", params={"loja": "loja-106"}, headers=login_headers(client)).json()[0]["id"]

    client.post(
        f"/api/v1/events/{alerta_id}/video",
        files={"file": ("evento.mp4", b"fake-mp4-bytes", "video/mp4")},
        headers=auth_headers(),
    )
    client.post(f"/api/v1/alerts/{alerta_id}/decision", json={"action": "save"}, headers=login_headers(client))

    resp = client.get(f"/api/v1/events/{alerta_id}/video", headers=login_headers(client))
    assert resp.status_code == 200
