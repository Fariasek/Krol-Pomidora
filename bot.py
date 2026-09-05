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


def add_point(guild_id, user_id):

    db.execute("""
        UPDATE players
        SET points = points + 1
        WHERE guild_id = ?
        AND user_id = ?
    """, (guild_id, user_id))

    db.commit()


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

class TomatoGame:

    def __init__(self):

        self.active = False
        self.host_id = None

        self.holder_id = None

        self.in_flight = False
        self.target_id = None
        self.thrower_id = None

        self.catch_event = None

        self.channel_id = None

        self.auto_throw_task = None

        # Licznik ręcznych rzutów w ramach jednej sesji START -> STOP
        # klucz: (rzucający_id, cel_id), wartość: liczba ręcznych rzutów
        self.manual_throw_counts = {}


games = {}


def get_game(guild_id):

    if guild_id not in games:
        games[guild_id] = TomatoGame()

    return games[guild_id]


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

def cancel_auto_throw(game):

    task = game.auto_throw_task

    if (
        task is not None
        and task is not asyncio.current_task()
        and not task.done()
    ):
        task.cancel()

    game.auto_throw_task = None


def schedule_auto_throw(guild_id):

    game = get_game(guild_id)

    if not game.active:
        return

    if game.holder_id is not None:
        return

    if game.in_flight:
        return

    cancel_auto_throw(game)

    game.auto_throw_task = asyncio.create_task(
        auto_throw_loop(guild_id)
    )


