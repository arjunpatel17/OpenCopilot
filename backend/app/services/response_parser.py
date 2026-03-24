import re
from app.models.chat import MessageContent, MessageContentType


CODE_BLOCK_RE = re.compile(
    r"```(\w*)\n(.*?)```",
    re.DOTALL,
)

FILE_PATH_RE = re.compile(
    r"(?:Created|Wrote|Saved|Generated)\s+(?:file\s+)?[`'\"]?([^\s`'\"]+\.\w{1,10})[`'\"]?",
    re.IGNORECASE,
)


def parse_copilot_output(raw_output: str) -> list[MessageContent]:
    """Parse raw Copilot CLI output into structured message content blocks."""
    contents: list[MessageContent] = []
    last_end = 0

    for m in CODE_BLOCK_RE.finditer(raw_output):
        # Text before the code block
        text_before = raw_output[last_end : m.start()].strip()
        if text_before:
            contents.append(MessageContent(
                type=MessageContentType.text,
                content=text_before,
            ))
        # The code block itself
        language = m.group(1) or None
        code = m.group(2).strip()
        contents.append(MessageContent(
            type=MessageContentType.code,
            content=code,
            language=language,
        ))
        last_end = m.end()

    # Remaining text after last code block
    remaining = raw_output[last_end:].strip()
    if remaining:
        contents.append(MessageContent(
            type=MessageContentType.text,
            content=remaining,
        ))

    # If no code blocks found, return the whole thing as text
    if not contents:
        contents.append(MessageContent(
            type=MessageContentType.text,
            content=raw_output.strip(),
        ))

    return contents


def detect_created_files(raw_output: str) -> list[str]:
    """Detect file paths mentioned as created in Copilot output."""
    return FILE_PATH_RE.findall(raw_output)
