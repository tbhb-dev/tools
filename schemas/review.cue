// cspell:ignore Submatch
package content

import (
	"list"
	"regexp"
	"strings"
)

#ReviewSHA:     #"[0-9a-f]{40}"#
#ReviewHost:    #"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"#
#ReviewVerdict: "GO" | "NO-GO"
#ReviewEvent:   "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
#FindingPrefix: #"^F([1-8]): P([0-3]) (correctness|security|design|test|docs)\s+([\s\S]+)$"#
#Word:          #"[\p{L}\p{N}_]+"#
#SentenceEnd:   #"([.!?]+) "#
#VerdictWord:   #"(NO-GO|GO|APPROVED?|BLOCK|HOLD|REQUEST_CHANGES)"#
#Verdict:       #"\b\#(#VerdictWord)\b|(?i)(\bverdict\s*:\s*)\#(#VerdictWord)\b|^\s*\#(#VerdictWord)(\s|[.:!?–—]|$)|[.!?]\s+\#(#VerdictWord)([.:!?–—]|$)"#
#ReviewStamp:   #"(?i)(\bjudged head\b|\b(commit|head)[ :=]+[0-9a-f]{7,40}\b|\bverdict\s*:\s*\#(#VerdictWord)\b|\b\#(#ReviewSHA)\b|(?m:^(Run|Provider|Model|Variant|Host|Head|Tier):))"#

#ReviewProse: {
	_text:  string
	_words: len(regexp.FindAll(#Word, _text, -1)) & >=1
	...
}

#ReviewSentences: #ReviewProse & {
	_text:      string
	_sentences: strings.Split(regexp.ReplaceAll(#SentenceEnd, _text, "$1\n"), "\n")
	_sentences: ([_] | [_, _]) & [...(=~#Word & =~#"[.!?]$"#)]
}

#ReviewItem: #ListItem & {
	blocks: [#Paragraph & {source: !~#ReviewStamp}]
	visible:  !~#ReviewStamp & !~#"\n"#
	checked?: error("Use a bullet without a task checkbox")
}

#ReviewFinding: #ReviewItem & #ReviewSentences & {
	visible: !~#"[^\s:]+:[1-9][0-9]*\b"#
	_parts:  regexp.FindSubmatch(#FindingPrefix, visible)
	_text:   strings.Join(strings.Fields(_parts[4]), " ")
	_sentences: [...string]
	_words: <=30
	for sentence in _sentences {
		let prose = strings.TrimSpace(strings.TrimRight(sentence, ".!?"))
		if prose =~ #CIRestatement {
			_err: error("A finding may not restate a CI gate")
		}
	}
}

#ReviewSuggestion: #ReviewItem & #ReviewProse & {
	visible: string
	_parts:  regexp.FindSubmatch(#"^S([1-5]):\s+([\s\S]+)$"#, visible)
	_text:   _parts[2]
	_words:  <=25
}

#ReviewVerification: #GFMTextValue & #UnorderedList & {
	items: [...(#ReviewItem & #ReviewProse & {
		visible: !~#"^\s*(\(?\d+[.):]|\[[A-Za-z]*\d+\]|[A-Za-z]+[0-9]*:)"#
		_text:   visible
		_words:  <=20
	})] & list.MaxItems(6)
}

#ReviewBody: #Draft & {
	object:  "pr-review"
	variant: "review"
	blockOrder: ["summary", "verification"] |
		["summary", "findings", "verification"] |
		["summary", "suggestions", "verification"] |
		["summary", "findings", "suggestions", "verification"]
	metadata: {
		event:  *"" | #ReviewEvent
		posted: *false | bool
		...
	}
	blocks: close({
		summary: {
			kind:    "section"
			heading: "Summary"
			content: [#Paragraph & #ReviewSentences & {
				source:  !~#ReviewStamp
				visible: !~#Verdict & !~#ReviewStamp
				_text:   strings.Join(strings.Fields(visible), " ")
				_words:  <=40
			}]
		}
		findings?: {
			kind:    "section"
			heading: "Findings"
			content: [#GFMTextValue & #UnorderedList & {
				items: [...#ReviewFinding] & list.MaxItems(8)
				for i, item in items {
					if item._parts[1] != "\(i+1)" {
						_err: error("Number items consecutively from 1")
					}
				}
			}]
		}
		suggestions?: {
			kind:    "section"
			heading: "Suggestions"
			content: [#GFMTextValue & #UnorderedList & {
				items: [...#ReviewSuggestion] & list.MaxItems(5)
				for i, item in items {
					if item._parts[1] != "\(i+1)" {
						_err: error("Number items consecutively from 1")
					}
				}
			}]
		}
		verification: {
			kind:    "section"
			heading: "Verification"
			content: [#ReviewVerification, ...#GFMBlock]
			if !metadata.posted {
				content: [#ReviewVerification]
			}
			if metadata.posted {
				content: [#ReviewVerification, #ReviewAttribution | #ReviewMarker]
			}
		}
	})
	_blocks: *blocks.findings.content[0].items | []
	_blocking: len([for item in _blocks if list.Contains(["0", "1"], item._parts[2]) {item}])
	if metadata.event == "REQUEST_CHANGES" && _blocking == 0 {
		_err: error("REQUEST_CHANGES requires a P0 or P1 finding")
	}
	if metadata.event != "" && metadata.event != "REQUEST_CHANGES" && _blocking > 0 {
		_err: error("P0 and P1 findings require REQUEST_CHANGES")
	}
}

#ReviewAttribution: #Paragraph & {
	source: =~#"^Generated-by: LLM [a-z_]+=[^\s;]+(;[a-z_]+=[^\s;]+)*\r?\n?$"#
}

#ReviewMarker: #HTMLBlock & {
	source: =~#"^<!-- tbhb-review v2 [0-9a-f]{64} -->\r?\n?$"#
}

#Review: #ReviewBody & {
	metadata: close({
		event?:    #ReviewEvent
		posted?:   bool
		schema!:   2
		repo!:     =~#"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$"#
		pr!:       int & >=1
		head_sha!: =~#"^\#(#ReviewSHA)$"#
		tier!:     0 | 1 | 2
		verdict!:  #ReviewVerdict
		run!: close({
			name!:     =~#"^\S+$"#
			provider!: =~#"^\S+$"#
			model!:    =~#"^\S+$"#
			variant!:  "a" | "b"
			host!:     =~#"^\#(#ReviewHost)$"#
		})
	}) & ({verdict: "GO", event: "APPROVE"} | {verdict: "NO-GO", event: "REQUEST_CHANGES"})
}
