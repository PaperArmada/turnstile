# Security Policy

## Supported versions

Turnstile is pre-1.0. Security fixes are made against the latest release and
the `main` branch. There is no back-porting to earlier `0.x` tags.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's ["Report a vulnerability" flow](https://github.com/PaperArmada/turnstile/security/advisories/new)
(the **Security** tab → **Advisories** → **Report a vulnerability**). This
opens a private advisory visible only to you and the maintainer.

Please include:

- what you observed, and the impact you think it has;
- a minimal reproduction (a process definition, gate, or command sequence);
- the version or commit you tested against.

This is a solo-maintained project with burst availability. Expect an
acknowledgement on a best-effort basis; a fix for a confirmed, high-impact
issue is prioritized ahead of feature work.

## What is in scope

Turnstile executes shell commands: **validation gates**, **notification
hooks**, and any command a process definition runs. Treat the following as
the trust boundary:

- **A process definition is code.** Installing or running a definition someone
  else authored runs their shell commands on your machine, on every gate and
  notification. Review definitions from third parties the way you would review
  a shell script before running it. The bundled starter pack is
  maintainer-authored.
- **Parameter, signal, and notification values are passed to gate and hook
  commands through the environment**, not interpolated into command text, so a
  value cannot inject shell syntax into a command. Gate and hook command
  *templates themselves* are trusted (they come from the definition).
- **Enforcement scope.** The Claude Code guard gates the Edit and Write tools
  at the hook layer. It does not gate the Bash tool and does not parse shell
  command contents, so a command that mutates a file (`sed -i`, `tee`, a shell
  redirect, `python -c`) runs outside enforcement. Enforcement is one
  cooperative layer over a trusted agent, not a sandbox.

Reports that amount to "a process definition I wrote can run a command I put in
it" are working as designed, not vulnerabilities. Reports of one actor's
supplied *values* escaping into another actor's gate command, of enforcement
being bypassed within its stated scope, or of state corruption silently
disabling enforcement, are in scope.
