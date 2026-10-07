"""Decisions the repository already records: decision records (ADRs) and decision commits.

Nobody should have to restate in chat what the team wrote in docs/adr or in a commit message. When a
session starts in a git repository that changed since the last look, the hook starts a background pass,
so the user never waits for it. The pass reads decision records with a parser (title, status, decision,
which record supersedes which) and asks jev which new commits record a decision; there is no second
generative model. What it learns is observed evidence attributed to the repository, with the file or
commit as its source, and it works like any other premise: when a record is superseded or deprecated,
or a commit is reverted, the recommendations that rested on it are flagged for review.
"""

import os
from pathlib import Path
import re
import subprocess
import sys

from .capture import _INSTRUCTION_VALUE, policy as capture_policy
from .common import KernelError, digest, timestamp, timestamp_offset
from .judge import JudgeError
from .language import fold, query_terms
from .turns import mask_secrets


# Git runs only in the background pass, never in a hook: it can be slow for reasons of its own. In a
# folder synced by iCloud, objects evicted to the cloud are downloaded on first read (measured: 44 s
# for one `git log` at 0% CPU, 0.01 s once local).
GIT_TIMEOUT = 60
PASS_TIMEOUT = 60
SCAN_SECONDS = 120          # how long a pass holds the repository before another may start
RECORD_DIRS = re.compile(r"(^|/)(adr|adrs|decisions|decision[-_]records|architecture[-_]decisions|decisiones)/"
                         r"[^/]+\.(md|markdown|txt)$", re.I)
NOT_RECORDS = re.compile(r"(^|/)(node_modules|vendor|third_party)/|(^|/)[^/]*(readme|index|template)[^/]*$", re.I)
RECORD_LIMIT = 200
FILE_LIMIT = 65536
VALUE_LIMIT = 200
ORIGIN = {"assertion_kind": "observed", "source_kind": "repository", "trust": "captured", "category": "project_decisions"}

ACTIVE = {"accepted", "approved", "adopted", "decided", "agreed", "implemented", "active", "done", "final",
          "aceptado", "aceptada", "aprobado", "aprobada", "adoptado", "adoptada", "vigente", "implementado", "implementada"}
PENDING = {"proposed", "draft", "pending", "open", "review", "progress", "discussion", "wip", "considering",
           "propuesto", "propuesta", "borrador", "pendiente", "revision", "discusion"}
RETIRED = {"rejected", "deprecated", "obsolete", "retired", "withdrawn", "abandoned", "declined",
           "rechazado", "rechazada", "obsoleto", "obsoleta", "retirado", "retirada", "descartado", "descartada"}
_SUPERSEDED = re.compile(r"\b(superseded|replaced|reemplazad[oa]|sustituid[oa])\b(\s+(by|por))?")

DECISION_COMMIT = ("Does `commit` state a project-wide choice (a technology, tool, platform, policy, or convention that "
                   "all later work must follow), rather than a change to one place in the code?")
# A commit is judged only when its subject uses the language of a decision (use, require, keep, stop,
# instead of...), one commit per request, and kept only when jev reads a project-wide choice. Measured
# with the local model on 112 labelled subjects (fixtures/calibration.jsonl: 59 written for
# calibration in English and Spanish, this repository's own 37 non-routine subjects, and 16 routine
# changes worded like decisions; `memory calibrate decision_commit --score`, which asks exactly as the
# pass does). The two filters removed every routine subject of the first two sets and left 52 for the
# judge. At 0.5: 25 of 28 written decisions kept, 2 of 13 of this repository's (behaviour changes of one
# tool, which the question reads as local), and none of the 16 look-alikes, the highest of which,
# "Use a context manager for the file handle", scored 0.482. A ranked relevance-style question put
# decisions and look-alikes in one band and moved with the project name in the query and with the
# batch, so it was dropped. Decision records stay the dependable source; commits add clear choices.
COMMIT_BAR = 0.5
REPLACES = "Does the decision in `query` replace or reverse the decision in `candidate`, so that `candidate` no longer holds?"
# Measured with the local model on 22 labelled pairs of decisions, one pair per request (`memory calibrate
# decision_replaces --score`): replacements scored 0.53-0.90 and other pairs 0.29-0.654, the highest
# being "Require Python 3.10 or newer" after "Drop support for Python 3.7", which both hold. At 0.70,
# 7 of 10 replacements and no other pair. Only decisions that share a topic word are compared.
REPLACES_BAR = 0.7
_TOPIC_NOISE = {"use", "uses", "using", "used", "usar", "usa", "usamos", "adopt", "adopts", "adoptar", "adopta", "switch",
                "switches", "move", "moves", "mover", "migrate", "migrar", "replace", "replaces", "reemplazar", "require",
                "requires", "requerir", "exigir", "keep", "keeps", "mantener", "stop", "stops", "dejar", "deja", "drop",
                "drops", "run", "runs", "allow", "allows", "permitir", "prefer", "standardize", "standardise", "pin",
                "pins", "instead", "rather", "favor", "favour", "again", "volver", "new", "nuevo", "nueva", "only", "solo",
                "always", "siempre", "never", "nunca", "all", "todo", "todos", "every", "cada", "decision", "decide"}
