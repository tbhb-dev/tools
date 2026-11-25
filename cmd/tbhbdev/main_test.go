package main

import (
	"bytes"
	"errors"
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
	link := filepath.Join(search, "tbhbdev")
	if err := os.Symlink(front, link); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(search, "relative"), 0o700); err != nil {
		t.Fatal(err)
	}
	check := func(args []string, status int, wantOut, wantErr string) {
		t.Helper()
		cmd := exec.Command(link, append([]string{"-test.run=^TestFront$", "--"}, args...)...)
		cmd.Dir = search
		cmd.Env = append(os.Environ(), "PATH=relative"+string(os.PathListSeparator)+search, "TBHBDEV_TEST_FRONT=1",
			"TBHBDEV_PROG=old", "TBHBDEV_BUNDLE_VERSION=old", "FRONT_TEST_VALUE=kept")
		cmd.Stdin = strings.NewReader("stdin\n")
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		out, err := cmd.Output()
		var exit *exec.ExitError
		if err != nil && !errors.As(err, &exit) {
			t.Fatal(err)
		}
		if code := cmd.ProcessState.ExitCode(); code != status || string(out) != wantOut || stderr.String() != wantErr {
			t.Fatalf("tbhbdev %q = (%d, %q, %q), want (%d, %q, %q)", args, code, out, stderr.String(), status, wantOut, wantErr)
		}
	}

	check([]string{"--version"}, 0, "tbhbdev dev\n", "")
	writeFile(t, search, "tbhbdev-py", nil, 0o700)
	writeFile(t, filepath.Join(search, "relative"), "tbhbdev-other", nil, 0o700)
	check([]string{"other"}, 127, "", "tbhbdev: tbhbdev-py: executable not found\n")
	writeFile(t, bundle, "tbhbdev-a", nil, 0o700)

	script := []byte(`#!/bin/sh
read -r input
printf '<%s>\n' "$TBHBDEV_PROG" "$TBHBDEV_BUNDLE_VERSION" "$FRONT_TEST_VALUE" "$input" "$@"
printf 'stderr\n' >&2
exit 23
`)
	writeFile(t, bundle, "tbhbdev-py", script, 0o700)
	writeFile(t, bundle, "tbhbdev-a-b", script, 0o700)
	writeFile(t, bundle, "tbhbdev-", nil, 0o700)
	if err := os.Chmod(bundle, 0o300); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(bundle, 0o700)
	check([]string{"a", "b"}, 126, "", "tbhbdev a: exec format error\n")
	check([]string{"a-b", "two words", ""}, 23, "<tbhbdev a-b>\n<dev>\n<kept>\n<stdin>\n<two words>\n<>\n", "stderr\n")
	check([]string{"py", ""}, 23, "<tbhbdev>\n<dev>\n<kept>\n<stdin>\n<py>\n<>\n", "stderr\n")
	check([]string{""}, 23, "<tbhbdev>\n<dev>\n<kept>\n<stdin>\n<>\n", "stderr\n")
}

func TestRunErrorsAndPrint(t *testing.T) {
	dir := t.TempDir()
	front := writeFile(t, dir, "tbhbdev", nil, 0o700)
	writeFile(t, dir, "tbhbdev-invalid", nil, 0o700)
	writeFile(t, dir, "tbhbdev-file", nil, 0o600)
	if err := os.Mkdir(filepath.Join(dir, "tbhbdev-dir"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TBHBDEV_PROG", "old")
	t.Setenv("TBHBDEV_BUNDLE_VERSION", "old")
	for _, tc := range []struct {
		arg, executable string
		want            int
	}{
		{"--version", front, 0},
		{"invalid", front, 126},
		{"file", front, 127},
		{"dir", front, 127},
		{"missing", front, 127},
		{"invalid", filepath.Join(dir, "missing"), 127},
		{"invalid", "tbhbdev", 127},
	} {
		if got := run([]string{tc.arg}, tc.executable); got != tc.want {
			t.Errorf("run(%q, %q) = %d, want %d", tc.arg, tc.executable, got, tc.want)
		}
	}
}
