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

# 3 -> 2 -> 1 -> 0
COUNTDOWN_SECONDS = 2

# Pierwszy automatyczny rzut Króla:
# 15 sekund - maksymalnie 3 minuty
AUTO_THROW_MIN_SECONDS = 15
AUTO_THROW_MAX_SECONDS = 180


# Role mogące zarządzać zabawą
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


# =========================================================
# MIGRACJA STAREJ BAZY
# Jeśli baza już istniała bez kolumny "active",
# bot doda ją automatycznie.
# =========================================================

columns = db.execute(
    "PRAGMA table_info(players)"
).fetchall()

column_names = [
    column["name"]
    for column in columns
]

if "active" not in column_names:

    db.execute(
        """
        ALTER TABLE players
        ADD COLUMN active INTEGER NOT NULL DEFAULT 1
        """
    )

    db.commit()

    print("🍅 Dodano kolumnę ACTIVE do starej bazy.")


# =========================================================
# FUNKCJE BAZY
# =========================================================

def player_exists(guild_id: int, user_id: int) -> bool:

    row = db.execute(
        """
        SELECT 1
        FROM players
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (guild_id, user_id)
    ).fetchone()

    return row is not None


def player_is_active(guild_id: int, user_id: int) -> bool:

    row = db.execute(
        """
        SELECT active
        FROM players
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (guild_id, user_id)
    ).fetchone()

    if row is None:
        return False

    return row["active"] == 1


def add_or_activate_player(
    guild_id: int,
    user_id: int
):

    if player_exists(
        guild_id,
        user_id
    ):

        # Osoba już kiedyś grała.
        # Przywracamy ją do aktywnej puli,
        # ale NIE zerujemy punktów.
        db.execute(
            """
            UPDATE players
            SET active = 1
            WHERE guild_id = ?
            AND user_id = ?
            """,
            (guild_id, user_id)
        )

    else:

        # Nowy gracz
        db.execute(
            """
            INSERT INTO players
            (guild_id, user_id, points, active)
            VALUES (?, ?, 0, 1)
            """,
            (guild_id, user_id)
        )

    db.commit()


def deactivate_player(
    guild_id: int,
    user_id: int
):

    db.execute(
        """
        UPDATE players
        SET active = 0
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (guild_id, user_id)
    )

    db.commit()


def get_active_players(guild_id: int):

    return db.execute(
        """
        SELECT user_id, points, active
        FROM players
        WHERE guild_id = ?
        AND active = 1
        ORDER BY points DESC, user_id ASC
        """,
        (guild_id,)
    ).fetchall()


def get_all_players(guild_id: int):

    return db.execute(
        """
        SELECT user_id, points, active
        FROM players
        WHERE guild_id = ?
        ORDER BY points DESC, user_id ASC
        """,
        (guild_id,)
    ).fetchall()


def add_point(
    guild_id: int,
    user_id: int
):

    db.execute(
        """
        UPDATE players
        SET points = points + 1
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (guild_id, user_id)
    )

    db.commit()


def reset_points(guild_id: int):

    db.execute(
        """
        UPDATE players
        SET points = 0
        WHERE guild_id = ?
        """,
        (guild_id,)
    )

    db.commit()


def reset_everything(guild_id: int):

    db.execute(
        """
        DELETE FROM players
        WHERE guild_id = ?
        """,
        (guild_id,)
    )

    db.commit()


# =========================================================
# STAN GRY
# =========================================================

class TomatoGame:

    def __init__(self):

        self.active = False

        # Osoba uruchamiająca zabawę
        self.host_id = None

        # Osoba posiadająca pomidora
        self.holder_id = None

        # Czy pomidor leci
        self.in_flight = False

        # Cel rzutu
        self.target_id = None

        # Osoba, która rzuciła
        # None = Król Pomidora / bot
        self.thrower_id = None

        # Event łapania
        self.catch_event = None

        # Kanał rozgrywki
        self.channel_id = None

        # Zadanie pierwszego automatycznego rzutu
        self.auto_throw_task = None


games = {}


def get_game(guild_id: int) -> TomatoGame:

    if guild_id not in games:
        games[guild_id] = TomatoGame()

    return games[guild_id]


# =========================================================
# TEKSTY
# =========================================================