COMMIT_WINDOW = 60
COMMIT_DAYS = 180
JUDGED_PER_PASS = 12
_ROUTINE = re.compile(
    r"^(fix|fixes|hotfix|test|tests|docs?|style|chore|ci|build|perf|refactor)(\(.+?\))?!?:|"
    r"^(wip|bump|lint|typo|cleanup|clean up|minor|tweak|fix|fixes|fixed|tests?|pruebas|corrige|corregir|arregla|arreglar|"
    r"merge (branch|pull|remote)|release (v?\d|notes|candidate)|format(ting)? (code|files|with)|"
    r"(update|upgrade) (the )?(deps|dependenc\w*|lockfile)|add (unit |missing )?tests?|"
    r"actualiza(r)? (las )?dependencias|formato|anadir (los )?tests)\b|^v?\d+(\.\d+)+\b|\badr\b")
_DECISION_WORDS = re.compile(
    r"\b(use|uses|adopt\w*|switch\w*|mov(e|es|ing) (\w+ )?(to|from|off)|migrat\w*|replac\w*|drop\w*|deprecat\w*|"
    r"requir\w*|only|must|never|always|instead of|rather than|in favou?r of|standardi[sz]\w*|pin|pins|pinned|stop\w*|"
    r"keep\w*|prefer\w*|default\w* to|no longer|from now on|polic\w*|licen[sc]\w*|"
    r"usar|usamos|usa|adoptar|cambiar a|migr\w*|reemplaz\w*|sustitu\w*|dejar de|deja de|exig\w*|requer\w*|"
    r"requier\w*|solo|solamente|nunca|siempre|en lugar de|en vez de|preferi\w*|manten\w*|estandariz\w*|fijar|"
    r"a partir de ahora|licencia)\b")
