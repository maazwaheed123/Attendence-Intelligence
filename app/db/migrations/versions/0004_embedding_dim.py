"""Embeddings become 768-dim (Ollama nomic-embed-text, already installed locally).

The original 384-dim columns were planned for bge-small via fastembed; the local
Ollama model avoids a new download. No embeddings existed before this revision,
so the type change loses nothing; the HNSW index is rebuilt for the new width.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _set_dim(dim: int) -> None:
    op.execute(
        f"""
        DROP INDEX IF EXISTS ix_chunks_embedding;
        ALTER TABLE document_chunks
            ALTER COLUMN embedding TYPE vector({dim}) USING NULL;
        ALTER TABLE feedback_examples
            ALTER COLUMN question_embedding TYPE vector({dim}) USING NULL;
        CREATE INDEX ix_chunks_embedding ON document_chunks
            USING hnsw (embedding vector_cosine_ops);
        """
    )


def upgrade() -> None:
    _set_dim(768)


def downgrade() -> None:
    _set_dim(384)
