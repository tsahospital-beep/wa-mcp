# Tutorials

Short, step-by-step lessons. Each one has a goal, what you need, numbered steps with commands you can copy,
what you should see after the steps, a few common problems and where to go next. The reference pages in
[Guides](../README.md#guides) describe every setting. These tutorials show how to get something working.

Commands are given for macOS and Linux shells and for Windows PowerShell where they differ. Where a path
reads `/path/to/wa-mcp`, use the folder of your clone. The examples use made-up names and numbers (Alice,
Bob, 15550100001, the Book club group).

## Start here

Do these three in order. After them your assistant can read your WhatsApp.

| Tutorial | What you get |
| --- | --- |
| [1. Install](01-install.md) | The bridge built and the MCP server installed on Windows, macOS or Linux. |
| [2. First login](02-first-login.md) | Your account linked by QR code or phone number, and a check that it is connected. |
| [3. Connect clients](03-connect-clients.md) | Claude Desktop, Claude Code, Cursor, VS Code or Windsurf using the server. |

## Use it

| Tutorial | What you get |
| --- | --- |
| [4. My day and tasks](04-my-day-and-tasks.md) | An answer to "what are my tasks today", the task tools, and your own words and projects. |
| [5. Voice transcription](05-voice-transcription.md) | Voice notes as text, with the backend of your choice and the language rules. |
| [6. Media and screenshots](06-media-and-screenshots.md) | Downloads, text read from screenshots, and search that finds it. |
| [7. Import old exports](07-import-old-exports.md) | History from an "Export chat" file that the phone no longer syncs. |
| [8. Scheduled and human-like sending](08-scheduled-and-human-like-sending.md) | Sending turned on with approval, pacing, limits, quiet hours and scheduled messages. |
| [9. Multiple accounts](09-multiple-accounts.md) | Two or more numbers in one MCP server. |

## Run it on a server and build on it

| Tutorial | What you get |
| --- | --- |
| [10. Server with Docker](10-server-with-docker.md) | The bridge and the server on a Linux host with HTTPS. CI builds both images on pushes to `main`. A real Docker build is still pending. |
| [11. Connect remotely](11-connect-remotely.md) | Tokens, OAuth and a claude.ai custom connector. |
| [12. Webhooks and the Python SDK](12-webhooks-and-python-sdk.md) | Signed webhooks, the SDK and the HTTP API from your own code. |
| [13. Security hardening](13-security-hardening.md) | A checklist of the protections to verify and the ones that need your action. |
| [14. Agent connector](14-agent-connector.md) | Connect a reply API, test it offline and review a draft. |

## Before you start

- Sending is off by default. Read tutorial 8 before you turn it on.
- WA MCP links to your account as a companion device, the same way WhatsApp Web does. It is not made or
  endorsed by WhatsApp or Meta. Automated use of a WhatsApp account may go against the WhatsApp terms of
  service. Use a test account while you learn.
- Looking for the checks that maintainers run with a real account? See
  [End-to-end check with a real account](../testing/e2e.md).
