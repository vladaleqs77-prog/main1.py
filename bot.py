import asyncio
import logging
import os
import random
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("izvne-mafia")

DB_PATH = os.getenv("DB_PATH", "game.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

REGISTRATION_SECONDS = 5 * 60
EXTEND_SECONDS = 2 * 60
DAY_SECONDS = 90
NIGHT_SECONDS = 60
MIN_PLAYERS = 4

ROLE_PEACE = "Мирный"
ROLE_MAFIA = "Тень"
ROLE_DOCTOR = "Проводник"
ROLE_SHERIFF = "Наблюдатель"

NIGHT_LOCK = asyncio.Lock()


@dataclass
class PlayerState:
    user_id: int
    full_name: str
    username: str
    is_alive: bool = True
    role: str = ROLE_PEACE
    voted_target: Optional[int] = None
    night_target: Optional[int] = None


@dataclass
class GameState:
    chat_id: int
    title: str = "ИЗВНЕ"
    registration_open: bool = False
    game_started: bool = False
    phase: str = "idle"  # idle, registration, night, day, ended
    panel_message_id: Optional[int] = None
    players: Dict[int, PlayerState] = field(default_factory=dict)
    registration_job_name: Optional[str] = None
    phase_job_name: Optional[str] = None
    day_number: int = 0
    mafia_kill_target: Optional[int] = None
    doctor_save_target: Optional[int] = None
    sheriff_check_target: Optional[int] = None
    sheriff_check_result: Optional[str] = None
    pending_night_actions: Dict[str, Optional[int]] = field(default_factory=lambda: {
        "mafia": None,
        "doctor": None,
        "sheriff": None,
    })


GAMES: Dict[int, GameState] = {}


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_panel (
            chat_id INTEGER PRIMARY KEY,
            panel_message_id INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def save_panel_message(chat_id: int, message_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO chat_panel(chat_id, panel_message_id)
        VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET panel_message_id = excluded.panel_message_id
        """,
        (chat_id, message_id),
    )
    conn.commit()
    conn.close()


def load_panel_message(chat_id: int) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT panel_message_id FROM chat_panel WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_game(chat_id: int) -> GameState:
    if chat_id not in GAMES:
        GAMES[chat_id] = GameState(chat_id=chat_id)
        GAMES[chat_id].panel_message_id = load_panel_message(chat_id)
    return GAMES[chat_id]


def role_set_for_count(count: int) -> List[str]:
    if count <= 5:
        roles = [ROLE_MAFIA, ROLE_DOCTOR, ROLE_SHERIFF]
    elif count <= 8:
        roles = [ROLE_MAFIA, ROLE_MAFIA, ROLE_DOCTOR, ROLE_SHERIFF]
    else:
        roles = [ROLE_MAFIA, ROLE_MAFIA, ROLE_MAFIA, ROLE_DOCTOR, ROLE_SHERIFF]
    while len(roles) < count:
        roles.append(ROLE_PEACE)
    random.shuffle(roles)
    return roles[:count]


def player_label(player: PlayerState) -> str:
    uname = f"@{player.username}" if player.username else player.full_name
    status = "🟢" if player.is_alive else "⚫️"
    return f"{status} {uname}"


def make_panel_text(game: GameState) -> str:
    lines = [
        f"🛰 <b>{game.title}</b>",
        f"Фаза: <b>{phase_human(game.phase)}</b>",
        "",
    ]
    if not game.players:
        lines.append("Пока никто не подключился.")
    else:
        lines.append("Участники:")
        for idx, p in enumerate(game.players.values(), start=1):
            lines.append(f"{idx}. {player_label(p)}")
    if game.game_started:
        alive = sum(1 for p in game.players.values() if p.is_alive)
        lines += ["", f"Живых игроков: <b>{alive}</b>", f"День: <b>{game.day_number}</b>"]
    elif game.registration_open:
        lines += ["", "Набор открыт. Нажимай /start, чтобы войти."]
    else:
        lines += ["", "Ожидание новой игры."]
    return "\n".join(lines)


def phase_human(phase: str) -> str:
    mapping = {
        "idle": "ожидание",
        "registration": "регистрация",
        "night": "ночь",
        "day": "день",
        "ended": "завершена",
    }
    return mapping.get(phase, phase)


async def ensure_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, game: GameState) -> None:
    if game.panel_message_id:
        return

    chat = update.effective_chat
    sent = await context.bot.send_message(
        chat_id=chat.id,
        text=make_panel_text(game),
        parse_mode=ParseMode.HTML,
    )
    game.panel_message_id = sent.message_id
    save_panel_message(chat.id, sent.message_id)
    try:
        await context.bot.pin_chat_message(chat.id, sent.message_id, disable_notification=True)
    except Exception as e:
        logger.warning("Не удалось закрепить панель: %s", e)


async def update_panel(context: ContextTypes.DEFAULT_TYPE, game: GameState) -> None:
    if not game.panel_message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=game.chat_id,
            message_id=game.panel_message_id,
            text=make_panel_text(game),
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Панель не обновлена: %s", e)


async def send_private_role(context: ContextTypes.DEFAULT_TYPE, chat_id: int, player: PlayerState) -> None:
    role_text = {
        ROLE_PEACE: "Ты <b>Мирный</b>. Твоя задача — вычислить Тени.",
        ROLE_MAFIA: "Ты <b>Тень</b>. Ночью выбери цель на устранение.",
        ROLE_DOCTOR: "Ты <b>Проводник</b>. Ночью можешь спасти одного игрока.",
        ROLE_SHERIFF: "Ты <b>Наблюдатель</b>. Ночью проверяешь игрока.",
    }[player.role]
    try:
        await context.bot.send_message(
            chat_id=player.user_id,
            text=f"🪪 Роль для игры <b>ИЗВНЕ</b>\n\n{role_text}",
            parse_mode=ParseMode.HTML,
        )
    except Forbidden:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{player.full_name}, открой ЛС с ботом и нажми /start в личке, иначе роль не дойдет.",
        )


async def registration_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    game = get_game(chat_id)
    if not game.registration_open:
        return
    await start_real_game(context, game)


async def phase_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    game = get_game(chat_id)
    if game.phase == "night":
        await resolve_night(context, game)
    elif game.phase == "day":
        await start_night(context, game)


def alive_players(game: GameState) -> List[PlayerState]:
    return [p for p in game.players.values() if p.is_alive]


async def start_real_game(context: ContextTypes.DEFAULT_TYPE, game: GameState) -> None:
    if len(game.players) < MIN_PLAYERS:
        game.registration_open = False
        game.phase = "idle"
        await context.bot.send_message(
            game.chat_id,
            f"❌ Недостаточно игроков. Нужно минимум {MIN_PLAYERS}.",
        )
        await update_panel(context, game)
        return

    game.registration_open = False
    game.game_started = True
    game.phase = "night"
    game.day_number = 1

    roles = role_set_for_count(len(game.players))
    for player, role in zip(game.players.values(), roles):
        player.role = role
        player.is_alive = True
        player.voted_target = None
        player.night_target = None
        await send_private_role(context, game.chat_id, player)

    await context.bot.send_message(
        game.chat_id,
        "🌑 Игра началась.\n"
        "Роли розданы в личку.\n"
        "Сейчас <b>ночь</b> — активные роли получают кнопки для хода.",
        parse_mode=ParseMode.HTML,
    )
    await update_panel(context, game)
    await send_night_actions(context, game)
    schedule_phase_job(context, game, NIGHT_SECONDS, "night")


def schedule_registration_job(context: ContextTypes.DEFAULT_TYPE, game: GameState, seconds: int) -> None:
    if game.registration_job_name:
        old = context.application.job_queue.get_jobs_by_name(game.registration_job_name)
        for job in old:
            job.schedule_removal()
    name = f"registration:{game.chat_id}"
    game.registration_job_name = name
    context.application.job_queue.run_once(
        registration_timeout,
        when=seconds,
        chat_id=game.chat_id,
        name=name,
    )


def schedule_phase_job(context: ContextTypes.DEFAULT_TYPE, game: GameState, seconds: int, phase_name: str) -> None:
    if game.phase_job_name:
        old = context.application.job_queue.get_jobs_by_name(game.phase_job_name)
        for job in old:
            job.schedule_removal()
    name = f"phase:{game.chat_id}:{phase_name}"
    game.phase_job_name = name
    context.application.job_queue.run_once(
        phase_timeout,
        when=seconds,
        chat_id=game.chat_id,
        name=name,
    )


def cancel_jobs(context: ContextTypes.DEFAULT_TYPE, game: GameState) -> None:
    for name in [game.registration_job_name, game.phase_job_name]:
        if name:
            jobs = context.application.job_queue.get_jobs_by_name(name)
            for job in jobs:
                job.schedule_removal()
    game.registration_job_name = None
    game.phase_job_name = None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    game = get_game(chat.id)

    if chat.type == "private":
        await update.message.reply_text(
            "Привет. Добавь меня в группу и используй там /start для входа в игру."
        )
        return

    await ensure_panel(update, context, game)

    if not game.registration_open and not game.game_started:
        game.registration_open = True
        game.phase = "registration"
        await update.message.reply_text(
            "🚪 Регистрация в игру <b>ИЗВНЕ</b> открыта на 5 минут.\n"
            "Все желающие пишут /start.",
            parse_mode=ParseMode.HTML,
        )
        schedule_registration_job(context, game, REGISTRATION_SECONDS)

    if user.id not in game.players:
        game.players[user.id] = PlayerState(
            user_id=user.id,
            full_name=user.full_name,
            username=user.username or "",
        )
        await update.message.reply_text(f"✅ {user.full_name} подключился к игре.")
    else:
        await update.message.reply_text("Ты уже в списке игроков.")

    await update_panel(context, game)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🎮 <b>ИЗВНЕ — мафия-бот</b>\n\n"
        "/start — открыть набор или войти в игру\n"
        "/stopgame — остановить текущую игру\n"
        "/info — показать справку\n"
        "/extend — продлить набор еще на 2 минуты\n\n"
        "Механика:\n"
        "• сначала 5 минут на подключение\n"
        "• затем бот раздает роли в личку\n"
        "• ночью активные роли делают ход кнопками\n"
        "• днем можно обсуждать, после таймера начинается новая ночь\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = get_game(update.effective_chat.id)
    if not game.registration_open:
        await update.message.reply_text("Сейчас нет активного набора.")
        return
    schedule_registration_job(context, game, EXTEND_SECONDS)
    await update.message.reply_text("⏳ Набор продлен еще на 2 минуты.")
    await update_panel(context, game)


async def cmd_stopgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = get_game(update.effective_chat.id)
    if not game.registration_open and not game.game_started:
        await update.message.reply_text("Сейчас нет активной игры.")
        return

    cancel_jobs(context, game)
    game.registration_open = False
    game.game_started = False
    game.phase = "ended"
    for p in game.players.values():
        p.is_alive = True
        p.role = ROLE_PEACE

    await update.message.reply_text("🛑 Игра остановлена.")
    await update_panel(context, game)

    # сброс после отображения статуса
    game.players.clear()
    game.phase = "idle"
    await update_panel(context, game)


def build_target_keyboard(game: GameState, action: str, actor_id: int) -> InlineKeyboardMarkup:
    buttons = []
    for p in alive_players(game):
        if p.user_id == actor_id and action in {"kill", "check"}:
            continue
        buttons.append([InlineKeyboardButton(
            text=p.full_name,
            callback_data=f"act:{action}:{p.user_id}"
        )])
    return InlineKeyboardMarkup(buttons)


async def send_night_actions(context: ContextTypes.DEFAULT_TYPE, game: GameState) -> None:
    game.pending_night_actions = {"mafia": None, "doctor": None, "sheriff": None}
    game.mafia_kill_target = None
    game.doctor_save_target = None
    game.sheriff_check_target = None
    game.sheriff_check_result = None

    for p in alive_players(game):
        if p.role == ROLE_MAFIA:
            await context.bot.send_message(
                chat_id=p.user_id,
                text="🌑 Ночь. Выбери цель для устранения.",
                reply_markup=build_target_keyboard(game, "kill", p.user_id),
            )
        elif p.role == ROLE_DOCTOR:
            await context.bot.send_message(
                chat_id=p.user_id,
                text="💉 Ночь. Кого спасти?",
                reply_markup=build_target_keyboard(game, "save", p.user_id),
            )
        elif p.role == ROLE_SHERIFF:
            await context.bot.send_message(
                chat_id=p.user_id,
                text="🔎 Ночь. Кого проверить?",
                reply_markup=build_target_keyboard(game, "check", p.user_id),
            )


async def start_night(context: ContextTypes.DEFAULT_TYPE, game: GameState) -> None:
    game.phase = "night"
    await context.bot.send_message(game.chat_id, "🌑 Наступает ночь. Активные роли делают ход в ЛС.")
    await update_panel(context, game)
    await send_night_actions(context, game)
    schedule_phase_job(context, game, NIGHT_SECONDS, "night")


async def resolve_night(context: ContextTypes.DEFAULT_TYPE, game: GameState) -> None:
    async with NIGHT_LOCK:
        if game.phase != "night":
            return

        victim_id = game.mafia_kill_target
        saved_id = game.doctor_save_target
        checked = game.sheriff_check_target

        sheriffs = [p for p in alive_players(game) if p.role == ROLE_SHERIFF]
        if sheriffs and checked:
            target = game.players.get(checked)
            if target:
                result = "ТЕНЬ" if target.role == ROLE_MAFIA else "НЕ ТЕНЬ"
                for sh in sheriffs:
                    try:
                        await context.bot.send_message(
                            sh.user_id,
                            f"🧠 Проверка завершена: {target.full_name} — <b>{result}</b>.",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

        killed_text = "Ночь прошла тихо. Никто не выбыл."
        if victim_id and victim_id != saved_id and victim_id in game.players and game.players[victim_id].is_alive:
            game.players[victim_id].is_alive = False
            killed_text = f"☠️ Ночью выбыл игрок: <b>{game.players[victim_id].full_name}</b>."
        elif victim_id and victim_id == saved_id and victim_id in game.players:
            killed_text = f"🛡 {game.players[victim_id].full_name} был спасен."

        await context.bot.send_message(game.chat_id, killed_text, parse_mode=ParseMode.HTML)
        winner = get_winner(game)
        if winner:
            await finish_game(context, game, winner)
            return

        game.phase = "day"
        await context.bot.send_message(
            game.chat_id,
            f"☀️ День {game.day_number}. Обсуждайте. Через {DAY_SECONDS} сек. снова наступит ночь."
        )
        await update_panel(context, game)
        schedule_phase_job(context, game, DAY_SECONDS, "day")


def get_winner(game: GameState) -> Optional[str]:
    mafia_alive = sum(1 for p in alive_players(game) if p.role == ROLE_MAFIA)
    civilians_alive = sum(1 for p in alive_players(game) if p.role != ROLE_MAFIA)
    if mafia_alive == 0 and game.game_started:
        return "Мирные победили"
    if mafia_alive >= civilians_alive and game.game_started:
        return "Тени победили"
    return None


async def finish_game(context: ContextTypes.DEFAULT_TYPE, game: GameState, winner: str) -> None:
    cancel_jobs(context, game)
    game.phase = "ended"
    game.game_started = False

    roles_text = "\n".join(
        f"• {p.full_name} — {p.role}" for p in game.players.values()
    )
    await context.bot.send_message(
        game.chat_id,
        f"🏁 <b>{winner}</b>\n\nРоли:\n{roles_text}",
        parse_mode=ParseMode.HTML,
    )
    await update_panel(context, game)

    # мягкий сброс
    game.players.clear()
    game.phase = "idle"
    await update_panel(context, game)


async def on_action_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("act:"):
        return

    _, action, target_id_raw = query.data.split(":")
    target_id = int(target_id_raw)
    user = query.from_user

    # ищем игру, в которой участник жив и есть активная ночь
    game = None
    for g in GAMES.values():
        if g.phase == "night" and user.id in g.players and g.players[user.id].is_alive:
            game = g
            break

    if not game:
        await query.edit_message_text("Сейчас у тебя нет активного ночного хода.")
        return

    actor = game.players[user.id]

    if action == "kill" and actor.role != ROLE_MAFIA:
        await query.edit_message_text("Эта кнопка не для твоей роли.")
        return
    if action == "save" and actor.role != ROLE_DOCTOR:
        await query.edit_message_text("Эта кнопка не для твоей роли.")
        return
    if action == "check" and actor.role != ROLE_SHERIFF:
        await query.edit_message_text("Эта кнопка не для твоей роли.")
        return

    if target_id not in game.players or not game.players[target_id].is_alive:
        await query.edit_message_text("Цель уже недоступна.")
        return

    actor.night_target = target_id

    if action == "kill":
        game.mafia_kill_target = target_id
        game.pending_night_actions["mafia"] = target_id
        await query.edit_message_text(f"🗡 Цель выбрана: {game.players[target_id].full_name}")
    elif action == "save":
        game.doctor_save_target = target_id
        game.pending_night_actions["doctor"] = target_id
        await query.edit_message_text(f"💉 Спасение выбрано: {game.players[target_id].full_name}")
    elif action == "check":
        game.sheriff_check_target = target_id
        game.pending_night_actions["sheriff"] = target_id
        await query.edit_message_text(f"🔎 Проверка выбрана: {game.players[target_id].full_name}")

    # если все активные роли уже сходили — завершаем ночь досрочно
    mafia_exists = any(p.is_alive and p.role == ROLE_MAFIA for p in game.players.values())
    doctor_exists = any(p.is_alive and p.role == ROLE_DOCTOR for p in game.players.values())
    sheriff_exists = any(p.is_alive and p.role == ROLE_SHERIFF for p in game.players.values())

    ready = True
    if mafia_exists and game.pending_night_actions["mafia"] is None:
        ready = False
    if doctor_exists and game.pending_night_actions["doctor"] is None:
        ready = False
    if sheriff_exists and game.pending_night_actions["sheriff"] is None:
        ready = False

    if ready:
        await resolve_night(context, game)


async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "войти в игру или открыть набор"),
        BotCommand("stopgame", "остановить игру"),
        BotCommand("info", "справка"),
        BotCommand("extend", "продлить набор"),
    ])


async def post_init(app: Application) -> None:
    await set_commands(app)


async def check_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    try:
        me = await context.bot.get_chat_member(update.effective_chat.id, context.bot.id)
        if me.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            await update.effective_message.reply_text(
                "⚠️ Дай боту админку, чтобы он мог закреплять и обновлять панель."
            )
    except Exception:
        pass


async def command_wrapper(handler, update: Update, context: ContextTypes.DEFAULT_TYPE):
    await check_permissions(update, context)
    await handler(update, context)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", lambda u, c: command_wrapper(cmd_start, u, c)))
    app.add_handler(CommandHandler("info", lambda u, c: command_wrapper(cmd_info, u, c)))
    app.add_handler(CommandHandler("extend", lambda u, c: command_wrapper(cmd_extend, u, c)))
    app.add_handler(CommandHandler("stopgame", lambda u, c: command_wrapper(cmd_stopgame, u, c)))
    app.add_handler(CallbackQueryHandler(on_action_click, pattern=r"^act:"))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
