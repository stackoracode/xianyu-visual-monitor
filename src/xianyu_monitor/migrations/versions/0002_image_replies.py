"""Image reply and first-contact rule snapshots.

Revision ID: 0002_image_replies
Revises: 0001_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_image_replies"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("keyword_rules", sa.Column("trigger_type", sa.String(24), nullable=False, server_default="keyword"))
    op.add_column("keyword_rules", sa.Column("send_image", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("reply_intents", sa.Column("first_contact", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("reply_intents", sa.Column("send_image", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("reply_intents", sa.Column("image_status", sa.String(24), nullable=False, server_default="none"))
    op.add_column("reply_intents", sa.Column("image_path", sa.Text(), nullable=True))
    op.add_column("reply_intents", sa.Column("image_sha256", sa.String(64), nullable=True))


def downgrade():
    for column in ["image_sha256", "image_path", "image_status", "send_image", "first_contact"]:
        op.drop_column("reply_intents", column)
    op.drop_column("keyword_rules", "send_image")
    op.drop_column("keyword_rules", "trigger_type")
