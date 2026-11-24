// cspell:ignore Submatch
package content

import (
	"list"
	"regexp"
	"strconv"
	"strings"
)

#ReviewHost:    #"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"#
#ReviewSHA:     #"[0-9a-f]{40}"#
#ReviewHeader:  #"^Run: (\S+)\nProvider: (\S+)\nModel: (\S+)\nVariant: ([ab])\nHost: (\#(#ReviewHost))\nHead: (\#(#ReviewSHA))\nTier: ([012])$"#
#ReviewVerdict: "GO" | "NO-GO"
#FindingClass:  "BLOCK" | "TECH-DEBT" | "SECURITY-DEBT"
#FindingPrefix: #"^(\S+)\s+([^\s:]+):([1-9][0-9]*)\s+([\s\S]+)$"#
#SentenceEnd:   #"([.!?]+) "#
#Word:          #"[\p{L}\p{N}_]+"#

#ReviewSentence: =~#Word & =~#"[.!?]$"#

#ReviewFinding: #ListItem & {
	blocks: [#Paragraph]
	visible:    string
	checked?:   error("Use a bullet without a task checkbox")
	_parts:     regexp.FindSubmatch(#FindingPrefix, visible)
	_class:     _parts[1] & #FindingClass
	_prose:     strings.Join(strings.Fields(_parts[4]), " ")
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

#ReviewBody: #Draft & {
	object:  "pr-review"
	variant: "review"
	blockOrder: ["preamble", "verdict", "findings", "verification"]
	blocks: close({
		preamble: {
			kind: "text"
			content: [#Paragraph & {visible: =~#ReviewHeader}]
		}
		verdict: {
			kind:    "section"
			heading: "Verdict"
			content: [#Paragraph & {visible: #ReviewVerdict}]
		}
		findings: {
			kind:    "section"
			heading: "Findings"
			content: [] | [#GFMTextValue & #UnorderedList & {
				items: [...#ReviewFinding] & list.MaxItems(8)
			}]
		}
		verification: {
			kind:    "section"
			heading: "Verification"
			content: [#Paragraph, ...#Paragraph]
			metrics: visible: words: <=100
		}
	})
	_findings: [for group in blocks.findings.content for item in group.items {item}]
	_blockCount: len([for item in _findings if item._class == "BLOCK" {item}])
	if blocks.verdict.content[0].visible == "NO-GO" && _blockCount == 0 {
		_err: error("NO-GO requires at least one BLOCK finding")
	}
	if blocks.verdict.content[0].visible == "GO" && _blockCount > 0 {
		_err: error("GO forbids BLOCK findings")
	}
}

#ReviewMetadata: close({
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
	findings!: [...close({
		class!:   #FindingClass
		path!:    =~#"^[^\s:]+$"#
		line!:    int & >=1
		summary!: string & =~#Word
		_words:   len(regexp.FindAll(#Word, summary, -1)) & <=160
	})] & list.MaxItems(8)
})

#Review: Body={
	#ReviewBody
	metadata: #ReviewMetadata
	let header = regexp.FindSubmatch(#ReviewHeader, Body.blocks.preamble.content[0].visible)

	// Match supplied metadata without filling in any missing fields.
	_matchingMetadata: metadata & {
		verdict:  Body.blocks.verdict.content[0].visible
		head_sha: header[6]
		tier:     strconv.Atoi(header[7])
		run: {
			name:     header[1]
			provider: header[2]
			model:    header[3]
			variant:  header[4]
			host:     header[5]
		}
		findings: [for item in Body._findings {
			class:   item._class
			path:    item._parts[2]
			line:    strconv.Atoi(item._parts[3])
			summary: item._prose
		}]
	}
}
