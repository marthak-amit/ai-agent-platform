"""Pure (no-DB) tests for the SellerTalk24 billing models: __repr__ and allowed-value constants."""

from datetime import datetime, timezone

from app.models.sellertalk24_billing import (
    BillingMode,
    BillingPlan,
    ClientSubscription,
    ConversationUsageLog,
    PaymentEvent,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentPurpose,
    SubscriptionStatus,
    UsageChannel,
    _in_list,
)


def test_in_list_renders_quoted_sql_check_expression():
    assert _in_list("status", ("a", "b")) == "status IN ('a', 'b')"


def test_status_constants_match_the_spec():
    assert SubscriptionStatus.ALL == ("pending", "active", "expired", "cancelled", "superseded", "revoked")
    assert PaymentOrderStatus.ALL == ("created", "attempted", "paid", "failed", "refunded")
    assert PaymentPurpose.ALL == ("new", "renewal", "upgrade")
    assert UsageChannel.ALL == ("whatsapp", "instagram", "website")
    assert BillingMode.ALL == ("test", "live")


def test_billing_plan_repr():
    plan = BillingPlan(id=1, code="starter_1500", price_paise=459900)
    assert repr(plan) == "<BillingPlan id=1 code='starter_1500' price_paise=459900>"


def test_client_subscription_repr():
    sub = ClientSubscription(
        id=2, client_id=9, status="active", conversations_used=10, conversation_limit=1500
    )
    assert repr(sub) == "<ClientSubscription id=2 client_id=9 status='active' used=10/1500>"


def test_payment_order_repr():
    order = PaymentOrder(id=3, client_id=9, status="created", amount_paise=542682)
    assert repr(order) == "<PaymentOrder id=3 client_id=9 status='created' amount_paise=542682>"


def test_payment_event_repr():
    event = PaymentEvent(id=4, razorpay_event_id="evt_1", event_type="payment.captured", processed=False)
    assert repr(event) == "<PaymentEvent id=4 event_id='evt_1' type='payment.captured' processed=False>"


def test_conversation_usage_log_repr():
    ts = datetime(2026, 10, 6, tzinfo=timezone.utc)
    row = ConversationUsageLog(id=5, client_id=9, channel="whatsapp", window_started_at=ts)
    assert repr(row) == f"<ConversationUsageLog id=5 client_id=9 channel='whatsapp' window_started_at={ts}>"


def test_billing_alert_repr():
    from app.models.sellertalk24_billing import BillingAlert

    alert = BillingAlert(id=6, client_id=9, kind="usage_80", read_at=None)
    assert repr(alert) == "<BillingAlert id=6 client_id=9 kind='usage_80' read=False>"
    assert repr(BillingAlert(id=7, client_id=9, kind="expired", read_at=datetime(2026, 1, 1, tzinfo=timezone.utc))).endswith("read=True>")
