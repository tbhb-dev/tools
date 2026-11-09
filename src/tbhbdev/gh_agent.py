"""Where the agent's own gh config lives, relative to its home directory.

`host` builds its `GH_CONFIG_DIR` shell word from this, and `alpha repo
bootstrap` builds the same directory for the gh it runs, so both name one place.
"""

# The gh config of the agent's GitHub identity. It holds no credential; gitleaks
# flags any path into .config.
CONFIG_DIR = ".config/gh-agent"  # gitleaks:allow