_CONVENTIONAL = re.compile(r"^[a-z]+(\(.+?\))?!?:\s*", re.I)
_REVERT = re.compile(r'^revert\s+"(.+)"\s*$', re.I)
_REVERTS_SHA = re.compile(r"this reverts commit ([0-9a-f]{7,40})", re.I)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def git(root, *args, timeout=GIT_TIMEOUT):
    try:
        # The owner's git config must not change what is parsed (signatures printed in the log, say).
        done = subprocess.run(["git", "-c", "log.showSignature=false", "-c", "core.quotepath=off", "-C", str(root), *args],
                              capture_output=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              timeout=timeout, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def root_of(cwd):
    top = git(cwd, "rev-parse", "--show-toplevel")
    return str(Path(top.strip()).resolve()) if top and top.strip() else None


def inside_repository(cwd):
    """Whether a directory is in a git work tree, by looking for `.git` upwards: no git process, so a
    hook can ask it without waiting on git."""
    try:
        here = Path(cwd).resolve()
    except OSError:
        return False
    return any((folder / ".git").exists() for folder in [here, *here.parents][:64])


def slug(value, limit=60):
    """snake_case, cut at a word boundary."""
    full = re.sub(r"[^a-z0-9]+", "_", fold(value)).strip("_")
    if len(full) <= limit:
        return full
    cut = full[:limit + 1]
    return (cut[:cut.rfind("_")] if "_" in cut else full[:limit]).strip("_")


def entity_for(store, root):
    """The repository's entity: its directory name, in the snake_case agents use for project keys,
    so what the repository records and what the user says about the project meet on one entity."""
    row = store.repository(root)
    if row:
        return row["entity_key"]
    base = slug(Path(root).name) or "repository"
    taken = {r["entity_key"] for r in store.repositories() if r["root"] != root}
    return base if base not in taken else f"{base}_{digest(root)[:6]}"


def record_files(root, timeout=GIT_TIMEOUT):
    listing = git(root, "ls-files", "-s", "-z", timeout=timeout)
    found = []
    for entry in (listing or "").split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 2 and RECORD_DIRS.search(path) and not NOT_RECORDS.search(path):
            found.append((path, parts[1]))
    return sorted(found)[:RECORD_LIMIT]


def signature(root, timeout=GIT_TIMEOUT):
    """HEAD and the blobs of the decision records: what a pass would read. None without commits."""
    head = git(root, "rev-parse", "HEAD", timeout=timeout)
    if head is None:
        return None
    return digest({"head": head.strip(), "records": record_files(root, timeout)})


# Decision records

def clean(value):
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", value)
    return mask_secrets(re.sub(r"\s+", " ", value).strip())


def shorten(value, limit=VALUE_LIMIT):
    if len(value) <= limit:
        return value
    cut = value[:limit - 1]
    return (cut[:cut.rfind(" ")] if " " in cut else cut).rstrip(" ,;:") + "…"


def record_number(path, title=""):
    match = re.match(r"^(?:adr|rfc|dr)?[-_ ]?0*(\d{1,5})(?=[-_. ]|$)", Path(path).name, re.I) \
        or re.match(r"^\s*(?:adr|rfc|dr)?[\s_-]*0*(\d{1,5})\b", title or "", re.I)
    return int(match.group(1)) if match else None


def _first_paragraph(lines):
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("```", "|", "<!--")):
            if kept:
                break
            continue
        kept.append(stripped.lstrip("*->+ ").strip())
    return clean(" ".join(kept)) or None


def status_of(text):
    """(accepted | proposed | retired | superseded, successor number). A record in a decisions folder
    without a status reads as adopted, which is how most teams keep them."""
    if not text or not text.strip():
        return "accepted", None
    folded = fold(text)
    match = _SUPERSEDED.search(folded)
    if match:
        rest = text[match.start():]
        link = re.search(r"\]\(([^)]+)\)", rest)
        number = record_number(link.group(1).split("/")[-1]) if link else None
        if number is None:
            digits = re.search(r"(\d{1,5})", rest)
            number = int(digits.group(1)) if digits else None
        return "superseded", number
    for word in re.findall(r"[a-z]+", folded)[:8]:
        if word in ACTIVE:
            return "accepted", None
        if word in PENDING:
            return "proposed", None
        if word in RETIRED:
            return "retired", None
    return "accepted", None


def parse_record(text, path):
    """Title, number, status, successor and decision of a decision record (Nygard, adr-tools or
    MADR, in English or Spanish). None when it has no title."""
    lines = text[:FILE_LIMIT].splitlines()
    front = {}
    if lines and lines[0].strip() == "---":
        for end in range(1, min(len(lines), 60)):
            if lines[end].strip() == "---":
                for line in lines[1:end]:
                    name, sep, value = line.partition(":")
                    if sep:
                        front[fold(name.strip())] = value.strip().strip("'\"")
                lines = lines[end + 1:]
                break
    title, status = front.get("title"), front.get("status") or front.get("estado")
    sections, current = {}, None
    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            if title is None and len(heading.group(1)) == 1:
                title, current = heading.group(2).strip(), None
            else:
                current = fold(heading.group(2)).strip()
                sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
        elif status is None:
            bullet = re.match(r"^\s*[*-]?\s*(status|estado)\s*:\s*(.+)$", line, re.I)
            if bullet:
                status = bullet.group(2).strip()
    if status is None:
        status = next((" ".join(l.strip() for l in body if l.strip()) for name, body in sections.items()
                       if re.match(r"^(status|estado)\b", name)), None)
    decision = next((_first_paragraph(body) for name, body in sections.items()
                     if re.match(r"^(decision|decision outcome|resolution|resolucion)\b", name)), None)
    if not title or not clean(title):
        return None
    number = record_number(path, title)
    plain = re.sub(r"^\s*(?:adr|rfc|dr)?[\s_-]*0*\d+\s*[.:)\-–—]\s*", "", title, flags=re.I).strip() or title
    state, successor = status_of(status)
    return {"number": number, "title": clean(plain), "status": state, "successor": successor,
            "decision": decision}


