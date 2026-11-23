"""Tools: chat analytics, task detection and tracking, mentions, and the daily overview (get_my_day)."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import db
from wa_mcp.ai import summaries
from wa_mcp.insight import analytics, mentions, tasks, tracker
from wa_mcp.insight import my_day as day_plan
from wa_mcp_server import outputs
from wa_mcp_server.common import (
    UNTRUSTED,
    Result,
    cfg,
    chat_jids,
    clamp,
    fail,
    hints,
    ok,
    structured,
)
from wa_mcp_server.tools.inputs import After, Before, Chat, ChatFilter, IncludeMe, Offset, PageSize


def _untrusted(res: dict[str, Any]) -> Result:
    """ok() with the untrusted-data note, keeping a note the result already has."""
    extra = res.pop("note", None)
    return ok(**res, note=f"{extra}. {UNTRUSTED}" if extra else UNTRUSTED)


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Get the daily WhatsApp briefing", destructive=True, open_world=True))
    @structured(outputs.Output[outputs.Day])
    def get_my_day(
        date: Annotated[
            str | None,
            Field(description="Local calendar date in YYYY-MM-DD form, such as 2026-11-23. Omit for today."),
        ] = None,
        chat: ChatFilter = None,
        refresh: Annotated[
            bool,
            Field(
                description="Refresh the local task tracker if stale before collecting the briefing. False uses stored task state."
            ),
        ] = True,
        summary: Annotated[
            bool,
            Field(
                description="Also request ai_summary from the configured AI provider using names, due hints and clipped context. Remote providers may charge. Default uses local rules."
            ),
        ] = False,
        account: Annotated[
            str | None,
            Field(
                description="Account name from list_accounts, such as work. Omit to merge all configured accounts, with account labels on multi-account results. Unknown account names fail. Accounts missing the chat or archive are skipped if another matches."
            ),
        ] = None,
    ) -> Result:
        """Find what needs your attention on a given day across configured accounts.

        Use list_tasks for tracked work across dates. Omit account to merge all accounts. Unknown accounts fail. Works offline and in demo. Empty days show no priorities. Refresh can prune expired tasks. Optional summaries may contact a paid provider.
        """
        if len(cfg().account_names) > 1:
            res = day_plan.my_day_accounts(
                date, chat=chat, refresh=refresh, accounts=[account] if account else None
            )
        else:
            with db.use_account(account):
                res = day_plan.my_day(date, chat=chat, refresh=refresh)
        if summary:
            res.update(summaries.day_summary(res))
        return _untrusted(res)

    @mcp.tool(annotations=hints("Analyze one conversation", destructive=True, idempotent=True))
    @structured(outputs.Output[outputs.Analytics])
    def get_chat_analytics(
        chat: Chat,
        after: After = None,
        before: Before = None,
        sections: Annotated[
            list[str] | None,
            Field(
                description='Sections to return, such as ["volume","reply_speed"]. Choices are volume, reply_speed, length, emoji, stickers, reactions, media, voice, quotes, deferrals, formality, script, topics_tasks, phrases, group_roles and capture_gaps. Omit for all applicable sections. Results always include capture_gaps.'
            ),
        ] = None,
        detail: Annotated[
            str, Field(description="Level of section detail: brief, normal or full.")
        ] = "normal",
        refresh: Annotated[
            bool, Field(description="Recompute the metrics even when a cached result exists.")
        ] = False,
        report: Annotated[
            bool,
            Field(description="Also write a Markdown report in exports_dir and include meta.report_path."),
        ] = False,
    ) -> Result:
        """Measure activity, reply times and language in one stored conversation.

        Use compare_chat_analytics for several chats. Runs offline and in demo. Unknown names fail and sparse histories flag limited data. Caches metrics and can replace an exported report.
        """
        res = analytics.analyze(
            chat, after=after, before=before, names=sections, detail=detail, refresh=refresh, report=report
        )
        return _untrusted(res)

    @mcp.tool(annotations=hints("Compare conversation metrics", idempotent=True))
    @structured(outputs.Output[outputs.Comparison])
    def compare_chat_analytics(
        chats: Annotated[
            list[str],
            Field(
                description='Array of 2 through 8 chat JIDs, international numbers or names, such as ["+15550100001","Project team"].'
            ),
        ],
        after: After = None,
        before: Before = None,
    ) -> Result:
        """Compare activity and reply patterns across two to eight stored chats.

        Use get_chat_analytics for a single report. Works offline and in demo. Unresolved names give per-chat errors. Computing missing metrics populates the local cache.
        """
        res = analytics.compare(chats, after=after, before=before)
        return _untrusted(res)

    @mcp.tool(annotations=hints("Detect requests in one chat", read_only=True))
    @structured(outputs.Output[outputs.Detections])
    def detect_tasks(
        chat: Chat,
        after: After = None,
        before: Before = None,
        min_score: Annotated[
            int,
            Field(
                description="Minimum detection score on a 0 through 100 scale. Use 0 to include every classification."
            ),
        ] = 0,
        gap_minutes: Annotated[
            float | None,
            Field(
                description="Positive maximum gap in minutes between messages in one sender's burst. Omit to use tasks.burst_gap_minutes (default 4)."
            ),
        ] = None,
        include_me: IncludeMe = False,
        limit: PageSize = 50,
        offset: Annotated[
            int,
            Field(
                description="How many detections to skip from the newest end, starting at 0. Use next_offset for the next older page."
            ),
        ] = 0,
    ) -> Result:
        """Score messages in one chat for possible requests without updating the tracker.

        Use list_tasks for tracked status. Works offline and in demo. An unknown name fails and an empty history gives no detections.
        """
        res = tasks.detect_tasks(
            chat,
            after=after,
            before=before,
            min_score=min_score,
            gap_minutes=gap_minutes,
            include_me=include_me,
        )
        dets = res.pop("detections")
        n = clamp(limit)
        end = max(len(dets) - max(offset, 0), 0)
        page = dets[max(end - n, 0) : end]
        return ok(**res, total=len(dets), detections=page, next_offset=offset + len(page), note=UNTRUSTED)

    @mcp.tool(annotations=hints("List tracked tasks", destructive=True))
    @structured(outputs.Output[outputs.Tasks])
    def list_tasks(
        status: Annotated[
            str | None,
            Field(
                description="Exact state: new, in_progress, waiting, done, cancelled or stale. Overrides open_only."
            ),
        ] = None,
        project: Annotated[
            str | None,
            Field(
                description="Project id or case-insensitive project name fragment, such as billing. Omit for all projects."
            ),
        ] = None,
        chat: ChatFilter = None,
        query: Annotated[
            str | None,
            Field(
                description="Case-insensitive substring of task title, chat name or requester, such as invoice. Omit for all matches."
            ),
        ] = None,
        open_only: Annotated[
            bool,
            Field(
                description="Show new, in_progress and waiting tasks. False includes closed tasks too. An explicit status takes precedence."
            ),
        ] = True,
        limit: PageSize = 50,
        offset: Offset = 0,
    ) -> Result:
        """List tracked tasks filtered by state, project, chat or search text.

        Use detect_tasks to analyze raw messages in one chat. Runs offline and in demo. A stale index refreshes and can prune expired entries. No matches gives an empty page.
        """
        tracker.ensure_fresh()
        convs = chat_jids(chat) if chat else None
        res = tracker.overview(
            status=status, project=project, chat=convs, query=query,
            open_only=open_only, limit=clamp(limit), offset=max(offset, 0),
        )  # fmt: skip
        return _untrusted(res)

    @mcp.tool(annotations=hints("Get a tracked task", read_only=True))
    @structured(outputs.Output[outputs.TaskDetail | outputs.Missing])
    def get_task(task_id: Annotated[str, Field(description="Tracked task id from list_tasks.")]) -> Result:
        """Read a tracked task and its chronological steps by task id.

        Use list_tasks to search by title. Reads the existing index offline and in demo. An unknown id gives found=false.
        """
        return _untrusted(tracker.detail(task_id))

    @mcp.tool(annotations=hints("Find the task for a message", read_only=True))
    @structured(outputs.Output[outputs.MessageTask | outputs.Missing])
    def find_task_for_message(
        message_id: Annotated[
            str,
            Field(
                description="Opaque id from list_messages, such as ABC123. Supply chat as well to disambiguate."
            ),
        ],
        chat: ChatFilter = None,
    ) -> Result:
        """Identify the tracked task that contains a particular message.

        Use get_task for all steps once you have the task id. Reads the existing index offline and in demo. An unindexed message gives found=false.
        """
        res = tracker.which(message_id, chat_jids(chat) if chat else None)
        return _untrusted(res)

    @mcp.tool(annotations=hints("Rebuild the task tracker", destructive=True))
    @structured(outputs.Output[outputs.Refresh])
    def refresh_tasks(
        days: Annotated[
            int,
            Field(
                description="Lookback in whole days, 1 through 3650. For example, 30 scans the past month."
            ),
        ] = 14,
    ) -> Result:
        """Rebuild the local task index from stored conversations over a chosen lookback.

        Use list_tasks for routine browsing. Runs offline and in demo. An empty archive adds no tasks. Retention rules can remove old tasks and steps.
        """
        res = tracker.refresh(days)
        return ok(**res)

    @mcp.tool(annotations=hints("Summarize one tracked project", destructive=True))
    @structured(outputs.Output[outputs.Project])
    def get_project_overview(
        project: Annotated[
            str, Field(description="Project id or name from tasks.projects, such as billing.")
        ],
    ) -> Result:
        """Summarize tracked work and requesters for one project.

        Use list_tasks to page through individual tasks. Works offline and in demo. Unconfigured projects are flagged. Refreshing a stale index can prune expired work.
        """
        tracker.ensure_fresh()
        return _untrusted(tracker.project_overview(project))

    @mcp.tool(annotations=hints("List recent task activity", destructive=True))
    @structured(outputs.Output[outputs.Changes])
    def list_whats_new(
        hours: Annotated[
            float,
            Field(
                description="Positive rolling lookback in hours, up to 87600. Use 0.5 for the last half hour."
            ),
        ] = 24,
        limit: Annotated[
            int, Field(description="Maximum recent task steps, up to 200. Zero or negative uses 100.")
        ] = 100,
    ) -> Result:
        """Find new tasks and task steps within a rolling time window.

        Use get_my_day for outstanding priorities. Runs offline and in demo. Quiet periods give an empty list. Refreshing a stale index can prune expired work.
        """
        tracker.ensure_fresh()
        res = tracker.whats_new(hours, limit=clamp(limit, 100))
        return _untrusted(res)

    @mcp.tool(annotations=hints("Find watched words in messages", read_only=True))
    @structured(outputs.Output[outputs.Mentions])
    def find_mentions(
        days: Annotated[
            float, Field(description="Positive lookback in days, up to 3650. Fractions are allowed.")
        ] = 7,
        words: Annotated[
            list[str] | None,
            Field(
                description='Override mentions.watch with words or phrases, such as ["invoice","quarterly report"]. Omit or pass an empty list to use the configured watch list.'
            ),
        ] = None,
        chat: ChatFilter = None,
        include_me: IncludeMe = False,
        limit: PageSize = 50,
        offset: Offset = 0,
    ) -> Result:
        """Find watched words in recent messages, transcripts and file text.

        Use list_messages for a one-off substring search. Reads locally offline and in demo. An empty watch list or no matches gives no mentions.
        """
        res = mentions.find(
            days, words=words, chat=chat_jids(chat) if chat else None, include_me=include_me,
            limit=clamp(limit), offset=max(offset, 0),
        )  # fmt: skip
        return _untrusted(res)


def register_legacy_detail(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Inspect a tracked task", destructive=True))
    @structured(outputs.Output[outputs.TaskDetail | outputs.Missing])
    def task_detail(
        task_id: Annotated[
            str | None,
            Field(
                description="Use id from list_tasks, such as t01ab23cd45ef. Takes priority when matched. Required when query is omitted."
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Fallback title, requester or chat name substring, such as invoice. Selects the first matching task in tracker order. Required when task_id is omitted."
            ),
        ] = None,
    ) -> Result:
        """Look up a tracked task by id or by the first matching title, requester or chat.

        Use get_task for an exact id and list_tasks for text search. Works offline and in demo. A stale tracker can refresh and prune expired entries. No match gives found=false.
        """
        if not task_id and not query:
            return fail("give task_id or query")
        tracker.ensure_fresh()
        res = tracker.detail(task_id, query)
        return _untrusted(res)
