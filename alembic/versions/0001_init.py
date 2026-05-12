"""init

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), index=True),
        sa.Column("symbol", sa.String(16), index=True),
        sa.Column("side", sa.String(4)),
        sa.Column("entry", sa.Float),
        sa.Column("sl", sa.Float),
        sa.Column("tp1", sa.Float),
        sa.Column("tp2", sa.Float),
        sa.Column("rr", sa.Float),
        sa.Column("score", sa.Float, default=0.0),
        sa.Column("ml_prob", sa.Float, nullable=True),
        sa.Column("taken", sa.Integer, default=0),
        sa.Column("reject_reason", sa.String(200), nullable=True),
        sa.Column("reasons", sa.Text, nullable=True),
        sa.Column("features", sa.JSON, nullable=True),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.Integer, nullable=True, index=True),
        sa.Column("ticket", sa.Integer, nullable=True, index=True),
        sa.Column("symbol", sa.String(16), index=True),
        sa.Column("side", sa.String(4)),
        sa.Column("qty", sa.Float),
        sa.Column("entry", sa.Float),
        sa.Column("sl", sa.Float),
        sa.Column("tp", sa.Float),
        sa.Column("open_ts", sa.DateTime(timezone=True), index=True),
        sa.Column("close_ts", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("exit", sa.Float, nullable=True),
        sa.Column("pnl_ccy", sa.Float, nullable=True),
        sa.Column("pnl_r", sa.Float, nullable=True),
        sa.Column("outcome", sa.String(8), nullable=True),
        sa.Column("features", sa.JSON, nullable=True),
    )
    op.create_table(
        "bars",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(16), index=True),
        sa.Column("timeframe", sa.String(4), index=True),
        sa.Column("ts", sa.DateTime(timezone=True), index=True),
        sa.Column("open", sa.Float),
        sa.Column("high", sa.Float),
        sa.Column("low", sa.Float),
        sa.Column("close", sa.Float),
        sa.Column("volume", sa.Float, default=0.0),
    )
    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("trained_at", sa.DateTime(timezone=True), index=True),
        sa.Column("n_trades", sa.Integer),
        sa.Column("auc", sa.Float, nullable=True),
        sa.Column("brier", sa.Float, nullable=True),
        sa.Column("path", sa.String(300)),
        sa.Column("notes", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("model_versions")
    op.drop_table("bars")
    op.drop_table("trades")
    op.drop_table("signals")