def record_value(record):
    decision = record["decision"]
    if decision and fold(decision).rstrip(". ") != fold(record["title"]).rstrip(". "):
        return shorten(f"{record['title']}: {decision}")
    return shorten(record["title"])


def record_predicate(record, path):
    if record["number"] is not None:
        return f"adr_{record['number']:04d}"
    return "adr_" + (slug(Path(path).stem, 50) or digest(path)[:8])


def record_label(record, path):
    return f"ADR {record['number']}" if record and record["number"] is not None else Path(path).stem


def _active(store, statement_id):
    try:
        return store._row(statement_id)["lifecycle"] == "active"
    except KernelError:
        return False


def _changed_at(root, path):
    when = git(root, "log", "-1", "--format=%cI", "--", path, timeout=PASS_TIMEOUT)
    try:
        return timestamp(when.strip()) if when and when.strip() else None
    except KernelError:
        return None


def _put_record(store, root, entity, label, source, path, blob, record, summary):
    predicate, value = record_predicate(record, path), record_value(record)
    changed = _changed_at(root, path) or store.clock()
    sid = None
    # Forgetting wins until the record itself changes again after the request.
    if not _INSTRUCTION_VALUE.search(value) and not store.tombstoned_since(entity, predicate, changed):
        current = [r for r in store.records() if r["entity_key"] == entity and r["predicate"] == predicate]
        try:
            with store.db:
                if current and current[0]["value"] == value:
                    sid = current[0]["id"]
                elif current:
                    sid = store._correct(current[0]["id"], value, f"{path}: {value}"[:300], source_ref=path, **ORIGIN)
                    summary["changed"].append(record_label(record, path))
                else:
                    sid = store._insert(entity, predicate, value, f"{path}: {value}"[:300], label=label, kind="project",
                                        source_ref=path, **ORIGIN)
                    summary["learned"].append(record_label(record, path))
                store._event("learn", {"statement_id": sid, "source": "record"})
        except KernelError:
            sid = None
    store.save_repo_source(root, source, blob, sid)
    return sid


def _learn_records(store, root, entity, label, summary):
    present = {f"adr:{path}": (path, blob) for path, blob in record_files(root, PASS_TIMEOUT)}
    known = {s: r for s, r in store.repo_sources(root).items() if s.startswith("adr:")}
    parsed = {}
    for source, (path, blob) in present.items():
        if known.get(source, {}).get("version") == blob:
            continue
        try:
            raw = (Path(root) / path).read_bytes()[:FILE_LIMIT].decode("utf-8", "replace")
        except OSError:
            continue
        parsed[source] = (parse_record(raw, path), path, blob)
    # Where each record number lives now, so "superseded by ADR 7" finds record 7.
    numbers = {record_number(s[4:]): r["statement_id"] for s, r in known.items()
               if s in present and r["statement_id"] and _active(store, r["statement_id"])}
    numbers.pop(None, None)
    for source, (record, path, blob) in parsed.items():
        if record and record["status"] == "accepted":
            sid = _put_record(store, root, entity, label, source, path, blob, record, summary)
            if sid and record["number"] is not None:
                numbers[record["number"]] = sid
    for source, (record, path, blob) in parsed.items():
        if record and record["status"] == "accepted":
            continue
        sid = known.get(source, {}).get("statement_id")
        if sid and _active(store, sid):
            successor = numbers.get(record["successor"]) if record and record["status"] == "superseded" \
                and record["successor"] is not None else None
            if successor and successor != sid and _active(store, successor):
                with store.db:
                    store._supersede(sid, successor)
                summary["superseded"].append(record_label(record, path))
            else:
                store.revoke(sid)
                summary["retired"].append(record_label(record, path))
        store.save_repo_source(root, source, blob, sid)
    for source, row in known.items():
        if source not in present:
            if row["statement_id"] and _active(store, row["statement_id"]):
                store.revoke(row["statement_id"])
                summary["retired"].append(Path(source[4:]).stem)
            store.drop_repo_source(root, source)


