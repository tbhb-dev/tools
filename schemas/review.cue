// cspell:ignore Submatch
package content

import (
	"list"
	"regexp"
	"strings"
)

#ReviewHeader:  #"^Run: \S+\nProvider: \S+\nModel: \S+\nHead: [0-9a-fA-F]{40}\nTier: [123]$"#
#FindingPrefix: #"^(BLOCK|TECH-DEBT|SECURITY-DEBT)\s+[^\s:]+:[1-9][0-9]*\s+([\s\S]+)$"#
#SentenceEnd:   #"([.!?]+) "#
#Word:          #"[\p{L}\p{N}_]+"#

#ReviewSentence: =~#Word & =~#"[.!?]$"#

#ReviewFinding: #ListItem & {
	blocks: [#Paragraph]
	visible:    string
	checked?:   error("Use a bullet without a task checkbox")
	_parts:     regexp.FindSubmatch(#FindingPrefix, visible)
	_class:     _parts[1]
	_prose:     strings.Join(strings.Fields(_parts[2]), " ")
	_words:     len(regexp.FindAll(#Word, _prose, -1)) & <=40
	_sentences: strings.Split(regexp.ReplaceAll(#SentenceEnd, _prose, "$1\n"), "\n")
	_sentences: ([_] | [_, _]) & [...#ReviewSentence]
	for sentence in _sentences {
		// Check the same CI gate pattern regardless of terminal punctuation.
		let prose = strings.TrimSpace(strings.TrimRight(sentence, ".!?"))
		if prose =~ #CIRestatement {
			_err: error("A finding may not restate a CI gate")
		}
	}
}

#Review: #Draft & {
	object:  "pr-review"
	variant: "review"
	metadata: close({})
	blockOrder: ["preamble", "verdict", "findings"]
	blocks: close({
		preamble: {
			kind: "text"
			content: [#Paragraph & {visible: =~#ReviewHeader}]
		}
		verdict: {
			kind:    "section"
			heading: "Verdict"
			content: [#Paragraph & {visible: "GO" | "HOLD"}]
		}
		findings: {
			kind:    "section"
			heading: "Findings"
			content: [] | [#GFMTextValue & #UnorderedList & {
				items: [...#ReviewFinding] & list.MaxItems(8)
			}]
		}
	})
	_findings: [for group in blocks.findings.content for item in group.items {item}]
	_blockCount: len([for item in _findings if item._class == "BLOCK" {item}])
	if blocks.verdict.content[0].visible == "HOLD" && _blockCount == 0 {
		_err: error("HOLD requires at least one BLOCK finding")
	}
	if blocks.verdict.content[0].visible == "GO" && _blockCount > 0 {
		_err: error("GO forbids BLOCK findings")
	}
	_totalWords: list.Sum([for item in _findings {item._words}]) & <=160
}
