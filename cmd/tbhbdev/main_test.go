package main

import (
	"bytes"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func writeFile(t *testing.T, dir, name string, body []byte, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, body, mode); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestFront(t *testing.T) {
	if os.Getenv("TBHBDEV_TEST_FRONT") == "1" {
		os.Args = append(os.Args[:1], os.Args[3:]...)
		main()
		return
	}

	bundle, search := t.TempDir(), t.TempDir()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	binary, err := os.ReadFile(executable)
	if err != nil {
		t.Fatal(err)
	}
	front := writeFile(t, bundle, "tbhbdev", binary, 0o700)
	link := filepath.Join(t.TempDir(), "tbhbdev")
	if err := os.Symlink(front, link); err != nil {
		t.Fatal(err)
	}
	check := func(args []string, status int, wantOut, wantErr string) {
		t.Helper()
		cmd := exec.Command(link, append([]string{"-test.run=^TestFront$", "--"}, args...)...)
		cmd.Env = append(os.Environ(), "PATH="+search, "TBHBDEV_TEST_FRONT=1",
			"TBHBDEV_PROG=old", "TBHBDEV_BUNDLE_VERSION=old", "FRONT_TEST_VALUE=kept")
		cmd.Stdin = strings.NewReader("stdin\n")
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		out, err := cmd.Output()
		code := cmd.ProcessState.ExitCode()
		if err != nil && code <= 0 {
			t.Fatal(err)
		}
		if code != status || string(out) != wantOut || stderr.String() != wantErr {
			t.Fatalf("tbhbdev %q = (%d, %q, %q), want (%d, %q, %q)", args, code, out, stderr.String(), status, wantOut, wantErr)
		}
	}

	check([]string{"--version"}, 0, "tbhbdev dev\n", "")
	check([]string{"unknown"}, 127, "", "tbhbdev: tbhbdev-py: executable not found\n")
	writeFile(t, search, "tbhbdev-py", nil, 0o700)
	check([]string{"unknown"}, 126, "", "tbhbdev: exec format error\n")
	writeFile(t, search, "tbhbdev-py", []byte("#!/bin/sh\nprintf 'PATH\\n'\n"), 0o700)
	check([]string{"unknown"}, 0, "PATH\n", "")

	script := []byte("#!/bin/sh\nread -r input\nprintf '<%s>\\n' \"$TBHBDEV_PROG\" \"$TBHBDEV_BUNDLE_VERSION\" \"$FRONT_TEST_VALUE\" \"$input\" \"$@\"\nprintf 'stderr\\n' >&2\nexit 23\n")
	writeFile(t, bundle, "tbhbdev-py", script, 0o700)
	check([]string{"py", "two words", ""}, 23, "<tbhbdev>\n<dev>\n<kept>\n<stdin>\n<py>\n<two words>\n<>\n", "stderr\n")
	writeFile(t, search, "tbhbdev-agent", nil, 0o700)
	check([]string{"agent"}, 126, "", "tbhbdev agent: exec format error\n")
	writeFile(t, bundle, "tbhbdev-agent-exec", script, 0o700)
	check([]string{"agent", "exec", "two words", ""}, 23, "<tbhbdev agent exec>\n<dev>\n<kept>\n<stdin>\n<two words>\n<>\n", "stderr\n")
}

func TestFindCommands(t *testing.T) {
	bundle, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	search := t.TempDir()
	t.Chdir(search)
	front := writeFile(t, bundle, "tbhbdev", nil, 0o700)
	link := filepath.Join(search, "tbhbdev")
	if err := os.Symlink(front, link); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{"relative", "tbhbdev-directory", "unreadable"} {
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Chmod("unreadable", 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Chmod(filepath.Join(search, "unreadable"), 0o700); err != nil {
			t.Error(err)
		}
	})
	writeFile(t, "relative", "tbhbdev-relative", nil, 0o700)
	writeFile(t, search, "tbhbdev-", nil, 0o700)
	writeFile(t, search, "tbhbdev-data", nil, 0o600)
	if err := os.Symlink("missing", "tbhbdev-dangling"); err != nil {
		t.Fatal(err)
	}
	local := writeFile(t, bundle, "tbhbdev-agent", nil, 0o700)
	onPath := writeFile(t, search, "tbhbdev-agent", nil, 0o700)
	// The front itself also produces a read error when used as a PATH entry.
	path := strings.Join([]string{"relative", filepath.Join(search, "unreadable"), front, search}, string(os.PathListSeparator))
	for executable, want := range map[string]string{link: local, "tbhbdev-data": onPath, "missing": onPath} {
		if got := findCommands(executable, path); !maps.Equal(got, map[string]string{"agent": want}) {
			t.Errorf("findCommands(%q) = %v, want agent at %q", executable, got, want)
		}
	}
}
