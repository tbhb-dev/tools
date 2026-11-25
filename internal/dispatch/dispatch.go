// Package dispatch resolves command lines and defines the plugin search order.
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

// Decide matches the longest installed plugin name at the start of args.
func Decide(args []string, available map[string]struct{}, version string) Decision {
	if len(args) == 0 {
		return Decision{Action: Print, Text: help}
	}
	switch args[0] {
	case "-h", "--help":
		return Decision{Action: Print, Text: help}
	case "-V", "--version":
		return Decision{Action: Print, Text: "tbhbdev " + version + "\n"}
	}

	maxNameLen := 0
	for name := range available {
		maxNameLen = max(maxNameLen, len(name))
	}
	lastMatch := 0
	matchedName := ""
	var name strings.Builder
	for index, arg := range args {
		if index > 0 {
			name.WriteByte('-')
		}
		if name.Len()+len(arg) > maxNameLen || arg == "" || strings.HasPrefix(arg, "-") {
			break
		}
		name.WriteString(arg)
		if _, found := available[name.String()]; found {
			lastMatch = index + 1
			matchedName = name.String()
		}
	}
	if lastMatch > 0 {
		return Decision{
			Action: Plugin, Name: matchedName, Args: args[lastMatch:],
			Prog: "tbhbdev " + strings.Join(args[:lastMatch], " "),
		}
	}
	return Decision{Action: Fallback, Name: Python, Args: args, Prog: "tbhbdev"}
}

// SearchDirs places the bundle directory before PATH entries.
func SearchDirs(bundleDir string, pathDirs []string) []string {
	if bundleDir == "" {
		return pathDirs
	}
	return append([]string{bundleDir}, pathDirs...)
}
