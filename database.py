import asyncpg
import os
from datetime import datetime, timezone

async def get_db_pool():
    db_url = os.getenv("DATABASE_URL")
    return await asyncpg.create_pool(db_url)

async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS mod_roles (
                guild_id BIGINT PRIMARY KEY,
                role_id BIGINT,
                ping_channel_id BIGINT,
                last_reset TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            
            ALTER TABLE mod_roles ADD COLUMN IF NOT EXISTS ping_channel_id BIGINT;
            ALTER TABLE mod_roles ADD COLUMN IF NOT EXISTS last_reset TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
            
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

async def set_guild_config(pool, guild_id, role_id, ping_channel_id):
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO mod_roles (guild_id, role_id, ping_channel_id, last_reset) 
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (guild_id) DO UPDATE 
            SET role_id = $2, ping_channel_id = $3
        ''', guild_id, role_id, ping_channel_id)

async def get_guild_config(pool, guild_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT role_id, ping_channel_id FROM mod_roles WHERE guild_id = $1', guild_id)

async def start_shift(pool, user_id, guild_id):
    async with pool.acquire() as conn:
        now = datetime.now(timezone.utc)
        await conn.execute('''
            INSERT INTO active_shifts (user_id, guild_id, start_time, last_ping) 
            VALUES ($1, $2, $3, $3)
            ON CONFLICT (user_id) DO NOTHING
        ''', user_id, guild_id, now)

async def get_active_shift(pool, user_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM active_shifts WHERE user_id = $1', user_id)

async def get_all_active_shifts(pool, guild_id=None):
    async with pool.acquire() as conn:
        if guild_id:
            return await conn.fetch('SELECT * FROM active_shifts WHERE guild_id = $1', guild_id)
        return await conn.fetch('SELECT * FROM active_shifts')

async def update_last_ping(pool, user_id):
    async with pool.acquire() as conn:
        await conn.execute('UPDATE active_shifts SET last_ping = $1 WHERE user_id = $2', datetime.now(timezone.utc), user_id)

async def end_shift(pool, user_id):
    async with pool.acquire() as conn:
        shift = await conn.fetchrow('SELECT * FROM active_shifts WHERE user_id = $1', user_id)
        if shift:
            duration = int((datetime.now(timezone.utc) - shift['start_time']).total_seconds() / 60)
            await conn.execute('''
                INSERT INTO past_shifts (user_id, guild_id, start_time, end_time, duration_minutes)
                VALUES ($1, $2, $3, $4, $5)
            ''', user_id, shift['guild_id'], shift['start_time'], datetime.now(timezone.utc), duration)
            await conn.execute('DELETE FROM active_shifts WHERE user_id = $1', user_id)
            return duration
        return None

async def get_stats(pool, user_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT duration_minutes FROM past_shifts WHERE user_id = $1', user_id)
        return sum(row['duration_minutes'] for row in rows)

async def get_weekly_stats(pool, user_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT duration_minutes FROM past_shifts 
            WHERE user_id = $1 AND start_time >= NOW() - INTERVAL '7 days'
        ''', user_id)
        return sum(row['duration_minutes'] for row in rows)

async def get_all_stats(pool, guild_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT user_id, SUM(duration_minutes) as total 
            FROM past_shifts WHERE guild_id = $1 
            GROUP BY user_id ORDER BY total DESC
        ''', guild_id)
        return rows

async def reset_all_stats(pool, guild_id):
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM past_shifts WHERE guild_id = $1', guild_id)
        await conn.execute('UPDATE mod_roles SET last_reset = CURRENT_TIMESTAMP WHERE guild_id = $1', guild_id)

async def reset_user_stats(pool, user_id, guild_id):
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM past_shifts WHERE user_id = $1 AND guild_id = $2', user_id, guild_id)

async def check_auto_reset(pool):
    async with pool.acquire() as conn:
        guilds = await conn.fetch("SELECT guild_id FROM mod_roles WHERE CURRENT_TIMESTAMP - last_reset >= INTERVAL '28 days'")
        reset_guilds = []
        for g in guilds:
            guild_id = g['guild_id']
            await conn.execute("DELETE FROM past_shifts WHERE guild_id = $1", guild_id)
            await conn.execute("UPDATE mod_roles SET last_reset = CURRENT_TIMESTAMP WHERE guild_id = $1", guild_id)
            reset_guilds.append(guild_id)
        return reset_guilds