# Decision commits

def recent_commits(root, now):
    listing = git(root, "log", "--no-merges", "--name-only", f"-n{COMMIT_WINDOW}",
                  "--format=%x1e%H%x1f%an%x1f%ae%x1f%cI%x1f%s%x1f%b%x1d", timeout=PASS_TIMEOUT)
    oldest = timestamp_offset(now, -COMMIT_DAYS * 86400)
    commits = []
    for chunk in (listing or "").split("\x1e"):
        header, _, files = chunk.partition("\x1d")
        fields = header.split("\x1f")
        if len(fields) < 6 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            continue
        try:
            when = timestamp(fields[3])
        except KernelError:
            continue
        if when >= oldest:
            commits.append({"sha": fields[0], "author": fields[1], "email": fields[2], "date": when,
                            "subject": fields[4].strip(), "body": fields[5].strip(),
                            "files": [f for f in files.splitlines() if f.strip()]})
    return list(reversed(commits))


def routine(subject):
    return bool(_ROUTINE.search(fold(subject).strip()))


def decision_language(subject):
    return bool(_DECISION_WORDS.search(fold(_CONVENTIONAL.sub("", subject.strip()))))


def commit_value(subject):
    stripped = _CONVENTIONAL.sub("", subject.strip()) if _CONVENTIONAL.match(subject.strip()) else subject.strip()
    stripped = clean(stripped)
    return shorten(stripped[:1].upper() + stripped[1:]) if stripped else ""


def _repository_rows(store, entity):
    return [r for r in store.records() if r["entity_key"] == entity and r["source_kind"] == "repository"]


def _revert(store, entity, known, sha, subject, summary):
    target = next((r["statement_id"] for s, r in known.items() if s.startswith("commit:") and sha
                   and (s[7:].startswith(sha) or sha.startswith(s[7:]))), None) if sha else None
    if not target:
        value = commit_value(subject)
        target = next((r["id"] for r in _repository_rows(store, entity) if r["value"] == value), None)
    if target and _active(store, target):
        store.revoke(target)
        summary["retired"].append(f"reverted {(sha or '')[:7]}".strip())


def topic_words(value):
    return {t for t in query_terms(value) if t not in _TOPIC_NOISE}


def _settle(store, judge, entity, statement, deadline, summary):
    """A decision ends the earlier commit decision it replaces. One learned late (an older commit judged
    in a later pass) ends at once if a later decision already replaced it. Only decisions that share a
    topic word are compared, one pair per request, so a score never depends on its neighbours."""
    rows = _repository_rows(store, entity)
    new = next((r for r in rows if r["id"] == statement), None)
    if not new:
        return
    words = topic_words(new["value"])
    others = [r for r in rows if r["id"] != statement and r["predicate"].startswith("decision_")
              and words & topic_words(r["value"])][:8]
    for other in sorted(others, key=lambda r: r["valid_from"]):
        later, earlier = (new, other) if other["valid_from"] <= new["valid_from"] else (other, new)
        try:
            timeout = deadline.timeout(judge.timeout) if deadline else None
            scores, _ = judge.rank(later["value"], [earlier["value"]], no_cache=True, timeout=timeout, question=REPLACES)
        except JudgeError:
            return
        if scores.get(0, 0.0) >= REPLACES_BAR and _active(store, earlier["id"]) and _active(store, later["id"]):
            with store.db:
                store._supersede(earlier["id"], later["id"])
            summary["superseded"].append(" ".join((earlier["source_ref"] or "commit").split()[:2]))
            if earlier is new:
                return


