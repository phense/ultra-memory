# ultra_memory/maintenance/notify.py
"""Maintenance-failure notification — the consumer notifier seam (the "bring your own
send" stub).

ultra-memory ships NO transport. When a maintenance run records beat errors, the
consumer's notifier hook (config ``[maintenance] notifier = "module:function"``,
resolved like ``wiki_linter``) is called with a :class:`NotifyEvent`; absent or
unresolvable -> a one-line stderr no-op. The hook is invoked FAIL-OPEN: a notifier
error never wedges or fails a maintenance run.

OAuth-only invariant (project hard rule): a notifier MUST NOT import the ``anthropic``
SDK or use an API key. MCP-based delivery (Gmail/M365) is only reachable from a
headless cron via a ``claude -p`` bridge (the OAuth CLI); SMTP/webhook touch no
Anthropic surface at all. See :func:`example_notifier`.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from ultra_memory.maintenance._hooks import resolve_hook


@dataclass
class NotifyEvent:
    """A maintenance-failure notification payload. The consumer notifier may use the
    pre-built ``subject``/``body`` or the structured fields."""
    kind: str
    project: str
    run_ts: str
    errors: dict = field(default_factory=dict)   # beat -> repr(exc)
    ran: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    subject: str = ""
    body: str = ""


def build_event(config, result, ts: str) -> NotifyEvent:
    errors = dict(result.errors)
    subject = (f"{len(errors)} beat error(s): {', '.join(errors)}"
               if errors else "maintenance ok")
    lines = [f"Maintenance run {ts} on project {getattr(config.project_dir, 'name', config.project_dir)} "
             f"recorded {len(errors)} beat error(s):", ""]
    for beat, exc in errors.items():
        lines.append(f"  - {beat}: {exc}")
    lines += ["", f"ran: {', '.join(result.ran) or '(none)'}",
              f"skipped: {dict(result.skipped)}"]
    return NotifyEvent(
        kind="maintenance_failure",
        project=str(config.project_dir),
        run_ts=ts,
        errors=errors,
        ran=list(result.ran),
        skipped=dict(result.skipped),
        subject=subject,
        body="\n".join(lines),
    )


def _noop_notifier(event: NotifyEvent) -> None:
    """The shipped default: log one line, send nothing, never raise."""
    sys.stderr.write(
        f"[maintenance] {len(event.errors)} beat error(s); no notifier configured — "
        f"set [maintenance] notifier = 'yourmod:func' to be alerted\n")


def resolve_notifier(config):
    """The consumer notifier callable, or :func:`_noop_notifier` when unset/unresolvable."""
    return resolve_hook(config, getattr(config, "notifier", ""), "notifier") or _noop_notifier


def notify_failure(config, *, result, ts: str, log=print) -> None:
    """Fire the consumer notifier for a failed maintenance run. FAIL-OPEN: a notifier
    error degrades to one log line and is swallowed — alerting MUST NOT wedge or fail a
    maintenance run. No-ops when ``result.errors`` is empty."""
    if not result.errors:
        return
    notifier = resolve_notifier(config)
    event = build_event(config, result, ts)
    try:
        notifier(event)
    except Exception as exc:  # noqa: BLE001 — alerting must never fail the run
        try:
            log(f"notifier {getattr(notifier, '__name__', notifier)!r} failed: {exc!r}")
        except Exception:
            pass


def example_notifier(event: NotifyEvent) -> None:
    """COPY ME. A reference notifier template — copy into your own module, wire ONE
    transport, then set ``[maintenance] notifier = "yourmod:yourfunc"``.

    The maintenance pipeline runs as a HEADLESS cron, so the transport must work
    without an interactive Claude session. Pick one:

    (1) SMTP (works headless, stdlib only)::

            import smtplib
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["From"], msg["To"] = "bot@you.tld", "you@you.tld"
            msg["Subject"] = f"[maintenance] {event.subject}"
            msg.set_content(event.body)
            with smtplib.SMTP("smtp.you.tld", 587) as s:
                s.starttls(); s.login(user, pw); s.send_message(msg)

    (2) Shell out to a CLI (your existing mailer / any language)::

            import subprocess
            subprocess.run(["my-mailer", "--to", "you@you.tld",
                            "--subject", f"[maintenance] {event.subject}",
                            "--body", event.body], check=False)

    (3) Own mail server / webhook (stdlib only)::

            import json, urllib.request
            req = urllib.request.Request(
                "https://your-endpoint/notify",
                data=json.dumps({"subject": event.subject, "body": event.body}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)

    (4) MCP (Gmail / M365): a headless cron CANNOT call an MCP tool directly. Route it
        through a ``claude -p`` bridge with your MCP servers loaded (heavier; costs
        OAuth tokens)::

            import subprocess
            subprocess.run(["claude", "-p",
                            f"Use the gmail MCP tool to email you@you.tld the subject "
                            f"'[maintenance] {event.subject}' and body: {event.body}"],
                           check=False)

    OAuth-only (project hard rule): do NOT import the `anthropic` SDK or use an API key.
    The `claude -p` path is OAuth via the CLI; SMTP/webhook touch no Anthropic surface.
    """
    raise NotImplementedError(
        "example_notifier is a template — copy it into your own module, wire a "
        "transport, and set [maintenance] notifier = 'yourmod:yourfunc'")
