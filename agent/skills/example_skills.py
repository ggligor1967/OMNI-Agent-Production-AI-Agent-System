"""
OMNI AGENT - Example Skills
Drop any .py file here. The SkillsManager will auto-load it.
Each skill must call skills_manager.register(...) via decorator.
"""

# skills_manager is injected by SkillsManager.load_from_directory()

@skills_manager.register(
    name="summarize",
    description="Summarize a block of text",
    triggers=["summarize", "tldr", "brief me", "sum up"],
)
async def summarize(text: str, session_id: str = "") -> str:
    """Extract first sentences as a quick summary."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    snippet = " ".join(sentences[:3])
    return f"📝 Summary: {snippet}"


@skills_manager.register(
    name="word_count",
    description="Count words in a message",
    triggers=["word count", "how many words", "count words"],
)
def word_count(text: str, session_id: str = "") -> str:
    count = len(text.split())
    return f"📊 Word count: {count}"


@skills_manager.register(
    name="reverse_text",
    description="Reverse any text",
    triggers=["reverse", "backwards"],
)
def reverse_text(text: str, session_id: str = "") -> str:
    clean = text.lower().replace("reverse", "").replace("backwards", "").strip()
    return f"🔁 {clean[::-1]}"


@skills_manager.register(
    name="translate_mock",
    description="Mock translation placeholder (connect to real API)",
    triggers=["translate to", "in spanish", "in french", "in german"],
)
async def translate_mock(text: str, session_id: str = "") -> str:
    return (
        "🌐 Translation feature requires a real translation API.\n"
        "Connect: LibreTranslate, DeepL, or Google Translate in this handler."
    )