PLAYER_THROW_TEXTS = [

    "🍅 **{thrower} bierze zamach i posyła pomidora prosto w {target}!**",

    "🍅💨 **UWAGA! {thrower} rzuca pomidorem w {target}!**",

    "😈🍅 **{thrower} uśmiecha się podejrzanie... POMIDOR LECI W {target}!**",

    "💥🍅 **{thrower} wybiera swoją kolejną ofiarę — {target}!**",

    "👀🍅 **{target}, lepiej patrz w górę! {thrower} właśnie wykonał rzut!**",

    "🎯🍅 **{thrower} celuje... i rzuca prosto w {target}!**",

]


BOT_THROW_TEXTS = [

    "👑🍅 **Król Pomidora wychyla się z ukrycia... i wybiera {target}!**",

    "😈🍅 **CISZA... aż nagle Król Pomidora rzuca prosto w {target}!**",

    "🍅💨 **NADLATUJE! Król Pomidora wybrał {target}!**",

    "👑🍅 **Król Pomidora długo czekał na ten moment... {target}, ŁAP!**",

    "💥🍅 **NIESPODZIANKA! Pomidor leci prosto w {target}!**",

    "👀🍅 **Ktoś chyba stracił czujność... Król Pomidora atakuje {target}!**",

    "🍅😈 **Król Pomidora chichocze złowieszczo i celuje w {target}!**",

]


CATCH_TEXTS = [

    "🍅✨ **ZŁAPANY! {target} przechwytuje pomidora! +1 punkt!**",

    "👏🍅 **Ale refleks! {target} łapie pomidora i zdobywa +1 punkt!**",

    "😎🍅 **{target} łapie go bez najmniejszego problemu! +1 punkt!**",

    "🔥🍅 **PIĘKNE ZŁAPANIE! {target} zdobywa +1 punkt!**",

    "👑🍅 **{target} pokazuje klasę! Pomidor złapany — +1 punkt!**",

    "🍅🤲 **JEST! {target} zdążył przed rozplaskaniem! +1 punkt!**",

]


PLAYER_HIT_TEXTS = [

    "💥🍅 **TRAFIENIE! {target} nie zdążył złapać pomidora!**\n"
    "🎯 {thrower} zdobywa **+1 punkt za trafienie!**",

    "🍅💥 **PLASK! {target} nie łapie pomidora!**\n"
    "😈 {thrower} zgarnia **+1 punkt!**",

    "🎯🍅 **CELNY RZUT! {target} był za wolny!**\n"
    "👏 {thrower} otrzymuje **+1 punkt za trafienie!**",

    "💥🍅 **POMIDOROWA KATASTROFA! {target} nie zdążył!**\n"
    "🎯 Punkt trafia do {thrower}! **+1**",

    "🍅😈 **Pomidor dosięga {target}!**\n"
    "🏆 {thrower} zdobywa **+1 punkt za skuteczny rzut!**",

]


BOT_MISS_TEXTS = [

    "💥🍅 **PLASK! {target} nie zdążył złapać pomidora Króla!**",

    "🍅💨 **Za późno! {target} nie łapie pomidora!**",

    "😈🍅 **Król Pomidora trafił! {target} nie zdążył zareagować!**",

    "💥🍅 **POMIDOROWA KATASTROFA! {target} nie złapał rzutu Króla!**",

]


CHEAT_TEXTS = [

    "🤨🍅 **Ładne próby, ale ten pomidor nawet nie leci w Twoją stronę!**",

    "😏🍅 **Oj, nie oszukujemy! Pomidor ma zupełnie inny cel.**",

    "🚨🍅 **POMIDOROWA POLICJA! To nie Twój pomidor do złapania!**",

    "😂🍅 **Sprytnie, ale nie tym razem. Poczekaj na swoją kolej!**",

]


NO_TOMATO_TEXTS = [

    "🍅 **Hola, hola! Najpierw trzeba mieć pomidora, żeby nim rzucać.**",

    "🤨🍅 **A skąd Ty masz tego pomidora? Aktualnie posiada go ktoś inny!**",

    "🍅🚫 **Nie tak szybko! Nie jesteś aktualnym posiadaczem pomidora.**",

    "😈🍅 **Próbujesz wyczarować własnego pomidora? Tak to nie działa!**",

]


