"""Wire types for MCP structured results. Optional keys stay absent when omitted by a handler."""

from typing import Annotated, Any, Generic, Literal, NotRequired, TypeVar

from pydantic import ConfigDict, Field, RootModel, with_config
from typing_extensions import TypedDict

Object = dict[str, Any]
T = TypeVar("T")


@with_config(ConfigDict(extra="allow"))
class Record(TypedDict):
    """Preserve extension fields supplied by the bridge or a derived-text backend."""


class Success(Record):
    ok: Literal[True]
    note: NotRequired[str]


class Candidate(Record):
    jid: str
    title: NotRequired[str]
    name: NotRequired[str]


class Failure(Record):
    ok: Literal[False]
    error: str
    demo: NotRequired[Literal[True]]
    candidates: NotRequired[list[Candidate] | None]
    senders: NotRequired[list[str]]
    partial: NotRequired[Literal[True]]
    ids: NotRequired[list[str]]


class BridgeState(Record):
    reachable: NotRequired[bool]
    connected: NotRequired[bool | None]
    logged_in: NotRequired[bool | None]
    account: NotRequired[str | None]
    version: NotRequired[str | None]
    error: NotRequired[str]


class SecurityEvent(Record):
    at: int
    kind: str
    level: str
    detail: str


class Protection(Record, total=False):
    field_encryption: str
    key_store: str
    api_token: str
    efs: str
    compaction: str
    plain_copy: str


class BridgeStatus(Success):
    database: bool
    sending_enabled: bool
    encryption: bool
    encryption_mode: str
    bridge: BridgeState
    tool_modules_failed: dict[str, str]
    demo: NotRequired[bool]
    counts: NotRequired[dict[str, int]]
    require_confirm: NotRequired[bool]
    token_found: NotRequired[bool]
    transcription_backend: NotRequired[str]
    transcription_ready: NotRequired[bool]
    transcription_hint: NotRequired[str]
    ai_provider: NotRequired[str]
    ocr_engine: NotRequired[str]
    ocr_ready: NotRequired[bool]
    ocr_hint: NotRequired[str]
    app_state: NotRequired[dict[str, str]]
    protection: NotRequired[Protection]
    security_alert: NotRequired[bool]
    security_events: NotRequired[list[SecurityEvent]]


class Account(Record):
    name: str
    default: bool
    number: str
    store: str
    database: bool
    sending_enabled: bool
    linked: bool
    connected: bool
    bridge_running: bool
    bridge_error: NotRequired[str]
    demo: NotRequired[bool]


class Accounts(Success):
    count: int
    default: str
    accounts: list[Account]


class Contact(Record):
    name: str
    name_source: str
    phone: str | None
    jids: list[str]


class Contacts(Success):
    contacts: list[Contact]
    count: int


class Chat(Record):
    jid: str
    jids: list[str]
    name: str
    name_source: str
    is_group: bool


class RecentChat(Chat):
    last_activity: str | None


class ChatInfo(Chat):
    phone: str | None


class Chats(Success):
    chats: list[RecentChat]
    offset: int
    next_offset: int


class Reply(Record):
    id: str
    author: str | None
    preview: str | None


class Reactions(Record):
    counts: dict[str, int]
    by: list[dict[str, str]]


class Message(Record):
    id: str
    chat: str
    time: str | None
    from_name: Annotated[str, Field(alias="from")]
    kind: str
    text: str | None
    from_jid: NotRequired[str]
    imported: NotRequired[bool]
    deleted: NotRequired[bool]
    edited: NotRequired[bool]
    file_name: NotRequired[str]
    local_path: NotRequired[str]
    reply_to: NotRequired[Reply]
    mentioned: NotRequired[list[str]]
    reactions: NotRequired[Reactions]
    transcript: NotRequired[str]
    transcript_lang: NotRequired[str]
    transcript_lang_from_history: NotRequired[bool]
    document_text: NotRequired[str]
    archive_text: NotRequired[str]
    image_text: NotRequired[str]
    image_description: NotRequired[str]
    link_text: NotRequired[str]


