// Package dispatch resolves command lines.
package dispatch

import "strings"

// Action tells the front how to handle a command line.
type Action int

const (
	Fallback Action = iota
	Plugin
	Print
)

// Decision describes the command to run or the text to print.
type Decision struct {
	Action Action
	Name   string
	Args   []string
	Prog   string
	Text   string
}

// Python is the console script for commands still implemented in Python.
const Python = "tbhbdev-py"

const help = "Usage: tbhbdev <command> [args...]\n       tbhbdev --version\n"

// PluginName gives the candidate executable name for the first argument.
func PluginName(args []string) string {
	if len(args) == 0 {
		return ""
	}
	word := args[0]
	if word == "" || word == "py" || strings.HasPrefix(word, "-") || strings.ContainsAny(word, "/\\") {
		return ""
	}
	return "tbhbdev-" + word
}

// Decide routes args given the name of the executable plugin, if available.
func Decide(args []string, available, version string) Decision {
	if len(args) == 0 {
		return Decision{Action: Print, Text: help}
	}
	switch args[0] {
	case "-h", "--help":
		return Decision{Action: Print, Text: help}
	case "-V", "--version":
		return Decision{Action: Print, Text: "tbhbdev " + version + "\n"}
	}
	if available != "" && available == PluginName(args) {
		return Decision{Action: Plugin, Name: available, Args: args[1:], Prog: "tbhbdev " + args[0]}
	}
	return Decision{Action: Fallback, Name: Python, Args: args, Prog: "tbhbdev"}
}
