package plugins

import (
	"errors"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestDiscover(t *testing.T) {
	bundle, first, second := t.TempDir(), t.TempDir(), t.TempDir()
	bundle, err := filepath.EvalSymlinks(bundle)
	if err != nil {
		t.Fatal(err)
	}
	write := func(dir, name string, mode os.FileMode) string {
		t.Helper()
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte("#!/bin/sh\n"), mode); err != nil {
			t.Fatal(err)
		}
		return path
	}
	front := write(bundle, "tbhbdev", 0o700)
	link := filepath.Join(t.TempDir(), "tbhbdev")
	if err := os.Symlink(front, link); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"agent":      write(bundle, "tbhbdev-agent", 0o700),
		"agent-exec": write(first, "tbhbdev-agent-exec", 0o700),
		"other":      write(second, "tbhbdev-other", 0o700),
	}
	write(first, "tbhbdev-agent", 0o700)
	write(second, "tbhbdev-agent-exec", 0o700)
	write(first, "tbhbdev-other", 0o600)
	write(bundle, "tbhbdev-py", 0o700)
	if got := Discover(link, first+string(os.PathListSeparator)+second); !maps.Equal(got, want) {
		t.Fatalf("Discover() = %v, want %v", got, want)
	}
}

func TestExec(t *testing.T) {
	if os.Getenv("TBHBDEV_EXEC_TEST") == "1" {
		if err := Exec(os.Args[len(os.Args)-1], "tbhbdev agent exec", "1.2.3", []string{"two words", ""}); err != nil {
			t.Fatal(err)
		}
		return
	}

	script := filepath.Join(t.TempDir(), "tbhbdev-agent-exec")
	body := "#!/bin/sh\nprintf '<%s>\\n' \"$TBHBDEV_PROG\" \"$TBHBDEV_BUNDLE_VERSION\" \"$@\"\nexit 23\n"
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestExec$", "--", script)
	cmd.Env = append(os.Environ(), "TBHBDEV_EXEC_TEST=1", "TBHBDEV_PROG=old", "TBHBDEV_BUNDLE_VERSION=old")
	out, err := cmd.CombinedOutput()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 23 {
		t.Fatalf("Exec() error = %v, want exit 23. Output: %s", err, out)
	}
	if want := "<tbhbdev agent exec>\n<1.2.3>\n<two words>\n<>\n"; string(out) != want {
		t.Fatalf("Exec() output = %q, want %q", out, want)
	}
}
