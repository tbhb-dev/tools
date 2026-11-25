"""Imports allowed in the dispatch package and its tests."""

load("@tbhb_rules_go//:defs.bzl", "golangci_lint_aspect_factory", "golangci_lint_test_factory")

golangci_lint = golangci_lint_aspect_factory(
    config = "//:.golangci.yml",
    go_mod = Label("//:go.mod"),
)

golangci_lint_test = golangci_lint_test_factory(golangci_lint)
