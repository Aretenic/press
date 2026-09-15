"""Weekly digest of what upstream did to press and agent (Aretenic ADR 045 §2-§3).

Both forks carry a long-lived branch merged from upstream weekly. Upstream moves too fast to read
commit by commit (~356 commits a month on press), so this reports only what we act on: security
fixes and changes to the backup, binlog and S3 paths ADR 041 depends on; new features; and upstream
changes to files our fork has patched, which are our conflict and silent-drift risk.

Run by .github/workflows/upstream-watch.yml, which opens one issue a week in this fork. It reads the
previous issue's marker to report only what is new since then, and falls back to 7 days.

Locally, without writing anything:
    python3 .github/upstream_watch.py --print
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile

FORKS = [
	{
		"name": "press",
		"fork": "https://github.com/Aretenic/press.git",
		"branch": "feature/r2-backups",
		"upstream": "https://github.com/frappe/press.git",
	},
	{
		"name": "agent",
		"fork": "https://github.com/Aretenic/agent.git",
		"branch": "aretenic",
		"upstream": "https://github.com/frappe/agent.git",
	},
]

ISSUE_REPO = "Aretenic/press"
ISSUE_LABEL = "upstream-watch"
MARKER = "<!-- upstream-watch: {} -->"
MARKER_RE = re.compile(r"<!-- upstream-watch: (\{.*?\}) -->", re.S)
FALLBACK_DAYS = 7
MAX_PER_SECTION = 40

#: ADR 041's paths: a change here can conflict with tenant storage, or fix something we carry.
SENSITIVE_PATH_RE = re.compile(r"(backup|binlog|s3|offsite|remote_file|bucket)", re.I)
SECURITY_RE = re.compile(r"\b(security|vulnerab|CVE-|SSRF|XSS|CSRF|RCE|injection|auth bypass)", re.I)
FEATURE_RE = re.compile(r"^feat(\(|!|:)", re.I)
#: Cosmetic work, listed only when it is security-shaped: formatting a file we patched is drift we
#: find at merge time anyway, and it drowns the sections that matter.
COSMETIC_RE = re.compile(r"^(style|test|docs|chore|ci)(\(|!|:)", re.I)


def run(*args: str, cwd: str | None = None) -> str:
	result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
	if result.returncode:
		# Without this, a failure reaches the workflow log as a bare exit status
		raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr.strip())
	return result.stdout.strip()


def clone(repo: dict, directory: str) -> str:
	path = os.path.join(directory, repo["name"])
	run("git", "clone", "--filter=blob:none", "--no-checkout", "--quiet", repo["fork"], path)
	run("git", "-C", path, "remote", "add", "upstream", repo["upstream"])
	run("git", "-C", path, "fetch", "--quiet", "upstream", "master")
	run("git", "-C", path, "fetch", "--quiet", "origin", repo["branch"])
	return path


def commits(path: str, revision_range: str, since: str | None = None) -> list[dict]:
	"""Commits as dicts, newest first. Merge commits are dropped: they carry no change of their own."""
	command = ["git", "-C", path, "log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", revision_range]
	if since:
		command.append(f"--since={since}")
	output = run(*command)
	result = []
	for record in output.split("\x1e"):
		record = record.strip()
		if not record:
			continue
		fields = record.split("\x1f", 2)  # a commit with no body has no third field
		sha, subject, body = (fields + ["", ""])[:3]
		result.append({"sha": sha, "subject": subject, "body": body})
	return result


def files_we_patch(path: str, branch: str) -> set[str]:
	base = run("git", "-C", path, "merge-base", f"origin/{branch}", "upstream/master")
	changed = run("git", "-C", path, "diff", "--name-only", base, f"origin/{branch}")
	return {line for line in changed.splitlines() if line}


def touched(path: str, sha: str) -> list[str]:
	return run("git", "-C", path, "show", "--pretty=", "--name-only", sha).splitlines()


def last_marker() -> dict:
	"""The previous digest's marker, or {} on the first run (the label may not exist yet)."""
	try:
		listing = run(
				"gh",
				"issue",
				"list",
				"--repo",
				ISSUE_REPO,
				"--label",
				ISSUE_LABEL,
				"--state",
				"all",
				"--limit",
				"1",
				"--json",
				"body",
			)
	except subprocess.CalledProcessError:
		return {}
	issues = json.loads(listing or "[]")
	if not issues:
		return {}
	found = MARKER_RE.search(issues[0].get("body") or "")
	return json.loads(found.group(1)) if found else {}


