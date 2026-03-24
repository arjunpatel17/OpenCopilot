from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from enum import Enum


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class MessageContentType(str, Enum):
    text = "text"
    code = "code"
    file = "file"
    command_output = "command_output"
    error = "error"


class MessageContent(BaseModel):
    type: MessageContentType = MessageContentType.text
    content: str = ""
    language: Optional[str] = None
    filename: Optional[str] = None
    blob_path: Optional[str] = None


class ChatMessage(BaseModel):
    role: MessageRole
    contents: list[MessageContent]
    timestamp: datetime
    agent_name: Optional[str] = None


class ChatSession(BaseModel):
    id: str
    title: str = "New Chat"
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessage] = []


class ChatSessionSummary(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ChatRequest(BaseModel):
    message: str
    agent_name: Optional[str] = None
    model_name: Optional[str] = None
    session_id: Optional[str] = None


class StreamChunk(BaseModel):
    type: str  # "text", "code_start", "code_end", "file", "done", "error"
    content: str = ""
    language: Optional[str] = None
    filename: Optional[str] = None
