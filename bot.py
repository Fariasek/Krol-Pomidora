import os
import asyncio
import random
import sqlite3

import discord
from discord.ext import commands
from dotenv import load_dotenv


# =========================================================
# KONFIGURACJA
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "!"

COUNTDOWN_SECONDS = 2

# Automatyczny rzut bota:
# minimum 15 sekund, maksimum 3 minuty
AUTO_THROW_MIN_SECONDS = 15
AUTO_THROW_MAX_SECONDS = 180

OPERATOR_ROLE_NAMES = {
    "Opiekun Zabaw",
    "Dyrekcja",
    "Dyrektor",
}


# =========================================================
# INTENTS
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    case_insensitive=True,
    help_command=None,
    allowed_mentions=discord.AllowedMentions(
        users=True,
        roles=False,
        everyone=False
    )
)


# =========================================================
# BAZA DANYCH
# =========================================================

db = sqlite3.connect("pomidor.db")
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS players (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, user_id)
)
""")

db.commit()


# Migracja starszej bazy
columns = db.execute(
    "PRAGMA table_info(players)"
).fetchall()

column_names = [
    column["name"]
    for column in columns
]

if "active" not in column_names:
    db.execute("""
    ALTER TABLE players
    ADD COLUMN active INTEGER NOT NULL DEFAULT 1
    """)
    db.commit()


# =========================================================
# FUNKCJE BAZY
# =========================================================

def player_exists(guild_id, user_id):

    row = db.execute("""
        SELECT 1
        FROM players
        WHERE guild_id = ?
        AND user_id = ?
    """, (guild_id, user_id)).fetchone()

    return row is not None


def player_is_active(guild_id, user_id):

    row = db.execute("""
        SELECT active
        FROM players
        WHERE guild_id = ?
        AND user_id = ?
    """, (guild_id, user_id)).fetchone()

    if row is None:
        return False

    return row["active"] == 1


def add_or_activate_player(guild_id, user_id):

    if player_exists(guild_id, user_id):

        db.execute("""
            UPDATE players
            SET active = 1
            WHERE guild_id = ?
            AND user_id = ?
        """, (guild_id, user_id))

    else:

        db.execute("""
            INSERT INTO players
            (guild_id, user_id, points, active)
            VALUES (?, ?, 0, 1)
        """, (guild_id, user_id))

    db.commit()


def deactivate_player(guild_id, user_id):

    db.execute("""
        UPDATE players
        SET active = 0
        WHERE guild_id = ?
        AND user_id = ?
    """, (guild_id, user_id))

    db.commit()


def get_active_players(guild_id):

    return db.execute("""
        SELECT user_id, points, active
        FROM players
        WHERE guild_id = ?
        AND active = 1
        ORDER BY points DESC, user_id ASC
    """, (guild_id,)).fetchall()


def get_all_players(guild_id):

    return db.execute("""
        SELECT user_id, points, active
        FROM players
        WHERE guild_id = ?
        ORDER BY points DESC, user_id ASC
    """, (guild_id,)).fetchall()


def add_points(guild_id, user_id, amount=1):

    db.execute("""
        UPDATE players
        SET points = points + ?
        WHERE guild_id = ?
        AND user_id = ?
    """, (amount, guild_id, user_id))

    db.commit()


def add_point(guild_id, user_id):
    add_points(guild_id, user_id, 1)


def reset_points(guild_id):

    db.execute("""
        UPDATE players
        SET points = 0
        WHERE guild_id = ?
    """, (guild_id,))

    db.commit()


def reset_everything(guild_id):

    db.execute("""
        DELETE FROM players
        WHERE guild_id = ?
    """, (guild_id,))

    db.commit()


# =========================================================
# STAN GRY
# =========================================================

class TomatoSlot:
    def __init__(self, number, name, emoji="🍅", catch_points=1):
        self.number = number
        self.name = name
        self.emoji = emoji
        self.catch_points = catch_points
        self.enabled = False
        self.holder_id = None
        self.in_flight = False
        self.target_id = None
        self.thrower_id = None
        self.catch_event = None
        self.auto_throw_task = None
        self.started_at = 0.0


class TomatoGame:
    def __init__(self):
        self.active = False
        self.host_id = None
        self.channel_id = None
        self.tomatoes = [
            TomatoSlot(1, "Pomidor #1"),
            TomatoSlot(2, "Pomidor #2"),
            TomatoSlot(3, "Pomidor #3"),
            TomatoSlot(4, "Złoty Pomidor", emoji="✨🍅", catch_points=2),
        ]
        # klucz: (rzucający_id, cel_id), wartość: liczba ręcznych !rzuc
        # Licznik trwa od !startpomidor do !stop i obejmuje wszystkie pomidory.
        self.manual_throw_counts = {}


games = {}


def get_game(guild_id):
    if guild_id not in games:
        games[guild_id] = TomatoGame()
    return games[guild_id]


def reset_tomato_slot(slot, enabled=None):
    task = slot.auto_throw_task
    if task is not None and not task.done():
        task.cancel()
    if slot.catch_event is not None:
        slot.catch_event.set()
    slot.holder_id = None
    slot.in_flight = False
    slot.target_id = None
    slot.thrower_id = None
    slot.catch_event = None
    slot.auto_throw_task = None
    slot.started_at = 0.0
    if enabled is not None:
        slot.enabled = enabled


def enabled_tomatoes(game):
    return [slot for slot in game.tomatoes if slot.enabled]


def held_tomatoes(game, user_id):
    return [
        slot for slot in enabled_tomatoes(game)
        if slot.holder_id == user_id and not slot.in_flight
    ]


def tomato_label(slot):
    return f"{slot.emoji} **{slot.name}**"


def tomato_short(slot):
    return f"{slot.emoji} {slot.name}"


# =========================================================
# FUNKCJE POMOCNICZE
# =========================================================

def member_name(guild, user_id):

    member = guild.get_member(user_id)

    if member:
        return member.display_name

    return "Nieznany gracz"


def is_operator(member):

    if member.guild_permissions.administrator:
        return True

    if member.guild_permissions.manage_guild:
        return True

    roles = {
        role.name
        for role in member.roles
    }

    return bool(
        roles.intersection(
            OPERATOR_ROLE_NAMES
        )
    )


async def operator_required(ctx):

    if not isinstance(
        ctx.author,
        discord.Member
    ):
        return False

    if is_operator(ctx.author):
        return True

    await ctx.send(
        "🍅🚫 **Ta komenda jest dostępna tylko dla Opiekuna Zabaw lub Dyrekcji.**"
    )

    return False


# =========================================================
# TEKSTY
# =========================================================

PLAYER_THROW_TEXTS = [

    "🍅💨 **{thrower} rzuca pomidorem w {target_ping}!**",

    "🎯🍅 **{thrower} celuje... pomidor leci w {target_ping}!**",

    "😈🍅 **{thrower} wybiera kolejny cel — {target_ping}!**",

    "💥🍅 **UWAGA! {target_ping}, {thrower} właśnie rzuca!**",

]


BOT_THROW_TEXTS = [

    "👑🍅 **Król Pomidora wychyla się z ukrycia... {target_ping}, ŁAP!**",

    "😈🍅 **CISZA... aż nagle Król Pomidora atakuje {target_ping}!**",

    "🍅💨 **NADLATUJE! Król Pomidora wybrał {target_ping}!**",

    "💥🍅 **NIESPODZIANKA! Pomidor leci prosto w {target_ping}!**",

]


CATCH_TEXTS = [

    "🍅✨ **ZŁAPANY! {target_name} zdobywa +1 punkt!**",

    "👏🍅 **Świetny refleks! {target_name} zgarnia +1 punkt!**",

    "🔥🍅 **Piękne złapanie! +1 punkt dla {target_name}!**",

    "👑🍅 **{target_name} łapie pomidora i zdobywa +1 punkt!**",

]


PLAYER_HIT_TEXTS = [

    "💥🍅 **POMIDOROWA KATASTROFA! {target_name} nie zdążył złapać pomidora.**\n"
    "🎯 **{thrower_name} zdobywa +1 punkt za trafienie i zachowuje pomidora.**",

    "🎯🍅 **CELNY RZUT! {target_name} nie złapał pomidora.**\n"
    "🏆 **+1 punkt dla {thrower_name}. Pomidor pozostaje u rzucającego.**",

    "🍅💥 **PLASK! {target_name} był za wolny.**\n"
    "😈 **{thrower_name} zdobywa +1 punkt i rzuca dalej.**",

]


BOT_MISS_TEXTS = [

    "💥🍅 **{target_name} nie zdążył złapać pomidora Króla!**",

    "🍅💨 **Za późno! {target_name} nie złapał pomidora.**",

    "😈🍅 **Król Pomidora trafił, ale punktów za to nie zbiera.**",

]


CHEAT_TEXTS = [

    "🤨🍅 **Ten pomidor nie leci w Twoją stronę!**",

    "🚨🍅 **POMIDOROWA POLICJA! Poczekaj na swoją kolej.**",

    "😏🍅 **Sprytnie, ale to nie Twój pomidor do złapania.**",

]


NO_TOMATO_TEXTS = [

    "🍅 **Najpierw trzeba mieć pomidora, żeby nim rzucać.**",

    "🤨🍅 **Nie jesteś aktualnym posiadaczem pomidora.**",

    "🍅🚫 **Nie tak szybko! Pomidor należy obecnie do kogoś innego.**",

]


# =========================================================
# AUTOMATYCZNY RZUT KRÓLA
# =========================================================

def cancel_auto_throw(slot):
    task = slot.auto_throw_task
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()
    slot.auto_throw_task = None


def schedule_auto_throw(guild_id, slot):
    game = get_game(guild_id)
    if not game.active or not slot.enabled or slot.holder_id is not None or slot.in_flight:
        return
    cancel_auto_throw(slot)
    slot.auto_throw_task = asyncio.create_task(auto_throw_loop(guild_id, slot.number))


async def auto_throw_loop(guild_id, tomato_number):
    game = get_game(guild_id)
    slot = game.tomatoes[tomato_number - 1]
    delay = random.randint(AUTO_THROW_MIN_SECONDS, AUTO_THROW_MAX_SECONDS)
    print(f"🍅 Pomidor #{tomato_number} Króla rzuci za {delay} sekund.")
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if not game.active or not slot.enabled or slot.holder_id is not None or slot.in_flight:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    channel = guild.get_channel(game.channel_id)
    if channel is None:
        return
    possible_targets = []
    for row in get_active_players(guild_id):
        member = guild.get_member(row["user_id"])
        if member is not None and not member.bot:
            possible_targets.append(member)
    if not possible_targets:
        await channel.send("🍅💤 **Król Pomidora nie ma obecnie żadnych aktywnych graczy.**")
        return
    target = random.choice(possible_targets)
    await perform_throw(channel, guild, target, slot, thrower=None, bot_throw=True)


# =========================================================
# START BOTA
# =========================================================

@bot.event
async def on_ready():

    print("========================================")
    print("🍅 KRÓL POMIDORA ZALOGOWANY")
    print(f"👑 Bot: {bot.user}")
    print("========================================")

    await bot.change_presence(
        activity=discord.Game(
            name="🍅 czeka na swoją ofiarę..."
        )
    )


# =========================================================
# DOŁĄCZ
# =========================================================

@bot.command(
    name="dolacz",
    aliases=["dołącz"]
)
async def dolacz(ctx):

    if not ctx.guild:
        return


    guild_id = ctx.guild.id
    user_id = ctx.author.id


    if player_is_active(
        guild_id,
        user_id
    ):

        await ctx.send(
            f"🍅 {ctx.author.mention}, **już jesteś w aktywnej puli!**"
        )

        return


    existed = player_exists(
        guild_id,
        user_id
    )


    add_or_activate_player(
        guild_id,
        user_id
    )


    if existed:

        await ctx.send(
            f"🍅👑 {ctx.author.mention} **wraca do zabawy!**\n"
            "Twój dotychczasowy wynik został zachowany."
        )

    else:

        await ctx.send(
            f"🍅👑 {ctx.author.mention} **dołącza do Króla Pomidora!**"
        )


# =========================================================
# WYJDŹ
# =========================================================

@bot.command(
    name="wyjdz",
    aliases=["wyjdź"]
)
async def wyjdz(ctx):

    if not ctx.guild:
        return


    guild_id = ctx.guild.id
    user_id = ctx.author.id


    if not player_is_active(
        guild_id,
        user_id
    ):

        await ctx.send(
            "🍅 **Nie znajdujesz się obecnie w aktywnej puli.**"
        )

        return


    game = get_game(
        guild_id
    )


    if game.active and any(slot.holder_id == user_id for slot in enabled_tomatoes(game)):
        await ctx.send(
            "🍅🚫 **Nie możesz wyjść, kiedy masz pomidora. Najpierw go rzuć.**"
        )
        return

    if game.active and any(slot.in_flight and slot.target_id == user_id for slot in enabled_tomatoes(game)):
        await ctx.send(
            "🍅💨 **Co najmniej jeden pomidor właśnie leci w Twoją stronę! Najpierw dokończ rzut.**"
        )
        return


    deactivate_player(
        guild_id,
        user_id
    )


    await ctx.send(
        f"👋🍅 {ctx.author.mention} **opuszcza aktywną pulę.**\n"
        "🏆 Twój wynik zostaje zachowany."
    )


# =========================================================
# DODAJ
# =========================================================

@bot.command(name="dodaj")
async def dodaj(
    ctx,
    member: discord.Member = None
):

    if not ctx.guild:
        return


    if not await operator_required(ctx):
        return


    if member is None:

        await ctx.send(
            "🍅 Użycie: `!dodaj @osoba`"
        )

        return


    if member.bot:

        await ctx.send(
            "🤖🍅 **Botów nie dodajemy do zabawy.**"
        )

        return


    if player_is_active(
        ctx.guild.id,
        member.id
    ):

        await ctx.send(
            f"🍅 {member.mention} **już znajduje się w aktywnej puli.**"
        )

        return


    add_or_activate_player(
        ctx.guild.id,
        member.id
    )


    await ctx.send(
        f"✅🍅 **Dodano {member.mention} do aktywnej puli.**"
    )


# =========================================================
# USUŃ Z AKTYWNEJ PULI
# Punkty zostają. Jeśli osoba ma pomidora, wraca on do Króla.
# =========================================================

@bot.command(name="usun", aliases=["usuń"])
async def usun(ctx, member: discord.Member = None):
    if not ctx.guild or not await operator_required(ctx):
        return
    if member is None:
        await ctx.send("🍅 Użycie: `!usun @osoba`")
        return
    if not player_is_active(ctx.guild.id, member.id):
        await ctx.send(f"🍅 {member.mention} **nie znajduje się w aktywnej puli.**")
        return
    game = get_game(ctx.guild.id)
    if any(slot.in_flight and slot.target_id == member.id for slot in enabled_tomatoes(game)):
        await ctx.send("🍅🚫 **Co najmniej jeden pomidor właśnie leci w tę osobę. Poczekaj na zakończenie rzutu.**")
        return
    returned = []
    for slot in enabled_tomatoes(game):
        if slot.holder_id == member.id:
            slot.holder_id = None
            returned.append(slot.number)
            schedule_auto_throw(ctx.guild.id, slot)
    deactivate_player(ctx.guild.id, member.id)
    text = f"🗑️🍅 **Usunięto {member.mention} z aktywnej puli.**\n🏆 Zdobyte punkty zostały zachowane."
    if returned:
        text += "\n👑 Do Króla wraca: " + ", ".join(f"**Pomidor #{n}**" for n in returned) + "."
    await ctx.send(text)


# =========================================================
# LISTA
# =========================================================

@bot.command(
    name="lista",
    aliases=["gracze"]
)
async def lista(ctx):

    if not ctx.guild:
        return


    players = get_active_players(
        ctx.guild.id
    )


    if not players:

        await ctx.send(
            "🍅 **Aktywna pula jest pusta.**"
        )

        return


    lines = []

    for index, row in enumerate(
        players,
        start=1
    ):

        lines.append(
            f"**{index}.** <@{row['user_id']}> "
            f"— 🍅 **{row['points']} pkt**"
        )


    embed = discord.Embed(
        title="🍅 Aktywna pula Króla Pomidora",
        description="\n".join(lines),
        color=discord.Color.red()
    )


    await ctx.send(
        embed=embed
    )


# =========================================================
# START / DODATKOWE POMIDORY
# =========================================================

async def activate_extra_tomato(ctx, slot_number):
    if not ctx.guild or not await operator_required(ctx):
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send("🍅💤 **Najpierw rozpocznij zabawę przez `!startpomidor`.**")
        return

    slot = game.tomatoes[slot_number - 1]

    if slot.enabled:
        await ctx.send(f"{slot.emoji} **{slot.name} jest już aktywny!**")
        return

    reset_tomato_slot(slot, enabled=True)

    if slot.catch_points == 2:
        await ctx.send(
            "# ✨🍅 ZŁOTY POMIDOR WCHODZI DO GRY!\n"
            "Za jego złapanie otrzymujesz **+2 punkty**.\n"
            "🤲 Nadal łapiesz go zwykłą komendą `!lapie`."
        )
    else:
        await ctx.send(
            f"# 🍅 {slot.name.upper()} WCHODZI DO GRY!\n"
            "Od teraz może latać równocześnie z pozostałymi pomidorami.\n"
            "🤲 Jeśli kilka leci w Ciebie naraz, wpisujesz `!lapie` osobno na każdy."
        )

    schedule_auto_throw(ctx.guild.id, slot)


async def stop_extra_tomato(ctx, slot_number):
    if not ctx.guild or not await operator_required(ctx):
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send("🍅💤 **Zabawa aktualnie nie trwa.**")
        return

    slot = game.tomatoes[slot_number - 1]

    if not slot.enabled:
        await ctx.send(f"{slot.emoji} **{slot.name} jest już wyłączony.**")
        return

    reset_tomato_slot(slot, enabled=False)
    await ctx.send(f"🛑 {slot.emoji} **{slot.name} został zatrzymany.** Pozostałe pomidory grają dalej.")


@bot.command(name="startpomidor", aliases=["start"])
async def startpomidor(ctx):
    if not ctx.guild or not await operator_required(ctx):
        return

    game = get_game(ctx.guild.id)

    if game.active:
        await ctx.send("🍅🚫 **Król Pomidora już trwa!**")
        return

    if not get_active_players(ctx.guild.id):
        await ctx.send("🍅 **Nie ma żadnych aktywnych graczy.**")
        return

    game.active = True
    game.host_id = ctx.author.id
    game.channel_id = ctx.channel.id
    game.manual_throw_counts.clear()

    # Nowa sesja zaczyna się zawsze tylko z Pomidorem #1.
    for slot in game.tomatoes:
        reset_tomato_slot(slot, enabled=(slot.number == 1))

    embed = discord.Embed(
        title="👑🍅 KRÓL POMIDORA ROZPOCZĘTY!",
        description=(
            "👑 Na początku aktywny jest **Pomidor #1** i należy do Króla Pomidora.\n"
            "Może zaatakować w dowolnym momencie w ciągu maksymalnie **3 minut**.\n\n"
            "🤲 Łapanie: `!lapie` lub `!łapie`\n"
            "🍅 Jeśli kilka pomidorów leci w Ciebie jednocześnie, wpisujesz `!lapie` **tyle razy, ile ich leci**.\n\n"
            "✅ Zwykły pomidor = **+1 pkt za złapanie**.\n"
            "✨🍅 Złoty Pomidor = **+2 pkt za złapanie**.\n"
            "🎯 Niezłapanie rzutu gracza = **+1 pkt dla rzucającego**."
        ),
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)
    schedule_auto_throw(ctx.guild.id, game.tomatoes[0])


@bot.command(name="drugipomidor", aliases=["pomidor2", "drugi"])
async def drugipomidor(ctx):
    await activate_extra_tomato(ctx, 2)


@bot.command(name="trzecipomidor", aliases=["pomidor3", "trzeci"])
async def trzecipomidor(ctx):
    await activate_extra_tomato(ctx, 3)


@bot.command(name="zlotypomidor", aliases=["złotypomidor", "zloty", "złoty"])
async def zlotypomidor(ctx):
    await activate_extra_tomato(ctx, 4)


@bot.command(name="stoppomidor2", aliases=["stop2"])
async def stoppomidor2(ctx):
    await stop_extra_tomato(ctx, 2)


@bot.command(name="stoppomidor3", aliases=["stop3"])
async def stoppomidor3(ctx):
    await stop_extra_tomato(ctx, 3)


@bot.command(name="stopzlotypomidor", aliases=["stopzłotypomidor", "stopzloty", "stopzłoty"])
async def stopzlotypomidor(ctx):
    await stop_extra_tomato(ctx, 4)


# =========================================================
# RZUT GRACZA
# =========================================================

@bot.command(name="rzuc", aliases=["rzuć"])
async def rzuc(ctx, target: discord.Member = None):
    if not ctx.guild:
        return
    game = get_game(ctx.guild.id)
    if not game.active:
        await ctx.send("🍅💤 **Zabawa aktualnie nie trwa.**")
        return
    if ctx.channel.id != game.channel_id:
        return
    available = held_tomatoes(game, ctx.author.id)
    if not available:
        await ctx.send(random.choice(NO_TOMATO_TEXTS))
        return
    if target is None:
        await ctx.send("🍅 Użycie: `!rzuc @osoba`")
        return
    if target.bot:
        await ctx.send("🤖🍅 **Nie rzucamy w boty.**")
        return
    if target.id == ctx.author.id:
        await ctx.send("😂🍅 **Nie możesz rzucić w samego siebie.**")
        return
    if not player_is_active(ctx.guild.id, target.id):
        await ctx.send(f"🍅🚫 {target.mention} **nie znajduje się w aktywnej puli.**")
        return

    throw_key = (ctx.author.id, target.id)
    manual_count = game.manual_throw_counts.get(throw_key, 0)
    if manual_count >= 2:
        await ctx.send(
            f"🍅🚫 **W {target.display_name} rzucałeś/aś już 2 razy ręcznie podczas tej rozgrywki.**\n"
            "Wybierz inną osobę albo użyj `!losuj` — losowanie nie podlega temu limitowi."
        )
        return
    game.manual_throw_counts[throw_key] = manual_count + 1
    slot = available[0]
    await perform_throw(ctx.channel, ctx.guild, target, slot, thrower=ctx.author, bot_throw=False)


@bot.command(name="losuj")
async def losuj(ctx):
    if not ctx.guild:
        return
    game = get_game(ctx.guild.id)
    if not game.active:
        await ctx.send("🍅💤 **Zabawa aktualnie nie trwa.**")
        return
    if ctx.channel.id != game.channel_id:
        return
    available = held_tomatoes(game, ctx.author.id)
    if not available:
        await ctx.send(random.choice(NO_TOMATO_TEXTS))
        return
    possible_targets = []
    for row in get_active_players(ctx.guild.id):
        if row["user_id"] == ctx.author.id:
            continue
        member = ctx.guild.get_member(row["user_id"])
        if member is not None and not member.bot:
            possible_targets.append(member)
    if not possible_targets:
        await ctx.send("🍅 **Nie ma kogo wylosować.**")
        return
    target = random.choice(possible_targets)
    slot = available[0]
    await perform_throw(ctx.channel, ctx.guild, target, slot, thrower=ctx.author, bot_throw=False)


# =========================================================
# MECHANIKA RZUTU — każdy pomidor ma własny stan
# =========================================================

async def perform_throw(channel, guild, target, slot, thrower=None, bot_throw=False):
    game = get_game(guild.id)

    if not game.active or not slot.enabled or slot.in_flight:
        return

    cancel_auto_throw(slot)

    slot.in_flight = True
    slot.target_id = target.id
    slot.thrower_id = thrower.id if thrower is not None else None
    slot.catch_event = asyncio.Event()
    slot.started_at = asyncio.get_running_loop().time()
    current_event = slot.catch_event

    if bot_throw:
        throw_text = random.choice(BOT_THROW_TEXTS).format(target_ping=target.mention)
    else:
        throw_text = random.choice(PLAYER_THROW_TEXTS).format(
            thrower=thrower.display_name,
            target_ping=target.mention
        )

    await channel.send(f"{tomato_label(slot)}\n{throw_text}")

    # Informacja o kilku pomidorach lecących jednocześnie w tę samą osobę.
    same_target = [
        t for t in enabled_tomatoes(game)
        if t.in_flight and t.target_id == target.id
    ]
    if len(same_target) >= 2:
        await channel.send(
            f"🍅🍅 {target.mention} **lecą w Ciebie {len(same_target)} pomidory!** "
            f"Musisz wpisać `!lapie` **{len(same_target)} razy**, żeby złapać wszystkie."
        )

    countdown_message = await channel.send(
        f"## {slot.emoji} {slot.name} **3...**\n**ŁAP!**"
    )

    for number in (2, 1):
        try:
            await asyncio.wait_for(current_event.wait(), timeout=COUNTDOWN_SECONDS)
            return
        except asyncio.TimeoutError:
            pass

        if (
            not game.active
            or not slot.enabled
            or not slot.in_flight
            or slot.catch_event is not current_event
        ):
            return

        await countdown_message.edit(
            content=f"## {slot.emoji} {slot.name} **{number}...**"
        )

    try:
        await asyncio.wait_for(current_event.wait(), timeout=COUNTDOWN_SECONDS)
        return
    except asyncio.TimeoutError:
        pass

    # Najpierw zamykamy możliwość złapania, dopiero potem pokazujemy 0.
    # Dzięki temu nie ma sytuacji: +pkt za złapanie i jednocześnie komunikat o niezłapaniu.
    if (
        not game.active
        or not slot.enabled
        or not slot.in_flight
        or slot.catch_event is not current_event
    ):
        return

    thrower_id = slot.thrower_id
    slot.in_flight = False
    slot.target_id = None
    slot.thrower_id = None
    slot.catch_event = None
    slot.started_at = 0.0

    await countdown_message.edit(
        content=f"## 💥 {slot.emoji} {slot.name} **0!**"
    )

    if thrower_id is not None:
        # Za niezłapany rzut każdy pomidor daje standardowo +1 rzucającemu.
        # Złoty daje +2 wyłącznie za ZŁAPANIE.
        add_points(guild.id, thrower_id, 1)
        slot.holder_id = thrower_id
        text = random.choice(PLAYER_HIT_TEXTS).format(
            target_name=target.display_name,
            thrower_name=member_name(guild, thrower_id)
        )
        await channel.send(f"{tomato_label(slot)}\n{text}")
    else:
        slot.holder_id = None
        text = random.choice(BOT_MISS_TEXTS).format(
            target_name=target.display_name
        )
        await channel.send(
            f"{tomato_label(slot)}\n{text}\n"
            f"👑 **{slot.name} pozostaje u Króla Pomidora. Kolejny atak może nadejść w każdej chwili...**"
        )
        schedule_auto_throw(guild.id, slot)


# =========================================================
# ŁAPANIE — jedna komenda obsługuje wszystkie pomidory
# =========================================================

@bot.command(name="lapie", aliases=["łapie", "lap", "łap"])
async def lapie(ctx):
    if not ctx.guild:
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send("🍅 **Zabawa aktualnie nie trwa.**")
        return

    if ctx.channel.id != game.channel_id:
        return

    incoming = [
        slot for slot in enabled_tomatoes(game)
        if slot.in_flight and slot.target_id == ctx.author.id
    ]
    incoming.sort(key=lambda s: (s.started_at, s.number))

    if not incoming:
        any_flying = any(slot.in_flight for slot in enabled_tomatoes(game))
        if any_flying:
            await ctx.send(random.choice(CHEAT_TEXTS))
        else:
            await ctx.send("🤲🍅 **Żaden pomidor aktualnie nie leci w Twoją stronę.**")
        return

    # Jedno !lapie = dokładnie jeden pomidor.
    slot = incoming[0]
    current_event = slot.catch_event

    slot.holder_id = ctx.author.id
    slot.in_flight = False
    slot.target_id = None
    slot.thrower_id = None
    slot.catch_event = None
    slot.started_at = 0.0

    add_points(ctx.guild.id, ctx.author.id, slot.catch_points)

    if current_event is not None:
        current_event.set()

    remaining = [
        t for t in enabled_tomatoes(game)
        if t.in_flight and t.target_id == ctx.author.id
    ]

    if slot.catch_points == 2:
        catch_text = (
            f"✨🍅 **ZŁOTY ZŁAPANY! {ctx.author.display_name} zdobywa +2 punkty!**"
        )
    else:
        catch_text = random.choice(CATCH_TEXTS).format(
            target_name=ctx.author.display_name
        )

    extra = ""
    if remaining:
        extra = (
            f"\n⚠️🍅 **Nadal leci w Ciebie jeszcze {len(remaining)} pomidor(y)! "
            "Użyj `!lapie` ponownie!**"
        )

    await ctx.send(
        f"{tomato_label(slot)} **złapany!**\n"
        f"{catch_text}\n"
        f"🍅 **{slot.name} został przejęty. Możesz nim rzucić dalej.**{extra}"
    )


# =========================================================
# STATUS
# =========================================================

@bot.command(name="pomidor")
async def pomidor(ctx):
    if not ctx.guild:
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send("🍅💤 **Król Pomidora aktualnie nie trwa.**")
        return

    lines = []
    for slot in game.tomatoes:
        if not slot.enabled:
            lines.append(f"{slot.emoji} **{slot.name}:** nieaktywny")
        elif slot.in_flight:
            lines.append(
                f"{slot.emoji} **{slot.name}:** leci w **{member_name(ctx.guild, slot.target_id)}**"
            )
        elif slot.holder_id is not None:
            lines.append(
                f"{slot.emoji} **{slot.name}:** posiada **{member_name(ctx.guild, slot.holder_id)}**"
            )
        else:
            lines.append(
                f"👑 {slot.emoji} **{slot.name}:** posiada Król Pomidora"
            )

    embed = discord.Embed(
        title="🍅 Aktualny stan pomidorów",
        description="\n".join(lines),
        color=discord.Color.orange()
    )
    await ctx.send(embed=embed)


# =========================================================
# RANKING
# =========================================================

@bot.command(name="ranking")
async def ranking(ctx):

    if not ctx.guild:
        return


    players = get_all_players(
        ctx.guild.id
    )


    ranked = [
        row
        for row in players
        if row["points"] > 0
    ]


    if not ranked:

        await ctx.send(
            "🍅 **Nikt nie zdobył jeszcze punktu.**"
        )

        return


    medals = {
        1: "🥇",
        2: "🥈",
        3: "🥉"
    }


    lines = []


    for position, row in enumerate(
        ranked,
        start=1
    ):

        member = ctx.guild.get_member(
            row["user_id"]
        )

        name = (
            member.display_name
            if member
            else "Nieznany gracz"
        )

        medal = medals.get(
            position,
            "🍅"
        )


        status = (
            "🟢"
            if row["active"] == 1
            else "⚫"
        )


        lines.append(
            f"{medal} **{position}.** "
            f"{status} {name} "
            f"— **{row['points']} pkt**"
        )


    embed = discord.Embed(
        title="👑🍅 Ranking Króla Pomidora",
        description="\n".join(lines),
        color=discord.Color.gold()
    )


    embed.set_footer(
        text=(
            "🟢 aktywny • ⚫ poza pulą | "
            "Punkty sumują się przez całą edycję."
        )
    )


    await ctx.send(
        embed=embed
    )


# =========================================================
# STOP — zatrzymuje WSZYSTKIE pomidory
# =========================================================

@bot.command(name="stop")
async def stop(ctx):
    if not ctx.guild or not await operator_required(ctx):
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send("🍅 **Zabawa jest już zatrzymana.**")
        return

    game.active = False
    game.manual_throw_counts.clear()

    for slot in game.tomatoes:
        reset_tomato_slot(slot, enabled=False)

    await ctx.send(
        "🛑🍅 **Dzisiejsza rozgrywka Króla Pomidora została zakończona!**\n\n"
        "🍅 Pomidor #1 — zatrzymany\n"
        "🍅 Pomidor #2 — zatrzymany\n"
        "🍅 Pomidor #3 — zatrzymany\n"
        "✨🍅 Złoty Pomidor — zatrzymany\n\n"
        "🏆 Punkty oraz lista uczestników zostały zachowane."
    )


# =========================================================
# FINAŁ EDYCJI
# =========================================================

@bot.command(
    name="koniecpomidora",
    aliases=["finalpomidor"]
)
async def koniecpomidora(ctx):

    if not ctx.guild:
        return


    if not await operator_required(ctx):
        return


    game = get_game(
        ctx.guild.id
    )


    if game.active:

        await ctx.send(
            "🍅🚫 **Najpierw zakończ bieżącą rozgrywkę przez `!stop`.**"
        )

        return


    players = get_all_players(
        ctx.guild.id
    )


    ranked = [
        row
        for row in players
        if row["points"] > 0
    ]


    if not ranked:

        await ctx.send(
            "🍅 **Nie ma jeszcze wyników do podsumowania.**"
        )

        return


    best_score = ranked[0]["points"]


    winners = [
        row
        for row in ranked
        if row["points"] == best_score
    ]


    if len(winners) == 1:

        member = ctx.guild.get_member(
            winners[0]["user_id"]
        )

        winner_name = (
            member.display_name
            if member
            else "Nieznany gracz"
        )


        winner_text = (
            f"👑🍅 **KRÓLEM POMIDORA ZOSTAJE {winner_name}!**\n"
            f"🏆 Łączny wynik: **{best_score} pkt**"
        )


    else:

        winner_names = []

        for row in winners:

            member = ctx.guild.get_member(
                row["user_id"]
            )

            winner_names.append(
                member.display_name
                if member
                else "Nieznany gracz"
            )


        winner_text = (
            "👑🍅 **MAMY REMIS!**\n"
            + ", ".join(winner_names)
            + f"\n🏆 Wynik: **{best_score} pkt**"
        )


    await ctx.send(
        winner_text
    )


# =========================================================
# RESET PUNKTÓW
# =========================================================

@bot.command(name="resetpunkty")
async def resetpunkty_command(ctx):

    if not ctx.guild:
        return


    if not await operator_required(ctx):
        return


    game = get_game(
        ctx.guild.id
    )


    if game.active:

        await ctx.send(
            "🍅🚫 **Najpierw zakończ zabawę przez `!stop`.**"
        )

        return


    reset_points(
        ctx.guild.id
    )


    await ctx.send(
        "🧹🍅 **Punkty zostały wyzerowane. Lista uczestników została zachowana.**"
    )


# =========================================================
# PEŁNY RESET
# =========================================================

@bot.command(name="resetpomidor")
async def resetpomidor_command(ctx):

    if not ctx.guild:
        return


    if not await operator_required(ctx):
        return


    game = get_game(
        ctx.guild.id
    )


    game.active = False
    game.host_id = None
    game.channel_id = None
    game.manual_throw_counts.clear()
    for slot in game.tomatoes:
        reset_tomato_slot(slot, enabled=False)


    reset_everything(
        ctx.guild.id
    )


    await ctx.send(
        "💣🍅 **PEŁNY RESET KRÓLA POMIDORA!**\n"
        "Usunięto listę uczestników oraz wszystkie punkty."
    )


# =========================================================
# POMOC
# =========================================================

@bot.command(
    name="pomocpomidor",
    aliases=["komendypomidor"]
)
async def pomocpomidor(ctx):

    embed = discord.Embed(
        title="👑🍅 Król Pomidora — komendy",
        description=(
            "**Dla graczy**\n"
            "`!dolacz` — dołącza lub wraca do puli\n"
            "`!wyjdz` / `!wyjdź` — wychodzi, zachowując wynik\n"
            "`!lista` — aktywni uczestnicy\n"
            "`!rzuc @osoba` — rzuca jednym posiadanym pomidorem; max 2 ręczne rzuty w tę samą osobę na sesję\n"
            "`!losuj` — losuje cel i nie podlega limitowi ręcznych rzutów\n"
            "`!lapie` / `!łapie` — łapie dokładnie 1 lecący pomidor\n"
            "`!pomidor` — pokazuje stan wszystkich pomidorów\n"
            "`!ranking` — ranking całej edycji\n\n"

            "**Opiekun Zabaw / Dyrekcja**\n"
            "`!startpomidor` — start rozgrywki i Pomidora #1\n"
            "`!drugipomidor` — uruchamia Pomidora #2\n"
            "`!trzecipomidor` — uruchamia Pomidora #3\n"
            "`!zlotypomidor` — uruchamia Złotego Pomidora\n"
            "`!stoppomidor2` — zatrzymuje tylko Pomidora #2\n"
            "`!stoppomidor3` — zatrzymuje tylko Pomidora #3\n"
            "`!stopzlotypomidor` — zatrzymuje tylko Złotego Pomidora\n"
            "`!stop` — zatrzymuje wszystkie pomidory i kończy sesję\n"
            "`!koniecpomidora` — finał całej edycji\n"
            "`!dodaj @osoba` — dodaje osobę\n"
            "`!usun @osoba` — usuwa z aktywnej puli bez kasowania punktów\n"
            "`!resetpunkty` — zeruje punkty\n"
            "`!resetpomidor` — czyści wszystko\n\n"

            "**Punktacja**\n"
            "🍅 Złapanie zwykłego = **+1 pkt**\n"
            "✨🍅 Złapanie Złotego = **+2 pkt**\n"
            "🎯 Niezłapanie rzutu gracza = **+1 pkt dla rzucającego**\n\n"

            "🤲 **Jeśli lecą w Ciebie 2, 3 albo 4 pomidory — musisz użyć `!lapie` odpowiednio 2, 3 albo 4 razy.**"
        ),
        color=discord.Color.red()
    )

    await ctx.send(embed=embed)


# =========================================================
# BŁĘDY
# =========================================================

@bot.event
async def on_command_error(ctx, error):

    if isinstance(
        error,
        commands.CommandNotFound
    ):
        return


    if isinstance(
        error,
        commands.MemberNotFound
    ):

        await ctx.send(
            "🍅❓ **Nie znalazłem tej osoby. Najlepiej oznacz ją przez @.**"
        )

        return


    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):

        await ctx.send(
            "🍅❓ **Brakuje argumentu. Użyj `!pomocpomidor`.**"
        )

        return


    print(
        f"❌ Błąd: {repr(error)}"
    )


# =========================================================
# URUCHOMIENIE
# =========================================================

if not TOKEN:

    raise RuntimeError(
        "Brak DISCORD_TOKEN!"
    )


bot.run(TOKEN)
