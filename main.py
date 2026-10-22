import discord
from discord.ext import commands, tasks
import os
import random
import asyncio
from datetime import datetime, timezone
import database
import keep_alive

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
db_pool = None
pending_confirmations = {}

shift_channels = {}

@bot.event
async def on_ready():
    global db_pool
    db_pool = await database.get_db_pool()
    await database.init_db(db_pool)
    shift_monitor.start()
    try:
        await bot.tree.sync()
        print(f"Logged in as {bot.user} with all commands loaded.")
    except Exception as e:
        print(f"Sync error: {e}")

@bot.tree.command(name="add-mod-role", description="Set the mod role and optional ping channel. / Установить роль и канал.")
@discord.app_commands.default_permissions(administrator=True)
async def add_mod_role(interaction: discord.Interaction, role: discord.Role, ping_channel: discord.TextChannel = None):
    channel_id = ping_channel.id if ping_channel else None
    await database.set_guild_config(db_pool, interaction.guild.id, role.id, channel_id)
    
    if channel_id:
        msg = f"🇬🇧 Mod role set to {role.name}. Pings will go to {ping_channel.mention}.\n🇷🇺 Роль: {role.name}. Уведомления в {ping_channel.mention}."
    else:
        msg = f"🇬🇧 Mod role set to {role.name}. Pings will be sent via DM.\n🇷🇺 Роль: {role.name}. Уведомления в ЛС."
        
    await interaction.response.send_message(msg, ephemeral=True)

async def is_mod(ctx):
    config = await database.get_guild_config(db_pool, ctx.guild.id)
    if not config or not config['role_id'] or not ctx.guild.get_role(config['role_id']) in ctx.author.roles:
        await ctx.send("🇬🇧 You don't have the mod role. / 🇷🇺 У вас нет роли модератора.")
        return False
    return True

@bot.command()
async def shift(ctx):
    if not await is_mod(ctx): return
    if await database.get_active_shift(db_pool, ctx.author.id):
        await ctx.send("🇬🇧 You're already on shift! / 🇷🇺 Вы уже на смене!")
        return
    
    await database.start_shift(db_pool, ctx.author.id, ctx.guild.id)
    shift_channels[ctx.author.id] = ctx.channel 
    
    msg = f"{ctx.author.mention}\n🇬🇧 Shift started.\n🇷🇺 Смена началась."
    await ctx.send(msg)

@bot.command()
async def endshift(ctx):
    duration = await database.end_shift(db_pool, ctx.author.id)
    if duration is not None:
        if ctx.author.id in pending_confirmations:
            pending_confirmations[ctx.author.id].cancel()
            del pending_confirmations[ctx.author.id]
        msg = f"{ctx.author.mention}\n🇬🇧 Shift ended. Duration: **{duration} min**.\n🇷🇺 Смена окончена. Время: **{duration} мин**."
        await ctx.send(msg)
    else:
        await ctx.send("🇬🇧 No active shift. / 🇷🇺 Нет активной смены.")

@bot.command()
async def stats(ctx):
    total = await database.get_stats(db_pool, ctx.author.id)
    msg = f"{ctx.author.mention}\n🇬🇧 You have completed **{total}** minutes total.\n🇷🇺 Всего отработано **{total}** минут."
    await ctx.send(msg)

@bot.command()
async def weekly(ctx, member: discord.Member = None):
    target = member or ctx.author
    total = await database.get_weekly_stats(db_pool, target.id)
    msg = f"{target.mention}\n🇬🇧 Weekly stats: **{total}** minutes.\n🇷🇺 За неделю: **{total}** минут."
    await ctx.send(msg)

@bot.command()
@commands.has_permissions(administrator=True)
async def allstats(ctx):
    rows = await database.get_all_stats(db_pool, ctx.guild.id)
    if not rows:
        await ctx.send("🇬🇧 No stats yet. / 🇷🇺 Статистики пока нет.")
        return
    
    leaderboard = "🏆 **Leaderboard / Таблица лидеров**\n"
    for idx, row in enumerate(rows[:15]):
        user = bot.get_user(row['user_id'])
        name = user.display_name if user else f"User {row['user_id']}"
        leaderboard += f"**{idx+1}.** {name} — {row['total']} min/мин\n"
    await ctx.send(leaderboard)

