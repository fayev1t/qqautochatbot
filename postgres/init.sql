-- Initial database setup
-- This file runs automatically when the postgres container starts

-- Create database (already created via POSTGRES_DB env var, but being explicit)
-- CREATE DATABASE qqbot;

-- Connect to qqbot database
\c qqbot

-- Create tables
CREATE TABLE IF NOT EXISTS group_messages (
    id SERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username VARCHAR(255),
    message_content TEXT NOT NULL,
    message_type VARCHAR(50) DEFAULT 'text',
    processed BOOLEAN DEFAULT FALSE,
    vectorized_at TIMESTAMP,
    recalled BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_group_messages_group_id ON group_messages(group_id);
CREATE INDEX IF NOT EXISTS idx_group_messages_user_id ON group_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_group_messages_processed ON group_messages(processed);
CREATE INDEX IF NOT EXISTS idx_group_messages_recalled ON group_messages(recalled);
CREATE INDEX IF NOT EXISTS idx_group_messages_created_at ON group_messages(created_at);

CREATE TABLE IF NOT EXISTS message_vectors (
    id SERIAL PRIMARY KEY,
    message_id INTEGER NOT NULL,
    embedding JSON NOT NULL,
    extra_data JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_message_vectors_message_id ON message_vectors(message_id);

CREATE TABLE IF NOT EXISTS vectorization_jobs (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL UNIQUE,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    total_messages INTEGER DEFAULT 0,
    processed_messages INTEGER DEFAULT 0,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    result_value VARCHAR(255),
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vectorization_jobs_date ON vectorization_jobs(date);
CREATE INDEX IF NOT EXISTS idx_vectorization_jobs_status ON vectorization_jobs(status);
