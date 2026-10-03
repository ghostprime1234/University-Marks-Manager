"""Update user_courses.user_id to UUID referencing auth.users(id) and drop legacy password_hash."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "023_supabase_auth"
down_revision = "022_add_course_gpa_scale"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # 1. Drop existing FK constraint on user_courses.user_id
        op.execute(sa.text("ALTER TABLE user_courses DROP CONSTRAINT IF EXISTS user_courses_user_id_fkey"))
        # 2. Alter user_courses.user_id to UUID
        op.execute(sa.text("ALTER TABLE user_courses ALTER COLUMN user_id TYPE UUID USING user_id::text::uuid"))
        # 3. Add FK constraint referencing auth.users(id) ON DELETE CASCADE
        op.execute(sa.text(
            "ALTER TABLE user_courses ADD CONSTRAINT user_courses_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE"
        ))
        # 4. Remove password_hash column if present in users table
        op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS password_hash"))
    else:
        # SQLite fallback for dev/testing
        with op.batch_alter_table("user_courses") as batch_op:
            batch_op.alter_column("user_id", type_=sa.Uuid())
        try:
            with op.batch_alter_table("users") as batch_op:
                batch_op.drop_column("password_hash")
        except Exception:
            pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(sa.text("ALTER TABLE user_courses DROP CONSTRAINT IF EXISTS user_courses_user_id_fkey"))
        op.execute(sa.text("ALTER TABLE user_courses ALTER COLUMN user_id TYPE INTEGER USING NULL"))
        op.execute(sa.text(
            "ALTER TABLE user_courses ADD CONSTRAINT user_courses_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
        ))
        op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)"))
    else:
        with op.batch_alter_table("user_courses") as batch_op:
            batch_op.alter_column("user_id", type_=sa.Integer())
        try:
            with op.batch_alter_table("users") as batch_op:
                batch_op.add_column(sa.Column("password_hash", sa.String(), nullable=True))
        except Exception:
            pass
