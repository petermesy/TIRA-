import sqlalchemy as sa
from alembic import op

revision = "0005_article_images"
down_revision = "0004_v3_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("articles", sa.Column("image_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    op.drop_column("articles", "image_url")
