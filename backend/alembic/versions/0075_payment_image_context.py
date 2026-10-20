"""Payment-image context: pending image, image-burst reply stamp, last payment instruction time, proof reclassification flag

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-20

conv=60 (ORD-2026-0077): screenshots were mistaken for product photos and the customer's correction was ignored.

  conversations.pending_proof_image_id      the first image of a "payment screenshot or product photo?" question that is still unanswered
  conversations.image_batch_reply_at        when the last reply to an image was sent: images within 60 s of it get no reply of their own
  conversations.last_payment_instruction_at when payment instructions were last sent (the "recent payment instructions" prior)
  payment_proofs.reclassified_by_customer   the customer said "this is the payment" about an image we had not taken as a proof
"""

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the columns (idempotent)."""
    op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_proof_image_id INTEGER")
    op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS image_batch_reply_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_payment_instruction_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE payment_proofs ADD COLUMN IF NOT EXISTS reclassified_by_customer BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    """Drop the columns."""
    op.execute("ALTER TABLE payment_proofs DROP COLUMN IF EXISTS reclassified_by_customer")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS last_payment_instruction_at")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS image_batch_reply_at")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS pending_proof_image_id")
