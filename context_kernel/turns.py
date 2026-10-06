"""One row per prompt: who sent it (as far as the host can tell), what was delivered, what was captured.

The prompt hook opens the turn with a random token that only the host-injected context carries.
Write tools must present that token, and the MCP server accepts it only for a session bound to its
own host process (see binding.py). The excerpt kept for validation is masked, bounded, and expires.
"""

import re
import secrets

from .binding import ancestors
from .common import digest, timestamp_offset
from .language import fold


EXCERPT_BYTES = 4096
EXCERPT_SECONDS = 600      # captures can still be validated against a turn for ten minutes
CONTINUATION_SECONDS = 2   # a prompt this soon after the previous Stop was not typed by a person

_SECRETS = [
    re.compile(r"(?i)\b(api[_ -]?key|password|passwd|secret[_ -]?token|access[_ -]?token|bearer)\b\s*[:=]?\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(sk|pk|rk|ghp|gho|xox[abpr])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b"),
]
_DO_NOT_REMEMBER = re.compile(
    r"\b(no (lo |la |me )?(recuerdes|guardes|memorices|anotes)|sin guardar|no quiero que (lo )?(recuerdes|guardes)|"
    r"don'?t (remember|save|store|memori[sz]e)|do not (remember|save|store|memori[sz]e)|off the record)\b")
# A request to forget: the verb opens a sentence or follows a polite lead-in ("please", "can you").
# "Don't forget my salary" asks the opposite and is excluded.
_FORGET = re.compile(
    r"(^|[.!?;,]\s*|\b(please|por favor|can you|could you|would you|puedes|podrias|quiero que)\s+)"
    r"(olvida|olvidate de|borra|elimina|forget|delete|remove|erase|revoca|revoke|deshaz|undo)\b|"
    r"\b(olvida|forget|deshaz|undo) (eso|esto|lo que|that|this|what)\b")
_KEEP = re.compile(r"\b(don'?t|do not|never|no te|no|nunca) (forget|olvides|te olvides|olvidar)\b")
# Taking back the last thing saved, without naming anything else. "Forget the checkout deadline"
# is not an undo: it names a fact, and goes through memory_forget with that fact's id.
_UNDO = re.compile(
    r"\b(olvida (eso|esto|lo ultimo|lo que (acabas de|has) guardad\w*)|deshaz(lo)?|deshacer eso|eso no|no era (asi|eso)|"
    r"undo( that| it| this)?|take (it|that) back|forget (that|this|it|what you just saved)|that'?s wrong|scratch that)\b")
_CONTINUATIONS = {"ok", "okay", "si", "sí", "yes", "y", "dale", "vale", "go", "go on", "continue", "continua", "sigue",
                  "listo", "done", "gracias", "thanks", "perfecto", "great", "good", "bien"}


def mask_secrets(value):
    for pattern in _SECRETS:
        value = pattern.sub("[masked]", value)
    return value


def excerpt(prompt):
    data = mask_secrets(prompt).encode()[:EXCERPT_BYTES]
    return data.decode(errors="ignore")


def do_not_remember(prompt):
    return bool(_DO_NOT_REMEMBER.search(fold(prompt)))


def forget_request(prompt):
    folded = fold(prompt)
    return bool(_FORGET.search(folded)) and not _KEEP.search(folded)


def undo_request(prompt):
    return bool(_UNDO.search(fold(prompt)))


# Picking among options the agent just listed ("haz 1 y 2", "la 3", "ambas", "go with option 2") says
# nothing about the user's world, yet the gate read "haz 1 y 2" as two facts, confidently (2026-10-06).
_CHOICE_REFS = re.compile(r"^(\d{1,2}[a-z]?|[a-d]|both|all|none|ambas|ambos|todas|todos|ninguna|ninguno|dos|tres|first|"
                          r"second|third|fourth|last|primera|primero|segunda|segundo|tercera|tercero|cuarta|cuarto|"
                          r"ultima|ultimo)$")
_CHOICE_WORDS = {"haz", "hazlo", "hazla", "hazlas", "hazlos", "hagamos", "haga", "hace", "dale", "vale", "ok", "okay", "si",
                 "va", "venga", "adelante", "sigue", "continua", "aplica", "implementa", "elige", "elijo", "prefiero",
                 "quiero", "me", "quedo", "con", "voy", "vamos", "por", "favor", "porfa", "y", "e", "o", "u", "tambien",
                 "solo", "el", "la", "los", "las", "lo", "ellas", "ellos", "opcion", "opciones", "punto", "puntos", "paso",
                 "pasos", "numero", "do", "go", "with", "pick", "choose", "take", "apply", "implement", "lets", "let", "s",
                 "yes", "please", "then", "and", "or", "also", "only", "just", "the", "one", "ones", "of", "them", "it",
                 "option", "options", "item", "items", "number", "step", "steps", "idea", "ideas", "a"}


def choice_reply(prompt):
    """A short pick among listed options: every word is a reference to an option or a filler."""
    words = re.sub(r"[^\w\s]", " ", fold(prompt)).split()
    if not words or len(words) > 8:
        return False
    return any(_CHOICE_REFS.match(w) for w in words) and all(_CHOICE_REFS.match(w) or w in _CHOICE_WORDS for w in words)


def trivial_continuation(prompt):
    """Acknowledgements that carry no new information. Privacy requests and replies to a pending
    question are checked by the caller first: "ok" can be meaningful when something is pending."""
    folded = re.sub(r"[^\w\s]", "", fold(prompt)).strip()
    return folded in _CONTINUATIONS or (len(folded) < 4 and not re.search(r"\d", folded))


def origin(event, prompt, previous):
    """interactive | continuation | unknown. A host-reported continuation or a prompt that follows
    the previous Stop within seconds was not typed by a person; host markup is of unknown authorship."""
    if event.get("is_continuation") or event.get("stop_hook_active"):
        return "continuation"
    if prompt.lstrip().startswith("<"):
        return "unknown"
    if previous and previous.get("closed_at"):
        opened = event.get("_now")
        if opened and opened <= timestamp_offset(previous["closed_at"], CONTINUATION_SECONDS):
            return "continuation"
    return "interactive"


def turn_key(event, prompt, now):
    return str(event.get("prompt_id") or event.get("turn_id") or digest({"prompt": prompt, "at": now})[:32])


def open_turn(store, event, client, prompt):
    """Register the session's host binding and open this prompt's turn. Returns the turn row."""
    session = str(event.get("session_id") or "unknown-session")
    now = store.clock()
    store.register_session(session, client, ancestors(depth=2))
    previous = next(iter(store.recent_turns(session, 1)), None)
    key = turn_key(event, prompt, now)
    kind = origin(dict(event, _now=now), prompt, previous)
    kept = None if do_not_remember(prompt) else excerpt(prompt)
    store.open_turn(session, key, secrets.token_urlsafe(18), kind, digest(prompt), kept,
                    timestamp_offset(now, EXCERPT_SECONDS))
    store.expire_turns()
    return store.turn(session, key)


def close_turn(store, event):
    session = str(event.get("session_id") or "unknown-session")
    key = event.get("prompt_id") or event.get("turn_id")
    turn = store.turn(session, str(key)) if key else next(iter(store.recent_turns(session, 1)), None)
    if turn and not turn["closed_at"]:
        store.update_turn(session, turn["turn_key"], closed_at=store.clock())
        turn = store.turn(session, turn["turn_key"])
    return turn
