# Single Process, Single Document

If a workflow runs as one pass, document it in one file. Split into separate documents only when sub-steps are independently triggered on different cadences or by different roles.

## Rules

1. **One pass, one document.** If an operator sits down and runs steps A through F in sequence, those steps belong in a single document. Splitting them across files forces the reader to navigate between documents mid-process, increasing the chance they skip a step or lose context.

2. **Split on trigger, not on source.** A review pass that covers bug reports, feature requests, and support tickets is one process with three inputs, not three processes. The trigger is the same ("run the review"), the operator is the same, and the output is the same. Separate documents are warranted when different events trigger different subsets of work on genuinely different schedules.

3. **Cross-cutting concerns need a single home.** When a concept spans the entire process (e.g., a shared timestamp anchor, a labeling convention), it must live in the document that owns the process. If the process is split across files, cross-cutting concerns have no natural home and end up duplicated or missing.

## How to apply it

When writing or editing a process document, ask:

- **Does the reader run these steps in one sitting?** If yes, they belong in one file.
- **Would splitting this create a cross-cutting concern without a home?** If yes, keep it together.
- **Are the sub-steps independently triggered?** If a sub-step runs on its own schedule, it may warrant a separate document, but consider whether a "scope" parameter within one document is simpler.

## Common violations

| Pattern | Fix |
|---------|-----|
| One process split by input source (e.g., separate docs for email triage and chat triage) | Merge into one doc with sections per source |
| A shared convention documented in neither sub-doc because it doesn't "belong" to either | Merge the docs; the convention now has a home |
| Reader must open two documents side by side to complete one task | Merge into one doc |