@allstats.error
async def allstats_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 Admins only. / Только для администраторов.")

@bot.command()
@commands.has_permissions(administrator=True)
async def allshifts(ctx):
    shifts = await database.get_all_active_shifts(db_pool, ctx.guild.id)
    if not shifts:
        await ctx.send("🇬🇧 No active shifts. / 🇷🇺 Нет активных смен.")
        return
    
    active_list = "🟢 **Active Shifts / Активные смены**\n"
    for s in shifts:
        duration = int((datetime.now(timezone.utc) - s['start_time']).total_seconds() / 60)
        active_list += f"<@{s['user_id']}> — {duration} min/мин\n"
    await ctx.send(active_list)

@allshifts.error
async def allshifts_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 Admins only. / Только для администраторов.")

class ConfirmView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60.0)
        self.user_id = user_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This confirmation is not for you! / Это не для вас!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm / Подтвердить", style=discord.ButtonStyle.green)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await database.update_last_ping(db_pool, self.user_id)
        await interaction.response.send_message("🇬🇧 Confirmed! / 🇷🇺 Подтверждено!", ephemeral=True)
        self.stop()

@tasks.loop(minutes=1.0)
async def shift_monitor():
    if not db_pool: return
    active_shifts = await database.get_all_active_shifts(db_pool)
    now = datetime.now(timezone.utc)
    
    for shift in active_shifts:
        user_id = shift['user_id']
        guild_id = shift['guild_id']
        last_ping = shift['last_ping']
        
        minutes_since_ping = (now - last_ping).total_seconds() / 60
        
        if minutes_since_ping >= 12 or (minutes_since_ping >= 7 and random.random() < 0.3):
            if user_id not in pending_confirmations:
                
                config = await database.get_guild_config(db_pool, guild_id)
                ping_channel_id = config['ping_channel_id'] if config else None
                
                view = ConfirmView(user_id)
                msg_content = f"<@{user_id}>\n🇬🇧 You have 1 minute to confirm activity.\n🇷🇺 У вас есть 1 минута на подтверждение."
                
                target_dest = None
                if ping_channel_id:
                    target_dest = bot.get_channel(ping_channel_id)
                
                if not target_dest:
                    target_dest = bot.get_user(user_id)
                
                if target_dest:
                    try:
                        msg = await target_dest.send(msg_content, view=view)
                        
                        async def wait_for_confirmation(u_id, v, m):
                            await asyncio.sleep(60)
                            if not v.confirmed:
                                await database.end_shift(db_pool, u_id)
                                fail_msg = f"<@{u_id}>\n🇬🇧 Failed to confirm. Shift ended.\n🇷🇺 Не подтвердил. Смена закрыта."
                                
                                # Always post failure in a server channel
                                fallback_chan = bot.get_channel(ping_channel_id) if ping_channel_id else shift_channels.get(u_id)
                                if fallback_chan:
                                    await fallback_chan.send(fail_msg)
                                    
                            if u_id in pending_confirmations:
                                del pending_confirmations[u_id]

                        task = asyncio.create_task(wait_for_confirmation(user_id, view, msg))
                        pending_confirmations[user_id] = task
                        await database.update_last_ping(db_pool, user_id)
                        
                    except discord.Forbidden:
                        await database.end_shift(db_pool, user_id)
                        channel = shift_channels.get(user_id)
                        if channel:
                            err_msg = f"<@{user_id}>\n🇬🇧 DMs closed! Shift aborted.\n🇷🇺 ЛС закрыты! Смена отменена."
                            await channel.send(err_msg)

if __name__ == "__main__":
    keep_alive.keep_alive() 
    bot.run(os.getenv("DISCORD_TOKEN"))
