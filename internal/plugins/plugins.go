// Package plugins discovers external commands and replaces the front process.
package plugins

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"

	"go.tbhb.dev/tools/internal/dispatch"
)

// Discover maps plugin names to the first executable found for each name.
func Discover(executable, path string) map[string]string {
	bundleDir := ""
	if resolved, err := filepath.EvalSymlinks(executable); err == nil && filepath.IsAbs(resolved) {
		bundleDir = filepath.Dir(resolved)
	}
	found := map[string]string{}
	for _, dir := range dispatch.SearchDirs(bundleDir, filepath.SplitList(path)) {
		// Like exec.LookPath, avoid resolving commands through relative PATH entries.
		if !filepath.IsAbs(dir) {
			continue
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			name, match := strings.CutPrefix(entry.Name(), "tbhbdev-")
			if !match || name == "" || entry.Name() == dispatch.Python || found[name] != "" {
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

// Exec passes the command display name and bundle version to the new process.
func Exec(path, prog, version string, args []string) error {
	env := slices.DeleteFunc(os.Environ(), func(entry string) bool {
		key, _, _ := strings.Cut(entry, "=")
		return key == "TBHBDEV_PROG" || key == "TBHBDEV_BUNDLE_VERSION"
	})
	env = append(env, "TBHBDEV_PROG="+prog, "TBHBDEV_BUNDLE_VERSION="+version)
	return syscall.Exec(path, append([]string{path}, args...), env)
}
