# 4. My day and tasks

Goal: ask your assistant what needs you today, look at the tasks that WA MCP tracks, and teach it your own
words and projects. This takes about fifteen minutes.

Everything here runs on your machine with plain rules. No language model is involved in detecting tasks.
The assistant only phrases the result.

## Prerequisites

- A client is connected and `bridge_status` works, see [Connect clients](03-connect-clients.md).
- Some history in the store. After linking, the phone sends recent history on its own. Let the bridge run
  for a while.

## Steps

### 1. Check that there is history to work with

Ask: "List my five most recent chats, with the date of the last message." You should see recent chats with
dates. If a chat has less history than you expect, ask the assistant to page back to a date, for example:
"Sync the history of the Book club chat back to 2026-11-01." The assistant calls `sync_chat_history`. The
phone must be online, and the call stops after its time budget, so repeat it if it says `complete=false`.

### 2. Ask what is on your plate

Ask: "What are my tasks today?" The assistant calls `my_day`. A typical answer:

```text
You:        What are my tasks today?

Assistant:  (calls my_day for 2026-11-23)

            Four things need you today.

            Alice asked for the updated invoice yesterday at 16:40 and wants it by the end of the
            day. It is still open.

            On Sunday you told Bob "I will send you the photos tomorrow". That promise is due today.

            The Book club group asked "Which day works for the next meeting?" about 95 minutes ago.
            Nobody has answered yet.

            A reminder for the Book club is scheduled to go out at 18:00.
```

`my_day` returns five kinds of items, most urgent first: open requests to you, your own promises that are
due or overdue, chats where the other side wrote last, missed calls that you did not return, and messages
scheduled for that day. Every item has the chat, the message id and a time, so you can ask for more:
"Show me the messages around Alice's invoice request."

You can narrow it: "What are my tasks today in the Book club chat?" or "What were my tasks on 2026-11-20?".

### 3. Look at the tracked tasks

The task tracker keeps requests with a status: `new`, `in_progress`, `waiting`, `done`, `cancelled` and
`stale`. It rebuilds itself from your chats when messages change. Useful questions and the tool behind each:

| Ask | Tool |
| --- | --- |
| "Which tasks are open?" | `tasks_overview` |
| "Show the steps of the invoice task." | `task_detail` |
| "Which task does this message belong to?" | `which_task` |
| "What moved in the last 24 hours?" | `whats_new` |
| "Rebuild the tracker from the last 30 days." | `refresh_tasks` |
| "Find requests in the Book club chat without changing anything." | `detect_tasks` |

You should see tasks with who asked, the project, a next step and a due hint, and for one task the list of
its steps with the reason each message was attached.

### 4. Teach it your words

The built-in word lists are English. Add your own under `tasks.keywords` in `config.json`. Your words extend
the lists and do not replace them. A `*` at the end of a word means any ending, so `rush*` matches `rush`,
`rushed` and `rushing`.

```json
{
  "tasks": {
    "keywords": {
      "urgent": ["asap", "rush*"],
      "deliverable": ["send the draft", "updated invoice"]
    }
  }
}
```

The lists are `request`, `urgent`, `want`, `deliverable`, `problem`, `follow_up`, `cancel`, `not_cancel`,
`chat`, `spec`, `accept`, `done`, `thanks`, `progress` and `waiting`. Words for dates and the style
analysis go under `insight.dates` and `insight.words`. Restart the client so that the server reads the file
again, then ask for `refresh_tasks`. You should see the new words change the scores and the tasks that are
found.

### 5. Group tasks by project

Projects tell the tracker which project a request belongs to. Longer keywords weigh more, `weak` keywords
weigh less, and `chats` names group chats that belong to one project only.

```json
{
  "tasks": {
    "projects": [
      {
        "id": "book-club",
        "name": "Book club",
        "keywords": ["reading list", "next meeting"],
        "chats": ["120363000000000001@g.us"]
      },
      {
        "id": "invoices",
        "name": "Invoices",
        "keywords": ["invoice", "payment"],
        "weak": ["quote"]
      }
    ]
  }
}
```

Restart the client and ask: "Give me the overview of the Invoices project." The assistant calls
`project_overview`. You should see counts by status, the open tasks, the ones closed recently and who asks
most.

### 6. Watch words and phrases

`mentions.watch` lists words that you want to hear about in any chat, including voice transcripts and file
text:

```json
{
  "mentions": {
    "watch": ["invoice", { "label": "Report", "words": ["report", "q3 numbers"] }]
  }
}
```

Ask: "Did anyone mention my watched words this week?" The assistant calls `find_mentions` for the last seven
days. You should see the matched words with the messages around them.

## Troubleshooting

- `my_day` returns nothing. The store may have no recent messages. Check with `list_messages`, ask for older
  history, and ask for `refresh_tasks`. The tracker rebuilds from the last `tasks.refresh_days` days (14).
- Requests in another language are missed. The built-in lists are English. Add your words under
  `tasks.keywords`, `insight.words` and `insight.dates`, see [Configuration](../guides/configuration.md).
- Group messages do not count as yours. A group message counts only when it mentions you, replies to you or
  contains one of your names. Add the names that people use for you to `insight.my_names`.
- A task disappeared from the list. An open task with no activity for `tasks.stale_days` days (60) becomes
  `stale` and is no longer shown by `my_day`. It stays in the tracker until `tasks.keep_days` (365) have
  passed. Raise `tasks.stale_days` if tasks go stale too early.
- A request lands in the wrong project. Add more specific `keywords`, move generic words to `weak`, list
  the chats of the project under `chats`, or mark one catch-all project with `"generic": true`.

## Next steps

- [Voice transcription](05-voice-transcription.md): voice notes then count for tasks and search too.
- [Scheduled and human-like sending](08-scheduled-and-human-like-sending.md): answer from the assistant.
- [Configuration](../guides/configuration.md): every key under `tasks`, `mentions` and `insight`.
- [MCP tools](../reference/tools.md): the arguments of every tool named here.
