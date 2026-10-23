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

# --- PANEL VIEW ---
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def check_mod(self, interaction: discord.Interaction):
        config = await database.get_guild_config(db_pool, interaction.guild.id)
        if not config or not config['role_id']:
            await interaction.response.send_message("🇬🇧 Mod role not setup. Admins must run `/add-mod-role`.\n🇷🇺 Роль модератора не настроена.", ephemeral=True)
            return False
        role = interaction.guild.get_role(config['role_id'])
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message("🇬🇧 You don't have the mod role. / 🇷🇺 У вас нет роли модератора.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Start Shift / Начать", style=discord.ButtonStyle.green, custom_id="panel_start")
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_mod(interaction): return
        if await database.get_active_shift(db_pool, interaction.user.id):
            await interaction.response.send_message("🇬🇧 You're already on shift! / 🇷🇺 Вы уже на смене!", ephemeral=True)
            return
        
        await database.start_shift(db_pool, interaction.user.id, interaction.guild.id)
        shift_channels[interaction.user.id] = interaction.channel 
        
        await interaction.response.send_message("🇬🇧 Shift started successfully. / 🇷🇺 Смена успешно начата.", ephemeral=True)

    @discord.ui.button(label="End Shift / Закончить", style=discord.ButtonStyle.red, custom_id="panel_end")
    async def btn_end(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_mod(interaction): return
        duration = await database.end_shift(db_pool, interaction.user.id)
        
        if duration is not None:
            if interaction.user.id in pending_confirmations:
                pending_confirmations[interaction.user.id].cancel()
                del pending_confirmations[interaction.user.id]
            await interaction.response.send_message(f"🇬🇧 Shift ended. Duration: **{duration} min**.\n🇷🇺 Смена окончена. Время: **{duration} мин**.", ephemeral=True)
        else:
            await interaction.response.send_message("🇬🇧 No active shift. / 🇷🇺 Нет активной смены.", ephemeral=True)

    @discord.ui.button(label="My Stats / Моя стата", style=discord.ButtonStyle.blurple, custom_id="panel_stats")
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_mod(interaction): return
        total = await database.get_stats(db_pool, interaction.user.id)
        await interaction.response.send_message(f"🇬🇧 You have completed **{total}** minutes this season.\n🇷🇺 В этом сезоне отработано **{total}** минут.", ephemeral=True)

    @discord.ui.button(label="Weekly / Неделя", style=discord.ButtonStyle.blurple, custom_id="panel_weekly")
    async def btn_weekly(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_mod(interaction): return
        total = await database.get_weekly_stats(db_pool, interaction.user.id)
        await interaction.response.send_message(f"🇬🇧 Weekly stats: **{total}** minutes.\n🇷🇺 За неделю: **{total}** минут.", ephemeral=True)

    @discord.ui.button(label="Active Shifts / Активные смены", style=discord.ButtonStyle.gray, custom_id="panel_active")
    async def btn_active(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🇬🇧 Admins only! / 🇷🇺 Только для админов!", ephemeral=True)
            return
        
        shifts = await database.get_all_active_shifts(db_pool, interaction.guild.id)
        if not shifts:
            await interaction.response.send_message("🇬🇧 No active shifts. / 🇷🇺 Нет активных смен.", ephemeral=True)
            return
        
        active_list = "🟢 **Active Shifts / Активные смены**\n"
        for s in shifts:
            duration = int((datetime.now(timezone.utc) - s['start_time']).total_seconds() / 60)
            active_list += f"<@{s['user_id']}> — {duration} min/мин\n"
        await interaction.response.send_message(active_list, ephemeral=True)

    @discord.ui.button(label="Leaderboard / Таблица лидеров", style=discord.ButtonStyle.gray, custom_id="panel_leaderboard")
    async def btn_leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🇬🇧 Admins only! / 🇷🇺 Только для админов!", ephemeral=True)
            return
        
        rows = await database.get_all_stats(db_pool, interaction.guild.id)
        if not rows:
            await interaction.response.send_message("🇬🇧 No stats yet this season. / 🇷🇺 В этом сезоне статистики пока нет.", ephemeral=True)
            return
        
        leaderboard = "🏆 **Season Leaderboard / Таблица лидеров сезона**\n"
        for idx, row in enumerate(rows[:15]):
            user = bot.get_user(row['user_id'])
            name = user.display_name if user else f"User {row['user_id']}"
            leaderboard += f"**{idx+1}.** {name} — {row['total']} min/мин\n"
        await interaction.response.send_message(leaderboard, ephemeral=True)

# --- CONFIRMATION VIEW ---
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
        shift = await database.get_active_shift(db_pool, self.user_id)
        time_str = ""
        if shift:
            duration = int((datetime.now(timezone.utc) - shift['start_time']).total_seconds() / 60)
            time_str = f" | {duration} min/мин"
            
        await database.update_last_ping(db_pool, self.user_id)
        
        await interaction.message.delete()
        await interaction.response.send_message(f"🇬🇧 Confirmed!{time_str} / 🇷🇺 Подтверждено!{time_str}", ephemeral=True)
        self.stop()

# --- EVENTS & TASKS ---
@bot.event
async def on_ready():
    global db_pool
    try:
        print("Attempting to connect to Supabase...")
        db_pool = await database.get_db_pool()
        await database.init_db(db_pool)
        print("✅ Database connected successfully!")
    except Exception as e:
        print(f"❌ CRITICAL DATABASE ERROR: {e}")
        
    bot.add_view(PanelView())
    shift_monitor.start()
    try:
        await bot.tree.sync()
        print(f"Logged in as {bot.user} with all commands loaded.")
    except Exception as e:
        print(f"Sync error: {e}")

@tasks.loop(minutes=1.0)
async def shift_monitor():
    if not db_pool: return
    
    reset_guilds = await database.check_auto_reset(db_pool)
    for guild_id in reset_guilds:
        config = await database.get_guild_config(db_pool, guild_id)
        if config and config['ping_channel_id']:
            channel = bot.get_channel(config['ping_channel_id'])
            if channel:
                await channel.send("🔄 **A new 28-day season has started!** All stats have been wiped.\n🔄 **Начался новый 28-дневный сезон!** Вся статистика удалена.")
    
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
                
                target_dest = bot.get_channel(ping_channel_id) if ping_channel_id else shift_channels.get(user_id) or bot.get_user(user_id)
                
                if target_dest:
                    try:
                        msg = await target_dest.send(msg_content, view=view)
                        
                        async def wait_for_confirmation(u_id, v, m, p_chan_id):
                            await asyncio.sleep(60)
                            if not v.confirmed:
                                await database.end_shift(db_pool, u_id)
                                try:
                                    await m.delete()
                                except discord.NotFound:
                                    pass 
                                
                                fail_msg = f"<@{u_id}>\n🇬🇧 Failed to confirm. Shift ended.\n🇷🇺 Не подтвердил. Смена закрыта."
                                fallback_chan = bot.get_channel(p_chan_id) if p_chan_id else shift_channels.get(u_id)
                                if fallback_chan:
                                    # This message will now auto-delete after 15 seconds
                                    await fallback_chan.send(fail_msg, delete_after=15)
                            if u_id in pending_confirmations:
                                del pending_confirmations[u_id]

                        task = asyncio.create_task(wait_for_confirmation(user_id, view, msg, ping_channel_id))
                        pending_confirmations[user_id] = task
                        await database.update_last_ping(db_pool, user_id)
                        
                    except discord.Forbidden:
                        await database.end_shift(db_pool, user_id)
                        channel = shift_channels.get(user_id)
                        if channel:
                            await channel.send(f"<@{user_id}>\n🇬🇧 DMs closed! Shift aborted.\n🇷🇺 ЛС закрыты! Смена отменена.", delete_after=15)

# --- ADMIN COMMANDS ---
@bot.tree.command(name="add-mod-role", description="Set the mod role. / Установить роль.")
@discord.app_commands.default_permissions(administrator=True)
async def add_mod_role(interaction: discord.Interaction, role: discord.Role):
    config = await database.get_guild_config(db_pool, interaction.guild.id)
    existing_ping_channel = config['ping_channel_id'] if config else None
    
    await database.set_guild_config(db_pool, interaction.guild.id, role.id, existing_ping_channel)
    
    msg = f"🇬🇧 Mod role set to {role.name}. Run `!sendpanel` in your shift channel to complete setup.\n🇷🇺 Роль: {role.name}. Запустите `!sendpanel` в канале смен для завершения настройки."
    await interaction.response.send_message(msg, ephemeral=True)

@bot.command()
@commands.has_permissions(administrator=True)
async def sendpanel(ctx):
    config = await database.get_guild_config(db_pool, ctx.guild.id)
    if not config or not config['role_id']:
        await ctx.send("❌ Please run `/add-mod-role` first to set the moderator role before sending the panel.", delete_after=10)
        await ctx.message.delete()
        return

    await database.set_guild_config(db_pool, ctx.guild.id, config['role_id'], ctx.channel.id)

    embed = discord.Embed(
        title="🛡️ Moderator Shift Panel",
        description="🇬🇧 Click the buttons below to manage your shift and view statistics.\n🇷🇺 Нажмите кнопки ниже для управления сменой и просмотра статистики.",
        color=0x2b2d31
    )
    await ctx.send(embed=embed, view=PanelView())
    await ctx.message.delete()

@bot.command()
@commands.has_permissions(administrator=True)
async def resetstats(ctx, member: discord.Member):
    await database.reset_user_stats(db_pool, member.id, ctx.guild.id)
    await ctx.send(f"🇬🇧 Stats for {member.mention} have been reset.\n🇷🇺 Статистика {member.mention} была сброшена.")

@bot.command()
@commands.has_permissions(administrator=True)
async def resetallstats(ctx):
    await database.reset_all_stats(db_pool, ctx.guild.id)
    await ctx.send("🇬🇧 All server stats have been completely wiped. A new season starts now.\n🇷🇺 Вся статистика сервера удалена. Начат новый сезон.")

@bot.command()
@commands.has_permissions(administrator=True)
async def factoryreset(ctx):
    await database.factory_reset(db_pool, ctx.guild.id)
    await ctx.send("🇬🇧 **Factory Reset Complete.** All server configurations, roles, channels, and statistics have been permanently wiped. Run `/add-mod-role` to setup the bot again.\n🇷🇺 **Полный сброс завершен.** Все настройки сервера, роли, каналы и статистика безвозвратно удалены. Запустите `/add-mod-role` для новой настройки.")

@bot.command()
async def info(ctx):
    info_text = (
        "🤖 **Moderator Shift Bot v2.0 (Panel Edition)**\n"
        "🇬🇧 Tracks shifts and completely resets all data every 28 days.\n"
        "🇷🇺 Отслеживает смены и полностью сбрасывает данные каждые 28 дней.\n\n"
        "**Admins / Администраторам:**\n"
        "`/add-mod-role` - Setup role / Настройка роли\n"
        "`!sendpanel` - Drop shift panel & set ping channel / Создать панель\n"
        "`!resetstats @user` - Wipe user / Сброс пользователя\n"
        "`!resetallstats` - Wipe stats / Сброс статистики\n"
        "`!factoryreset` - Deep clean bot / Полный сброс бота"
    )
    await ctx.send(info_text)

if __name__ == "__main__":
    keep_alive.keep_alive() 
    bot.run(os.getenv("DISCORD_TOKEN"))
