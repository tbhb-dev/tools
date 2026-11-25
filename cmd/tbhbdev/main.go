package main

import (
	"fmt"
	"os"
	"os/exec"

	"go.tbhb.dev/tools/internal/dispatch"
	"go.tbhb.dev/tools/internal/plugins"
)

var bundleVersion = "dev"

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	executable, _ := os.Executable()
	paths := plugins.Discover(executable, os.Getenv("PATH"))
	available := make(map[string]struct{}, len(paths))
	for name := range paths {
		available[name] = struct{}{}
	}

	decision := dispatch.Decide(args, available, bundleVersion)
	if decision.Action == dispatch.Print {
		fmt.Print(decision.Text)
		return 0
	}
	path := paths[decision.Name]
	if decision.Action == dispatch.Fallback {
		var err error
		path, err = exec.LookPath(decision.Name)
		if err != nil {
			fmt.Fprintf(os.Stderr, "tbhbdev: %v\n", err)
			return 127
		}
	}
	if err := plugins.Exec(path, decision.Prog, bundleVersion, decision.Args); err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", decision.Prog, err)
	}
	return 126
}
