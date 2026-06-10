"""
Tests for app/services/onboarding_service.py and app/routers/onboarding.py.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.client import Client
from app.services import onboarding_service


# ── onboarding_service unit tests ────────────────────────────────────────────


def test_generate_system_prompt_textile():
    """Textile prompt contains fabric-specific language."""
    prompt = onboarding_service.generate_system_prompt(
        business_type="textile",
        business_name="Sharma Fabrics",
        business_description="Premium cotton and silk fabrics since 1990",
    )
    assert "Sharma Fabrics" in prompt
    assert "fabric" in prompt.lower()


def test_generate_system_prompt_clinic():
    """Clinic prompt mentions appointment booking and doctor advice."""
    prompt = onboarding_service.generate_system_prompt(
        business_type="clinic",
        business_name="City Care Clinic",
        business_description="Multi-specialty outpatient clinic in Pune",
    )
    assert "City Care Clinic" in prompt
    assert "appointment" in prompt.lower()
    assert "doctor" in prompt.lower()


def test_generate_system_prompt_realestate():
    """Real estate prompt mentions properties and site visits."""
    prompt = onboarding_service.generate_system_prompt(
        business_type="realestate",
        business_name="Dream Homes",
        business_description="Affordable flats in Mumbai",
    )
    assert "Dream Homes" in prompt
    assert "propert" in prompt.lower()


def test_generate_system_prompt_ecommerce():
    """Ecommerce prompt mentions products and orders."""
    prompt = onboarding_service.generate_system_prompt(
        business_type="ecommerce",
        business_name="QuickMart",
        business_description="Online grocery delivery",
    )
    assert "QuickMart" in prompt
    assert "product" in prompt.lower()


def test_generate_system_prompt_unknown_falls_back_to_other():
    """Unknown business_type falls back to the generic template."""
    prompt = onboarding_service.generate_system_prompt(
        business_type="zoo",
        business_name="Wild Things",
        business_description="Animal experience park",
    )
    assert "Wild Things" in prompt


def test_generate_system_prompt_includes_products():
    """Products are injected into the generated prompt."""
    products = [{"name": "Silk Saree", "price": 2500, "stock": 10}]
    prompt = onboarding_service.generate_system_prompt(
        business_type="textile",
        business_name="Ravi Silks",
        business_description="Traditional silk store",
        products=products,
    )
    assert "Silk Saree" in prompt
    assert "2500" in prompt


def test_generate_api_key_format():
    """API key starts with 'vp_' and is at least 40 chars long."""
    key = onboarding_service.generate_api_key()
    assert key.startswith("vp_")
    assert len(key) >= 40


def test_generate_api_key_is_unique():
    """Two consecutive API keys are never identical."""
    assert onboarding_service.generate_api_key() != onboarding_service.generate_api_key()


def test_get_setup_status_empty_client():
    """Fresh client has only 'registered' done — 25%."""
    c = Client(id=1, email="x@y.com", hashed_password="h")
    status = onboarding_service.get_setup_status(c)
    assert status["completion_percentage"] == 25
    assert status["steps_done"] == ["registered"]
    assert "agent_configured" in status["steps_pending"]
    assert "products_added" in status["steps_pending"]
    assert "whatsapp_connected" in status["steps_pending"]


def test_get_setup_status_after_setup_agent():
    """Client with all onboarding fields set reaches 100%."""
    c = Client(
        id=1,
        email="x@y.com",
        hashed_password="h",
        business_type="ecommerce",
        business_description="Online store",
        products=[{"name": "Widget", "price": 99}],
        whatsapp_number="+919876543210",
    )
    status = onboarding_service.get_setup_status(c)
    assert status["completion_percentage"] == 100
    assert status["steps_pending"] == []


def test_get_setup_status_partial():
    """Client with type+description but no products/whatsapp is 50%."""
    c = Client(
        id=1,
        email="x@y.com",
        hashed_password="h",
        business_type="clinic",
        business_description="A clinic",
    )
    status = onboarding_service.get_setup_status(c)
    assert status["completion_percentage"] == 50
    assert "agent_configured" in status["steps_done"]
    assert "products_added" in status["steps_pending"]
    assert "whatsapp_connected" in status["steps_pending"]


# ── router tests ─────────────────────────────────────────────────────────────


def test_setup_agent_creates_prompt_and_api_key(client, mock_db, mock_settings):
    """POST /onboarding/setup-agent returns client_id, api_key, and setup_status."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=7,
        email="owner@biz.com",
        hashed_password="h",
        is_active=True,
        api_key=None,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result
    mock_db.refresh = AsyncMock(side_effect=lambda obj: None)

    response = client.post(
        "/onboarding/setup-agent",
        json={
            "business_name": "Sharma Textiles",
            "business_type": "textile",
            "business_description": "Premium cotton fabrics",
            "products": [{"name": "Cotton Saree", "price": 1500, "stock": 20}],
            "whatsapp_number": "+919876543210",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["client_id"] == 7
    assert data["api_key"].startswith("vp_")
    assert data["setup_status"]["completion_percentage"] == 100
    assert data["setup_status"]["steps_pending"] == []


def test_setup_agent_without_products_and_whatsapp(client, mock_db, mock_settings):
    """POST /onboarding/setup-agent with only required fields returns 50% completion."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(id=3, email="owner@biz.com", hashed_password="h", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result
    mock_db.refresh = AsyncMock(side_effect=lambda obj: None)

    response = client.post(
        "/onboarding/setup-agent",
        json={
            "business_name": "City Clinic",
            "business_type": "clinic",
            "business_description": "Outpatient clinic in Pune",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["setup_status"]["completion_percentage"] == 50
    assert "products_added" in data["setup_status"]["steps_pending"]
    assert "whatsapp_connected" in data["setup_status"]["steps_pending"]


def test_setup_agent_invalid_business_type(client, mock_db, mock_settings):
    """POST /onboarding/setup-agent returns 422 for an unknown business_type."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/onboarding/setup-agent",
        json={
            "business_name": "Mystery Co",
            "business_type": "alien_shop",
            "business_description": "Out of this world",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


def test_setup_agent_preserves_existing_api_key(client, mock_db, mock_settings):
    """POST /onboarding/setup-agent does not regenerate an already-set api_key."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing_key = "vp_existingkeyvalue"
    existing = Client(
        id=2,
        email="owner@biz.com",
        hashed_password="h",
        is_active=True,
        api_key=existing_key,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result
    mock_db.refresh = AsyncMock(side_effect=lambda obj: None)

    response = client.post(
        "/onboarding/setup-agent",
        json={
            "business_name": "Repeat Biz",
            "business_type": "ecommerce",
            "business_description": "Selling stuff",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["api_key"] == existing_key


def test_setup_agent_requires_auth(client):
    """POST /onboarding/setup-agent returns 401 without a token."""
    response = client.post(
        "/onboarding/setup-agent",
        json={
            "business_name": "No Auth",
            "business_type": "other",
            "business_description": "Test",
        },
    )
    assert response.status_code == 401


def test_onboarding_status_fresh_client(client, mock_db, mock_settings):
    """GET /onboarding/status returns 25% for a client with no onboarding data."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.get("/onboarding/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    data = response.json()
    assert data["completion_percentage"] == 25
    assert data["steps_done"] == ["registered"]


def test_onboarding_status_requires_auth(client):
    """GET /onboarding/status returns 401 without a token."""
    response = client.get("/onboarding/status")
    assert response.status_code == 401
