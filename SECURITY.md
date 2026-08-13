# Security Policy

## Supported versions

PerfStudio is pre-alpha and has not had a tagged release yet. Only `main` is supported.
When releases begin, this section will name the versions that receive fixes.

## Reporting a vulnerability

**Please do not open a public issue.**

Report privately through GitHub:
[open a security advisory](https://github.com/medinstech/perfstudio/security/advisories/new).

Please include what you were doing, what happened, and — if it involves a file — the
smallest input that still reproduces it. You will get an acknowledgement, and a fix or an
explanation of why it is not one.

## What the realistic attack surface is

PerfStudio is a desktop application with no network service, no accounts and no
credentials. It is worth being specific about where untrusted input can actually reach,
because that is where a report is most likely to be a real finding:

- **`.perf` documents.** `persist.py` parses project files, including files a user was
  sent by somebody else. A hand-edited or malicious document should be *refused or
  reported*, never crash the application or escape the document model. Note that an
  invalid orthogonal chain in a solder trace loads with a warning and is reported by DRC
  by design — that is not a vulnerability, it is a decision not to lock a user out of
  their own project.
- **KiCad netlists.** `parsers/sexpr.py` and `parsers/kicad.py` parse text from outside
  the application.
- **Exported build guides.** `guide_export.py` writes HTML containing document, net and
  component names. Those names come from imported netlists, so they are untrusted input;
  everything interpolated into the HTML is escaped, and a way past that escaping is a
  genuine finding.
- **The MCP server.** `python -m perfstudio.mcp` speaks stdio by default, which has no
  listening socket. `--http` starts a streamable-HTTP server with no authentication of
  its own — it is meant for attaching a local client to an already-open session, and
  should not be exposed beyond the machine it runs on.

Out of scope: the retired TypeScript engine in `packages/`, which is kept as a reference
fixture and is not shipped or executed by the application; and anything in `prototypes/`.
