// Shared building blocks for issue variants. This file defines no variant.
// Load it with core.cue, taxonomy.cue and exactly one variant file per run.
package content

// A section holds plain prose or one flat list. Everything else is refused,
// so a section can't hide text in a table, quote, heading or HTML block.
#IssueSection: #DraftBlock & {
	kind: "section"
	metrics: {
		tables:                   0
		htmlBlocks:               0
		blockQuotes:              0
		atxHeadings:              0
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
	}
}
