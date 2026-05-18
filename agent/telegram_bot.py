"""
OMNI AGENT - Telegram Bot
Full async Telegram interface with command routing, auth, and agent integration.
"""
import logging
import asyncio
from typing import Optional, Callable, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from agent.hooks import hooks, Event, EventType
from agent.memory import MemoryDB
from config import CONFIG

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot with authentication, routing, and agent bridge."""

    def __init__(self, memory: MemoryDB, agent_handler: Callable):
        if not CONFIG.TELEGRAM_TOKEN:
            raise ValueError("TELEGRAM_TOKEN not set in environment")
        self.memory = memory
        self.agent_handler = agent_handler  # async fn(user_id, session_id, text) -> str
        self.app = Application.builder().token(CONFIG.TELEGRAM_TOKEN).build()
        self._register_handlers()

    def _is_authorized(self, user_id: int) -> bool:
        if not CONFIG.TELEGRAM_ALLOWED_USERS:
            return True  # open if no whitelist set
        return user_id in CONFIG.TELEGRAM_ALLOWED_USERS

    def _register_handlers(self):
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("memory", self.cmd_memory))
        self.app.add_handler(CommandHandler("skills", self.cmd_skills))
        self.app.add_handler(CommandHandler("clear", self.cmd_clear))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("exec", self.cmd_exec))
        self.app.add_handler(CommandHandler("model", self.cmd_model))
        self.app.add_handler(CommandHandler("models", self.cmd_models))
        self.app.add_handler(CommandHandler("route", self.cmd_route))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_error_handler(self.handle_error)

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        self.memory.audit("telegram.start", actor=str(user.id),
                         details={"username": user.username})
        await update.message.reply_text(
            f"🤖 *OMNI Agent* online!\n\n"
            f"Hello, {user.first_name}. I'm your AI agent.\n"
            f"Send any message to interact. Use /help for commands.",
            parse_mode="Markdown"
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📋 *Commands:*\n"
            "/start — Introduction\n"
            "/help — This message\n"
            "/memory — View stored memories\n"
            "/skills — List available skills\n"
            "/clear — Clear conversation history\n"
            "/status — Agent system status\n"
            "/exec `<code>` — Execute Python code\n\n"
            "🤖 *Model Commands:*\n"
            "/models — List all 24 cloud models\n"
            "/model — Show current model\n"
            "/model `<id>` — Pin a specific model\n"
            "/model auto — Restore auto-routing\n"
            "/route `<text>` — Preview routing decision\n\n"
            "💬 Or just send a message to chat!",
            parse_mode="Markdown"
        )

    async def cmd_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        memories = self.memory.get_memories_by_category("user")
        if not memories:
            await update.message.reply_text("🧠 No memories stored yet.")
            return
        lines = [f"• *{m['key']}*: {str(m['value'])[:80]}" for m in memories[:10]]
        await update.message.reply_text(
            "🧠 *Stored Memories:*\n" + "\n".join(lines),
            parse_mode="Markdown"
        )

    async def cmd_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Retrieve skills list via hook
        if not self._is_authorized(update.effective_user.id):
            return
        skills = self.memory.get_state("skills_list") or []
        if not skills:
            await update.message.reply_text("⚙️ No skills loaded.")
            return
        lines = [f"• *{s['name']}* — {s['description']}" for s in skills[:15]]
        await update.message.reply_text(
            "⚙️ *Available Skills:*\n" + "\n".join(lines),
            parse_mode="Markdown"
        )

    async def cmd_clear(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = f"tg:{update.effective_user.id}"
        self.memory.clear_session(session_id)
        await update.message.reply_text("🗑️ Conversation history cleared.")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        status = self.memory.get_state("agent_status") or {"state": "running"}
        await update.message.reply_text(
            f"📊 *Agent Status:*\n```{status}```",
            parse_mode="Markdown"
        )

    async def cmd_exec(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        code = update.message.text.replace("/exec", "").strip()
        if not code:
            await update.message.reply_text("Usage: /exec `print('hello')`")
            return
        result = await self.agent_handler(
            update.effective_user.id,
            f"tg:{update.effective_user.id}",
            f"Execute this Python code and return the result:\n```python\n{code}\n```"
        )
        await update.message.reply_text(
            f"🔧 *Result:*\n```\n{result[:2000]}\n```",
            parse_mode="Markdown"
        )

    async def cmd_models(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """List all 24 cloud models."""
        if not self._is_authorized(update.effective_user.id):
            return
        from agent.model_registry import MODELS, ModelTier
        tiers = {
            ModelTier.FLAGSHIP: "🏆",
            ModelTier.BALANCED: "⚖️",
            ModelTier.FAST:     "⚡",
            ModelTier.MICRO:    "🔹",
        }
        lines = ["📋 *Available Models* (24 cloud models)\n"]
        by_provider: dict = {}
        for spec in MODELS.values():
            by_provider.setdefault(spec.provider, []).append(spec)
        for provider, specs in sorted(by_provider.items()):
            lines.append(f"*{provider}*")
            for s in specs:
                icon = tiers.get(s.tier, "•")
                lines.append(f"  {icon} `{s.id}`")
                lines.append(f"    _{s.best_for[0] if s.best_for else s.description[:50]}_")
        lines.append(
            "\n💡 Use `/model <model_id>` to pin a model for this session.\n"
            "Use `/model auto` to restore automatic routing."
        )
        await update.message.reply_text(
            "\n".join(lines)[:4096], parse_mode="Markdown"
        )

    async def cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Pin or check the model for this session."""
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = f"tg:{update.effective_user.id}"
        args = update.message.text.replace("/model", "").strip()

        # Inject router from agent_handler via memory state
        from agent.model_registry import MODELS, get_model
        if not args:
            # Show current model
            current = self.memory.get_state(f"session_model:{session_id}") or "auto"
            await update.message.reply_text(
                f"🤖 Current model: `{current}`\n"
                "Use `/model <model_id>` to pin, `/model auto` to auto-route, "
                "or `/models` to see all.",
                parse_mode="Markdown"
            )
            return
        if args == "auto":
            self.memory.set_state(f"session_model:{session_id}", "auto")
            await update.message.reply_text(
                "✅ Auto-routing *enabled*. The best model will be chosen per message.",
                parse_mode="Markdown"
            )
            return
        spec = get_model(args)
        if not spec:
            await update.message.reply_text(
                f"❌ Model `{args}` not found. Use `/models` to see the full list.",
                parse_mode="Markdown"
            )
            return
        self.memory.set_state(f"session_model:{session_id}", args)
        caps = ", ".join(c.value for c in spec.capabilities)
        await update.message.reply_text(
            f"✅ Pinned to *{spec.display_name}*\n"
            f"Provider: {spec.provider} | Tier: {spec.tier.value}\n"
            f"Context: {spec.context_window // 1000}k tokens\n"
            f"Capabilities: {caps}\n"
            f"Best for: {', '.join(spec.best_for[:3])}",
            parse_mode="Markdown"
        )

    async def cmd_route(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show routing decision for a sample text."""
        if not self._is_authorized(update.effective_user.id):
            return
        text = update.message.text.replace("/route", "").strip()
        if not text:
            await update.message.reply_text(
                "Usage: `/route <your message>` — shows which model would handle it.",
                parse_mode="Markdown"
            )
            return
        from agent.model_router import ModelRouter, classify_task, TASK_TO_CAPABILITY
        router = ModelRouter()
        task_type, confidence = classify_task(text)
        decision = router.route(text)
        spec = decision.model_spec
        fallbacks = " → ".join(decision.fallback_chain[:3]) or "none"
        await update.message.reply_text(
            f"🧭 *Routing Decision*\n\n"
            f"Task type: `{task_type.value}` (conf: {confidence:.0%})\n"
            f"Selected: *{spec.display_name}*\n"
            f"Provider: {spec.provider}\n"
            f"Reason: {decision.reason}\n"
            f"Fallbacks: `{fallbacks}`",
            parse_mode="Markdown"
        )

    # ── Message Handler ───────────────────────────────────────────────────────

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("⛔ Unauthorized.")
            return

        text = update.message.text
        session_id = f"tg:{user.id}"

        await hooks.emit(Event(EventType.MESSAGE_RECEIVED, {
            "platform": "telegram", "user_id": user.id,
            "session_id": session_id, "text": text[:200]
        }))

        # Typing indicator
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

        try:
            response = await self.agent_handler(user.id, session_id, text)
            await update.message.reply_text(response[:4096])
            await hooks.emit(Event(EventType.MESSAGE_SENT, {
                "platform": "telegram", "session_id": session_id
            }))
        except Exception as e:
            logger.error(f"Agent error for user {user.id}: {e}")
            await update.message.reply_text(
                "⚠️ An error occurred. Please try again."
            )

    async def handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(f"Selected: {query.data}")

    async def handle_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Telegram error: {ctx.error}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_polling(self):
        logger.info("Telegram bot starting...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started.")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def send_message(self, chat_id: int, text: str):
        """Send proactive message (used by scheduler jobs etc.)."""
        await self.app.bot.send_message(chat_id=chat_id, text=text[:4096])

    async def broadcast(self, text: str):
        """Broadcast to all allowed users."""
        for uid in CONFIG.TELEGRAM_ALLOWED_USERS:
            try:
                await self.send_message(uid, text)
            except Exception as e:
                logger.warning(f"Broadcast failed for {uid}: {e}")
