import asyncpg
import os
from datetime import datetime, timezone

async def get_db_pool():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is not set.")
    # asyncpg handles Render's internal database URL natively
    return await asyncpg.create_pool(db_url)

async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS mod_roles (
                guild_id BIGINT PRIMARY KEY,
                role_id BIGINT
            );
            CREATE TABLE IF NOT EXISTS active_shifts (
                user_id BIGINT PRIMARY KEY,
                guild_id BIGINT,
                start_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                last_ping TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS past_shifts (
                shift_id SERIAL PRIMARY KEY,
                user_id BIGINT,
                guild_id BIGINT,
                start_time TIMESTAMP WITH TIME ZONE,
                end_time TIMESTAMP WITH TIME ZONE,
                duration_minutes INT
            );
        ''')

async def set_mod_role(pool, guild_id, role_id):
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO mod_roles (guild_id, role_id) 
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET role_id = $2
        ''', guild_id, role_id)

async def get_mod_role(pool, guild_id):
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT role_id FROM mod_roles WHERE guild_id = $1', guild_id)

async def start_shift(pool, user_id, guild_id):
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO active_shifts (user_id, guild_id) VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
        ''', user_id, guild_id)

async def get_active_shift(pool, user_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM active_shifts WHERE user_id = $1', user_id)

async def get_all_active_shifts(pool):
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT * FROM active_shifts')

async def update_last_ping(pool, user_id):
    async with pool.acquire() as conn:
        await conn.execute('UPDATE active_shifts SET last_ping = CURRENT_TIMESTAMP WHERE user_id = $1', user_id)

async def end_shift(pool, user_id):
    async with pool.acquire() as conn:
        shift = await conn.fetchrow('SELECT * FROM active_shifts WHERE user_id = $1', user_id)
        if shift:
            duration = int((datetime.now(timezone.utc) - shift['start_time']).total_seconds() / 60)
            await conn.execute('''
                INSERT INTO past_shifts (user_id, guild_id, start_time, end_time, duration_minutes)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP, $4)
            ''', user_id, shift['guild_id'], shift['start_time'], duration)
            await conn.execute('DELETE FROM active_shifts WHERE user_id = $1', user_id)
            return duration
        return None

async def get_stats(pool, user_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT duration_minutes FROM past_shifts WHERE user_id = $1', user_id)
        return sum(row['duration_minutes'] for row in rows)
