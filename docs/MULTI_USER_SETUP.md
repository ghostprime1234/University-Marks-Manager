# Multi-User Setup Guide with Supabase Auth (GoTrue)

This guide explains how authentication and multi-user course management function using Supabase Auth (GoTrue).

## Architecture Overview

Authentication is delegated to Supabase Auth (GoTrue), replacing custom username/password credential tables, bcrypt/argon2 hashing, and custom session tokens.

### 1. Database Schema

- **Supabase Auth (`auth.users`)**:
  Managed by Supabase GoTrue. Each user is identified by a `UUID` primary key (`auth.users.id`).

- **User Courses (`user_courses`)**:
  Association table linking Supabase auth users to degrees and courses:
  - `id`: Primary key (`SERIAL`)
  - `user_id`: Foreign key to `auth.users(id)` of type `UUID` with `ON DELETE CASCADE`
  - `course_id`: Foreign key to `courses(id)` with `ON DELETE CASCADE`
  - `is_default`: Boolean flag indicating the user's active/default degree
  - Unique constraint on `(user_id, course_id)`

```sql
-- Link user_courses to Supabase auth.users
ALTER TABLE user_courses DROP CONSTRAINT IF EXISTS user_courses_user_id_fkey;
ALTER TABLE user_courses ALTER COLUMN user_id TYPE UUID USING user_id::text::uuid;
ALTER TABLE user_courses 
    ADD CONSTRAINT user_courses_user_id_fkey 
    FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_user_courses_user_id ON user_courses(user_id);
CREATE INDEX IF NOT EXISTS idx_user_courses_is_default ON user_courses(user_id, is_default);
```

### 2. Backend Integration & JWT Validation

- In FastAPI, incoming API and HTMX requests include the Bearer JWT token in the `Authorization: Bearer <token>` header, or use the `sb_access_token` cookie for browser navigation.
- The token is verified against `SUPABASE_JWT_SECRET` (HS256) or `SUPABASE_PUBLIC_KEY` (RS256/ES256) via `src.core.auth.verify_supabase_token`.
- Endpoints inject `current_user_id: uuid.UUID = Depends(get_current_user_id)` to filter queries by the authenticated user's UUID.

### 3. Frontend Integration

- The login (`/login`) and signup (`/signup`) views utilize the `@supabase/supabase-js` client SDK:
  - Client signs in or registers via `supabase.auth.signInWithPassword` or `supabase.auth.signUp`.
  - On authentication, the access token is synchronized to `/auth/session` and stored in a secure cookie `sb_access_token`.
  - HTMX requests automatically attach `Authorization: Bearer <token>`.
- Password resets and email verification are handled via Supabase GoTrue helpers (`supabase.auth.resetPasswordForEmail`).
- Profile view maintains degree switching (toggling `is_default`), GPA scales, and data export/import.

### 4. Environment Variables

Configure the following in `.env`:

```env
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_ANON_KEY=your-supabase-anon-key
SUPABASE_JWT_SECRET=your-supabase-jwt-secret
SESSION_SECRET_KEY=secure-random-session-key
```
