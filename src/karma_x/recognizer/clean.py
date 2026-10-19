"""Step 1: strip text that must never be read as recognition."""

import re

FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
URL = re.compile(r"<https?://[^>]*>|https?://\S+", re.IGNORECASE)
BROADCAST = re.compile(r"<!(?:here|channel|everyone)(?:\|[^>]*)?>|<!subteam\^[^>]*>")
RAW_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")
HAS_MENTION = re.compile(r"<@[UW][A-Z0-9]+")


def clean(text: str) -> str:
    text = FENCE.sub(" ", text)
    text = INLINE_CODE.sub(" ", text)
    text = "\n".join(
        line for line in text.split("\n") if not line.lstrip().startswith((">", "&gt;"))
    )
    text = URL.sub(" ", text)
    text = BROADCAST.sub(" ", text)
    return RAW_MENTION.sub(r"<@\1>", text)
