// The normalized content model that variant schemas constrain.
// The parser builds this value; CUE only constrains it.
package content

#TextMetrics: {
	characters!: int & >=0
	words!:      int & >=0
	paragraphs!: int & >=0
}

#SourcePoint: {
	offset!: int & >=0
	line!:   int & >=1
	column!: int & >=1
}

#SourceSpan: {
	start!: #SourcePoint
	end!:   #SourcePoint
}

#Metrics: {
	source!:  #TextMetrics
	visible!: #TextMetrics

	lists!:              int & >=0
	listItems!:          int & >=0
	unorderedLists!:     int & >=0
	orderedLists!:       int & >=0
	taskLists!:          int & >=0
	taskItems!:          int & >=0
	uncheckedItems!:     int & >=0
	thematicBreaks!:     int & >=0
	atxHeadings!:        int & >=0
	setextHeadings!:     int & >=0
	indentedCodeBlocks!: int & >=0
	fencedCodeBlocks!:   int & >=0
	// Lines inside fenced and indented code blocks, so a section can cap code.
	codeLines!:                int & >=0
	htmlBlocks!:               int & >=0
	linkReferenceDefinitions!: int & >=0
	paragraphs!:               int & >=0
	blankLines!:               int & >=0
	tables!:                   int & >=0
	blockQuotes!:              int & >=0
	maxBlockDepth!:            int & >=0
	maxListDepth!:             int & >=0
}

#NoGFMBlocks: {
	lists:                    0
	listItems:                0
	unorderedLists:           0
	orderedLists:             0
	taskLists:                0
	taskItems:                0
	uncheckedItems:           0
	thematicBreaks:           0
	atxHeadings:              0
	setextHeadings:           0
	indentedCodeBlocks:       0
	fencedCodeBlocks:         0
	codeLines:                0
	htmlBlocks:               0
	linkReferenceDefinitions: 0
	paragraphs:               0
	blankLines:               0
	tables:                   0
	blockQuotes:              0
	maxBlockDepth:            0
	maxListDepth:             0
}

#TextValue: {
	format!:  "plaintext" | "markdown"
	source!:  string
	visible!: string
	span!:    #SourceSpan
	metrics!: #Metrics
	...
}

#GFMTextValue: #TextValue & {
	format: "markdown"
}

#InlineContext: "prose" | "link-label" | "image-alt"

#InlineKind: "text" |
	"backslash-escape" |
	"character-reference" |
	"code-span" |
	"emphasis" |
	"strong-emphasis" |
	"strikethrough" |
	"link" |
	"image" |
	"autolink" |
	"raw-html" |
	"hard-break" |
	"soft-break"

#InlineValue: {
	kind!:    #InlineKind
	source!:  string
	visible!: string
	span!:    #SourceSpan
	...
}

#TextInline: #InlineValue & {
	kind: "text"
}

#BackslashEscape: #InlineValue & {
	kind:       "backslash-escape"
	character!: string
}

#CharacterReference: #InlineValue & {
	kind:       "character-reference"
	character!: string
}

#CodeSpan: #InlineValue & {
	kind:  "code-span"
	code!: string
}

#Emphasis: #InlineValue & {
	kind: "emphasis"
	children!: [...#Inline]
}

#StrongEmphasis: #InlineValue & {
	kind: "strong-emphasis"
	children!: [...#Inline]
}

#Strikethrough: #InlineValue & {
	kind: "strikethrough"
	children!: [...#Inline]
}

#Link: #InlineValue & {
	kind:         "link"
	style!:       "inline" | "full-reference" | "collapsed-reference" | "shortcut-reference"
	destination!: string
	title?:       string
	children!: [...#Inline]
}

#Image: #InlineValue & {
	kind:         "image"
	style!:       "inline" | "full-reference" | "collapsed-reference" | "shortcut-reference"
	destination!: string
	title?:       string
	children!: [...#Inline]
}

#Autolink: #InlineValue & {
	kind:         "autolink"
	style!:       "uri" | "email" | "extended-url" | "extended-www" | "extended-email"
	destination!: string
}

#RawHTML: #InlineValue & {
	kind:        "raw-html"
	disallowed!: bool
}

#HardBreak: #InlineValue & {
	kind: "hard-break"
}

#SoftBreak: #InlineValue & {
	kind: "soft-break"
}

#Inline: #TextInline |
	#BackslashEscape |
	#CharacterReference |
	#CodeSpan |
	#Emphasis |
	#StrongEmphasis |
	#Strikethrough |
	#Link |
	#Image |
	#Autolink |
	#RawHTML |
	#HardBreak |
	#SoftBreak

