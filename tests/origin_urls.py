"""Origin URLs for the tbhb-dev origin check, shared by its tests.

test_alpha_repo checks repo.is_tbhb_dev_url against these, and the identity
shim's parity test in test_alpha_install runs the shim's shell pattern over
the same rows, so the two checks must agree on every one.
"""

TBHB_DEV_URLS = [
    "git@github.com:tbhb-dev/tools.git",
    "git@github.com:tbhb-dev/tools",
    "git@github.com:tbhb-dev/tools/",
    "git@github.com:TBHB-DEV/Tools.git",
    "org-1@github.com:tbhb-dev/tools.git",
    "ssh://git@github.com/tbhb-dev/tools.git",
    "ssh://git@github.com:22/tbhb-dev/tools.git",
    "ssh://git@ssh.github.com:443/tbhb-dev/tools.git",
    "git+ssh://git@github.com/tbhb-dev/tools.git",
    "https://github.com/tbhb-dev/tools.git",
    "https://github.com/tbhb-dev/tools",
    "https://github.com/tbhb-dev/tools/",
    "https://x-access-token@github.com/tbhb-dev/tools.git",
    "HTTPS://GitHub.com/tbhb-dev/tools",
    "  https://github.com/tbhb-dev/tools\n",
    "\nhttps://github.com/tbhb-dev/tools\t",
    "https://github.com/tbhb-dev/t\u00f6ols",
]
OTHER_URLS = [
    "",
    "git@github.com:someone/tools.git",
    "git@github.com:tbhb-dev-fork/tools.git",
    "git@github.com:tbhb-dev-evil/tools.git",
    "https://github.com/tbhb-dev-evil/tools",
    "git@github.com:xtbhb-dev/tools.git",
    "git@github.com:tbhb-dev/",
    "git@github.com:tbhb-dev/a/b",
    "git@gitlab.com:tbhb-dev/tools.git",
    "git@github.com.evil.example:tbhb-dev/tools.git",
    "git@evil.example:github.com:tbhb-dev/tools.git",
    "https://github.com.evil.example/tbhb-dev/tools",
    "https://evil.example/github.com/tbhb-dev/tools",
    "http://github.com/tbhb-dev/tools",
    "https://github.com/someone/tbhb-dev",
    "https://github.com/tbhb-dev/tools other",
    "ssh://git@github.com/someone/tbhb-dev/tools",
    "/srv/git/tbhb-dev/tools.git",
    "file:///github.com/tbhb-dev/tools",
    "github.com/tbhb-dev/tools",
    "git@github.com:tbhb-dev/tools.git\nx",
    "x\ngit@github.com:tbhb-dev/tools.git",
    "ssh://git@github.com:2a/tbhb-dev/tools.git",
    # Only ASCII counts: a non-ASCII digit, space or case fold doesn't match.
    "ssh://git@github.com:\u0662\u0662/tbhb-dev/tools.git",
    "\u00a0https://github.com/tbhb-dev/tools",
    "http\u017f://github.com/tbhb-dev/tools",
]
