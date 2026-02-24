# Render, Don't Record

**If a fact can change without a commit, the document should contain the query that retrieves it, not a snapshot of the answer.**

## Rules

1. **Mutable state belongs in systems, not files.** When a document needs to present dynamic information, it should provide the command that constructs the current truth. A hardcoded table is correct once and wrong forever after.

2. **Templates use placeholders, not filled-in examples.** Version numbers, dates, issue IDs, and other identifiers in process docs should appear as `vX.Y.Z`, `YYYY-MM-DD`, `#NNN`. A filled-in example invites copy-paste without substitution; a placeholder forces the reader to supply the real value.

3. **Records are not state.** A changelog entry documenting what shipped is an immutable historical fact. The test: "Will this be wrong tomorrow without someone updating it?" If yes, it's state (render it). If no, it's a record (write it).

## How to apply it

When writing or editing a document, ask:

- **Will this fact change without someone updating this file?** If yes, replace the hardcoded value with the query that retrieves it.
- **Does this information live in a system with an API or CLI?** If yes, provide the command. The reader gets fresh data; you avoid maintenance.
- **Am I copying this from somewhere that also maintains it?** If yes, you've created a second copy that will drift. Link to the source or provide the query.
- **If I showed this to someone in 3 months, would it still be correct?** If the answer is "only if nothing changes," it's state, not a record.

## The query pattern

Instead of hardcoding values, provide the command that retrieves current truth:

```bash
# Instead of "there are 12 open issues", use:
git log --oneline --no-merges main..HEAD | wc -l

# Instead of a hardcoded test coverage number, use:
pytest --cov --cov-report=term-missing | tail -1
```

The document stays correct indefinitely because it never contains the answer, only the question.
