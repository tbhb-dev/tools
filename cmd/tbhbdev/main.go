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

func main() {
	executable, _ := os.Executable()
	os.Exit(run(os.Args[1:], executable))
}

func run(args []string, executable string) int {
	dir := bundleDir(executable)
	name := dispatch.PluginName(args)
	path := launchPath(dir, name)
	if path == "" {
		name = ""
	}
	decision := dispatch.Decide(args, name, bundleVersion)
	if decision.Action == dispatch.Print {
		fmt.Print(decision.Text)
		return 0
	}
	if decision.Action == dispatch.Fallback {
		path = launchPath(dir, decision.Name)
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

func bundleDir(executable string) string {
	if !filepath.IsAbs(executable) {
		return ""
	}
	resolved, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return ""
	}
	return filepath.Dir(resolved)
}

func launchPath(dir, name string) string {
	if dir == "" || name == "" {
		return ""
	}
	path := filepath.Join(dir, name)
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		return ""
	}
	return path
}
