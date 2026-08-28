import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import hmac
import hashlib
import json
import os
from unittest.mock import patch

from app.main import app
from app.database import SessionLocal, get_db
from app.models import RecoveryCase, CaseType

def override_get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def mock_webhook_secret(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test_secret")
    return "test_secret"

@pytest.fixture
def mock_inngest_client():
    with patch("app.main.inngest_client") as mock:
        yield mock

def generate_signature(secret: str, payload: dict) -> str:
    msg = json.dumps(payload).encode('utf-8')
    return hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()

import uuid
def test_valid_webhook_signature_creates_case(client, mock_webhook_secret, mock_inngest_client):
    event_id = f"evt_test_1_{uuid.uuid4().hex}"
    payload = {
        "entity": "event",
        "account_id": "acc_123",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_123",
                    "amount": 99900,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "customer_id": "cust_123"
                }
            }
        },
        "created_at": 1600000000,
        "id": event_id
    }
    
    signature = generate_signature(mock_webhook_secret, payload)
    
    response = client.post(
        "/webhooks/razorpay",
        content=json.dumps(payload),
        headers={"X-Razorpay-Signature": signature}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "created"
    assert "case_id" in data
    
    # Verify in DB
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == data["case_id"]).first()
    assert case is not None
    assert case.amount_paise == 99900
    assert case.payment_rail == "card"
    assert case.customer_id == "cust_123"
    assert case.case_type == CaseType.SUBSCRIPTION_FAILED
    db.close()

def test_invalid_signature_is_rejected(client, mock_webhook_secret, mock_inngest_client):
    payload = {
        "event": "payment.failed",
        "id": "evt_test_2"
    }
    
    # Use wrong secret
    signature = generate_signature("wrong_secret", payload)
    
    response = client.post(
        "/webhooks/razorpay",
        content=json.dumps(payload),
        headers={"X-Razorpay-Signature": signature}
    )
    
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid signature"

def test_idempotency_prevents_duplicates(client, mock_webhook_secret, mock_inngest_client):
    event_id = f"evt_test_idempotent_{uuid.uuid4().hex}"
    payload = {
        "event": "payment.failed",
        "id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "amount": 100,
                    "currency": "INR",
                    "customer_id": "cust_idemp"
                }
            }
        }
    }
    
    signature = generate_signature(mock_webhook_secret, payload)
    
    # First call
    response1 = client.post(
        "/webhooks/razorpay",
        content=json.dumps(payload),
        headers={"X-Razorpay-Signature": signature}
    )
    assert response1.status_code == 200
    assert response1.json()["status"] == "created"
    
    # Second call
    response2 = client.post(
        "/webhooks/razorpay",
        content=json.dumps(payload),
        headers={"X-Razorpay-Signature": signature}
    )
    assert response2.status_code == 200
    assert response2.json()["status"] == "idempotent"
    assert response1.json()["case_id"] == response2.json()["case_id"]
    
    # Verify only one case in DB for this event
    db = SessionLocal()
    cases = db.query(RecoveryCase).filter(
        RecoveryCase.razorpay_event_id == event_id
    ).all()
    assert len(cases) == 1
    db.close()

import concurrent.futures

def test_idempotency_concurrency(client, mock_webhook_secret, mock_inngest_client):
    """
    Fire 10 genuinely concurrent requests with an identical payload and signature.
    Assert exactly one row exists in recovery_cases for that event_id afterward.
    Run this test at least 20 times in a loop locally.
    """
    for i in range(20):
        event_id = f"evt_concurrent_{uuid.uuid4().hex}"
        payload = {
            "event": "payment.failed",
            "id": event_id,
            "payload": {
                "payment": {
                    "entity": {
                        "amount": 100,
                        "currency": "INR",
                        "customer_id": "cust_concurrent"
                    }
                }
            }
        }
        
        signature = generate_signature(mock_webhook_secret, payload)
        headers = {"X-Razorpay-Signature": signature}
        content = json.dumps(payload)
        
        def make_request():
            return client.post("/webhooks/razorpay", content=content, headers=headers)
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request) for _ in range(10)]
            responses = [f.result() for f in futures]
            
        # Exactly 1 should be created, 9 should be idempotent
        created_count = sum(1 for r in responses if r.json().get("status") == "created")
        idempotent_count = sum(1 for r in responses if r.json().get("status") == "idempotent")
        
        assert created_count == 1, f"Iteration {i}: Expected 1 created, got {created_count}"
        assert idempotent_count == 9, f"Iteration {i}: Expected 9 idempotent, got {idempotent_count}"
        
        db = SessionLocal()
        cases = db.query(RecoveryCase).filter(
            RecoveryCase.razorpay_event_id == event_id
        ).all()
        assert len(cases) == 1, f"Iteration {i}: Expected 1 case in DB, got {len(cases)}"
        db.close()