async def auto_throw_loop(guild_id):

    game = get_game(guild_id)

    delay = random.randint(
        AUTO_THROW_MIN_SECONDS,
        AUTO_THROW_MAX_SECONDS
    )

    print(
        f"🍅 Król Pomidora rzuci za {delay} sekund."
    )

    try:
        await asyncio.sleep(delay)

    except asyncio.CancelledError:
        return


    if not game.active:
        return

    if game.holder_id is not None:
        return

    if game.in_flight:
        return


    guild = bot.get_guild(guild_id)

    if guild is None:
        return


    channel = guild.get_channel(
        game.channel_id
    )

    if channel is None:
        return


    players = get_active_players(
        guild_id
    )


    possible_targets = []

    for row in players:

        member = guild.get_member(
            row["user_id"]
        )

        if member is None:
            continue

        if member.bot:
            continue

        possible_targets.append(member)


    if not possible_targets:

        await channel.send(
            "🍅💤 **Król Pomidora nie ma obecnie żadnych aktywnych graczy.**"
        )

        return


    target = random.choice(
        possible_targets
    )


    await perform_throw(
        channel=channel,
        guild=guild,
        target=target,
        thrower=None,
        bot_throw=True
    )


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


    if (
        game.active
        and game.holder_id == user_id
    ):

        await ctx.send(
            "🍅🚫 **Nie możesz wyjść, kiedy masz pomidora. Najpierw go rzuć.**"
        )

        return


    if (
        game.active
        and game.in_flight
        and game.target_id == user_id
    ):

        await ctx.send(
            "🍅💨 **Pomidor właśnie leci w Twoją stronę! Najpierw dokończ ten rzut.**"
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

@bot.command(
    name="usun",
    aliases=["usuń"]
)
async def usun(
    ctx,
    member: discord.Member = None
):

    if not ctx.guild:
        return

    if not await operator_required(ctx):
        return

    if member is None:
        await ctx.send(
            "🍅 Użycie: `!usun @osoba`"
        )
        return

    if not player_is_active(
        ctx.guild.id,
        member.id
    ):
        await ctx.send(
            f"🍅 {member.mention} **nie znajduje się w aktywnej puli.**"
        )
        return

    game = get_game(
        ctx.guild.id
    )

    # Jeśli pomidor właśnie leci w tę osobę, najpierw kończymy ten rzut.
    if (
        game.active
        and game.in_flight
        and game.target_id == member.id
    ):
        await ctx.send(
            "🍅🚫 **Pomidor właśnie leci w tę osobę. Poczekaj na zakończenie rzutu.**"
        )
        return

    had_tomato = (
        game.active
        and game.holder_id == member.id
    )

    deactivate_player(
        ctx.guild.id,
        member.id
    )

    if had_tomato:
        # Pomidor wraca do Króla i po chwili bot znów rzuci.
        game.holder_id = None
        game.thrower_id = None
        game.target_id = None
        game.in_flight = False

        current_event = game.catch_event
        game.catch_event = None

        if current_event is not None:
            current_event.set()

        schedule_auto_throw(
            ctx.guild.id
        )

        await ctx.send(
            f"🗑️🍅 **Usunięto {member.mention} z aktywnej puli.**\n"
            "🏆 Zdobyte punkty zostały zachowane.\n"
            "👑 Pomidor wraca do Króla Pomidora, który za chwilę wybierze nowy cel."
        )
        return

    await ctx.send(
        f"🗑️🍅 **Usunięto {member.mention} z aktywnej puli.**\n"
        "🏆 Zdobyte punkty zostały zachowane."
    )


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
# START
# =========================================================

@bot.command(
    name="startpomidor",
    aliases=["start"]
)
async def startpomidor(ctx):

    if not ctx.guild:
        return


    if not await operator_required(ctx):
        return


    game = get_game(
        ctx.guild.id
    )


    if game.active:

        await ctx.send(
            "🍅🚫 **Król Pomidora już trwa!**"
        )

        return


    players = get_active_players(
        ctx.guild.id
    )


    if not players:

        await ctx.send(
            "🍅 **Nie ma żadnych aktywnych graczy.**"
        )

        return


    cancel_auto_throw(
        game
    )


    game.active = True
    game.host_id = ctx.author.id

    # Pomidor na początku należy do Króla
    # dlatego holder = None
    game.holder_id = None

    game.in_flight = False
    game.target_id = None
    game.thrower_id = None
    game.catch_event = None

    # Nowa sesja = świeże limity ręcznych rzutów
    game.manual_throw_counts.clear()

    game.channel_id = ctx.channel.id


    embed = discord.Embed(
        title="👑🍅 KRÓL POMIDORA ROZPOCZĘTY!",
        description=(
            "👑 Pierwszy pomidor należy do Króla Pomidora.\n"
            "Może zaatakować w dowolnym momencie "
            "w ciągu maksymalnie **3 minut**.\n\n"

            "🍅 Bot odlicza **3 → 2 → 1 → 0**.\n"
            "🤲 Złapanie: `!lapie` lub `!łapie`\n\n"

            "✅ Złapanie = **+1 pkt dla osoby łapiącej**.\n"
            "🎯 Niezłapanie rzutu gracza = "
            "**+1 pkt dla rzucającego**.\n\n"

            "📌 **Pomidor zmienia właściciela tylko wtedy, "
            "gdy zostanie złapany.**"
        ),
        color=discord.Color.red()
    )


    await ctx.send(
        embed=embed
    )


    schedule_auto_throw(
        ctx.guild.id
    )


# =========================================================
# RZUT GRACZA
# =========================================================

@bot.command(
    name="rzuc",
    aliases=["rzuć"]
)
async def rzuc(
    ctx,
    target: discord.Member = None
):

    if not ctx.guild:
        return


    game = get_game(
        ctx.guild.id
    )


    if not game.active:

        await ctx.send(
            "🍅💤 **Zabawa aktualnie nie trwa.**"
        )

        return


    if ctx.channel.id != game.channel_id:

        return


    if game.in_flight:

        await ctx.send(
            "🍅💨 **Jeden pomidor już leci!**"
        )

        return


    if game.holder_id != ctx.author.id:

        await ctx.send(
            random.choice(
                NO_TOMATO_TEXTS
            )
        )

        return


    if target is None:

        await ctx.send(
            "🍅 Użycie: `!rzuc @osoba`"
        )

        return


    if target.bot:

        await ctx.send(
            "🤖🍅 **Nie rzucamy w boty.**"
        )

        return


    if target.id == ctx.author.id:

        await ctx.send(
            "😂🍅 **Nie możesz rzucić w samego siebie.**"
        )

        return


    if not player_is_active(
        ctx.guild.id,
        target.id
    ):

        await ctx.send(
            f"🍅🚫 {target.mention} **nie znajduje się w aktywnej puli.**"
        )

        return


    # Limit dotyczy TYLKO ręcznej komendy !rzuc.
    # !losuj nie sprawdza i nie zwiększa tego licznika.
    throw_key = (ctx.author.id, target.id)
    manual_count = game.manual_throw_counts.get(throw_key, 0)

    if manual_count >= 2:
        await ctx.send(
            f"🍅🚫 **W {target.display_name} rzucałeś/aś już 2 razy ręcznie podczas tej rozgrywki.**\n"
            "Wybierz inną osobę albo użyj `!losuj` — losowanie nie podlega temu limitowi."
        )
        return

    # Zapisujemy ręczny rzut dopiero po przejściu wszystkich walidacji.
    game.manual_throw_counts[throw_key] = manual_count + 1

    # WAŻNE:
    # nie zerujemy holder_id.
    # Dopóki cel nie złapie,
    # pomidor formalnie nadal należy do rzucającego.


    await perform_throw(
        channel=ctx.channel,
        guild=ctx.guild,
        target=target,
        thrower=ctx.author,
        bot_throw=False
    )


# =========================================================
# LOSOWANIE CELU
# =========================================================

@bot.command(name="losuj")
async def losuj(ctx):

    if not ctx.guild:
        return


    game = get_game(
        ctx.guild.id
    )


    if not game.active:

        await ctx.send(
            "🍅💤 **Zabawa aktualnie nie trwa.**"
        )

        return


    if game.in_flight:

        await ctx.send(
            "🍅💨 **Jeden pomidor już leci.**"
        )

        return


    if game.holder_id != ctx.author.id:

        await ctx.send(
            random.choice(
                NO_TOMATO_TEXTS
            )
        )

        return


    rows = get_active_players(
        ctx.guild.id
    )


    possible_targets = []


    for row in rows:

        user_id = row["user_id"]

        if user_id == ctx.author.id:
            continue


        member = ctx.guild.get_member(
            user_id
        )


        if member is None:
            continue


        if member.bot:
            continue


        possible_targets.append(
            member
        )


    if not possible_targets:

        await ctx.send(
            "🍅 **Nie ma kogo wylosować.**"
        )

        return


    target = random.choice(
        possible_targets
    )


    await perform_throw(
        channel=ctx.channel,
        guild=ctx.guild,
        target=target,
        thrower=ctx.author,
        bot_throw=False
    )


# =========================================================
# MECHANIKA RZUTU
# =========================================================

async def perform_throw(
    channel,
    guild,
    target,
    thrower=None,
    bot_throw=False
):

    game = get_game(
        guild.id
    )


    if not game.active:
        return


    cancel_auto_throw(
        game
    )


    game.in_flight = True
    game.target_id = target.id

    game.thrower_id = (
        thrower.id
        if thrower is not None
        else None
    )


    game.catch_event = asyncio.Event()

    current_event = game.catch_event


    target_ping = target.mention

    target_name = target.display_name


    if bot_throw:

        throw_text = random.choice(
            BOT_THROW_TEXTS
        ).format(
            target_ping=target_ping
        )

    else:

        throw_text = random.choice(
            PLAYER_THROW_TEXTS
        ).format(
            thrower=thrower.display_name,
            target_ping=target_ping
        )


    await channel.send(
        throw_text
    )


    countdown_message = await channel.send(
        f"## 🍅 **3...**\n"
        "**ŁAP!**"
    )


    # =====================================================
    # 3 -> 2
    # =====================================================

    try:

        await asyncio.wait_for(
            current_event.wait(),
            timeout=COUNTDOWN_SECONDS
        )

        return

    except asyncio.TimeoutError:
        pass


    if (
        not game.active
        or not game.in_flight
    ):
        return


    await countdown_message.edit(
        content="## 🍅 **2...**"
    )


    # =====================================================
    # 2 -> 1
    # =====================================================

    try:

        await asyncio.wait_for(
            current_event.wait(),
            timeout=COUNTDOWN_SECONDS
        )

        return

    except asyncio.TimeoutError:
        pass


    if (
        not game.active
        or not game.in_flight
    ):
        return


    await countdown_message.edit(
        content="## 🍅 **1...**"
    )


    # =====================================================
    # 1 -> 0
    # =====================================================

    try:

        await asyncio.wait_for(
            current_event.wait(),
            timeout=COUNTDOWN_SECONDS
        )

        return

    except asyncio.TimeoutError:
        pass


    if (
        not game.active
        or not game.in_flight
    ):
        return


    await countdown_message.edit(
        content="## 💥🍅 **0!**"
    )


    thrower_id = game.thrower_id


    game.in_flight = False
    game.target_id = None
    game.thrower_id = None
    game.catch_event = None


    # =====================================================
    # NIEZŁAPANY RZUT GRACZA
    # Pomidor zostaje u rzucającego.
    # =====================================================

    if thrower_id is not None:

        add_point(
            guild.id,
            thrower_id
        )


        thrower_name = member_name(
            guild,
            thrower_id
        )


        # Pomidor nadal należy do rzucającego
        game.holder_id = thrower_id


        text = random.choice(
            PLAYER_HIT_TEXTS
        ).format(
            target_name=target_name,
            thrower_name=thrower_name
        )


        await channel.send(
            text
        )


    # =====================================================
    # NIEZŁAPANY RZUT KRÓLA
    # Pomidor zostaje u bota.
    # =====================================================

    else:

        # None oznacza, że pomidor jest u Króla
        game.holder_id = None


        text = random.choice(
            BOT_MISS_TEXTS
        ).format(
            target_name=target_name
        )


        await channel.send(
            text
            +
            "\n👑 **Pomidor pozostaje u Króla Pomidora. "
            "Kolejny atak może nadejść w każdej chwili...**"
        )


        schedule_auto_throw(
            guild.id
        )


# =========================================================
# ŁAPANIE
# =========================================================

@bot.command(
    name="lapie",
    aliases=[
        "łapie",
        "lap",
        "łap"
    ]
)
async def lapie(ctx):

    if not ctx.guild:
        return


    game = get_game(
        ctx.guild.id
    )


    if not game.active:

        await ctx.send(
            "🍅 **Zabawa aktualnie nie trwa.**"
        )

        return


    if ctx.channel.id != game.channel_id:
        return


    if not game.in_flight:

        await ctx.send(
            "🤲🍅 **Żaden pomidor aktualnie nie leci.**"
        )

        return


    if ctx.author.id != game.target_id:

        await ctx.send(
            random.choice(
                CHEAT_TEXTS
            )
        )

        return


    target_name = ctx.author.display_name


    # Cel złapał, więc przejmuje pomidora.
    game.holder_id = ctx.author.id

    game.in_flight = False
    game.target_id = None
    game.thrower_id = None


    add_point(
        ctx.guild.id,
        ctx.author.id
    )


    current_event = game.catch_event

    game.catch_event = None


    if current_event is not None:
        current_event.set()


    catch_text = random.choice(
        CATCH_TEXTS
    ).format(
        target_name=target_name
    )


    await ctx.send(
        catch_text
        +
        "\n🍅 **Pomidor został przejęty. Teraz możesz rzucić dalej.**"
    )


# =========================================================
# STATUS
# =========================================================

@bot.command(name="pomidor")
async def pomidor(ctx):

    if not ctx.guild:
        return


    game = get_game(
        ctx.guild.id
    )


    if not game.active:

        await ctx.send(
            "🍅💤 **Król Pomidora aktualnie nie trwa.**"
        )

        return


    if game.in_flight:

        target_name = member_name(
            ctx.guild,
            game.target_id
        )

        status = (
            f"🍅 Pomidor jest w powietrzu.\n"
            f"🎯 Cel: **{target_name}**"
        )


    elif game.holder_id is not None:

        holder_name = member_name(
            ctx.guild,
            game.holder_id
        )

        status = (
            f"🍅 Pomidora posiada **{holder_name}**."
        )


    else:

        status = (
            "👑🍅 Pomidora posiada **Król Pomidora**.\n"
            "Może zaatakować w każdej chwili."
        )


    embed = discord.Embed(
        title="🍅 Aktualny stan zabawy",
        description=status,
        color=discord.Color.orange()
    )


    await ctx.send(
        embed=embed
    )


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
# STOP
# =========================================================

@bot.command(name="stop")
async def stop(ctx):

    if not ctx.guild:
        return


    if not await operator_required(ctx):
        return


    game = get_game(
        ctx.guild.id
    )


    if not game.active:

        await ctx.send(
            "🍅 **Zabawa jest już zatrzymana.**"
        )

        return


    game.active = False
    game.in_flight = False
    game.target_id = None
    game.thrower_id = None
    game.holder_id = None
    game.manual_throw_counts.clear()


    cancel_auto_throw(
        game
    )


    current_event = game.catch_event
    game.catch_event = None


    if current_event is not None:
        current_event.set()


    await ctx.send(
        "🛑🍅 **Dzisiejsza rozgrywka Króla Pomidora została zakończona!**\n\n"
        "🏆 Punkty oraz lista uczestników zostały zachowane.\n"
        "Ranking będzie kontynuowany podczas następnej rozgrywki."
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
    game.in_flight = False
    game.target_id = None
    game.thrower_id = None
    game.holder_id = None
    game.host_id = None
    game.channel_id = None
    game.manual_throw_counts.clear()


    cancel_auto_throw(
        game
    )


    current_event = game.catch_event
    game.catch_event = None


    if current_event is not None:
        current_event.set()


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
            "`!rzuc @osoba` — rzuca pomidorem (max 2 ręczne rzuty w tę samą osobę na sesję)\n"
            "`!losuj` — losuje cel i nie podlega limitowi ręcznych rzutów\n"
            "`!lapie` / `!łapie` — łapie pomidora\n"
            "`!pomidor` — pokazuje właściciela pomidora\n"
            "`!ranking` — ranking całej edycji\n\n"

            "**Opiekun Zabaw / Dyrekcja**\n"
            "`!startpomidor` — start rozgrywki\n"
            "`!stop` — koniec danego dnia\n"
            "`!koniecpomidora` — finał całej edycji\n"
            "`!dodaj @osoba` — dodaje osobę\n"
            "`!usun @osoba` — usuwa z aktywnej puli\n"
            "`!resetpunkty` — zeruje punkty\n"
            "`!resetpomidor` — czyści wszystko\n\n"

            "**Punktacja**\n"
            "🤲 Złapanie = **+1 pkt dla łapiącego**\n"
            "🎯 Niezłapanie rzutu gracza = **+1 pkt dla rzucającego**\n\n"

            "🍅 **Pomidor zmienia właściciela tylko po udanym złapaniu.**"
        ),
        color=discord.Color.red()
    )


    await ctx.send(
        embed=embed
    )


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