class Missing(Success):
    found: Literal[False]


class ChatFound(Success):
    found: Literal[True]
    chat: ChatInfo
    recent_messages: list[Message]


class Interaction(Success):
    chat: str
    name: str
    last: Message | None
    last_from_them: Message | None
    last_from_me: Message | None


class Messages(Success):
    messages: list[Message]
    count: int
    next_offset: int


class Context(Success):
    message: Message
    before: list[Message]
    after: list[Message]


class Receipts(Success):
    marked: int
    chat: str


class ChatExport(Success):
    path: str
    messages: int


class PeriodExport(ChatExport):
    chats: int


class DayItem(Record):
    type: str
    chat: str
    message_id: str | None
    time: str | None
    context: str
    account: NotRequired[str]
    call_id: NotRequired[str]
    missed: NotRequired[int]
    video: NotRequired[bool]
    scheduled_id: NotRequired[int]
    recurrence: NotRequired[str]
    task_id: NotRequired[str]
    status: NotRequired[str]
    pings: NotRequired[int]
    next_step: NotRequired[str]
    due: NotRequired[str | None]
    due_status: NotRequired[str | None]
    waiting_minutes: NotRequired[int]
    question: NotRequired[bool]
    unanswered: NotRequired[int]


class DayGroup(Record):
    chat: str
    name: str
    is_group: bool
    items: list[DayItem]
    account: NotRequired[str]


class Day(Success):
    date: str
    as_of: str
    summary: str
    counts: dict[str, int]
    people: list[DayGroup]
    accounts: NotRequired[list[str]]
    skipped: NotRequired[list[dict[str, str]]]
    ai_provider: NotRequired[str | None]
    ai_summary: NotRequired[str | None]
    ai_note: NotRequired[str]
    ai_error: NotRequired[str]


class AnalyticsMeta(Record):
    chat: str
    name: str
    is_group: bool
    jids: int
    messages: int
    period: list[str | None]
    after: str | None
    before: str | None
    sections: list[str]
    version: int
    timing_ms: dict[str, int]
    cache: str
    detail: str
    report_path: NotRequired[str]


class MetricSection(Record):
    n: int
    low_data: bool
    data: Object
    examples: NotRequired[list[Object]]


class Analytics(Success):
    summary: str
    meta: AnalyticsMeta
    sections: dict[str, MetricSection]


class ComparisonRow(Record):
    chat: str
    error: NotRequired[str]
    jid: NotRequired[str]
    name: NotRequired[str]
    is_group: NotRequired[bool]
    messages: NotRequired[int]
    my_share: NotRequired[float | None]
    reply_median_min: NotRequired[float | None]
    their_reply_median_min: NotRequired[float | None]
    deferred_share: NotRequired[float | None]
    words_median: NotRequired[float | None]
    emoji_per_100: NotRequired[float | None]
    laugh_per_100: NotRequired[float | None]
    stickers_per_100: NotRequired[float | None]
    voice_share: NotRequired[float | None]
    formal_share: NotRequired[float | None]
    main_script: NotRequired[str | None]
    cache: NotRequired[str]


class Difference(Record):
    chat: str
    metric: str
    value: str | float
    median: NotRequired[float]
    x: NotRequired[float | None]


class Comparison(Success):
    rows: list[ComparisonRow]
    differences: list[Difference]


class ProjectMatch(Record):
    id: str | None
    name: str | None
    confidence: int
    why: str


class DetectionReply(Record):
    id: str
    preview: str | None


class Detection(Record):
    time: str | None
    chat: str
    from_name: Annotated[str, Field(alias="from")]
    from_jid: str
    category: str
    score: int
    reasons: list[str]
    text: str
    ids: list[str]
    to_me: list[str]
    project: NotRequired[ProjectMatch]
    reply_to: NotRequired[DetectionReply]
    media: NotRequired[list[str]]


