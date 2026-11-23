// Org fields and label families that variant schemas share.
package content

// Org Project and issue fields.
#Priority: "Expedite" | "Standard" | "Intangible"
#WorkType: "Planned" | "Unplanned"
#Severity: "SEV1" | "SEV2" | "SEV3"

// Set on Requests only.
#Handler: "Operator" | "Agent"

// area:<repo> names an org repository. CUE checks the spelling and the
// content module checks the repository against the live org list.
#AreaLabel: =~"^area:[a-z0-9][a-z0-9-]*$"

// role:<name> names the agent role that owns the issue. The content module
// checks the name against the role list kept outside this public file.
#RoleLabel: =~"^role:[a-z0-9][a-z0-9-]*$"

// Where a draft came from. A variant that sets it to Import is a tracker import.
#Source: "Import"

// Time limit for a Spike.
#Timebox: "2h" | "1d" | "3d"

// owner/repo#N. Whether the parent has the right issue type is a kernel check.
#IssueRef: =~"^[a-z0-9][a-z0-9-]*/[a-z0-9._-]+#[1-9][0-9]*$"
