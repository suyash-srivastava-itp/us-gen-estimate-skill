# Scoring rubric

Read this before writing `ticket.json`. The model is only as good as these inputs, and the most common error is generous scoring of spec clarity.

## 1. spec_clarity (0 to 1)

Score six criteria. Give each one of three values:

- **1**: clearly met, and you can quote the ticket text that shows it
- **0.5**: partly met (present but vague, or only in a comment)
- **0**: absent

`spec_clarity` is the mean of the six scores.

| # | Criterion | Met means |
|---|-----------|-----------|
| 1 | Acceptance criteria | Testable statements of done. "Users can export CSV" fails. "CSV export includes all rows matching the active filter, max 50k rows" passes. |
| 2 | Edge cases and errors | Named failure modes and their expected behavior (empty input, permissions, concurrency, timeouts). |
| 3 | Concrete example | At least one example input and expected output, or a sample payload. |
| 4 | Scope and dependencies | Affected components, services, and interfaces are named. |
| 5 | Design decided | No open "should we do X or Y" architecture question. If one exists, this scores 0. |
| 6 | Non-functional constraints | Performance, security, data retention, compatibility. If the work genuinely has none, score 1 and say so. |

Rules:
- Do not give 1 without quoting the supporting text. If you cannot quote it, the score is 0.5 at most.
- A ticket that says "see Slack thread" for requirements scores 0 on criterion 1 unless the thread is linked and summarized.
- When unsure between two values, pick the lower one.

## 2. open_questions (integer)

Count each distinct unresolved decision or question in the ticket. Include "TBD", "?", "maybe", "confirm with X", and "depends on Y" when Y is not done. Do not count a question twice if it recurs. Do not count a question answered by a linked, dated decision.

## 3. scope_hours (number)

Human-equivalent hours to implement, including tests and docs. Exclude review, deployment, and waiting.

Anchor first on the median actual hours of 3 to 5 similar completed tickets, found in the tracker or git history. Estimate from scratch only when there are no comparable tickets, and say so.

## 4. diff_lines (integer)

Expected added plus changed lines, including tests. Anchor on the median diff size of similar merged PRs. If there is no history, use about 80 lines per scope hour as a rough starting point and mark it as an assumption in the report.

## 5. Codebase fields: test_coverage, complexity, tacit

Take these from `codebase_features.py`'s `ticket_fields`. Do not overwrite them from intuition. Override only with a written reason, and then report the override.

- `test_coverage` with provenance `measured` is reliable. With `proxy`, it means file-level test presence, which is a weak signal. Say so.
- `complexity` and `tacit` are always proxies. Treat them as rough.

## Common scoring errors

- **Scoring the intent, not the text.** A ticket titled "Improve invoice export" with a one-line description is clarity 0.2, however obvious the intent seems to you.
- **Counting the agent's ability as clarity.** A capable agent can fill gaps, but the gaps still cost retries or wrong turns. Score the ticket.
- **Anchoring on the requester's optimism.** If the ticket author says it's "just a small change," score the content, not the adjective.
- **Scoring tacit knowledge as low because the author is senior.** Tacit knowledge is about whether the codebase and domain are documented, not who wrote the ticket.
