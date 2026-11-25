package dispatch

import (
	"slices"
	"testing"
)

func TestDecide(t *testing.T) {
	for _, tc := range []struct {
		name, version string
		args, plugins []string
		want          Decision
	}{
		{name: "no arguments", want: Decision{Action: Print, Text: help}},
		{name: "help", args: []string{"--help"}, want: Decision{Action: Print, Text: help}},
		{name: "short help", args: []string{"-h"}, want: Decision{Action: Print, Text: help}},
		{name: "version", args: []string{"--version"}, version: "1.2.3", want: Decision{Action: Print, Text: "tbhbdev 1.2.3\n"}},
		{name: "short version", args: []string{"-V"}, version: "dev", want: Decision{Action: Print, Text: "tbhbdev dev\n"}},
		{
			name: "longest match", args: []string{"agent", "exec", "X"}, plugins: []string{"agent", "agent-exec"},
			want: Decision{Action: Plugin, Name: "agent-exec", Args: []string{"X"}, Prog: "tbhbdev agent exec"},
		},
		{
			name: "shorter match", args: []string{"agent", "exec", "X"}, plugins: []string{"agent"},
			want: Decision{Action: Plugin, Name: "agent", Args: []string{"exec", "X"}, Prog: "tbhbdev agent"},
		},
		{
			name: "hyphenated name", args: []string{"agent-exec"}, plugins: []string{"agent-exec"},
			want: Decision{Action: Plugin, Name: "agent-exec", Prog: "tbhbdev agent-exec"},
		},
		{
			name: "plugin options", args: []string{"agent", "--version"}, plugins: []string{"agent", "agent---version"},
			want: Decision{Action: Plugin, Name: "agent", Args: []string{"--version"}, Prog: "tbhbdev agent"},
		},
		{
			name: "full fallback", args: []string{"agent", "exec", "X"}, plugins: []string{"agent-exec-other"},
			want: Decision{Action: Fallback, Name: "tbhbdev-py", Args: []string{"agent", "exec", "X"}, Prog: "tbhbdev"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			available := make(map[string]struct{}, len(tc.plugins))
			for _, name := range tc.plugins {
				available[name] = struct{}{}
			}
			got := Decide(tc.args, available, tc.version)
			if got.Action != tc.want.Action || got.Name != tc.want.Name || got.Prog != tc.want.Prog ||
				got.Text != tc.want.Text || !slices.Equal(got.Args, tc.want.Args) {
				t.Fatalf("Decide() = %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestSearchDirs(t *testing.T) {
	for _, tc := range []struct {
		bundle string
		want   []string
	}{
		{"bundle", []string{"bundle", "first", "second"}},
		{"", []string{"first", "second"}},
	} {
		got := SearchDirs(tc.bundle, []string{"first", "second"})
		if !slices.Equal(got, tc.want) {
			t.Errorf("SearchDirs(%q) = %q, want %q", tc.bundle, got, tc.want)
		}
	}
}
