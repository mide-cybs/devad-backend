from dataclasses import dataclass, field
from typing import Optional, List
from uuid import UUID, uuid4

@dataclass
class Organization:
    id: UUID = field(default_factory=uuid4)
    name: str = "My Company"
    agent_name: str = "DevBot"
    agent_tone: str = "friendly"
    confidence_threshold: int = 80
    escalation_topics: List[str] = field(default_factory=lambda: ["billing","security","legal"])

@dataclass
class Question:
    id: UUID = field(default_factory=uuid4)
    org_id: Optional[UUID] = None
    platform: str = ""
    channel: Optional[str] = None
    author_username: str = ""
    author_external_id: str = ""
    content: str = ""
    external_id: Optional[str] = None
    status: str = "pending"

@dataclass
class AgentResponse:
    id: UUID = field(default_factory=uuid4)
    question_id: Optional[UUID] = None
    org_id: Optional[UUID] = None
    content: str = ""
    confidence: float = 0.0
    status: str = "pending_review"

@dataclass
class KnowledgeSource:
    id: UUID = field(default_factory=uuid4)
    label: str = ""
    url: str = ""
    status: str = "pending"
    chunks: int = 0

@dataclass
class DigestReport:
    id: UUID = field(default_factory=uuid4)
    full_report: str = ""

@dataclass
class FrictionAlert:
    id: UUID = field(default_factory=uuid4)
    trigger_topic: str = ""
    status: str = "open"
