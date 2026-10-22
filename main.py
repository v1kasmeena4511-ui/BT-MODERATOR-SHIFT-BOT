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

# Channel to send errors to if DM fails (set dynamically when a shift starts)
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

@bot.tree.command(name="add-mod-role", description="Set the moderator role. / Установить роль модератора.")
@discord.app_commands.default_permissions(administrator=True)
async def add_mod_role(interaction: discord.Interaction, role: discord.Role):
    await database.set_mod_role(db_pool, interaction.guild.id, role.id)
    msg = f"🇬🇧 Moderator role set to {role.name}\n🇷🇺 Роль модератора установлена на {role.name}"
    await interaction.response.send_message(msg, ephemeral=True)

async def is_mod(ctx):
    role_id = await database.get_mod_role(db_pool, ctx.guild.id)
    if not role_id or not ctx.guild.get_role(role_id) in ctx.author.roles:
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
    shift_channels[ctx.author.id] = ctx.channel # Save channel in case DMs fail
    
    msg = f"{ctx.author.mention}\n🇬🇧 Shift started. I will DM you to confirm activity.\n🇷🇺 Смена началась. Я напишу в ЛС для проверки."
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
async def allstats(ctx):
    rows = await database.get_all_stats(db_pool, ctx.guild.id)
    if not rows:
        await ctx.send("🇬🇧 No stats yet. / 🇷🇺 Статистики пока нет.")
        return
    
    leaderboard = "🏆 **Leaderboard / Таблица лидеров**\n"
    for idx, row in enumerate(rows[:10]):
        user = bot.get_user(row['user_id'])
        name = user.display_name if user else f"User {row['user_id']}"
        leaderboard += f"**{idx+1}.** {name} — {row['total']} min/мин\n"
    await ctx.send(leaderboard)

@bot.command()
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

@bot.command()
async def info(ctx):
    info_text = (
        "🤖 **Moderator Shift Bot v1.0**\n"
        "🇬🇧 Tracks shifts, verifies activity via DM, and logs statistics.\n"
        "🇷🇺 Отслеживает смены, проверяет активность через ЛС и ведет статистику.\n\n"
        "**Commands / Команды:**\n"
        "`!shift` - Start / Начать\n"
        "`!endshift` - End / Закончить\n"
        "`!stats` - Personal stats / Личная статистика\n"
        "`!allstats` - Leaderboard / Таблица\n"
        "`!allshifts` - Current shifts / Текущие смены"
    )
    await ctx.send(info_text)

class ConfirmView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60.0)
        self.user_id = user_id
        self.confirmed = False

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
        last_ping = shift['last_ping']
        
        minutes_since_ping = (now - last_ping).total_seconds() / 60
        
        # Random ping between 7 and 12 minutes
        if minutes_since_ping >= 12 or (minutes_since_ping >= 7 and random.random() < 0.3):
            if user_id not in pending_confirmations:
                user = bot.get_user(user_id)
                if user:
                    try:
                        view = ConfirmView(user_id)
                        dm_msg = "🇬🇧 You have 1 minute to confirm activity.\n🇷🇺 У вас есть 1 минута на подтверждение."
                        msg = await user.send(dm_msg, view=view)
                        
                        async def wait_for_confirmation(u_id, v, m):
                            await asyncio.sleep(60)
                            if not v.confirmed:
                                await database.end_shift(db_pool, u_id)
                                channel = shift_channels.get(u_id)
                                if channel:
                                    fail_msg = f"<@{u_id}>\n🇬🇧 Failed to confirm. Shift ended.\n🇷🇺 Не подтвердил. Смена закрыта."
                                    await channel.send(fail_msg)
                            if u_id in pending_confirmations:
                                del pending_confirmations[u_id]

                        task = asyncio.create_task(wait_for_confirmation(user_id, view, msg))
                        pending_confirmations[user_id] = task
                        await database.update_last_ping(db_pool, user_id)
                        
                    except discord.Forbidden:
                        # User has DMs disabled. End shift and notify in the server.
                        await database.end_shift(db_pool, user_id)
                        channel = shift_channels.get(user_id)
                        if channel:
                            err_msg = f"<@{user_id}>\n🇬🇧 Your DMs are closed! Enable them in Privacy Settings. Shift aborted.\n🇷🇺 Ваши ЛС закрыты! Включите их в настройках. Смена отменена."
                            await channel.send(err_msg)

if __name__ == "__main__":
    keep_alive.keep_alive() 
    bot.run(os.getenv("DISCORD_TOKEN"))
