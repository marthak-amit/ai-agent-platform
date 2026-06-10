"""
Import all ORM models here so Alembic's env.py finds them via Base.metadata.

Order matters: import tables with no FKs first.
"""

from app.models.campaign import Campaign
from app.models.campaign_recipient import CampaignRecipient
from app.models.client import Client
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.follow_up import FollowUp
from app.models.lead import Lead
from app.models.message import Message
from app.models.order import Order
from app.models.payment import Payment
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.restock_notification import RestockNotification
from app.models.stock_log import StockLog
from app.models.knowledge_base import KnowledgeBase
from app.models.usage_log import UsageLog

__all__ = [
    "Campaign", "CampaignRecipient", "Client", "Conversation", "Customer", "FollowUp",
    "KnowledgeBase", "Lead", "Message", "Order", "Payment", "Product", "ProductVariant",
    "RestockNotification", "StockLog", "UsageLog",
]
