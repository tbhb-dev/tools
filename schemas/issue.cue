// Shared building blocks for issue variants. This file defines no variant.
// Load it with core.cue, taxonomy.cue and exactly one variant file per run.
package content

// A section holds plain prose or one flat list. Everything else is refused,
// so a section can't hide text in a table, quote, heading or HTML block.
#IssueSection: #DraftBlock & {
	kind: "section"
	metrics: {
		tables:      0
		htmlBlocks:  0
		blockQuotes: 0
		// The section's own H2 line counts once, so a second heading is refused.
		atxHeadings:              1
		setextHeadings:           0
		thematicBreaks:           0
		linkReferenceDefinitions: 0
		maxListDepth:             <=1
	}
}

// First line of the body. Needs three words, no trailing . ! or :.
#IssueTitle: #DraftBlock & {
	kind:    "heading"
	level:   1
	visible: !~#"[.!:]$"#
	metrics: visible: {
		characters: >=15 & <=70
		words:      >=3
	}
}

// A list item is one short paragraph: no nested list and no code.
#IssueItem: {
	metrics: {
		paragraphs: <=1
		lists:      0
		codeLines:  0
		...
	}
	...
}

// Matches an item that only restates a gate CI already checks, such as
// "tests pass", "CI is green" or "12/12 pass". The kernel hint says: CI
// already checks this; state the observable outcome instead.
#CIRestatement: #"(?i)^((all|the|every) )?((unit|integration|ci|lint|build) )?(tests?|ci|lint(ing)?|checks?|build|pipeline|gates?)( checks?)?( (is|are))? ?(pass(es|ing)?|green|clean|succeed(s|ing)?)\.?$|^[0-9]+ ?/ ?[0-9]+( tests?)? pass"#

// One acceptance criterion: a single observable outcome, one sentence, about
// 20 words at most. Whether "and" joins two checks is a kernel warning.
#Criterion: #IssueItem & {
	visible: !~#CIRestatement
	metrics: visible: words: >=2 & <=20
}

// One verification item: the evidence that proves the work is done, such as a
// merged PR, a command with its expected result, a live read or a record.
// Same shape and caps as a criterion.
#VerificationItem: #Criterion

// Per-type title shapes. Each adds to #IssueTitle and bans type words, so the
// title names the work, not its type. RE2 has no lookahead, so the shape
// checks are forms the title must or must not match.
#TitleTypeWord: #"(?i)\b(initiative|epic|feature|story|task|chore|defect|spike)\b"#

// The world once done, one outcome, 12 words at most.
#InitiativeTitle: #IssueTitle & {
	visible: !~#TitleTypeWord
	metrics: visible: words: <=12
}

// A capability as a noun phrase, so no "can" and no leading verb form.
#EpicTitle: #IssueTitle & {
	visible: !~#TitleTypeWord & !~#" can "# & !~#"^[A-Z][a-z]*(ing|ed)\b"#
	metrics: visible: words: <=12
}

// "<Role> can <action>".
#FeatureTitle: #IssueTitle & {
	visible: !~#TitleTypeWord & =~#"^[A-Z][^ ]*( [^ ]+){0,3} can [a-z]"#
	metrics: visible: words: <=14
}

// An imperative verb phrase. A leading -ing or -ed word is refused.
#TaskTitle: #IssueTitle & {
	visible: !~#TitleTypeWord & =~#"^[A-Z][a-z]+ "# & !~#"^[A-Z][a-z]*(ing|ed)\b"# & !~#"\?$"#
	metrics: visible: words: <=12
}

#ChoreTitle: #TaskTitle

// "<Thing> <does wrong> when <condition>".
#DefectTitle: #IssueTitle & {
	visible: !~#TitleTypeWord & =~#" when "# & !~#"\?$"#
	metrics: visible: words: <=16
}

// A question.
#SpikeTitle: #IssueTitle & {
	visible: !~#TitleTypeWord & =~#"\?$"#
	metrics: visible: words: <=15
}
