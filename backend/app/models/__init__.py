"""
Import all ORM models here so Alembic's env.py finds them via Base.metadata.

Order matters: import tables with no FKs first.
"""

from app.models.plan import Plan
from app.models.campaign import Campaign
from app.models.campaign_recipient import CampaignRecipient
from app.models.client import Client
from app.models.client_monthly_usage import ClientMonthlyUsage
from app.models.conversation import Conversation
from app.models.cost_log import CostLogEntry
from app.models.llm_usage import LLMUsage
from app.models.customer import Customer
from app.models.follow_up import FollowUp
from app.models.ig_comment_reply import IgCommentReply
from app.models.lead import Lead
from app.models.message import Message
from app.models.message_template import MessageTemplate
from app.models.order import Order
from app.models.order_audit_log import OrderAuditLog
from app.models.order_line_item import OrderLineItem
from app.models.payment_proof import PaymentProof
from app.models.sellertalk24_billing import (
    BillingAdminLog,
    BillingAlert,
    BillingPlan,
    BillingJobRun,
    BillingOpsEvent,
    ClientSubscription,
    ConversationUsageLog,
    CreditNote,
    CreditNoteCounter,
    Invoice,
    InvoiceCounter,
    PaymentEvent,
    PaymentOrder,
)
from app.models.stock_reservation import StockReservation
from app.models.payment import Payment
from app.models.product import Product
from app.models.style_reference import StyleReference
from app.models.product_variant import ProductVariant
from app.models.photo_generation_log import PhotoGenerationLog
from app.models.restock_notification import RestockNotification
from app.models.stock_log import StockLog
from app.models.knowledge_base import KnowledgeBase
from app.models.usage_log import UsageLog
from app.models.user import User

__all__ = [
    "BillingAdminLog", "BillingAlert", "BillingPlan", "ClientSubscription", "ConversationUsageLog",
    "BillingJobRun", "BillingOpsEvent", "CreditNote", "CreditNoteCounter",
    "Invoice", "InvoiceCounter", "PaymentEvent", "PaymentOrder",
    "Campaign", "CampaignRecipient", "Client", "ClientMonthlyUsage", "Conversation", "CostLogEntry",
    "Customer", "FollowUp", "IgCommentReply", "KnowledgeBase", "LLMUsage", "Lead", "Message", "MessageTemplate", "Order", "OrderAuditLog", "OrderLineItem", "PaymentProof", "StockReservation",
    "Payment", "PhotoGenerationLog", "Plan", "Product", "ProductVariant", "RestockNotification", "StockLog",
    "StyleReference", "UsageLog", "User",
]
