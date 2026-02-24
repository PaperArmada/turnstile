# Rough Cuts

> Move fast and remove material aggressively when far from critical surfaces. Dial in precision only where tolerances are tight.

**Not all work requires the same level of care. Match your precision to the proximity of the final surface.**

## Rules

1. **Hog out material early.** When exploring, prototyping, or building scaffolding, speed matters more than polish. Get the shape right before worrying about the finish. A rough-cut spike that answers the question in an hour beats a polished implementation that takes a day.

2. **Identify critical surfaces.** Know which parts of the system have tight tolerances: public APIs, data persistence formats, security boundaries, user-facing messages. These deserve careful attention. Everything else can be rougher.

3. **Progressive refinement, not uniform precision.** First pass: does it work at all? Second pass: does it handle edge cases? Third pass: is it clean? Don't jump to the third pass on code that might not survive the first.

4. **Ship the rough cut, then iterate.** A working rough version that can be tested and improved is more valuable than a theoretical perfect version. Real feedback from real use (friction testing, dogfooding) reveals what actually needs precision.

## Anti-patterns

- Spending hours on error messages for an internal prototype
- Writing comprehensive tests for code you're still exploring whether to keep
- Polishing documentation before the API is stable
- Treating every file change with the same level of review rigor
- Refusing to merge because a non-critical edge case isn't handled

## Application to Turnstile

The starter pack processes use `severity: warning` (not `error`) on most gates because they're rough cuts: good enough to guide, not strict enough to block. Users tighten tolerances by changing severity to `error` on the gates that matter to them. The spike process exists specifically for rough-cut investigation: get in, answer the question, get out. Process definitions themselves follow this pattern: start with a simple linear flow, add branching and validation gates only where friction testing reveals they're needed.