def _put_commit(store, entity, label, commit, summary):
    """The commit's decision as a statement, valid from the commit's date. Returns (id, created)."""
    value = commit_value(commit["subject"])
    if not value or _INSTRUCTION_VALUE.search(value):
        return None, False
    same = next((r for r in _repository_rows(store, entity) if r["value"] == value), None)
    if same:
        return same["id"], False  # the same decision under another hash: a rebase, a cherry-pick, a rewrite
    predicate = "decision_" + (slug(value, 48) or commit["sha"][:7])
    if any(r["entity_key"] == entity and r["predicate"] == predicate for r in store.records(history=True)):
        predicate = f"{predicate}_{commit['sha'][:7]}"
    if store.tombstoned_since(entity, predicate, commit["date"]):
        return None, False
    body = _first_paragraph(commit["body"].splitlines())
    evidence = mask_secrets(commit["subject"] + (". " + body if body else ""))[:300]
    try:
        with store.db:
            sid = store._insert(entity, predicate, value, evidence, label=label, kind="project",
                                valid_from=min(commit["date"], store.clock()),
                                source_ref=f"commit {commit['sha'][:7]} {commit['date'][:10]}", **ORIGIN)
            store._event("learn", {"statement_id": sid, "source": "commit"})
    except KernelError:
        return None, False
    summary["learned"].append(f"commit {commit['sha'][:7]}")
    return sid, True


def _learn_commits(store, judge, root, entity, label, deadline, allow_remote, summary):
    known = {s: r for s, r in store.repo_sources(root).items() if s.startswith("commit:")}
    pending, reverted = [], set()
    for commit in recent_commits(root, store.clock()):
        source = "commit:" + commit["sha"]
        if source in known:
            continue
        revert = _REVERT.match(commit["subject"])
        if revert:
            target = _REVERTS_SHA.search(commit["body"])
            _revert(store, entity, known, target.group(1) if target else None, revert.group(1), summary)
            reverted.add(target.group(1) if target else commit_value(revert.group(1)))
        # A commit that writes a decision record says what the record says: the record is read instead.
        writes_record = any(RECORD_DIRS.search(f) for f in commit["files"])
        if revert or writes_record or "[bot]" in commit["author"] + commit["email"] or routine(commit["subject"]) \
                or not decision_language(commit["subject"]):
            store.save_repo_source(root, source, commit["sha"])
            continue
        pending.append(commit)
    # A decision reverted later in the same window was never in force: it is not judged at all.
    for commit in [c for c in pending if commit_value(c["subject"]) in reverted
                   or any(c["sha"].startswith(r) for r in reverted if re.fullmatch(r"[0-9a-f]{7,40}", r))]:
        pending.remove(commit)
        store.save_repo_source(root, "commit:" + commit["sha"], commit["sha"])
    if not pending:
        return
    if judge is None:
        summary["skipped"].append("no_judge")
        return
    try:
        judge.require_local(allow_remote)
    except JudgeError:
        summary["skipped"].append("judge_remote")
        return
    batch = pending[-JUDGED_PER_PASS:]
    if len(pending) > len(batch):
        summary["more"] = len(pending) - len(batch)
    # One commit per request, newest first: a score must not depend on which other commits came along.
    verdicts = []
    for commit in reversed(batch):
        try:
            timeout = deadline.timeout(judge.timeout) if deadline else None
            answers, _ = judge.ask({"commit": re.sub(r"\s+", " ", commit["subject"])},
                                   {"decision": ("noul", DECISION_COMMIT)}, timeout=timeout)
        except JudgeError:
            summary["skipped"].append("judge_unavailable")
            break
        summary["judged"] += 1
        verdicts.append((commit, answers["decision"] >= COMMIT_BAR))
    # Stored oldest first, so a later decision can end the earlier one it replaces.
    for commit, accepted in sorted(verdicts, key=lambda v: v[0]["date"]):
        statement, created = _put_commit(store, entity, label, commit, summary) if accepted else (None, False)
        if created:
            _settle(store, judge, entity, statement, deadline, summary)
        store.save_repo_source(root, "commit:" + commit["sha"], commit["sha"], statement)


def _retire_commits(store, root, entity, summary):
    """Commit learning is off: decisions read from commit messages stop counting, without a tombstone, and
    their commits are forgotten as seen, so turning it back on reads them again. On this repository the
    pass had kept three subjects as project decisions (a license, a test runner's options, a release note)
    that no one had decided for the whole project."""
    for row in _repository_rows(store, entity):
        if (row.get("source_ref") or "").startswith("commit "):
            store.retire(row["id"])
            summary["unused_commits"] = summary.get("unused_commits", 0) + 1
    for source in [s for s in store.repo_sources(root) if s.startswith("commit:")]:
        store.drop_repo_source(root, source)