class Detections(Success):
    chat: str
    name: str
    is_group: bool
    counts: dict[str, int]
    detections: list[Detection]
    total: int
    next_offset: int


class Task(Record):
    id: str
    title: str
    status: str
    status_why: str
    category: str
    score: int
    chat: str
    chat_name: str
    from_name: Annotated[str, Field(alias="from")]
    from_jid: str
    project: dict[str, str] | None
    next_step: str
    due: str | None
    created: str | None
    updated: str | None
    pings: int
    first_message: dict[str, str]


class Tasks(Success):
    count: int
    by_status: dict[str, int]
    tasks: list[Task]
    next_offset: int
    refreshed_at: str | None


class Step(Record):
    time: str | None
    kind: str
    why: str


class TaskStep(Step):
    chat: str
    message_id: str
    who: str
    text: str


class TaskFound(Success):
    found: Literal[True]
    task: Task


class TaskDetail(TaskFound):
    steps: list[TaskStep]


class MessageTask(TaskFound):
    step: Step


class Refresh(Success):
    chats: int
    tasks: int
    open: int
    since: str | None
    refreshed_at: str | None
    partial: NotRequired[bool]
    chats_done: NotRequired[int]


class ConfiguredProject(Record):
    id: str
    name: str
    keywords: list[str]


class ProjectQuery(Record):
    query: str


class Project(Success):
    project: ConfiguredProject | ProjectQuery
    configured: bool
    tasks: int
    by_status: dict[str, int]
    open: list[Task]
    recent_closed: list[Task]
    people: list[tuple[str, int]]
    last_activity: str | None


class RecentStep(Step):
    task_id: str
    task: str
    status: str
    chat_name: str
    who: str
    text: str
    message_id: str


class Changes(Success):
    since: str | None
    new_tasks: int
    steps: list[RecentStep]
    count: int
    as_of: str


class Mention(Record):
    time: str | None
    chat: str
    chat_name: str
    from_name: Annotated[str, Field(alias="from")]
    message_id: str
    watch: list[str]
    matched: list[str]
    text: str
    context: dict[str, str | None]


class Mentions(Success):
    watch: list[str]
    count: int
    has_more: bool
    mentions: list[Mention]
    by_watch: NotRequired[dict[str, int]]
    next_offset: NotRequired[int]


class HistoryRequest(Success):
    requested: int
    anchor_id: str
    oldest_known: int


class SyncPage(Record):
    chat: str
    pages: int
    oldest: int | None
    oldest_time: str | None
    reached: bool
    stopped: str
    empty_pages: int
    exhausted: bool
    state: Literal["reached", "pending", "exhausted"]


class ChatSync(Success, SyncPage):
    pass


class HistorySync(Success):
    chats: list[SyncPage]
    skipped_done: int
    total_active: int
    remaining: int
    complete: bool
    stopped: NotRequired[str]
    resume: NotRequired[str]


class Attachment(Record):
    msg_id: str
    kind: str
    path: str


class Download(Success, Attachment):
    chat: str


class FileFailure(Record):
    msg_id: str
    error: str
    kind: NotRequired[str]
    url: NotRequired[str]


class Downloads(Success):
    saved: list[Attachment]
    failed: list[FileFailure]
    already_local: int
    matched: int


class DerivedText(Record):
    source: str
    lang: str | None
    confidence: float | None
    text: str


class MediaText(Success):
    message_id: str
    kind: str
    items: list[DerivedText]
    hint: NotRequired[str]


class Stickers(Success):
    stickers: list[Message]
    next_offset: int


class Extracted(Success):
    message_id: str
    path: str
    text: str
    source: str
    kind: str
    truncated: bool


class Document(Record):
    msg_id: str
    url: str
    kind: str
    chars: int


class Documents(Success):
    fetched: list[Document]
    failed: list[FileFailure]


class Link(Record):
    msg_id: str
    url: str
    ok: bool
    status: NotRequired[int]
    final_url: NotRequired[str]
    error: NotRequired[str]


class Links(Success):
    links: list[Link]
    count: int