def section(title: str, note: str, entries: list[str]) -> str:
	if not entries:
		return f"### {title}\n\n_None._\n"
	shown = entries[:MAX_PER_SECTION]
	more = f"\n\n_{len(entries) - len(shown)} more not listed._" if len(entries) > len(shown) else ""
	return f"### {title}\n\n{note}\n\n" + "\n".join(shown) + more + "\n"


def report(directory: str, days: int = FALLBACK_DAYS) -> tuple[str, dict]:
	previous = last_marker()
	marker = {}
	heading, parts = [], []

	for repo in FORKS:
		path = clone(repo, directory)
		behind = len(commits(path, f"origin/{repo['branch']}..upstream/master"))
		head = run("git", "-C", path, "rev-parse", "upstream/master")
		marker[repo["name"]] = head

		seen_from = previous.get(repo["name"])
		if seen_from and run("git", "-C", path, "cat-file", "-t", seen_from) == "commit":
			new = commits(path, f"{seen_from}..upstream/master")
			window = f"since the last digest ({seen_from[:9]})"
		else:
			new = commits(path, f"origin/{repo['branch']}..upstream/master", since=f"{days} days ago")
			window = f"last {days} days"

		ours = files_we_patch(path, repo["branch"])
		security, features, conflicts = [], [], []
		for commit in new:
			files = touched(path, commit["sha"])
			line = f"- [`{commit['sha'][:9]}`](https://github.com/{repo['upstream'].split('github.com/')[1][:-4]}/commit/{commit['sha']}) {commit['subject']}"
			is_security = SECURITY_RE.search(commit["subject"] + commit["body"])
			if COSMETIC_RE.search(commit["subject"]) and not is_security:
				continue
			if is_security or any(SENSITIVE_PATH_RE.search(f) for f in files):
				security.append(line)
			elif FEATURE_RE.search(commit["subject"]):
				features.append(line)
			elif set(files) & ours:
				overlap = sorted(set(files) & ours)[:3]
				conflicts.append(line + f"<br>  ↳ `{'`, `'.join(overlap)}`")

		# Everything else is deliberately not listed, but counted, so the filters stay visible
		others = len(new) - len(security) - len(features) - len(conflicts)
		heading.append(
			f"- **{repo['name']}**: {behind} commits behind `upstream/master`, {len(new)} new {window} "
			f"({len(security)} + {len(features)} + {len(conflicts)} below, {others} of no interest)"
		)
		parts.append(f"## {repo['name']}\n")
		parts.append(section("Security, backups, binlogs and S3", "ADR 041's paths, and anything security-shaped.", security))
		parts.append(section("New features", "What upstream built that we might want.", features))
		parts.append(
			section("Changes to files we patched", "Conflict and drift risk: they may already fix something we carry.", conflicts)
		)

	body = (
		"Weekly upstream digest (ADR 045). Merge upstream into our branches, read this, open follow-ups "
		"for anything worth taking, then close.\n\n" + "\n".join(heading) + "\n\n" + "\n".join(parts)
	)
	return body, marker


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument("--print", action="store_true", help="write nothing, print the digest")
	parser.add_argument(
		"--days",
		type=int,
		default=FALLBACK_DAYS,
		help=f"window used when there is no previous digest (default {FALLBACK_DAYS})",
	)
	arguments = parser.parse_args()

	with tempfile.TemporaryDirectory() as directory:
		body, marker = report(directory, arguments.days)
	body += "\n" + MARKER.format(json.dumps(marker))

	if arguments.print:
		print(body)
		return

	title = f"Upstream digest, week of {run('date', '-u', '+%Y-%m-%d')}"
	with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
		handle.write(body)
		body_file = handle.name
	try:
		print(run("gh", "issue", "create", "--repo", ISSUE_REPO, "--title", title, "--label", ISSUE_LABEL, "--body-file", body_file))
	except subprocess.CalledProcessError as error:
		raise SystemExit(f"could not open the digest issue: {error.stderr or error.output}") from error


if __name__ == "__main__":
	main()
