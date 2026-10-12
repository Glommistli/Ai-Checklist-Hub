"""Add recoverable workflow actions, model usage and replan proposals."""
from alembic import op
import sqlalchemy as sa
from app.models.workflow import WorkflowAction, ModelCall, ReplanProposal
revision = "8d4a62c0f719"
down_revision = "7c2d91a8e504"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    for model in (WorkflowAction, ModelCall, ReplanProposal):
        model.__table__.create(bind, checkfirst=True)

def downgrade():
    bind = op.get_bind()
    for model in (ReplanProposal, ModelCall, WorkflowAction):
        model.__table__.drop(bind, checkfirst=True)
