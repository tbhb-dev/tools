package dispatch

import (
	"reflect"
	"testing"
)

func TestDecide(t *testing.T) {
	for _, tc := range []struct {
		name      string
		args      []string
		candidate string
		available string
		want      Decision
	}{
		{"no arguments", nil, "", "", Decision{Action: Print, Text: help}},
		{"help", []string{"--help"}, "", "", Decision{Action: Print, Text: help}},
		{"short help", []string{"-h"}, "", "", Decision{Action: Print, Text: help}},
		{"version", []string{"--version"}, "", "", Decision{Action: Print, Text: "tbhbdev dev\n"}},
		{"short version", []string{"-V"}, "", "", Decision{Action: Print, Text: "tbhbdev dev\n"}},
		{"first word", []string{"a", "b", ""}, "tbhbdev-a", "tbhbdev-a", Decision{Plugin, "tbhbdev-a", []string{"b", ""}, "tbhbdev a", ""}},
		{"hyphenated word", []string{"a-b", "--help"}, "tbhbdev-a-b", "tbhbdev-a-b", Decision{Plugin, "tbhbdev-a-b", []string{"--help"}, "tbhbdev a-b", ""}},
		{"no nesting", []string{"a", "b"}, "tbhbdev-a", "tbhbdev-a-b", Decision{Fallback, Python, []string{"a", "b"}, "tbhbdev", ""}},
		{"empty argument", []string{""}, "", "", Decision{Fallback, Python, []string{""}, "tbhbdev", ""}},
		{"reserved name", []string{"py"}, "", Python, Decision{Fallback, Python, []string{"py"}, "tbhbdev", ""}},
		{"leading flag", []string{"--repo"}, "", "", Decision{Fallback, Python, []string{"--repo"}, "tbhbdev", ""}},
		{"path separator", []string{"a/b"}, "", "", Decision{Fallback, Python, []string{"a/b"}, "tbhbdev", ""}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := PluginName(tc.args); got != tc.candidate {
				t.Errorf("PluginName(%q) = %q, want %q", tc.args, got, tc.candidate)
			}
			if got := Decide(tc.args, tc.available, "dev"); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("Decide(%q, %q) = %+v, want %+v", tc.args, tc.available, got, tc.want)
			}
		})
	}
}
