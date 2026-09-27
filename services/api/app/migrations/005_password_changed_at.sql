-- Sessions issued before the last password change are rejected (sign-out everywhere).
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at timestamptz NOT NULL DEFAULT now();
