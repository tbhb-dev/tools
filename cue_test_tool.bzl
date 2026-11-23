"""Make the runtime's pinned CUE release available to the Bazel tests."""

def _cue_test_tool_impl(ctx):
    pin = json.decode(ctx.read(ctx.attr.pin))["cue"]
    system = "darwin" if ctx.os.name == "mac os x" else ctx.os.name
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(ctx.os.arch, ctx.os.arch)
    platform = "{}_{}".format(system, arch)
    entry = pin["platforms"].get(platform)
    if entry == None:
        fail("no pinned CUE archive for {}".format(platform))
    ctx.download_and_extract(url = entry["url"], sha256 = entry["sha256"])
    ctx.file("BUILD.bazel", 'exports_files(["cue"], visibility = ["//visibility:public"])\n')

cue_test_tool = repository_rule(
    implementation = _cue_test_tool_impl,
    attrs = {"pin": attr.label(allow_single_file = True, mandatory = True)},
)
