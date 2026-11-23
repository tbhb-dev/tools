---
description: CUE definitions for pull request drafts, review templates, review drafts, and verdicts, with the parse step, runtime, and migration.
generated:
  - role: generated
    harness: claude-code:2.1.330
    model: anthropic:claude-opus-5-5
    effort: high
    surface: sdk-cli
    session: sha-256-64:ef51955cb2cd5a35
    at: "2026-11-23T15:00:43Z"
status: draft
tags: [alpha, cue, pull-requests, reviews]
title: Pull request verification in CUE
type: Design Draft
---

<!-- cspell:ignore Bacchelli Bosu Gonçalves Greiler Lassenius Mäntylä Pirouzkhah Wurzel Zhang -->

# Pull request verification in CUE

Configure Unify Execute (CUE) is the one schema language for four documents.

- The pull request draft, whose body becomes the pull request description.
- The pull request and review templates.
- The review draft.
- The verdict.

The CUE definitions replace the regular expression checks in `src/tbhbdev/alpha/pull_request.py` and `src/tbhbdev/alpha/review.py`, the baseline files `.github/schemas/pull-request-draft.schema.json` and `.github/pull-request-sections.toml`, and the review draft schema in `src/tbhbdev/alpha/review_data/`. The verdict definition `#Review` in `schemas/review.cue` sets the pattern the other three follow.

## Research

