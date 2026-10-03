-- Migration 023: Update user_courses to reference Supabase Auth (GoTrue) auth.users
--
-- Tasks:
-- 1. Drop the existing foreign key constraint on user_courses.user_id
-- 2. Alter user_courses.user_id to type UUID
-- 3. Add foreign key constraint referencing auth.users(id) with ON DELETE CASCADE
-- 4. Remove legacy password hashing column (password_hash) from users table

-- 1. Drop the existing foreign key constraint on user_courses.user_id
ALTER TABLE user_courses DROP CONSTRAINT IF EXISTS user_courses_user_id_fkey;

-- 2. Alter user_courses.user_id to type UUID
ALTER TABLE user_courses ALTER COLUMN user_id TYPE UUID USING user_id::text::uuid;

-- 3. Add foreign key constraint referencing auth.users(id) with ON DELETE CASCADE
ALTER TABLE user_courses 
    ADD CONSTRAINT user_courses_user_id_fkey 
    FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

-- 4. Ensure index on user_courses.user_id and composite index on (user_id, is_default)
DROP INDEX IF EXISTS idx_user_courses_user_id;
CREATE INDEX IF NOT EXISTS idx_user_courses_user_id ON user_courses(user_id);

DROP INDEX IF EXISTS idx_user_courses_is_default;
CREATE INDEX IF NOT EXISTS idx_user_courses_is_default ON user_courses(user_id, is_default);

-- 5. Remove legacy password hashing column from users table if present
ALTER TABLE IF EXISTS users DROP COLUMN IF EXISTS password_hash;

COMMENT ON TABLE user_courses IS 'Association table linking Supabase auth users to their courses';
COMMENT ON COLUMN user_courses.user_id IS 'UUID referencing auth.users(id)';
