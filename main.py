import discord
from discord.ext import commands, tasks
import os
import random
import asyncio
from datetime import datetime, timezone
import database
import keep_alive # New import

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
db_pool = None
pending_confirmations = {}

@bot.event
async def on_ready():
    global db_pool
    db_pool = await database.get_db_pool()
    await database.init_db(db_pool)
    shift_monitor.start()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    print(f"Logged in as {bot.user}")

@bot.tree.command(name="add-mod-role", description="Set the moderator role for shifts.")
@discord.app_commands.default_permissions(administrator=True)
async def add_mod_role(interaction: discord.Interaction, role: discord.Role):
    await database.set_mod_role(db_pool, interaction.guild.id, role.id)
    await interaction.response.send_message(f"Moderator role set to {role.name}", ephemeral=True)

async def is_mod(ctx):
    role_id = await database.get_mod_role(db_pool, ctx.guild.id)
    if not role_id:
        await ctx.send("The server admin hasn't set a mod role yet using `/add-mod-role`.")
        return False
    role = ctx.guild.get_role(role_id)
    if role in ctx.author.roles:
        return True
    await ctx.send("You do not have the required moderator role to start a shift.")
    return False

@bot.command()
async def shift(ctx):
    if not await is_mod(ctx): return
    active = await database.get_active_shift(db_pool, ctx.author.id)
    if active:
        await ctx.send("You are already on an active shift!")
        return
    await database.start_shift(db_pool, ctx.author.id, ctx.guild.id)
    await ctx.send(f"{ctx.author.mention}, your shift has started. I will DM you periodically to confirm you are active.")

@bot.command()
async def endshift(ctx):
    duration = await database.end_shift(db_pool, ctx.author.id)
    if duration is not None:
        await ctx.send(f"{ctx.author.mention}, your shift has ended. Duration: {duration} minutes.")
        if ctx.author.id in pending_confirmations:
            pending_confirmations[ctx.author.id].cancel()
            del pending_confirmations[ctx.author.id]
    else:
        await ctx.send("You don't have an active shift.")

@bot.command()
async def stats(ctx):
    total_minutes = await database.get_stats(db_pool, ctx.author.id)
    await ctx.send(f"{ctx.author.mention}, you have completed a total of {total_minutes} minutes on shift.")

class ConfirmView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60.0)
        self.user_id = user_id
        self.confirmed = False

    @discord.ui.button(label="Confirm Activity", style=discord.ButtonStyle.green)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await database.update_last_ping(db_pool, self.user_id)
        await interaction.response.send_message("Activity confirmed! Keep up the good work.", ephemeral=True)
        self.stop()

@tasks.loop(minutes=1.0)
async def shift_monitor():
    active_shifts = await database.get_all_active_shifts(db_pool)
    now = datetime.now(timezone.utc)
    
    for shift in active_shifts:
        user_id = shift['user_id']
        last_ping = shift['last_ping']
        
        minutes_since_ping = (now - last_ping).total_seconds() / 60
        
        if minutes_since_ping >= 12 or (minutes_since_ping >= 7 and random.random() < 0.3):
            if user_id not in pending_confirmations:
                user = bot.get_user(user_id)
                if user:
                    try:
                        view = ConfirmView(user_id)
                        msg = await user.send("Are you still active on your shift? You have 1 minute to confirm.", view=view)
                        
                        async def wait_for_confirmation(u_id, v, m):
                            await asyncio.sleep(60)
                            if not v.confirmed:
                                await database.end_shift(db_pool, u_id)
                                await m.channel.send("You failed to confirm in time. Your shift has been automatically ended.")
                            if u_id in pending_confirmations:
                                del pending_confirmations[u_id]

                        task = asyncio.create_task(wait_for_confirmation(user_id, view, msg))
                        pending_confirmations[user_id] = task
                        await database.update_last_ping(db_pool, user_id)
                    except discord.Forbidden:
                        await database.end_shift(db_pool, user_id)

if __name__ == "__main__":
    # Start the background web server before starting the bot
    keep_alive.keep_alive() 
    bot.run(os.getenv("DISCORD_TOKEN"))