# =========================================================
# UPRAWNIENIA
# =========================================================

def is_operator(
    member: discord.Member
) -> bool:

    if member.guild_permissions.administrator:
        return True

    if member.guild_permissions.manage_guild:
        return True

    member_roles = {
        role.name
        for role in member.roles
    }

    return bool(
        member_roles.intersection(
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
        "🍅🚫 **Ta komenda jest dostępna tylko dla "
        "Opiekuna Zabaw lub Dyrekcji.**"
    )

    return False


def mention(user_id):

    if user_id is None:
        return "—"

    return f"<@{user_id}>"


# =========================================================
# AUTOMATYCZNY RZUT KRÓLA
# =========================================================

def cancel_auto_throw(
    game: TomatoGame
):

    task = game.auto_throw_task

    if (
        task is not None
        and task is not asyncio.current_task()
        and not task.done()
    ):

        task.cancel()

    game.auto_throw_task = None


def schedule_auto_throw(
    guild_id: int
):

    game = get_game(
        guild_id
    )

    if not game.active:
        return

    if game.holder_id is not None:
        return

    if game.in_flight:
        return

    cancel_auto_throw(
        game
    )

    game.auto_throw_task = asyncio.create_task(
        auto_throw_loop(
            guild_id
        )
    )


async def auto_throw_loop(
    guild_id: int
):

    game = get_game(
        guild_id
    )

    delay = random.randint(
        AUTO_THROW_MIN_SECONDS,
        AUTO_THROW_MAX_SECONDS
    )

    print(
        f"🍅 Automatyczny rzut za {delay} sekund."
    )

    try:

        await asyncio.sleep(
            delay
        )

    except asyncio.CancelledError:
        return

    if not game.active:
        return

    if game.holder_id is not None:
        return

    if game.in_flight:
        return

    guild = bot.get_guild(
        guild_id
    )

    if guild is None:
        return

    channel = guild.get_channel(
        game.channel_id
    )

    if channel is None:
        return

    rows = get_active_players(
        guild_id
    )

    possible_targets = []

    for row in rows:

        member = guild.get_member(
            row["user_id"]
        )

        if member is None:
            continue

        if member.bot:
            continue

        possible_targets.append(
            member
        )

    if not possible_targets:

        await channel.send(
            "🍅💤 **Król Pomidora nie ma obecnie żadnej aktywnej ofiary!**"
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
# BOT ONLINE
# =========================================================

@bot.event
async def on_ready():

    print(
        "========================================"
    )

    print(
        "🍅 KRÓL POMIDORA ZALOGOWANY"
    )

    print(
        f"👑 Bot: {bot.user}"
    )

    print(
        f"🆔 ID: {bot.user.id}"
    )

    print(
        "========================================"
    )

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
            f"🍅 {ctx.author.mention}, "
            f"**już jesteś w aktywnej puli Króla Pomidora!**"
        )

        return

    old_player = player_exists(
        guild_id,
        user_id
    )

    add_or_activate_player(
        guild_id,
        user_id
    )

    if old_player:

        await ctx.send(
            f"🍅👑 {ctx.author.mention} **wraca do zabawy!**\n"
            "Twój dotychczasowy wynik został zachowany. 😈"
        )

    else:

        await ctx.send(
            f"🍅👑 {ctx.author.mention} "
            f"**dołącza do pomidorowej bitwy!**\n"
            "Od teraz możesz zostać celem rzutu. 😈"
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

    if not player_exists(
        guild_id,
        user_id
    ):

        await ctx.send(
            f"🍅 {ctx.author.mention}, "
            "**nie znajdujesz się jeszcze na liście Króla Pomidora.**"
        )

        return

    if not player_is_active(
        guild_id,
        user_id
    ):

        await ctx.send(
            f"🍅 {ctx.author.mention}, "
            "**już nie znajdujesz się w aktywnej puli.**\n"
            "Możesz wrócić przez `!dolacz`."
        )

        return

    game = get_game(
        guild_id
    )

    # Nie można wyjść, kiedy posiada się pomidora.
    if (
        game.active
        and game.holder_id == user_id
    ):

        await ctx.send(
            "🍅🚫 **Nie możesz teraz wyjść z zabawy — "
            "aktualnie masz pomidora!**\n"
            "Najpierw rzuć go w inną osobę."
        )

        return

    # Nie można wyjść, gdy pomidor właśnie leci w tę osobę.
    if (
        game.active
        and game.in_flight
        and game.target_id == user_id
    ):

        await ctx.send(
            "🍅💨 **Nie uciekniesz tak łatwo! "
            "Pomidor właśnie leci w Twoją stronę.**\n"
            "Najpierw dokończ ten rzut."
        )

        return

    deactivate_player(
        guild_id,
        user_id
    )

    await ctx.send(
        f"👋🍅 {ctx.author.mention} **opuszcza aktywną pulę Króla Pomidora.**\n\n"
        "🏆 Twój zdobyty wynik **zostaje zachowany w rankingu**.\n"
        "Jeśli zechcesz wrócić, użyj `!dolacz`."
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
            "🤖🍅 **Botów nie zapisujemy do zabawy!**"
        )

        return

    if player_is_active(
        ctx.guild.id,
        member.id
    ):

        await ctx.send(
            f"🍅 {member.mention} "
            "**już znajduje się w aktywnej puli.**"
        )

        return

    existed = player_exists(
        ctx.guild.id,
        member.id
    )

    add_or_activate_player(
        ctx.guild.id,
        member.id
    )

    if existed:

        await ctx.send(
            f"✅🍅 **{member.mention} wraca do aktywnej puli!**\n"
            "Jego dotychczasowe punkty zostały zachowane."
        )

    else:

        await ctx.send(
            f"✅🍅 **Dodano {member.mention} "
            "do puli Króla Pomidora!**"
        )


# =========================================================
# USUŃ Z AKTYWNEJ PULI
# Punkty również zostają.
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
            f"🍅 {member.mention} "
            "**nie znajduje się w aktywnej puli.**"
        )

        return

    game = get_game(
        ctx.guild.id
    )

    if (
        game.active
        and game.holder_id == member.id
    ):

        await ctx.send(
            "🍅🚫 **Ta osoba aktualnie posiada pomidora!**"
        )

        return

    if (
        game.active
        and game.in_flight
        and game.target_id == member.id
    ):

        await ctx.send(
            "🍅🚫 **Pomidor właśnie leci w tę osobę!**"
        )

        return

    deactivate_player(
        ctx.guild.id,
        member.id
    )

    await ctx.send(
        f"🗑️🍅 **Usunięto {member.mention} z aktywnej puli.**\n"
        "🏆 Zdobyte punkty tej osoby zostały zachowane."
    )


# =========================================================
# LISTA AKTYWNYCH GRACZY
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
            "🍅 **Aktywna lista jest obecnie pusta.**\n"
            "Użyj `!dolacz`, żeby wejść do zabawy!"
        )

        return

    lines = []

    for index, row in enumerate(
        players,
        start=1
    ):

        lines.append(
            f"**{index}.** "
            f"<@{row['user_id']}> "
            f"— 🍅 **{row['points']} pkt**"
        )

    embed = discord.Embed(
        title="🍅 Aktywna pula Króla Pomidora",
        description="\n".join(
            lines
        ),
        color=discord.Color.red()
    )

    embed.set_footer(
        text=(
            f"Aktywnych graczy: "
            f"{len(players)}"
        )
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
            "🍅 **Nie ma obecnie żadnych aktywnych graczy!**\n"
            "Ktoś musi najpierw użyć `!dolacz`."
        )

        return

    cancel_auto_throw(
        game
    )

    game.active = True
    game.host_id = ctx.author.id
    game.holder_id = None
    game.in_flight = False
    game.target_id = None
    game.thrower_id = None
    game.catch_event = None
    game.channel_id = ctx.channel.id

    embed = discord.Embed(
        title="👑🍅 KRÓL POMIDORA ROZPOCZĘTY!",
        description=(
            f"Zabawę uruchomił {ctx.author.mention}.\n\n"

            "👑 **Pierwszy rzut należy do Króla Pomidora.**\n"
            "Może nastąpić w każdej chwili "
            "w ciągu maksymalnie **3 minut**.\n\n"

            "🍅 Gdy pomidor poleci, bot odlicza:\n"
            "**3 → 2 → 1 → 0**\n\n"

            "🤲 Złapanie: `!lapie` lub `!łapie`\n\n"

            "✅ Złapanie = **+1 pkt dla celu**.\n"
            "🎯 Niezłapanie = **+1 pkt dla gracza rzucającego**.\n\n"

            "🍅 Po rzucie pomidor zawsze przechodzi "
            "do osoby będącej jego celem.\n\n"

            "👋 Chcesz przestać brać udział? "
            "Użyj `!wyjdz`.\n"
            "Twój wynik pozostanie zapisany.\n\n"

            "**Nie traćcie czujności. 😈**"
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
            "🍅💤 **Król Pomidora aktualnie nie trwa.**"
        )

        return

    if ctx.channel.id != game.channel_id:

        await ctx.send(
            "🍅 **Rozgrywka trwa na innym kanale.**"
        )

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
            "🤖🍅 **Nie rzucamy pomidorami w boty!**"
        )

        return

    if target.id == ctx.author.id:

        await ctx.send(
            "😂🍅 **Nie możesz rzucić w samego siebie!**"
        )

        return

    # CEL MUSI BYĆ AKTYWNY
    if not player_is_active(
        ctx.guild.id,
        target.id
    ):

        await ctx.send(
            f"🍅🚫 {target.mention} "
            "**nie znajduje się obecnie w aktywnej puli!**"
        )

        return

    game.holder_id = None

    await perform_throw(
        channel=ctx.channel,
        guild=ctx.guild,
        target=target,
        thrower=ctx.author,
        bot_throw=False
    )


