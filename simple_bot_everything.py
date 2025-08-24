
import os
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
from dotenv import load_dotenv
import aiosqlite

# =====================================================
#                      CONFIG
# =====================================================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# IDs pulled from .env (you said these are set correctly)
VERIFY_CHANNEL_ID = int(os.getenv("VERIFY_CHANNEL_ID", "0"))
VERIFY_ROLE_ID    = int(os.getenv("VERIFY_ROLE_ID", "0"))
COUNTING_CHANNEL_ID = int(os.getenv("COUNTING_CHANNEL_ID", "0"))
HUMAN_COUNTER_CHANNEL_ID = int(os.getenv("HUMAN_COUNTER_CHANNEL_ID", "0"))
TICKETS_CATEGORY_ID = int(os.getenv("TICKETS_CATEGORY_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))  # who can see/close tickets too

COMMAND_PREFIX = "$"
DB_PATH = "economy.db"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.invites = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

# =====================================================
#                      DATABASE
# =====================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # economy + xp + invites
        await db.execute("""
        CREATE TABLE IF NOT EXISTS economy (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            invites INTEGER DEFAULT 0,
            last_daily TEXT
        )""")
        # missions
        await db.execute("""
        CREATE TABLE IF NOT EXISTS missions (
            user_id INTEGER,
            code TEXT,
            progress INTEGER DEFAULT 0,
            goal INTEGER NOT NULL,
            reward INTEGER NOT NULL,
            PRIMARY KEY (user_id, code)
        )""")
        await db.commit()

# =====================================================
#                      UTIL
# =====================================================
async def econ_upsert(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO economy (user_id, balance, xp, level, invites, last_daily)
            VALUES (?, 0, 0, 1, 0, NULL)
            ON CONFLICT(user_id) DO NOTHING
        """, (user_id,))
        await db.commit()

async def add_coins(user_id: int, amount: int):
    await econ_upsert(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE economy SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def add_xp(user_id: int, amount: int):
    await econ_upsert(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT xp, level FROM economy WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        xp, level = row
        xp += amount
        # simple curve
        if xp >= level * 100:
            level += 1
            xp = 0
        await db.execute("UPDATE economy SET xp=?, level=? WHERE user_id=?", (xp, level, user_id))
        await db.commit()

async def get_balance(user_id: int) -> int:
    await econ_upsert(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM economy WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_level_xp(user_id: int) -> Tuple[int, int]:
    await econ_upsert(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT level, xp FROM economy WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (1, 0)

async def add_invite_for(user_id: int, amt: int = 1):
    await econ_upsert(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE economy SET invites = invites + ? WHERE user_id=?", (amt, user_id))
        await db.commit()

# =====================================================
#                      MISSIONS
# =====================================================
MISSION_POOL = {
    "send_20":  ("Send 20 messages", 20, 150),
    "work_5":   ("Use $work 5 times", 5, 120),
    "coin_3":   ("Win coinflip 3 times", 3, 200),
    "invite_1": ("Invite 1 member", 1, 250),
}

async def ensure_mission(user_id: int):
    """Guarantee the user has at least one active mission (up to 2)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM missions WHERE user_id=?", (user_id,))
        (count,) = await cur.fetchone()
        need = max(0, 2 - count)
        if need > 0:
            keys = list(MISSION_POOL.keys())
            random.shuffle(keys)
            for code in keys:
                # avoid duplicates
                cur2 = await db.execute("SELECT 1 FROM missions WHERE user_id=? AND code=?", (user_id, code))
                if await cur2.fetchone():
                    continue
                title, goal, reward = MISSION_POOL[code]
                await db.execute("INSERT INTO missions (user_id, code, progress, goal, reward) VALUES (?, ?, 0, ?, ?)",
                                 (user_id, code, goal, reward))
                need -= 1
                if need == 0:
                    break
        await db.commit()

async def mission_progress(user_id: int, code: str, amt: int = 1):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE missions SET progress = MIN(goal, progress + ?) WHERE user_id=? AND code=?",
                         (amt, user_id, code))
        await db.commit()

async def mission_claimable(user_id: int) -> List[Tuple[str, int]]:
    """Return list of (code, reward) that are ready to claim (progress >= goal)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT code, reward, progress, goal FROM missions WHERE user_id=?", (user_id,))
        out = []
        async for code, reward, progress, goal in cur:
            if progress >= goal:
                out.append((code, reward))
        return out

async def mission_remove(user_id: int, code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM missions WHERE user_id=? AND code=?", (user_id, code))
        await db.commit()

# =====================================================
#                      INVITE TRACKING
# =====================================================
# cache: guild_id -> {invite_code: uses}
invite_cache: Dict[int, Dict[str, int]] = {}

async def refresh_invites_for_guild(guild: discord.Guild):
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        return
    cache = {}
    for inv in invites:
        cache[inv.code] = inv.uses or 0
    invite_cache[guild.id] = cache

@bot.event
async def on_invite_create(invite: discord.Invite):
    await refresh_invites_for_guild(invite.guild)

@bot.event
async def on_invite_delete(invite: discord.Invite):
    await refresh_invites_for_guild(invite.guild)

@bot.event
async def on_member_join(member: discord.Member):
    # detect inviter by comparing uses delta
    guild = member.guild
    try:
        new_invites = await guild.invites()
    except discord.Forbidden:
        return
    old = invite_cache.get(guild.id, {})
    inviter: Optional[discord.Member] = None
    for inv in new_invites:
        uses = inv.uses or 0
        old_uses = old.get(inv.code, 0)
        if uses > old_uses:
            inviter = inv.inviter
            break
    await refresh_invites_for_guild(guild)
    if inviter and not inviter.bot:
        await add_invite_for(inviter.id, 1)

# =====================================================
#                      COUNTING CHANNEL
# =====================================================
# We store last number in memory per guild; you said IDs are correct
count_state: Dict[int, int] = {}  # guild_id -> expected next number
last_counter_user: Dict[int, int] = {}  # guild_id -> user_id who last counted

async def init_counting(guild: discord.Guild):
    if COUNTING_CHANNEL_ID == 0:
        return
    count_state[guild.id] = count_state.get(guild.id, 1)
    last_counter_user[guild.id] = 0

# =====================================================
#                      VERIFY BUTTON
# =====================================================
class VerifyView(discord.ui.View):
    def __init__(self, role_id: int, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.role_id = role_id
        self.add_item(VerifyButton(role_id))

class VerifyButton(discord.ui.Button):
    def __init__(self, role_id: int):
        super().__init__(style=discord.ButtonStyle.success, label="✅ Verify")
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("Role not found. Please tell an admin.", ephemeral=True)
        if role in interaction.user.roles:
            return await interaction.response.send_message("You're already verified!", ephemeral=True)
        try:
            await interaction.user.add_roles(role, reason="Verification button")
            await interaction.response.send_message("🫂You're verified!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to give that role.", ephemeral=True)

async def ensure_verify_message(guild: discord.Guild):
    if VERIFY_CHANNEL_ID == 0 or VERIFY_ROLE_ID == 0:
        return
    ch = guild.get_channel(VERIFY_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return
    async for msg in ch.history(limit=10):
        if msg.author == bot.user and msg.components:
            # already posted
            return
    # post fresh
    embed = discord.Embed(title="Verify to Access the Server", description="Click the button below to get verified.", color=discord.Color.green())
    await ch.send(embed=embed, view=VerifyView(VERIFY_ROLE_ID))

# =====================================================
#                      HUMAN COUNTER
# =====================================================
@tasks.loop(minutes=5)
async def human_counter_task():
    for guild in bot.guilds:
        if HUMAN_COUNTER_CHANNEL_ID == 0:
            continue
        ch = guild.get_channel(HUMAN_COUNTER_CHANNEL_ID)
        if isinstance(ch, discord.VoiceChannel) or isinstance(ch, discord.StageChannel) or isinstance(ch, discord.TextChannel):
            humans = sum(1 for m in guild.members if not m.bot)
            try:
                await ch.edit(name=f"humans-{humans}")
            except Exception:
                pass

# =====================================================
#                      EVENTS
# =====================================================
@bot.event
async def on_ready():
    await init_db()
    for guild in bot.guilds:
        await refresh_invites_for_guild(guild)
        await init_counting(guild)
        await ensure_verify_message(guild)
    if not human_counter_task.is_running():
        human_counter_task.start()
    print(f"✅ {bot.user} online | prefix: {COMMAND_PREFIX}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # XP on every user message
    await add_xp(message.author.id, 5)

    # Counting channel logic
    if COUNTING_CHANNEL_ID and message.channel.id == COUNTING_CHANNEL_ID:
        guild_id = message.guild.id
        expected = count_state.get(guild_id, 1)
        # must be pure integer content
        try:
            num = int(message.content.strip())
        except ValueError:
            # delete and ignore
            try:
                await message.delete()
            except Exception:
                pass
            return

        if last_counter_user.get(guild_id, 0) == message.author.id or num != expected:
            # wrong user twice in a row or wrong number -> reset
            count_state[guild_id] = 1
            last_counter_user[guild_id] = 0
            try:
                await message.add_reaction("❌")
            except Exception:
                pass
        else:
            # good
            count_state[guild_id] = expected + 1
            last_counter_user[guild_id] = message.author.id
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        return  # don't treat as command

    await bot.process_commands(message)

# =====================================================
#                      HELP (INTERACTIVE)
# =====================================================
help_categories = {
    "1": "**Fun & Games**\nmemes, truth, dare, connect4, tictactoe",
    "2": "**Economy**\nbal, daily, work, coin, missions",
    "3": "**Leveling**\nlevel, lb (xp|invites|coins)",
    "4": "**Admin**\nverify, ticket, counters",
    "5": "**Server Info**\ncoming soon"
}

@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="📖 Nebuverse Bot Help",
        description="Type a **number (1–5)** to open a category.\nType `exit` to close this menu.",
        color=discord.Color.green()
    )
    for k, v in help_categories.items():
        embed.add_field(name=f"{k}️⃣", value=v, inline=False)

    msg = await ctx.send(embed=embed)

    def check(m: discord.Message):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        reply = await bot.wait_for("message", check=check, timeout=15.0)
    except asyncio.TimeoutError:
        await msg.edit(content="⏰ Help menu expired.", embed=None)
        return

    if reply.content.lower() == "exit":
        await msg.edit(content="❌ Help menu closed.", embed=None)
        return
    elif reply.content in help_categories:
        cat_embed = discord.Embed(
            title=f"📂 Category {reply.content}",
            description=help_categories[reply.content],
            color=discord.Color.blurple()
        )
        await ctx.send(embed=cat_embed)
    else:
        await ctx.send("❌ Invalid input. Menu auto-expired.")

# =====================================================
#                      ECONOMY
# =====================================================
@bot.command()
async def bal(ctx, member: Optional[discord.Member] = None):
    target = member or ctx.author
    bal = await get_balance(target.id)
    embed = discord.Embed(title="💰 Balance", description=f"{target.mention} has **{bal}** coins.", color=discord.Color.gold())
    await ctx.send(embed=embed)

@bot.command()
async def daily(ctx):
    await econ_upsert(ctx.author.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT last_daily FROM economy WHERE user_id=?", (ctx.author.id,))
        (last_daily,) = await cur.fetchone()
        now = datetime.utcnow()
        if last_daily:
            last = datetime.fromisoformat(last_daily)
            if now - last < timedelta(hours=20):  # 20h cooldown
                left = timedelta(hours=20) - (now - last)
                hours = int(left.total_seconds() // 3600)
                mins = int((left.total_seconds() % 3600) // 60)
                return await ctx.send(f"⏳ Daily not ready. Try again in **{hours}h {mins}m**.")
        reward = random.randint(150, 300)
        await db.execute("UPDATE economy SET balance=balance+?, last_daily=? WHERE user_id=?", (reward, now.isoformat(), ctx.author.id))
        await db.commit()
    await ctx.send(embed=discord.Embed(title="🎁 Daily Reward", description=f"You received **{reward}** coins!", color=discord.Color.green()))
    await mission_progress(ctx.author.id, "work_5", 1)  # small progress

@bot.command()
async def work(ctx):
    reward = random.randint(50, 120)
    await add_coins(ctx.author.id, reward)
    await mission_progress(ctx.author.id, "work_5", 1)
    await ctx.send(embed=discord.Embed(title="🛠️ Work", description=f"You earned **{reward}** coins!", color=discord.Color.green()))

@bot.command()
async def coin(ctx, choice: Optional[str] = None, amount: Optional[int] = None):
    """$coin heads 50 — coinflip gamble"""
    if choice is None or amount is None:
        return await ctx.send("Usage: `$coin <heads|tails> <amount>`")
    choice = choice.lower()
    if choice not in ("heads", "tails"):
        return await ctx.send("Pick `heads` or `tails`.")
    amt = max(1, int(amount))
    bal = await get_balance(ctx.author.id)
    if bal < amt:
        return await ctx.send("💸 You don't have enough coins.")
    result = random.choice(["heads", "tails"])
    if result == choice:
        await add_coins(ctx.author.id, amt)
        await mission_progress(ctx.author.id, "coin_3", 1)
        msg = f"🪙 **{result.upper()}!** You won **+{amt}**."
    else:
        await add_coins(ctx.author.id, -amt)
        msg = f"🪙 **{result.upper()}!** You lost **-{amt}**."
    await ctx.send(embed=discord.Embed(title="Coinflip", description=msg, color=discord.Color.gold()))

@bot.command()
async def missions(ctx):
    await ensure_mission(ctx.author.id)
    # list missions
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT code, progress, goal, reward FROM missions WHERE user_id=?", (ctx.author.id,))
        lines = []
        async for code, progress, goal, reward in cur:
            title, _, _ = MISSION_POOL.get(code, (code, 0, 0))
            done = "✅" if progress >= goal else ""
            lines.append(f"{done} **{title}** — {progress}/{goal} — Reward: {reward} coins")
    claimable = await mission_claimable(ctx.author.id)
    footer = "\nType `$claim` to collect finished missions." if claimable else ""
    desc = "\n".join(lines) if lines else "No missions yet. New ones will appear soon."
    await ctx.send(embed=discord.Embed(title="📜 Missions", description=desc + footer, color=discord.Color.teal()))

@bot.command()
async def claim(ctx):
    ready = await mission_claimable(ctx.author.id)
    if not ready:
        return await ctx.send("Nothing to claim yet.")
    total = 0
    for code, reward in ready:
        await add_coins(ctx.author.id, reward)
        total += reward
        await mission_remove(ctx.author.id, code)
    await ensure_mission(ctx.author.id)
    await ctx.send(embed=discord.Embed(title="✅ Claimed", description=f"You claimed **{total}** coins. New missions assigned.", color=discord.Color.green()))

# =====================================================
#                      LEVEL / LB
# =====================================================
@bot.command()
async def level(ctx, member: Optional[discord.Member] = None):
    target = member or ctx.author
    lvl, xp = await get_level_xp(target.id)
    await ctx.send(embed=discord.Embed(title="⭐ Level", description=f"{target.mention}: Level **{lvl}**, XP **{xp}/{lvl*100}**", color=discord.Color.purple()))

@bot.command(name="lb")
async def leaderboard(ctx, kind: Optional[str] = "xp"):
    kind = (kind or "xp").lower()
    if kind not in ("xp", "invites", "coins"):
        return await ctx.send("Usage: `$lb xp|invites|coins`")
    field = {"xp": "xp", "invites": "invites", "coins": "balance"}[kind]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT user_id, {field} FROM economy ORDER BY {field} DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        return await ctx.send("No data yet.")
    desc = []
    for i, (uid, val) in enumerate(rows, start=1):
        user = ctx.guild.get_member(uid) or await bot.fetch_user(uid)
        name = user.display_name if isinstance(user, discord.Member) else user.name
        desc.append(f"**{i}.** {name} — {val}")
    await ctx.send(embed=discord.Embed(title=f"🏆 Leaderboard — {kind}", description="\n".join(desc), color=discord.Color.gold()))

# =====================================================
#                      FUN COMMANDS
# =====================================================
@bot.command()
async def memes(ctx):
    await ctx.send("🤣 Here's a meme (placeholder)")

@bot.command()
async def truth(ctx):
    await ctx.send("🗣️ Truth: What's the most chaotic thing you've coded?")

@bot.command()
async def dare(ctx):
    await ctx.send("🔥 Dare: Rename yourself to `I Love Bugs` for 10 minutes.")

# =====================================================
#                      GAMES
# =====================================================
class Connect4View(discord.ui.View):
    def __init__(self, player1: discord.Member, player2: discord.Member, timeout=60):
        super().__init__(timeout=timeout)
        self.board = [[0 for _ in range(7)] for _ in range(6)]
        self.players = [player1, player2]
        self.turn = 0
        self.game_over = False
        for col in range(7):
            self.add_item(Connect4Button(col))

    async def make_embed(self):
        symbols = {0: "⚪", 1: "🔴", 2: "🟡"}
        desc = "\n".join("".join(symbols[cell] for cell in row) for row in self.board)
        e = discord.Embed(title="🎮 Connect4", description=desc, color=discord.Color.blue())
        e.set_footer(text=f"{self.players[self.turn].display_name}'s turn")
        return e

    def drop_piece(self, col: int, piece: int) -> bool:
        for row in reversed(self.board):
            if row[col] == 0:
                row[col] = piece
                return True
        return False

    def check_winner(self, piece: int) -> bool:
        # horizontal / vertical / diag
        for r in range(6):
            for c in range(7):
                if c + 3 < 7 and all(self.board[r][c+i] == piece for i in range(4)):
                    return True
                if r + 3 < 6 and all(self.board[r+i][c] == piece for i in range(4)):
                    return True
                if r + 3 < 6 and c + 3 < 7 and all(self.board[r+i][c+i] == piece for i in range(4)):
                    return True
                if r - 3 >= 0 and c + 3 < 7 and all(self.board[r-i][c+i] == piece for i in range(4)):
                    return True
        return False

class Connect4Button(discord.ui.Button):
    def __init__(self, col: int):
        super().__init__(label=str(col+1), style=discord.ButtonStyle.secondary)
        self.col = col

    async def callback(self, interaction: discord.Interaction):
        view: Connect4View = self.view
        if view.game_over:
            return await interaction.response.send_message("❌ Game already ended.", ephemeral=True)
        if interaction.user != view.players[view.turn]:
            return await interaction.response.send_message("Not your turn!", ephemeral=True)

        piece = view.turn + 1
        if not view.drop_piece(self.col, piece):
            return await interaction.response.send_message("⚠️ Column is full!", ephemeral=True)

        if view.check_winner(piece):
            view.game_over = True
            embed = await view.make_embed()
            embed.title = f"🎉 {interaction.user.display_name} wins!"
            for item in view.children:
                item.disabled = True
            return await interaction.response.edit_message(embed=embed, view=view)

        # draw?
        if all(all(cell != 0 for cell in row) for row in view.board):
            view.game_over = True
            for item in view.children:
                item.disabled = True
            embed = await view.make_embed()
            embed.title = "🤝 Draw!"
            return await interaction.response.edit_message(embed=embed, view=view)

        view.turn = 1 - view.turn
        embed = await view.make_embed()
        await interaction.response.edit_message(embed=embed, view=view)

@bot.command()
async def connect4(ctx, opponent: discord.Member):
    if opponent == ctx.author:
        return await ctx.send("❌ You can't play against yourself!")
    view = Connect4View(ctx.author, opponent, timeout=60)
    embed = await view.make_embed()
    await ctx.send(embed=embed, view=view)

class TicTacToeButton(discord.ui.Button):
    def __init__(self, x: int, y: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="⬜", row=y)
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view
        if view.game_over:
            return await interaction.response.send_message("Game over!", ephemeral=True)
        if interaction.user != view.players[view.turn]:
            return await interaction.response.send_message("Not your turn!", ephemeral=True)
        if view.board[self.y][self.x] != 0:
            return await interaction.response.send_message("Cell already taken!", ephemeral=True)

        piece = view.turn + 1
        view.board[self.y][self.x] = piece
        self.label = "❌" if piece == 1 else "⭕"
        self.style = discord.ButtonStyle.danger if piece == 1 else discord.ButtonStyle.primary
        self.disabled = True

        if view.check_winner(piece):
            view.game_over = True
            for item in view.children:
                item.disabled = True
            embed = view.make_embed(f"🎉 {interaction.user.display_name} wins!")
            return await interaction.response.edit_message(embed=embed, view=view)

        if all(all(cell != 0 for cell in row) for row in view.board):
            view.game_over = True
            embed = view.make_embed("🤝 It's a draw!")
            for item in view.children:
                item.disabled = True
            return await interaction.response.edit_message(embed=embed, view=view)

        view.turn = 1 - view.turn
        embed = view.make_embed()
        await interaction.response.edit_message(embed=embed, view=view)

class TicTacToeView(discord.ui.View):
    def __init__(self, player1: discord.Member, player2: discord.Member, timeout=60):
        super().__init__(timeout=timeout)
        self.players = [player1, player2]
        self.turn = 0
        self.game_over = False
        self.board = [[0, 0, 0] for _ in range(3)]
        for y in range(3):
            for x in range(3):
                self.add_item(TicTacToeButton(x, y))

    def check_winner(self, piece: int) -> bool:
        b = self.board
        return any(all(cell == piece for cell in row) for row in b) or \
               any(all(row[i] == piece for row in b) for i in range(3)) or \
               all(b[i][i] == piece for i in range(3)) or \
               all(b[i][2-i] == piece for i in range(3))

    def make_embed(self, title="❌⭕ TicTacToe"):
        embed = discord.Embed(title=title, color=discord.Color.orange())
        embed.set_footer(text=f"{self.players[self.turn].display_name}'s turn")
        return embed

@bot.command()
async def tictactoe(ctx, opponent: discord.Member):
    if opponent == ctx.author:
        return await ctx.send("❌ You can't play against yourself!")
    view = TicTacToeView(ctx.author, opponent, timeout=60)
    embed = view.make_embed()
    await ctx.send(embed=embed, view=view)

# =====================================================
#                      ADMIN / VERIFY / TICKETS
# =====================================================
@bot.command()
@commands.has_permissions(manage_guild=True)
async def verify(ctx):
    """Force-post the verify button again in the verify channel."""
    await ensure_verify_message(ctx.guild)
    await ctx.send("Posted verify button (if not present already).")

@bot.command()
async def ticket(ctx, *, reason: str = "No reason provided"):
    """Create a private ticket channel for the user under the Tickets category."""
    if TICKETS_CATEGORY_ID == 0:
        return await ctx.send("Ticket category not configured.")
    category = ctx.guild.get_channel(TICKETS_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return await ctx.send("Ticket category invalid.")
    # permissions: only author + staff role + bot can see
    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
        ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True, manage_messages=True)
    }
    staff_role = ctx.guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel_name = f"ticket-{ctx.author.name}".lower().replace(" ", "-")
    ticket = await category.create_text_channel(name=channel_name, overwrites=overwrites)
    embed = discord.Embed(title="🎫 Support Ticket", description=f"Reason: {reason}\nType `close` to close this ticket.", color=discord.Color.blue())
    await ticket.send(ctx.author.mention, embed=embed)

    def check(m: discord.Message):
        return m.channel == ticket and (m.author == ctx.author or (staff_role and staff_role in m.author.roles)) and m.content.lower().strip() == "close"

    try:
        await bot.wait_for("message", check=check, timeout=3600)  # 1 hour
        await ticket.send("Closing ticket…")
        await asyncio.sleep(1)
        await ticket.delete()
    except asyncio.TimeoutError:
        await ticket.send("⏰ No activity. Ticket will be archived soon.")

# =====================================================
#                      STARTUP
# =====================================================
def warn_missing_ids():
    problems = []
    if VERIFY_CHANNEL_ID == 0 or VERIFY_ROLE_ID == 0:
        problems.append("Verify channel/role not set (VERIFY_CHANNEL_ID / VERIFY_ROLE_ID).")
    if COUNTING_CHANNEL_ID == 0:
        problems.append("Counting channel not set (COUNTING_CHANNEL_ID).")
    if HUMAN_COUNTER_CHANNEL_ID == 0:
        problems.append("Human counter channel not set (HUMAN_COUNTER_CHANNEL_ID).")
    if TICKETS_CATEGORY_ID == 0:
        problems.append("Tickets category not set (TICKETS_CATEGORY_ID).")
    if STAFF_ROLE_ID == 0:
        problems.append("Staff role not set (STAFF_ROLE_ID).")
    if problems:
        print("⚠️ Config warnings:")
        for p in problems:
            print(" -", p)

if __name__ == "__main__":
    warn_missing_ids()
    if not TOKEN:
        raise SystemExit("Missing BOT_TOKEN in .env")
    bot.run(TOKEN)
