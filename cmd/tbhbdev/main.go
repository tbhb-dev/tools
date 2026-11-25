package main

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"

	"go.tbhb.dev/tools/internal/dispatch"
)

var bundleVersion = "dev"

const pluginPrefix = "tbhbdev-"

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	executable, _ := os.Executable()
	paths := findCommands(executable, os.Getenv("PATH"))
	pythonName := strings.TrimPrefix(dispatch.Python, pluginPrefix)
	fallback := paths[pythonName]
	delete(paths, pythonName)
	available := make(map[string]struct{}, len(paths))
	for name := range paths {
		available[name] = struct{}{}
	}

	decision := dispatch.Decide(args, available, bundleVersion)
	if decision.Action == dispatch.Print {
		fmt.Print(decision.Text)
		return 0
	}
	path := fallback
	if decision.Action == dispatch.Plugin {
		path = paths[decision.Name]
	}
	if path == "" {
		fmt.Fprintf(os.Stderr, "tbhbdev: %s: executable not found\n", decision.Name)
		return 127
	}
	env := slices.DeleteFunc(os.Environ(), func(entry string) bool {
		key, _, _ := strings.Cut(entry, "=")
		return key == "TBHBDEV_PROG" || key == "TBHBDEV_BUNDLE_VERSION"
	})
	env = append(env, "TBHBDEV_PROG="+decision.Prog, "TBHBDEV_BUNDLE_VERSION="+bundleVersion)
	err := syscall.Exec(path, append([]string{path}, decision.Args...), env)
	fmt.Fprintf(os.Stderr, "%s: %v\n", decision.Prog, err)
	return 126
}

func findCommands(executable, path string) map[string]string {
	bundleDir := ""
	if resolved, err := filepath.EvalSymlinks(executable); err == nil && filepath.IsAbs(resolved) {
		bundleDir = filepath.Dir(resolved)
	}
	found := map[string]string{}
	for _, dir := range dispatch.SearchDirs(bundleDir, filepath.SplitList(path)) {
		if !filepath.IsAbs(dir) {
			continue
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			name, match := strings.CutPrefix(entry.Name(), pluginPrefix)
			if !match || name == "" || found[name] != "" {
				continue
			}
			candidate := filepath.Join(dir, entry.Name())
			if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0 {
				found[name] = candidate
			}
		}
	}
	return found
}
