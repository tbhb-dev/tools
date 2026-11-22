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
		...
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
#CIRestatement: #"(?i)^((all|the|every) )?((unit|integration|ci|lint|build|test) )?(tests?|suite|ci|lint(ing)?|checks?|build|pipeline|gates?)( checks?)?( (is|are|was|were|has|have|had|all|now|been))*( ?(pass(es|ed|ing)?|green|clean|succeed(s|ed|ing)?))\.?$|^[0-9]+ ?/ ?[0-9]+( tests?)?( (is|are|have|has))? pass(es|ed|ing)?\.?$"#

// One acceptance criterion: a single observable outcome, one sentence, about
// 20 words at most. Whether "and" joins two checks is a kernel warning.
#Criterion: #IssueItem & {
	visible: !~#CIRestatement
	metrics: visible: {words: >=2 & <=20, ...}
}

// One verification item: the evidence that proves the work is done, such as a
// merged PR, a command with its expected result, a live read or a record.
// Same shape and caps as a criterion.
#VerificationItem: #Criterion

// Per-type title checks. Each adds to #IssueTitle and refuses only what is
// certain: a type word, a bracket or ticket prefix, a word cap, and the
// Spike question mark. The shapes (Epic noun phrase, Feature "<Role> can
// <action>", Task imperative, Defect "when") cannot be checked exactly by a
// regular expression, so they are patterns the kernel applies as warnings
// with a hint. The issue type field decides the type, not the title.
#TitleTypeWord: #"(?i)\b(initiative|epic|feature|story|task|chore|defect|spike)\b"#
#TitlePrefix: #"^(\[|\(|[A-Za-z]+-[0-9]+)"#

#EpicShapeBan: #" can "#
#FeatureShape:   #"^[A-Z][^ ]*( [^ ]+){0,3} can [a-z]"#
#TaskShape:      #"^[A-Z][a-z]+ "#
#DefectShape:    #" when "#

#_Titled: #IssueTitle & {
	visible: !~#TitleTypeWord & !~#TitlePrefix
}

// The world once done, one outcome, 12 words at most.
#InitiativeTitle: #_Titled & {
	metrics: visible: {words: <=12, ...}
}

#EpicTitle: #_Titled & {
	metrics: visible: {words: <=12, ...}
}

#FeatureTitle: #_Titled & {
	metrics: visible: {words: <=14, ...}
}

#TaskTitle: #_Titled & {
	metrics: visible: {words: <=12, ...}
}

#ChoreTitle: #TaskTitle

#DefectTitle: #_Titled & {
	metrics: visible: {words: <=16, ...}
}

// A question.
#SpikeTitle: #_Titled & {
	visible: =~#"\?$"#
	metrics: visible: {words: <=15, ...}
}