#InlineOccurrence: {
	kind!:    #InlineKind
	source!:  string
	visible!: string
	span!:    #SourceSpan
	context!: #InlineContext
	ancestors!: [...#InlineKind]
	style?:       string
	destination?: string
	title?:       string
	disallowed?:  bool
}

#RepositoryReferenceBase: {
	kind!:    "issue-or-pull-request"
	form!:    "bare" | "qualified"
	source!:  string
	number!:  int & >=1
	span!:    #SourceSpan
	context!: #InlineContext
}

#BareRepositoryReference: #RepositoryReferenceBase & {
	form: "bare"
}

#QualifiedRepositoryReference: #RepositoryReferenceBase & {
	form:        "qualified"
	owner!:      string & !=""
	repository!: string & !=""
}

#RepositoryReference: #BareRepositoryReference | #QualifiedRepositoryReference

#InlineIndex: {
	elements!: [...#InlineOccurrence]
	repositoryReferences!: [...#RepositoryReference]
}

#InlineBlock: #GFMTextValue & {
	inlines!: [...#Inline]
}

#ThematicBreak: #GFMTextValue & {
	kind!: "thematic-break"
}

#ATXHeading: #InlineBlock & {
	kind!:  "heading"
	style!: "atx"
	level!: int & >=1 & <=6
}

#SetextHeading: #InlineBlock & {
	kind!:  "heading"
	style!: "setext"
	level!: 1 | 2
}

#IndentedCodeBlock: #GFMTextValue & {
	kind!:  "code-block"
	style!: "indented"
}

#FencedCodeBlock: #GFMTextValue & {
	kind!:  "code-block"
	style!: "fenced"
	info!:  string
}

#HTMLBlock: #GFMTextValue & {
	kind!:     "html-block"
	htmlType!: int & >=1 & <=7
}

#LinkReferenceDefinition: #GFMTextValue & {
	kind!:        "link-reference-definition"
	label!:       string & !=""
	destination!: string & !=""
	title?:       string
}

#Paragraph: #InlineBlock & {
	kind!: "paragraph"
}

#BlankLine: #GFMTextValue & {
	kind!: "blank-line"
}

#TableCell: #InlineBlock & {
	kind!: "table-cell"
}

#TableRow: {
	cells!: [#TableCell, ...#TableCell]
}

#Table: #GFMTextValue & {
	kind!: "table"
	alignments!: [...("none" | "left" | "center" | "right")]
	header!: #TableRow
	rows!: [...#TableRow]
	columnCount!: int & >=1
}

#BlockQuote: #GFMTextValue & {
	kind!: "block-quote"
	blocks!: [...#GFMBlock]
}

#ListItem: #GFMTextValue & {
	kind!:    "list-item"
	checked?: bool
	blocks!: [...#GFMBlock]
}

#TaskListItem: #ListItem & {
	checked!: bool
}

#UnorderedList: {
	kind!:  "list"
	style!: "unordered"
	tight!: bool
	items!: [#ListItem, ...#ListItem]
	...
}

#OrderedList: {
	kind!:  "list"
	style!: "ordered"
	start!: int & >=1
	tight!: bool
	items!: [#ListItem, ...#ListItem]
	...
}

#List: #GFMTextValue & (#UnorderedList | #OrderedList)

#GFMBlock: #ThematicBreak |
	#ATXHeading |
	#SetextHeading |
	#IndentedCodeBlock |
	#FencedCodeBlock |
	#HTMLBlock |
	#LinkReferenceDefinition |
	#Paragraph |
	#BlankLine |
	#Table |
	#BlockQuote |
	#List

#DraftBlock: #TextValue & {
	kind!: "text" | "heading" | "section"

	level?:   int & >=1 & <=6
	heading?: string & !=""
	content?: [...#GFMBlock]
	inline!: #InlineIndex
}

#Draft: {
	schemaVersion!: 1
	object!:        "commit" |
		"pr" |
		"issue" |
		"issue-comment" |
		"pr-review" |
		"commit-comment" |
		"release" |
		"record" |
		"project-item" |
		"milestone" |
		"discussion" |
			"discussion-comment"
	variant!: string

	metadata!: {...}

	blockOrder!: [...string]
	blocks!: {
		[string]: #DraftBlock
	}

	inline!:  #InlineIndex
	metrics!: #Metrics
}
