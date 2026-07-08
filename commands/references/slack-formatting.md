# Slack Formatting Reference

Reference for any skill that posts to Slack.

## Links

Slack uses mrkdwn, not standard Markdown.

- Generic link: `<https://example.com|display text>`
- Jira: `<https://<your-company>.atlassian.net/browse/<KEY>|<KEY>>`
- GitHub PR: `<https://github.com/<owner>/<repo>/pull/<n>|PR #<n>>`

## Bold & Text

- Use `*bold*` (Slack mrkdwn). Some Slack clients accept `**bold**` too — test in your environment. When posting via the Slack API, `*bold*` is the safe default.
- No em dashes. Use periods, commas, or semicolons.
- Keep titles short — truncate long ones to ~40 chars.

## Indentation

- Slack strips leading spaces, tabs, and non-breaking spaces.
- Use the braille blank character (U+2800: ⠀) for indentation — two per indent level.
- Braille inside `*bold*` renders as visible dots — keep bold headers flush left.

## Icons & Status

Example status-icon legend (customize per use case):

- ✅ approved / ready
- 👀 reviewed / in review
- ⏳ awaiting
- ❌ CI failed
- 🔄 CI running

Legend line: `⠀✅ approved ⠀👀 reviewed ⠀⏳ awaiting`

## Bullets

- Use `•` (bullet) for list items.
- Each primary item on its own line.
- Sub-bullets: braille-blank indent + `◦` or `-`.
