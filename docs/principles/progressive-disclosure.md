# Progressive Disclosure

> Adapted from [Nielsen Norman Group's definition](https://www.nngroup.com/articles/progressive-disclosure/): "Defer advanced or rarely used features to a secondary screen, making applications easier to learn and less error-prone."

**Show the minimum needed at each level of depth, with clear links to the next level. Never duplicate details across levels.**

## Rules

1. **Each document serves one level of depth.** Parent docs provide the map; child docs own the details. A reader at any level should see just enough to orient themselves and know where to go next.

2. **A fact lives in exactly one document.** Other documents reference it with a link. If you need to change a fact, you change it in one place. If you find the same explanation in two documents, one of them is wrong (or will be, eventually).

3. **Links set expectations.** A cross-reference should tell the reader what they'll find before they follow it. "See [Deployment](deployment.md)" is insufficient. "See [Deployment: rollback procedure](deployment.md#rollback)" tells them exactly what's on the other side.

## How to apply it

When writing or editing a document, ask:

- **Am I explaining something that's already explained elsewhere?** Link to it instead.
- **Would a reader at this level need this detail to do their immediate task?** If not, it belongs one level deeper.
- **If I deleted this paragraph, would the information still exist somewhere in the docs?** If yes, delete it and link. If no, keep it (this is the canonical location).

## Common violations

| Pattern | Fix |
|---------|-----|
| A process doc re-explains a concept defined in another doc | Replace with a link |
| An index/README includes step-by-step instructions | Move steps to a child doc, keep only the summary |
| Two docs describe the same configuration, API, or workflow | Pick one as canonical, link from the other |

## References

- [Progressive Disclosure (NN/g)](https://www.nngroup.com/articles/progressive-disclosure/)