class Transcript(Record):
    msg_id: str
    text: str
    lang: str | None
    cached: bool
    time: NotRequired[str | None]


class MediaBatch(Success):
    already_done: int
    failed: list[FileFailure]
    candidates: int
    remaining: int
    complete: bool
    demo: NotRequired[bool]
    cached: NotRequired[list[Transcript]]
    stopped: NotRequired[str]
    resume: NotRequired[str]


class Transcription(MediaBatch):
    transcribed: list[Transcript]


class ImageRead(Record):
    time: NotRequired[str | None]
    msg_id: str
    cached: bool
    text: NotRequired[str]
    no_text: NotRequired[bool]
    description: NotRequired[str]


class Images(MediaBatch):
    read: list[ImageRead]
    notes: NotRequired[list[str]]


class Sent(Success):
    id: str | None
    ids: list[str] | None
    sent_at: int | None
    to: str
    to_name: str


class Queued(Success):
    queued: Literal[True]
    scheduled_id: int | None
    to: str
    to_name: str


class ActionSent(Success):
    id: str
    sent_at: int
    waited_ms: NotRequired[int]
    ids: NotRequired[list[str]]
    resumed: NotRequired[bool]
    http_status: int


class ActionQueued(Success):
    queued: Literal[True]
    scheduled_id: int
    http_status: int


class Scheduled(Success):
    id: int | None
    next_run: str | None
    to: str
    to_name: str


class QueueEntry(Record):
    id: int
    to: str
    to_name: str
    text: str | None
    media_path: str | None
    next_run: str | None
    recurrence: str
    status: str
    last_error: str | None
    action: NotRequired[str]
    source_chat: NotRequired[str]
    message_id: NotRequired[str]


class Queue(Success):
    scheduled: list[QueueEntry]
    total: int
    next_offset: int


class Cancelled(Success):
    cancelled: bool
    id: int


class Call(Record):
    id: str
    with_name: Annotated[str | None, Field(alias="with")]
    peer: str
    group: str | None
    time: str | None
    direction: str
    video: bool
    outcome: str
    duration_seconds: int | None


class Calls(Success):
    calls: list[Call]
    next_offset: int


class Sender(Record):
    name: str
    messages: int
    author: str
    outgoing: bool


class SkippedLines(Record):
    count: int
    first: list[int] | None


class ImportReport(Success):
    dry_run: bool
    chat: str
    messages_added: int
    duplicates: int
    media_added: int
    chat_title: NotRequired[str]
    new_chat: NotRequired[bool]
    already_imported: NotRequired[int]
    already_synced: NotRequired[int]
    media_missing: NotRequired[int]
    media_omitted: NotRequired[int]
    media_skipped: NotRequired[int]
    system_lines: NotRequired[int]
    skipped_lines: NotRequired[SkippedLines]
    date_order: NotRequired[str]
    date_order_source: NotRequired[str]
    timezone: NotRequired[str]
    first: NotRequired[str]
    last: NotRequired[str]
    me: NotRequired[str]
    senders: NotRequired[list[Sender] | None]
    warnings: NotRequired[list[str]]


class AgentStatus(Success):
    last_poll_at: int | None
    pending_drafts: int
    enabled: bool
    paused: bool
    mode: str
    provider: str
    voice_provider: str
    dry_run: bool


class Draft(Record):
    id: str
    message_id: str
    chat: str
    kind: str
    sender: str
    text: str
    voice_note: bool


class Drafts(Success):
    drafts: list[Draft]


class Decision(Record):
    ok: bool
    status: str
    reason: str
    chat: str
    message_id: str
    draft_id: str


class AgentSwitch(Success):
    paused: bool


class ProviderTest(Success):
    provider: str
    preview: str
    skip: bool
    provider_ms: int
    total_ms: int
    voice_file: str | None


class Output(RootModel[T | Failure], Generic[T]):
    """MCP requires an object at the root. Preserve the same envelope for text clients."""

    model_config = ConfigDict(json_schema_extra={"type": "object"})
