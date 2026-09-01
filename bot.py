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

# Odstęp między 3 -> 2 -> 1 -> 0
COUNTDOWN_SECONDS = 2

# Role, które mogą zarządzać zabawą.
# Administrator / osoba z "Zarządzanie serwerem" też zawsze może.
OPERATOR_ROLE_NAMES = {
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
    PRIMARY KEY (guild_id, user_id)
)
""")

db.commit()


def add_player(guild_id: int, user_id: int):
    db.execute(
        """
        INSERT OR IGNORE INTO players (guild_id, user_id, points)
        VALUES (?, ?, 0)
        """,
        (guild_id, user_id)
    )
    db.commit()


def player_exists(guild_id: int, user_id: int) -> bool:
    row = db.execute(
        """
        SELECT 1
        FROM players
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id)
    ).fetchone()

    return row is not None


def remove_player(guild_id: int, user_id: int):
    db.execute(
        """
        DELETE FROM players
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id)
    )
    db.commit()


def get_players(guild_id: int):
    return db.execute(
        """
        SELECT user_id, points
        FROM players
        WHERE guild_id = ?
        ORDER BY points DESC, user_id ASC
        """,
        (guild_id,)
    ).fetchall()


def add_point(guild_id: int, user_id: int):
    db.execute(
        """
        UPDATE players
        SET points = points + 1
        WHERE guild_id = ? AND user_id = ?
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
# STAN AKTUALNEJ RUNDY
# =========================================================

class TomatoGame:
    def __init__(self):
        self.active = False

        # Osoba, która uruchomiła rundę
        self.host_id = None

        # Osoba aktualnie posiadająca pomidora
        self.holder_id = None

        # Czy pomidor aktualnie leci
        self.in_flight = False

        # W kogo leci
        self.target_id = None

        # Event informujący, że pomidor został złapany
        self.catch_event = None

        # Kanał rozgrywki
        self.channel_id = None


games = {}


def get_game(guild_id: int) -> TomatoGame:
    if guild_id not in games:
        games[guild_id] = TomatoGame()

    return games[guild_id]


# =========================================================
# LOSOWE TEKSTY
# =========================================================

THROW_TEXTS = [
    "🍅 **{thrower} bierze zamach i posyła pomidora prosto w {target}!**",
    "🍅💨 **UWAGA! {thrower} rzuca pomidorem w {target}!**",
    "😈🍅 **{thrower} uśmiecha się podejrzanie... POMIDOR LECI W {target}!**",
    "🍅 **{thrower} nie ma litości! Celem zostaje {target}!**",
    "💥🍅 **Nadciąga pomidorowa katastrofa! {thrower} rzuca w {target}!**",
    "👀🍅 **{target}, lepiej patrz w górę! {thrower} właśnie rzucił pomidorem!**",
]


CATCH_TEXTS = [
    "🍅✨ **ZŁAPANY! {target} przechwytuje pomidora! +1 punkt!**",
    "👏🍅 **Ale refleks! {target} łapie pomidora i zdobywa punkt!**",
    "😎🍅 **{target} łapie go bez najmniejszego problemu! +1!**",
    "🔥🍅 **PIĘKNE ZŁAPANIE! {target} przejmuje pomidora!**",
    "👑🍅 **{target} pokazuje klasę! Pomidor złapany — +1 punkt!**",
    "🍅🤲 **JEST! {target} zdążył przed rozplaskaniem! +1!**",
]


MISS_TEXTS = [
    "💥🍅 **PLASK! {target} nie zdążył złapać pomidora!**",
    "🍅💨 **Za późno! Pomidor przemknął obok {target}!**",
    "😈🍅 **Oj... refleks dziś nie dopisał. {target} nie łapie pomidora!**",
    "💥 **POMIDOROWA KATASTROFA! {target} nie zdążył! 🍅**",
    "🍅🫠 **Pomidor kończy swój żywot. {target} był o chwilę za wolny!**",
    "👀🍅 **Było blisko... ale pomidor nie został złapany przez {target}!**",
]


CHEAT_TEXTS = [
    "🤨🍅 **Ładne próby, ale ten pomidor nawet nie leci w Twoją stronę!**",
    "😏 **Oj, nie oszukujemy! Pomidor ma zupełnie inny cel. 🍅**",
    "🚨🍅 **POMIDOROWA POLICJA! To nie Twój pomidor do złapania!**",
    "😂🍅 **Sprytnie, ale nie tym razem. Poczekaj, aż ktoś rzuci w Ciebie!**",
]


NO_TOMATO_TEXTS = [
    "🍅 **Hola, hola! Najpierw trzeba mieć pomidora, żeby nim rzucać.**",
    "🤨 **A skąd Ty masz tego pomidora? Aktualnie należy do kogoś innego! 🍅**",
    "🍅🚫 **Nie tak szybko! Nie jesteś aktualnym posiadaczem pomidora.**",
]


# =========================================================
# FUNKCJE POMOCNICZE
# =========================================================

def is_operator(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True

    if member.guild_permissions.manage_guild:
        return True

    member_roles = {role.name for role in member.roles}

    return bool(member_roles.intersection(OPERATOR_ROLE_NAMES))


async def operator_required(ctx):
    if not isinstance(ctx.author, discord.Member):
        return False

    if is_operator(ctx.author):
        return True

    await ctx.send(
        "🍅🚫 **Ta komenda jest dostępna tylko dla prowadzącego/Dyrekcji.**"
    )

    return False


def mention(user_id):
    if user_id is None:
        return "—"

    return f"<@{user_id}>"


# =========================================================
# START BOTA
# =========================================================

@bot.event
async def on_ready():
    print("========================================")
    print(f"🍅 KRÓL POMIDORA ZALOGOWANY")
    print(f"👑 Bot: {bot.user}")
    print(f"🆔 ID: {bot.user.id}")
    print("========================================")

    await bot.change_presence(
        activity=discord.Game(name="🍅 poluje z pomidorami")
    )


# =========================================================
# DOŁĄCZANIE DO PULI
# =========================================================

@bot.command(name="dolacz", aliases=["dołącz"])
async def dolacz(ctx):
    if not ctx.guild:
        return

    if player_exists(ctx.guild.id, ctx.author.id):
        await ctx.send(
            f"🍅 {ctx.author.mention}, **już znajdujesz się w puli Króla Pomidora!**"
        )
        return

    add_player(ctx.guild.id, ctx.author.id)

    await ctx.send(
        f"🍅👑 {ctx.author.mention} **dołącza do pomidorowej bitwy!**\n"
        f"Od teraz można rzucać w Ciebie pomidorem. 😈"
    )


# =========================================================
# RĘCZNE DODAWANIE
# =========================================================

@bot.command(name="dodaj")
async def dodaj(ctx, member: discord.Member = None):
    if not ctx.guild:
        return

    if not await operator_required(ctx):
        return

    if member is None:
        await ctx.send("🍅 Użycie: `!dodaj @osoba`")
        return

    if member.bot:
        await ctx.send("🤖🍅 **Botów do bitwy pomidorowej nie zapisujemy!**")
        return

    if player_exists(ctx.guild.id, member.id):
        await ctx.send(
            f"🍅 {member.mention} **już znajduje się na liście.**"
        )
        return

    add_player(ctx.guild.id, member.id)

    await ctx.send(
        f"✅🍅 **Dodano {member.mention} do puli Króla Pomidora!**"
    )


# =========================================================
# USUWANIE Z LISTY
# =========================================================

@bot.command(name="usun", aliases=["usuń"])
async def usun(ctx, member: discord.Member = None):
    if not ctx.guild:
        return

    if not await operator_required(ctx):
        return

    if member is None:
        await ctx.send("🍅 Użycie: `!usun @osoba`")
        return

    game = get_game(ctx.guild.id)

    if game.active:
        if game.holder_id == member.id:
            await ctx.send(
                "🍅🚫 **Nie możesz teraz usunąć tej osoby — aktualnie posiada pomidora!**"
            )
            return

        if game.in_flight and game.target_id == member.id:
            await ctx.send(
                "🍅🚫 **Nie możesz teraz usunąć tej osoby — pomidor właśnie w nią leci!**"
            )
            return

    if not player_exists(ctx.guild.id, member.id):
        await ctx.send(
            f"🍅 {member.mention} **nie znajduje się na liście.**"
        )
        return

    remove_player(ctx.guild.id, member.id)

    await ctx.send(
        f"🗑️🍅 **Usunięto {member.mention} z puli graczy.**"
    )


# =========================================================
# LISTA GRACZY
# =========================================================

@bot.command(name="lista", aliases=["gracze"])
async def lista(ctx):
    if not ctx.guild:
        return

    players = get_players(ctx.guild.id)

    if not players:
        await ctx.send(
            "🍅 **Lista jest jeszcze pusta.**\n"
            "Użyj `!dolacz`, żeby wejść do zabawy!"
        )
        return

    lines = []

    for index, row in enumerate(players, start=1):
        member = ctx.guild.get_member(row["user_id"])

        if member:
            name = member.mention
        else:
            name = f"<@{row['user_id']}>"

        lines.append(
            f"**{index}.** {name} — 🍅 **{row['points']} pkt**"
        )

    embed = discord.Embed(
        title="🍅 Pula Króla Pomidora",
        description="\n".join(lines),
        color=discord.Color.red()
    )

    embed.set_footer(
        text=f"Liczba graczy: {len(players)}"
    )

    await ctx.send(embed=embed)


# =========================================================
# START RUNDY
# =========================================================

@bot.command(name="startpomidor", aliases=["start"])
async def startpomidor(ctx):
    if not ctx.guild:
        return

    if not await operator_required(ctx):
        return

    game = get_game(ctx.guild.id)

    if game.active:
        await ctx.send(
            "🍅🚫 **Król Pomidora już trwa!**"
        )
        return

    players = get_players(ctx.guild.id)

    if len(players) < 1:
        await ctx.send(
            "🍅 **Nie mamy jeszcze żadnych graczy!**\n"
            "Najpierw niech ktoś użyje `!dolacz`."
        )
        return

    game.active = True
    game.host_id = ctx.author.id
    game.holder_id = ctx.author.id
    game.in_flight = False
    game.target_id = None
    game.catch_event = None
    game.channel_id = ctx.channel.id

    embed = discord.Embed(
        title="👑🍅 KRÓL POMIDORA ROZPOCZĘTY!",
        description=(
            f"Rozgrywkę prowadzi {ctx.author.mention}!\n\n"
            "🍅 Prowadzący otrzymuje pierwszego pomidora.\n"
            "🍅 Osoba posiadająca pomidora używa `!rzuc @osoba`.\n"
            "🍅 Cel musi zdążyć wpisać `!lapie` przed **0**.\n"
            "🍅 Każde udane złapanie = **+1 punkt**.\n\n"
            "**3... 2... 1... POMIDOROWA WOJNA!** 😈"
        ),
        color=discord.Color.red()
    )

    await ctx.send(embed=embed)


# =========================================================
# RZUT
# =========================================================

@bot.command(name="rzuc", aliases=["rzuć"])
async def rzuc(ctx, target: discord.Member = None):
    if not ctx.guild:
        return

    game = get_game(ctx.guild.id)

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
            "🍅💨 **Spokojnie! Jeden pomidor już leci!**"
        )
        return

    if game.holder_id != ctx.author.id:
        await ctx.send(random.choice(NO_TOMATO_TEXTS))
        return

    if target is None:
        await ctx.send(
            "🍅 Użycie: `!rzuc @osoba`"
        )
        return

    if target.bot:
        await ctx.send(
            "🤖🍅 **Nie rzucamy pomidorami w boty! One nie mają refleksu.**"
        )
        return

    if target.id == ctx.author.id:
        await ctx.send(
            "😂🍅 **Rzut w samego siebie? Ambitnie, ale nie. Wybierz kogoś innego!**"
        )
        return

    if not player_exists(ctx.guild.id, target.id):
        await ctx.send(
            f"🍅🚫 {target.mention} **nie znajduje się w puli graczy!**\n"
            f"Ta osoba może użyć `!dolacz`."
        )
        return

    await perform_throw(ctx, target)


# =========================================================
# LOSOWY RZUT
# =========================================================

@bot.command(name="losuj")
async def losuj(ctx):
    if not ctx.guild:
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send(
            "🍅💤 **Król Pomidora aktualnie nie trwa.**"
        )
        return

    if ctx.channel.id != game.channel_id:
        return

    if game.in_flight:
        await ctx.send(
            "🍅💨 **Jeden pomidor już jest w powietrzu!**"
        )
        return

    if game.holder_id != ctx.author.id:
        await ctx.send(random.choice(NO_TOMATO_TEXTS))
        return

    rows = get_players(ctx.guild.id)

    possible_targets = []

    for row in rows:
        user_id = row["user_id"]

        if user_id == ctx.author.id:
            continue

        member = ctx.guild.get_member(user_id)

        if member and not member.bot:
            possible_targets.append(member)

    if not possible_targets:
        await ctx.send(
            "🍅 **Nie mam kogo wylosować. Potrzebujemy więcej aktywnych graczy!**"
        )
        return

    target = random.choice(possible_targets)

    await ctx.send(
        f"🎲🍅 **Losowanie celu... padło na {target.mention}!**"
    )

    await perform_throw(ctx, target)


# =========================================================
# MECHANIKA RZUTU + ODLICZANIE
# =========================================================

async def perform_throw(ctx, target: discord.Member):
    game = get_game(ctx.guild.id)

    game.in_flight = True
    game.target_id = target.id
    game.catch_event = asyncio.Event()

    throw_text = random.choice(THROW_TEXTS).format(
        thrower=ctx.author.mention,
        target=target.mention
    )

    await ctx.send(throw_text)

    countdown_message = await ctx.send(
        f"## 🍅 **3...**\n"
        f"{target.mention} — **ŁAP!**"
    )

    # 3 -> 2
    try:
        await asyncio.wait_for(
            game.catch_event.wait(),
            timeout=COUNTDOWN_SECONDS
        )

        return

    except asyncio.TimeoutError:
        pass

    if not game.active or not game.in_flight:
        return

    await countdown_message.edit(
        content=(
            f"## 🍅 **2...**\n"
            f"{target.mention} — **SZYBKO!**"
        )
    )

    # 2 -> 1
    try:
        await asyncio.wait_for(
            game.catch_event.wait(),
            timeout=COUNTDOWN_SECONDS
        )

        return

    except asyncio.TimeoutError:
        pass

    if not game.active or not game.in_flight:
        return

    await countdown_message.edit(
        content=(
            f"## 🍅 **1...**\n"
            f"{target.mention} — **OSTATNIA CHWILA!**"
        )
    )

    # 1 -> 0
    try:
        await asyncio.wait_for(
            game.catch_event.wait(),
            timeout=COUNTDOWN_SECONDS
        )

        return

    except asyncio.TimeoutError:
        pass

    if not game.active or not game.in_flight:
        return

    # NIE ZŁAPAŁ
    game.in_flight = False
    game.target_id = None

    # Pomidor wraca do prowadzącego
    game.holder_id = game.host_id

    await countdown_message.edit(
        content="## 💥🍅 **0!**"
    )

    miss_text = random.choice(MISS_TEXTS).format(
        target=target.mention
    )

    await ctx.send(
        miss_text
        + f"\n\n👑 Pomidor wraca do prowadzącego: "
          f"{mention(game.host_id)}."
    )


# =========================================================
# ŁAPANIE
# =========================================================

@bot.command(name="lapie", aliases=["łapie", "lap", "łap"])
async def lapie(ctx):
    if not ctx.guild:
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send(
            "🍅 **Nie ma teraz czego łapać — zabawa nie trwa.**"
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
        await ctx.send(random.choice(CHEAT_TEXTS))
        return

    # ZŁAPANY
    game.in_flight = False
    game.target_id = None
    game.holder_id = ctx.author.id

    add_point(
        ctx.guild.id,
        ctx.author.id
    )

    if game.catch_event:
        game.catch_event.set()

    catch_text = random.choice(CATCH_TEXTS).format(
        target=ctx.author.mention
    )

    await ctx.send(
        catch_text
        + f"\n\n🍅 Teraz **{ctx.author.mention} posiada pomidora** "
          f"i może użyć `!rzuc @osoba` lub `!losuj`."
    )


# =========================================================
# STATUS POMIDORA
# =========================================================

@bot.command(name="pomidor")
async def pomidor(ctx):
    if not ctx.guild:
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send(
            "🍅💤 **Król Pomidora aktualnie nie trwa.**"
        )
        return

    if game.in_flight:
        status = (
            f"🍅 **Pomidor jest w powietrzu!**\n"
            f"🎯 Cel: {mention(game.target_id)}"
        )
    else:
        status = (
            f"🍅 Aktualny posiadacz: "
            f"{mention(game.holder_id)}"
        )

    embed = discord.Embed(
        title="🍅 Aktualny stan zabawy",
        description=(
            f"👑 Prowadzący: {mention(game.host_id)}\n"
            f"{status}"
        ),
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

    players = get_players(ctx.guild.id)

    if not players:
        await ctx.send(
            "🍅 **Ranking jest jeszcze pusty.**"
        )
        return

    ranked = [
        row
        for row in players
        if row["points"] > 0
    ]

    if not ranked:
        await ctx.send(
            "🍅 **Nikt nie zdobył jeszcze żadnego punktu!**"
        )
        return

    medals = {
        1: "🥇",
        2: "🥈",
        3: "🥉"
    }

    lines = []

    for position, row in enumerate(ranked, start=1):
        medal = medals.get(position, "🍅")

        lines.append(
            f"{medal} **{position}.** "
            f"<@{row['user_id']}> — "
            f"**{row['points']} pkt**"
        )

    embed = discord.Embed(
        title="👑🍅 Ranking Króla Pomidora",
        description="\n".join(lines),
        color=discord.Color.gold()
    )

    await ctx.send(embed=embed)


# =========================================================
# STOP
# =========================================================

@bot.command(name="stop")
async def stop(ctx):
    if not ctx.guild:
        return

    if not await operator_required(ctx):
        return

    game = get_game(ctx.guild.id)

    if not game.active:
        await ctx.send(
            "🍅 **Zabawa i tak już jest zatrzymana.**"
        )
        return

    # Zatrzymujemy ewentualne odliczanie
    game.active = False
    game.in_flight = False
    game.target_id = None
    game.holder_id = None

    if game.catch_event:
        game.catch_event.set()

    game.catch_event = None

    players = get_players(ctx.guild.id)

    ranked = [
        row
        for row in players
        if row["points"] > 0
    ]

    if not ranked:
        await ctx.send(
            "🛑🍅 **KONIEC KRÓLA POMIDORA!**\n\n"
            "Tym razem nikt nie zdobył punktów.\n\n"
            "Lista graczy została zachowana."
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
            f"👑 **KRÓLEM POMIDORA ZOSTAJE "
            f"{mention(winners[0])}!**\n"
            f"🍅 Wynik: **{best_score} pkt**"
        )
    else:
        names = ", ".join(
            mention(user_id)
            for user_id in winners
        )

        winner_text = (
            f"👑🍅 **MAMY REMIS!**\n"
            f"{names}\n"
            f"Każdy zdobył **{best_score} pkt**!"
        )

    embed = discord.Embed(
        title="🛑🍅 KONIEC KRÓLA POMIDORA!",
        description=winner_text,
        color=discord.Color.gold()
    )

    embed.add_field(
        name="📌 Ważne",
        value=(
            "Lista graczy i zdobyte punkty **zostały zachowane**.\n"
            "`!resetpunkty` — zeruje punkty.\n"
            "`!resetpomidor` — czyści wszystko."
        ),
        inline=False
    )

    await ctx.send(embed=embed)


# =========================================================
# RESET SAMYCH PUNKTÓW
# =========================================================

@bot.command(name="resetpunkty")
async def resetpunkty_command(ctx):
    if not ctx.guild:
        return

    if not await operator_required(ctx):
        return

    game = get_game(ctx.guild.id)

    if game.active:
        await ctx.send(
            "🍅🚫 **Najpierw zakończ aktualną rundę komendą `!stop`.**"
        )
        return

    reset_points(ctx.guild.id)

    await ctx.send(
        "🧹🍅 **Ranking i wszystkie punkty zostały wyzerowane!**\n"
        "Lista zapisanych graczy została zachowana. ✅"
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

    game = get_game(ctx.guild.id)

    # Zatrzymujemy wszystko
    game.active = False
    game.in_flight = False
    game.target_id = None
    game.holder_id = None
    game.host_id = None
    game.channel_id = None

    if game.catch_event:
        game.catch_event.set()

    game.catch_event = None

    reset_everything(ctx.guild.id)

    await ctx.send(
        "💣🍅 **PEŁNY RESET KRÓLA POMIDORA!**\n\n"
        "✅ Usunięto listę graczy\n"
        "✅ Usunięto ranking\n"
        "✅ Wyzerowano punkty\n"
        "✅ Zakończono aktualną rundę\n"
        "✅ Pomidor został schowany do następnej wojny 😈"
    )


# =========================================================
# POMOC
# =========================================================

@bot.command(name="pomocpomidor", aliases=["komendypomidor"])
async def pomocpomidor(ctx):
    embed = discord.Embed(
        title="👑🍅 Król Pomidora — komendy",
        description=(
            "**Dla graczy**\n"
            "`!dolacz` — dołącza do stałej puli\n"
            "`!lista` — pokazuje zapisanych graczy\n"
            "`!rzuc @osoba` / `!rzuć @osoba` — rzuca pomidorem\n"
            "`!losuj` — losuje cel z puli\n"
            "`!lapie` / `!łapie` — próbuje złapać pomidora\n"
            "`!pomidor` — pokazuje aktualny stan\n"
            "`!ranking` — pokazuje wyniki\n\n"

            "**Dla prowadzącego**\n"
            "`!startpomidor` — rozpoczyna rundę\n"
            "`!stop` — kończy rundę\n"
            "`!dodaj @osoba` — dodaje osobę do puli\n"
            "`!usun @osoba` — usuwa osobę z puli\n"
            "`!resetpunkty` — zeruje punkty, zostawia listę\n"
            "`!resetpomidor` — usuwa absolutnie wszystko"
        ),
        color=discord.Color.red()
    )

    await ctx.send(embed=embed)


# =========================================================
# OBSŁUGA BŁĘDÓW
# =========================================================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MemberNotFound):
        await ctx.send(
            "🍅❓ **Nie znalazłem takiej osoby. Najlepiej oznacz ją przez @.**"
        )
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "🍅❓ **Brakuje argumentu w tej komendzie. Użyj `!pomocpomidor`.**"
        )
        return

    print(f"Błąd komendy: {repr(error)}")


# =========================================================
# URUCHOMIENIE
# =========================================================

if not TOKEN:
    raise RuntimeError(
        "Brak DISCORD_TOKEN! Dodaj token bota do zmiennej środowiskowej."
    )

bot.run(TOKEN)