- Google's [CL description guide](https://google.github.io/eng-practices/review/developer/cl-descriptions.html) puts what changed in the first line and why in the body, and asks the author to recheck the description against the final change. `Why` and `Behavior change` stay required, and the match between description and diff stays with the reviewer (checklist item R8).
- In [Expectations, Outcomes, and Challenges of Modern Code Review](https://doi.org/10.1109/ICSE.2013.6606617) (2013), Bacchelli and Bird found that understanding the change is the core reviewer task. Required sections give the reviewer the problem and the behavior before the diff.
- In [What Makes a Code Change Easier to Review](https://doi.org/10.1145/3236024.3236080) (2018), Ram and others rank description quality and change size among reviewability factors. Word limits bound the description, and each migration step stays under 400 code lines.
- In [an investigation of pull request templates](https://doi.org/10.1016/j.infsof.2021.106797) (2022), Zhang and others associate templates that ask for key information with shorter reviews. The template marks most sections optional.
- In [The Value of Effective Pull Request Description](https://arxiv.org/abs/2602.14611) (2026 preprint), Pirouzkhah, Wurzel Gonçalves, and Bacchelli found that stating the desired feedback type best predicts acceptance. `Needs human judgment` holds that statement as prose.
- Google's [guide to review comments](https://google.github.io/eng-practices/review/reviewer/comments.html) marks severity with labels such as `Nit` and `FYI`, and [Conventional Comments](https://conventionalcomments.org/) starts a comment with a label and a `blocking` or `non-blocking` decoration. A finding starts with a fixed class word, which lets CUE tie `HOLD` to a blocking finding.
- In [What Types of Defects Are Really Discovered in Code Reviews?](https://mmantyla.github.io/wiki/What+Types+of+Defects+Are+Really+Discovered+in+Code+Reviews%253F.html) (2009), Mäntylä and Lassenius found that 75 percent of review defects affect evolvability, not visible behavior. `TECH-DEBT` differs from `BLOCK`, so a `GO` verdict can record evolvability findings.
- In [Characteristics of Useful Code Reviews](https://doi.org/10.1109/MSR.2015.21) (2015), Bosu, Greiler, and Bird tie useful comments to concrete defects. Each finding cites a `path:line`.

## Constraints and prose rules

| Rule | Kind |
| --- | --- |
| Section names, order, required sections, and no text before the first section | CUE |
| Word limits per section, counted as prose words | CUE |
| One H1 title of at most 64 characters, and no other H1 | CUE |
| Title is a Conventional Commit header | `cog verify` |
| Each paragraph and list item on one source line | CUE |
| Zero HTML comments and zero trailer lines in a draft (`Generated-by`, `Assisted-by`, `Opened-by`, `Signed-off-by`, or `Co-authored-by`) | CUE |
| Each reference uses the form `owner/repo#N` | CUE |
| Frontmatter fields, types, and defaults | CUE |
| `Behavior change`: required, at most 120 words | CUE |
| `Risk`: optional, at most 80 words | CUE |
| `Behavior change` names a file only when the file is the interface | Prose |
| `Risk` names specific failure modes and who they affect | Prose |
| `Decisions` reads as "chose X over Y because Z" | Prose |
| Spelling, prose style, and Markdown style | vale, cspell, rumdl |

A rule becomes a CUE constraint when a parser and a pattern decide it without reading intent. Other rules live in the template guidance and the review checklist.

## Parse step

1. `parse_frontmatter` reads the YAML block and refuses duplicate keys. It keeps timestamps as text. A YAML error stops the check at line 1.
2. `normalize` from `tbhbdev.content_model` builds the body value with object `pr` and variant `draft`, or object `pr-review` and variant `review-draft` or `review`. The frontmatter mapping becomes `metadata`.
3. An unclosed code fence stops the check, because a parser reads the rest of the file as code.

The normalizer gains two outputs. `inline.repositoryReferences` records the form and span of each `#N` or `owner/repo#N` outside code and links. `metrics.prose.words` matches `pull_request.count_words`, which skips code spans, link destinations, URLs, and HTML comments. Word limits read it, never `metrics.visible.words`, which counts code spans and splits contractions.

## Definitions

Each document has one definition in `src/tbhbdev/schemas/`, package `content`, unified with `#Draft` from `core.cue`. The draft and template definitions both read one section table.

```cue
#Section: {heading: string, key: string, required: bool, maxWords: int}

#PullRequestSections: [...#Section] & [
	{heading: "Why", key: "why", required: true, maxWords: 60},
	{heading: "Behavior change", key: "behavior-change", required: true, maxWords: 120},
	// ... one entry per row of the template, in order
]
```

- `#PullRequestDraft` (`pr.cue`): `metadata` is a closed struct with `base` (default `"main"`, branch pattern), `draft` (default `false`), `labels`, `reviewers`, `assignees`, and `milestone`, carrying the JSON schema's patterns and lengths. `blockOrder` is `"title"` followed by section keys in table order without repeats. Every required key is present, and every section has prose or code.
- `#ReviewDraft` (`review-draft.cue`): `metadata` is a closed struct with `pr`, `commit_id` (40 hex), and `event` (`"APPROVE" | "REQUEST_CHANGES" | "COMMENT"`). The body has no title block. `#ReviewSections` lists Verdict (required, 60), Findings (200), Suggestions (120), and Verification (required, 100). `REQUEST_CHANGES` requires a nonempty Findings section.
- `#Template` (`template.cue`): checks `.github/PULL_REQUEST_TEMPLATE.md` against `#PullRequestSections` and a review template against `#ReviewSections`. Each section starts with an HTML block whose source matches `Required, at most N words.` or `Optional, at most N words.` with the table's values, so the template can't drift from the table. A repository template may make an optional section required and lower a limit, and may not do the reverse.
- `#Review` (`review.cue`): the verdict, unchanged in shape. A preamble of `Run`, `Provider`, `Model`, `Head`, and `Tier` lines, `GO` or `HOLD`, and up to 8 findings of class `BLOCK`, `TECH-DEBT`, or `SECURITY-DEBT` with a `path:line`, one or two sentences, 40 words each, and 160 in total.

A repository adds constraints in `.github/pull-request.cue` or `.github/pull-request-review.cue`. Unification with the packaged definition lets a repository narrow the floor and never widen it.

## Verdict checks

Run facts enter CUE as typed `-t` tags that `review.cue` declares with `@tag(...)`, which keeps run values out of CUE source text. The host passes `run`, `provider`, `model`, `head`, and `tier` from the run request, and `#Review` requires each header line to equal its tag, comparing `Head` in lowercase. `Tier` takes `1`, `2`, or `3` and must equal the requested tier. Before posting, `review_post.create` still reads the head from GitHub and refuses when it differs from `Head`. A `#RunPost` definition narrows `event` to `COMMENT`. `HOLD` requires a `BLOCK` finding and `GO` forbids one. A finding that restates a CI gate (`#CIRestatement` from `issue.cue`) fails.

## Error messages

Each rule is an `if` clause that raises `error()` with a message stating the section, the rule ID, the limit, and the value found, as in `#Review`. The wrapper maps each CUE error path under `blocks.<key>` to that block's span plus the frontmatter offset. It prints `path:line: <section>: <message> (<rule-id>)`. A `metadata.<field>` path points at the field's frontmatter line. The wrapper prints an unmapped error at line 1 with the raw CUE text. The check runs `cue vet -c --all-errors`, so it reports every problem and treats a missing field as an error.

## Linters

vale, cspell, and rumdl keep running over the rendered title and body with the repository's own configs, through `pull_request.lint`, after CUE. CUE never checks spelling, prose style, or Markdown style. Run hosts keep `lint=False`.

## Runtime

`cue_runtime.resolve()` returns the pinned binary from `cue-pin.json` at its install path and refuses any other. `tbhbdev cue install` fetches and verifies it once. Because the schemas import only the CUE standard library and belong to no CUE module, `cue vet` runs offline without a registry. It reads the draft JSON from standard input and the schema files from the package through `importlib.resources`. In Bazel, `pytest_test` gets the same release from `@cue_test_tool//:cue` through `path_tools`, and a fixture points `resolve` at it. Without `cue`, the tests fail. One test asserts that each file in `src/tbhbdev/schemas/` matches its copy in `schemas/`.

## Migration

Each step is one pull request under 400 code lines as `scc` counts them. Until its switch step, the old code decides each result.

1. Move the inline drafts from the pull request, review, and review post tests into fixture files under `tests/testdata/alpha/`, each with an expected result file listing refused lines.
2. Add `inline.repositoryReferences` and `metrics.prose` to the normalizer, with their tests.
3. `pr.cue` and `template.cue` arrive with a parity test, which runs `pull_request.check_text` without linters and the CUE check over every fixture and `passing-draft.md`. Both must return the same result and refused lines.
4. `tbhbdev alpha pr check`, `create`, and `edit` switch to CUE, and `block_problems`, `wrap_problems`, `trailer_problems`, `ref_problems`, `load_sections`, and `load_schema` go away. From here on, the parity test compares CUE against the expected result files.
5. `review-draft.cue` lands, `review.check_text` and `review_post.validate` switch to it, and `review.SCHEMA` and the packaged JSON schema go away.
6. `review.cue` declares the `@tag` run facts, and `review_post` passes them.
7. `review_policy.check_template` and `check_draft_schema` switch to `#Template` and repository CUE files.
8. Rules beyond parity land one per pull request afterward, with their own fixtures.

Agent hosts install `cue` before step 5, because `review_post` validates on the run host.

## Failure modes

- A missing or wrong `cue` binary, a schema that doesn't compile, a timeout, or a nonzero exit with nothing parsed refuses the draft with exit 2 and `verification could not run`. No path treats a failed `cue` run as a pass, and `alpha pr create` opens nothing.
- A repository CUE file that conflicts with the floor refuses every draft in that repository.
- CUE reports a failed disjunction as a list of branch errors, so author-facing rules use `error()` clauses instead.

## Open questions

For rules:

- Does tbhb-dev/rules distribute `pr.cue` as the baseline and drop `pull-request-sections.toml` and the JSON schema, or does tools own the definitions?
- Should CUE also check the Conventional Commit type list, or does `cog` stay the only judge of the title?
- Should `Why` require an issue link?
- Should optional sections refuse `None` and `Low risk` placeholders?
- Should `Verification` refuse a restated CI gate?

For pm:

- What sets `Tier`, and what does it gate? It appears only in `review.cue` and its tests.
- Do the review draft and the verdict remain separate documents, and does `GO` map to `COMMENT` and `HOLD` to `REQUEST_CHANGES`?
- The review drafts reference refuses `APPROVE` in a draft, while the schema accepts it. Which one holds?
- Do content plugins share the rule IDs and message format with `content_vet.Finding` codes?
