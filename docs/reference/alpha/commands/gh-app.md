---
description: GitHub App installation tokens read through Connect.
status: stable
tags: [alpha]
title: GitHub App token
type: Reference
---

# GitHub App token

`gh-app` requests a GitHub App installation token, reading the App's id and RSA key through Connect, and keeps it under `$XDG_STATE_HOME/gh-app/VAULT/APP`.
`--permissions NAME:read,NAME:write` narrows the token to those permissions, such as `contents:read`, and keeps it in its own directory, so a narrowed token and a full one never replace each other.
The command serves a cached token with more than ten minutes left, even during a cooldown, and a cooldown refuses any other request.
Only a 404 sends the installation lookup from the repository to its owner.
The command syncs each file to disk before it replaces the old one.