# =========================================================
# LOSUJ CEL
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
            "🍅💤 **Król Pomidora aktualnie nie trwa.**"
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
            "🍅 **Nie mam kogo wylosować! "
            "Potrzebujemy więcej aktywnych graczy.**"
        )

        return

    target = random.choice(
        possible_targets
    )

    await ctx.send(
        f"🎲🍅 **Los wskazuje na {target.mention}!**"
    )

    game.holder_id = None

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

    if thrower is None:
        game.thrower_id = None
    else:
        game.thrower_id = thrower.id

    game.catch_event = asyncio.Event()

    current_catch_event = game.catch_event


    # =====================================================
    # TEKST RZUTU
    # =====================================================

    if bot_throw:

        throw_text = random.choice(
            BOT_THROW_TEXTS
        ).format(
            target=target.mention
        )

    else:

        throw_text = random.choice(
            PLAYER_THROW_TEXTS
        ).format(
            thrower=thrower.mention,
            target=target.mention
        )

    await channel.send(
        throw_text
    )


    countdown_message = await channel.send(
        f"## 🍅 **3...**\n"
        f"{target.mention} — **ŁAP!**"
    )


    # =====================================================
    # 3 -> 2
    # =====================================================

    try:

        await asyncio.wait_for(
            current_catch_event.wait(),
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
        content=(
            f"## 🍅 **2...**\n"
            f"{target.mention} — **SZYBKO!**"
        )
    )


    # =====================================================
    # 2 -> 1
    # =====================================================

    try:

        await asyncio.wait_for(
            current_catch_event.wait(),
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
        content=(
            f"## 🍅 **1...**\n"
            f"{target.mention} — **OSTATNIA CHWILA!**"
        )
    )


    # =====================================================
    # 1 -> 0
    # =====================================================

    try:

        await asyncio.wait_for(
            current_catch_event.wait(),
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


    # =====================================================
    # 0 - NIE ZŁAPAŁ
    # =====================================================

    await countdown_message.edit(
        content="## 💥🍅 **0!**"
    )


    thrower_id = game.thrower_id


    game.in_flight = False
    game.target_id = None
    game.thrower_id = None
    game.catch_event = None

    # Pomidor ZAWSZE przechodzi do celu.
    game.holder_id = target.id


    # =====================================================
    # NIEZŁAPANY RZUT GRACZA
    # =====================================================

    if thrower_id is not None:

        add_point(
            guild.id,
            thrower_id
        )

        text = random.choice(
            PLAYER_HIT_TEXTS
        ).format(
            target=target.mention,
            thrower=mention(
                thrower_id
            )
        )

        await channel.send(
            text
            +
            f"\n\n🍅 Pomidor przechodzi do {target.mention}."
            f"\nTeraz to **{target.mention} rzuca dalej!**"
        )


    # =====================================================
    # NIEZŁAPANY RZUT KRÓLA
    # =====================================================

    else:

        text = random.choice(
            BOT_MISS_TEXTS
        ).format(
            target=target.mention
        )

        await channel.send(
            text
            +
            "\n\n👑 Król Pomidora nie zdobywa punktów."
            f"\n🍅 Pomidor przechodzi do {target.mention}."
            f"\nTeraz to **{target.mention} rzuca dalej!**"
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
            "🤲🍅 **Żaden pomidor aktualnie nie leci!**"
        )

        return

    if ctx.author.id != game.target_id:

        await ctx.send(
            random.choice(
                CHEAT_TEXTS
            )
        )

        return


    # =====================================================
    # PRAWIDŁOWE ZŁAPANIE
    # =====================================================

    game.in_flight = False
    game.target_id = None
    game.thrower_id = None

    # Osoba przejmuje pomidora
    game.holder_id = ctx.author.id


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
        target=ctx.author.mention
    )


    await ctx.send(
        catch_text
        +
        f"\n\n🍅 Pomidor należy teraz do {ctx.author.mention}."
        "\nMożesz użyć `!rzuc @osoba` lub `!losuj`."
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

        status = (
            "🍅 **Pomidor jest właśnie w powietrzu!**\n"
            f"🎯 Cel: {mention(game.target_id)}"
        )


    elif game.holder_id is not None:

        status = (
            "🍅 Pomidora posiada: "
            f"{mention(game.holder_id)}"
        )


    else:

        status = (
            "👑🍅 **Król Pomidora przygotowuje pierwszy rzut...**"
        )


    embed = discord.Embed(
        title="🍅 Aktualny stan zabawy",
        description=(
            f"🎮 Zabawę uruchomił: "
            f"{mention(game.host_id)}\n\n"
            f"{status}"
        ),
        color=discord.Color.orange()
    )


    await ctx.send(
        embed=embed
    )


# =========================================================
# RANKING
# Ranking pokazuje WSZYSTKICH,
# także osoby, które użyły !wyjdz.
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
            "🍅 **Nikt nie zdobył jeszcze punktu!**"
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
            f"{status} <@{row['user_id']}> "
            f"— **{row['points']} pkt**"
        )


    embed = discord.Embed(
        title="👑🍅 Ranking Króla Pomidora",
        description="\n".join(
            lines
        ),
        color=discord.Color.gold()
    )


    embed.set_footer(
        text=(
            "🟢 aktywny w puli • ⚫ poza aktywną pulą | "
            "Punkty sumują się przez wszystkie dni."
        )
    )


    await ctx.send(
        embed=embed
    )


