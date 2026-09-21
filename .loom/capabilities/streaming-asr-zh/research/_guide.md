# Research guide for this capability

> This file guides what to write in the research/ directory. Delete it when you have added your own
> research materials. `loom capability synthesize` reads all .md files in this directory (except this
> guide) and expects them to contain expert narratives, case studies, or methodology sources that
> inform the decision tree.

## What to write

Create one .md file per research source. Each file should answer: **how does an expert in this field
think through the problems this project faces?**

Good research materials include:
- Expert narratives: how a practitioner describes their own decision process
- Case studies: real projects where this field's decisions mattered, and what happened
- Methodology sources: established frameworks, heuristics, or principles from the field
- Failure accounts: what went wrong when the field's judgment was absent or ignored

Bad research materials (will produce weak decision trees):
- Generic textbook summaries with no project-specific relevance
- Tool documentation or API references (those are not professional judgment)
- Marketing copy or opinion pieces without evidence

## File format

Name files descriptively: `expert-decision-process.md`, `case-study-X.md`, `failure-account-Y.md`.
Each file should be 1-3 paragraphs of substantive content. Include the source at the top:

```markdown
# <descriptive title>

Source: <book, article, interview, observation, or personal experience>

<content: how the expert thinks, what they decided, what evidence they used, what happened>
```

## How this feeds synthesize

`loom capability synthesize` checks that:
1. At least one .md file exists in research/ (besides this guide)
2. Every decision tree node (### C1, C2, ...) in capability.md has a `source:` field
3. Every node has a `counterexample:` field

The source field in each node should reference which research file supports it. Write research that
you can cite by name when you build the decision tree.
