"""The review tests use the same CUE release as the content checker."""

def _cue_test_tool_impl(repository_ctx):
    pin = json.decode(repository_ctx.read(repository_ctx.attr._pin))["cue"]
    os_name = repository_ctx.os.name
    arch = repository_ctx.os.arch
    if os_name == "mac os x" and arch in ["aarch64", "arm64"]:
        platform = "darwin_arm64"
    elif os_name == "linux" and arch in ["amd64", "x86_64"]:
        platform = "linux_amd64"
    elif os_name == "linux" and arch in ["aarch64", "arm64"]:
        platform = "linux_arm64"
    else:
        fail("No pinned CUE test tool for {} {}".format(os_name, arch))
    archive = pin["platforms"][platform]
    repository_ctx.download_and_extract(url = archive["url"], sha256 = archive["sha256"])
    repository_ctx.file("BUILD.bazel", 'exports_files(["cue"])\n')

cue_test_tool = repository_rule(
    implementation = _cue_test_tool_impl,
    attrs = {"_pin": attr.label(default = Label("//:src/tbhbdev/cue-pin.json"))},
)