# =========================================================
# STOP - KONIEC TYLKO DANEGO DNIA
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


    cancel_auto_throw(
        game
    )


    current_event = game.catch_event

    game.catch_event = None


    if current_event is not None:
        current_event.set()


    players = get_all_players(
        ctx.guild.id
    )


    ranked = [
        row
        for row in players
        if row["points"] > 0
    ]


    if ranked:

        top_lines = []

        for position, row in enumerate(
            ranked[:5],
            start=1
        ):

            top_lines.append(
                f"**{position}.** "
                f"<@{row['user_id']}> "
                f"— 🍅 **{row['points']} pkt**"
            )

        ranking_text = "\n".join(
            top_lines
        )

    else:

        ranking_text = (
            "Nikt nie zdobył jeszcze punktów."
        )


    embed = discord.Embed(
        title="🛑🍅 Dzisiejsza rozgrywka zakończona!",
        description=(
            "Król Pomidora na dziś chowa swoje pomidory. 👑\n\n"
            "**Wszystkie punkty zostały zachowane.**\n"
            "Ranking będzie kontynuowany podczas kolejnego dnia."
        ),
        color=discord.Color.red()
    )


    embed.add_field(
        name="🍅 Aktualna czołówka",
        value=ranking_text,
        inline=False
    )


    await ctx.send(
        embed=embed
    )


