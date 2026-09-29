-- Runs once on first container start (empty volume).
-- Creates the test database and the extensions both databases need.
-- Least-privilege roles (app_rw, rag_reader) and RLS are created by the Alembic migrations.

CREATE DATABASE attendance_test;

\connect attendance
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

\connect attendance_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