def describe(summary):
    learned = summary["learned"] + summary["changed"]
    gone = summary["superseded"] + summary["retired"]
    parts = []
    if learned:
        parts.append(f"learned {len(learned)} decision(s) from the repository ({', '.join(learned[:3])}"
                     + (", …" if len(learned) > 3 else "") + ")")
    if gone:
        parts.append(f"{len(gone)} earlier one(s) no longer apply ({', '.join(gone[:3])}" + (", …" if len(gone) > 3 else "") + ")")
    if summary.get("unused_commits"):
        parts.append(f"stopped using {summary['unused_commits']} decision(s) read from commit messages; decision records "
                     "still count (`memory policy --repository-commits on` reads commits again)")
    return "Memory: " + "; ".join(parts) + "." if parts else ""


def learn(store, judge, root, deadline=None, session=None, if_changed=False):
    """One pass over a repository. Returns what changed; the note goes to the session that started it.
    `if_changed` (the session-start pass) returns at once when nothing changed since the last pass."""
    rules = capture_policy(store)
    if not rules.get("repository", True) or not rules["categories"].get("project_decisions", False):
        return {"skipped": ["policy"]}
    root = str(Path(root).resolve())
    commits = bool(rules.get("repository_commits", False))
    # The signature names the sources read, so a change of policy is a change worth a pass.
    marked = lambda value: value and value + ("+commits" if commits else "+records")
    if if_changed:
        row = store.repository(root)
        if row and row["signature"] and row["signature"] == marked(signature(root, PASS_TIMEOUT)):
            return {"skipped": ["unchanged"]}
    entity = entity_for(store, root)
    if not store.claim_scan(root, entity, timestamp_offset(store.clock(), SCAN_SECONDS)):
        return {"skipped": ["busy"]}
    summary = {"repository": entity, "learned": [], "changed": [], "superseded": [], "retired": [], "judged": 0, "skipped": []}
    complete, current = False, None
    try:
        current = marked(signature(root, PASS_TIMEOUT))
        label = Path(root).name
        _learn_records(store, root, entity, label, summary)
        if commits:
            _learn_commits(store, judge, root, entity, label, deadline, rules.get("allow_remote_judge", False), summary)
        else:
            _retire_commits(store, root, entity, summary)
        # Commits left unjudged (no time, too many) leave the signature unset: the next session tries again.
        complete = current is not None and "judge_unavailable" not in summary["skipped"] and not summary.get("more")
    finally:
        store.release_scan(root, current if complete else None)
    summary["note"] = describe(summary)
    if summary["note"] and session:
        store.set_session_field(session, "notice", summary["note"])
    return summary


def learn_command(db, scope, workspace, session=None, jev=None):
    """The repository pass alone, as a command line and environment for `start_background`."""
    package = str(Path(__file__).resolve().parent.parent)
    argv = [sys.executable, "-m", "context_kernel", "--db", str(db), "--scope", scope, "learn", "--workspace", str(workspace),
            "--if-changed"]
    argv += (["--session", session] if session else []) + (["--jev-command", jev] if jev else [])
    path = os.environ.get("PYTHONPATH")
    return argv, dict(os.environ, PYTHONPATH=package + (os.pathsep + path if path else ""))


def warm_command(db, scope, workspace=None, session=None, jev=None, learn_repository=False):
    """The session-start background pass (`memory warm`): load the local model, check the judge's
    calibration when needed, and read the repository's decisions when `learn_repository`."""
    package = str(Path(__file__).resolve().parent.parent)
    argv = [sys.executable, "-m", "context_kernel", "--db", str(db), "--scope", scope, "warm"]
    argv += (["--workspace", str(workspace)] if workspace else []) + (["--learn"] if learn_repository else [])
    argv += (["--session", session] if session else []) + (["--jev-command", jev] if jev else [])
    path = os.environ.get("PYTHONPATH")
    return argv, dict(os.environ, PYTHONPATH=package + (os.pathsep + path if path else ""))


def start_background(argv, env):
    """Detached, with no pipe back to the hook: the host's wait for the hook ends when the hook does."""
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "close_fds": True, "env": env}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen(argv, **options)
        return True
    except OSError:
        return False