# =========================================================
# KONIEC CAŁEJ EDYCJI
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
            "🍅🚫 **Najpierw zakończ bieżącą rozgrywkę "
            "komendą `!stop`.**"
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
        row["user_id"]
        for row in ranked
        if row["points"] == best_score
    ]


    if len(winners) == 1:

        winner_text = (
            f"👑🍅 **KRÓLEM POMIDORA ZOSTAJE "
            f"{mention(winners[0])}!**\n\n"
            f"🏆 Łączny wynik: **{best_score} pkt**"
        )

    else:

        names = ", ".join(
            mention(user_id)
            for user_id in winners
        )

        winner_text = (
            "👑🍅 **MAMY REMIS NA SZCZYCIE!**\n\n"
            f"{names}\n\n"
            f"Każdy zdobył **{best_score} pkt**!"
        )


    lines = []


    for position, row in enumerate(
        ranked,
        start=1
    ):

        lines.append(
            f"**{position}.** "
            f"<@{row['user_id']}> "
            f"— 🍅 **{row['points']} pkt**"
        )


    embed = discord.Embed(
        title="👑🍅 FINAŁ KRÓLA POMIDORA",
        description=winner_text,
        color=discord.Color.gold()
    )


    embed.add_field(
        name="🏆 Końcowy ranking",
        value="\n".join(
            lines
        ),
        inline=False
    )


    embed.set_footer(
        text=(
            "Wyniki nie zostały automatycznie usunięte."
        )
    )


    await ctx.send(
        embed=embed
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
            "🍅🚫 **Najpierw zakończ zabawę "
            "komendą `!stop`.**"
        )

        return


    reset_points(
        ctx.guild.id
    )


    await ctx.send(
        "🧹🍅 **Ranking został wyzerowany!**\n"
        "✅ Lista uczestników została zachowana."
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
        "💣🍅 **PEŁNY RESET KRÓLA POMIDORA!**\n\n"
        "✅ Usunięto aktywną pulę\n"
        "✅ Usunięto nieaktywnych uczestników\n"
        "✅ Usunięto wszystkie punkty\n"
        "✅ Wyczyszczono ranking\n"
        "✅ Zatrzymano aktywną zabawę\n\n"
        "👑 Król Pomidora rozpoczyna od zera."
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
            "### 🍅 Dla uczestników\n"
            "`!dolacz` — dołącza lub wraca do aktywnej puli\n"
            "`!wyjdz` / `!wyjdź` — opuszcza pulę, ale zachowuje wynik\n"
            "`!lista` — pokazuje aktywnych uczestników\n"
            "`!rzuc @osoba` — rzuca pomidorem\n"
            "`!rzuć @osoba` — rzuca pomidorem\n"
            "`!losuj` — losuje aktywny cel\n"
            "`!lapie` / `!łapie` — łapie pomidora\n"
            "`!pomidor` — pokazuje, kto ma pomidora\n"
            "`!ranking` — wspólny ranking całej zabawy\n\n"

            "### 👑 Opiekun Zabaw / Dyrekcja\n"
            "`!startpomidor` — rozpoczyna rozgrywkę\n"
            "`!stop` — kończy rozgrywkę danego dnia\n"
            "`!koniecpomidora` — finał całej edycji\n"
            "`!dodaj @osoba` — dodaje lub przywraca osobę\n"
            "`!usun @osoba` — usuwa z aktywnej puli, zachowuje wynik\n"
            "`!resetpunkty` — zeruje wszystkie punkty\n"
            "`!resetpomidor` — pełne usunięcie wszystkiego\n\n"

            "### 🎯 Punktacja\n"
            "**+1 pkt** za złapanie pomidora.\n"
            "**+1 pkt** dla rzucającego, jeśli cel nie złapie.\n\n"

            "🍅 Po każdym rzucie pomidor przechodzi "
            "do osoby będącej celem."
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
async def on_command_error(
    ctx,
    error
):

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
            "🍅❓ **Nie znalazłem takiej osoby. "
            "Najlepiej oznacz ją przez @.**"
        )

        return


    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):

        await ctx.send(
            "🍅❓ **Brakuje czegoś w tej komendzie. "
            "Użyj `!pomocpomidor`.**"
        )

        return


    print(
        f"❌ Błąd komendy: {repr(error)}"
    )


# =========================================================
# URUCHOMIENIE
# =========================================================

if not TOKEN:

    raise RuntimeError(
        "Brak DISCORD_TOKEN! "
        "Dodaj token bota do zmiennej środowiskowej."
    )


bot.run(TOKEN)
