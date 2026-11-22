---
schema: 1
reviewer: standard
lenses: [correctness, security]
escalate:
  - id: FE1
    class: law
    reason: The change may cause foreseeable harm to property or people, expose tbhb, their hosts or networks to attack or legal liability, expose a credential, spend money or work around a limit, add a payment method or API key, or weaken, hide or work around a stop or the means to stop an agent.
  - id: FE2
    class: authority
    reason: The change adds, removes or widens anyone's authority over decisions, an agent's included, or touches tbhb's own data beyond the access given.
  - id: FE3
    class: reserved
    reason: The change decides a matter reserved for tbhb, namely changing the Laws, irreversibly destroying anything tbhb-dev didn't create, or destroying the last working copy of anything.
  - id: FE4
    class: weakens-review
    reason: The change weakens the control that reviews it, such as review files, rulesets, CI gates, the broker's rules or this floor.
  - id: FE5
    class: conflicts-with-decision
    reason: The change contradicts a recorded decision of tbhb.
  - id: FE6
    class: credential-grant
    reason: A new credential or a wider grant is a one-way door.
  - id: FE7
    class: network-boundary
    reason: A firewall or NAS change, or loosening a trust boundary, is a one-way door.
  - id: FE8
    class: spend
    reason: A change that spends or commits to spending is a one-way door.
  - id: FE9
    class: public-release
    reason: A public release can't be taken back.
---

# Pull request review checklist: the org floor

<!-- The floor reserves item IDs R1 to R99. Floor escalation IDs start with FE, and a repository's own start with E. A repository's checklist adds items from R100 and escalation rules, and can't remove or reword any of these. -->

## Context

This is the organization's floor. It applies to every repository, and a repository's own checklist adds to it. The authority behind items R10 to R13 is the tbhb-dev constitution.

## Items

- **R1** The event fits the evidence: APPROVE only when the head's checks passed and no blocking issue remains. REQUEST_CHANGES names at least one blocking issue.
- **R2** A finding needs grounding. It cites a `path:line` in the diff, or a check and its result, and that code or result shows what the finding claims.
- **R3** The review covers the risky parts of the diff. It addresses or explicitly clears security, data loss, concurrency, public interfaces and anything irreversible.
- **R4** Verification reports only what the reviewer read and the check results it relied on, as they stand.
- **R5** The review claims only what the evidence shows. It never reports a test pass, a file read or a behavior check without a trace in the evidence.
- **R6** Reserved for inline comments, which v1 leaves out. The reviewer reports this item as `not_applicable` until inline comments exist.
- **R7** Findings list only blocking issues, and Suggestions list only non-blocking ones.
- **R8** The pull request's description matches the diff. A mismatch is a finding.
- **R9** The output lists every escalation rule that applies, with the reason for each.
- **R10** Laws (constitution section 2). The change can't cause foreseeable harm to property or people. It doesn't expose tbhb, their hosts or networks to attack or legal liability, and doesn't expose a credential. It doesn't spend money or work around a limit, and it doesn't add a payment method or API key. It doesn't weaken, hide or work around a stop or the means to stop an agent. A change that may do any of these escalates under `law`.
- **R11** The operator (constitution section 3). The change doesn't add, remove or widen anyone's authority, an agent's included. It touches none of tbhb's own data beyond the access given, and decides no reserved matter. The constitution reserves exactly these for tbhb: changing the Laws, irreversibly destroying anything tbhb-dev didn't create, and destroying the last working copy of anything. Widening authority escalates under `authority`, and a reserved matter under `reserved`.
- **R12** Change (constitution section 5). The change doesn't weaken the control that reviews it, including review files, rulesets, CI gates, the broker's rules and this floor. A weakening escalates under `weakens-review`.
- **R13** Evidence (constitution section 4). Every claim in the pull request says how its author knows it. The change cites authority for what it does, such as a task, a role, a decision or the silence of the rules. A gap is a finding. It escalates only when the authority would have to come from tbhb.

## Escalation

The escalation rules above describe what a change does, and the reviewer applies them to the diff. Path patterns never trigger one. These are the only route to tbhb.

## Out of scope

The floor doesn't judge style that a linter checks, or a repository's own conventions. A repository's checklist covers those.
