"""Small bilingual vocabulary, not unrestricted language understanding."""

import calendar
from datetime import date, datetime, time, timedelta
import re
import unicodedata

from .common import timestamp


def fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c))


PREDICATES = {
    "salary": ("salary", "salario", "sueldo"),
    "availability": ("availability", "disponibilidad"),
    "work_schedule": ("work_schedule", "horario_laboral", "horario"),
    "employment": ("employment", "empleo"),
    "constraint": ("constraint", "restriccion", "restricciones"),
    "preference": ("preference", "preferencia"),
    "goal": ("goal", "objetivo"),
    "allergy": ("allergy", "allergies", "alergia", "alergias"),
    "dietary_constraint": ("dietary_constraint", "restriccion_alimentaria", "diet"),
    "mobility_limit": ("mobility_limit", "movilidad_reducida", "limitacion_movilidad"),
    "accessibility": ("accessibility", "accesibilidad"),
    "budget": ("budget", "presupuesto"),
    "project_status": ("project_status", "estado_proyecto"),
    "decision": ("decision",),
    "release_approver": ("release_approver", "aprobador_release", "responsable_lanzamiento"),
    "delivery_status": ("delivery_status", "estado_entrega"),
    "ownership_status": ("ownership_status", "estado_propiedad"),
    "open_loop": ("open_loop", "pendiente"),
}
_CANONICAL = {alias: name for name, aliases in PREDICATES.items() for alias in aliases}
STOPWORDS = set("a an the is are of for to and or in on with who what how should i me my our this that it current "
                "el la los las un una unos unas es son de del para por y o en con quien que como debo yo mi mis "
                "nuestro nuestra este esta esto ese esa eso actual al se si mas".split())
TRANSLATIONS = {
    "responsable": ("approver", "manager"), "jefe": ("manager",),
    "lanzamiento": ("release",), "entrega": ("delivery",),
    "paquete": ("package",), "proyecto": ("project",), "despliegue": ("deployment",),
    "regalo": ("gift",), "amigo": ("friend",), "amiga": ("friend",),
    "alergia": ("allergy",), "vivienda": ("housing",), "piso": ("apartment",),
}


# Words a later question uses when a kind of fact matters, for facts stored without the agent's own cues
# (older captures, the owner CLI, decision records). Keyed by a word of the predicate; matched lexically only.
CONCEPTS = {
    "deadline": "deadline due date ship release launch timeline schedule plazo entrega lanzamiento fecha",
    "due": "deadline due date ship release timeline plazo entrega fecha",
    "plazo": "deadline due date ship release timeline plazo entrega fecha",
    "launch": "release launch ship go live date lanzamiento salida publicar",
    "release": "release launch ship version publish lanzamiento version publicar",
    "budget": "budget cost spend money price afford presupuesto coste gasto dinero",
    "cost": "budget cost spend money price presupuesto coste gasto",
    "presupuesto": "budget cost spend money presupuesto coste gasto dinero",
    "approver": "approve approval sign off review permission aprobar aprobacion firma visto bueno",
    "owner": "owner responsible handles contact maintainer responsable encargado",
    "manager": "manager boss lead reports jefe responsable",
    "region": "region residency hosting datacenter deploy cloud region alojamiento servidor",
    "residency": "region residency hosting datacenter deploy cloud privacy region alojamiento",
    "database": "database db sql storage data migration schema base datos",
    "db": "database db sql storage data migration base datos",
    "framework": "framework stack library frontend backend tecnologia libreria",
    "stack": "framework stack library language tecnologia",
    "ci": "ci pipeline build tests deploy integracion",
    "schedule": "schedule hours available meeting time calendar horario disponible reunion",
    "horario": "schedule hours available meeting time horario disponible reunion",
    "allergy": "food eat meal snack restaurant dinner lunch comida comer cena restaurante alergia dieta",
    "alergia": "food eat meal snack restaurant dinner comida comer cena restaurante alergia",
    "diet": "food eat meal snack restaurant dinner lunch comida comer cena dieta",
    "language": "language spanish english reply answer idioma espanol ingles responder",
    "idioma": "language spanish english reply answer idioma espanol ingles responder",
    "deploy": "deploy release production ship rollout desplegar produccion",
    "call": "on call pager incident outage weekend guardia incidente",
    "guardia": "on call pager incident outage weekend guardia incidente",
    "package": "install dependency dependencies npm yarn pnpm package dependencias instalar",
    "commit": "commit git message push branch pull request mensaje rama",
    "client": "client customer invoice contract cliente factura contrato",
    "cliente": "client customer invoice contract cliente factura contrato",
    "invoice": "invoice billing payment charge factura cobro pago",
    "billing": "invoice billing payment charge pricing factura cobro pago",
    "team": "team people capacity headcount hire equipo personas capacidad",
    "capacity": "team people capacity headcount availability equipo capacidad",
    "compliance": "privacy gdpr personal data compliance legal audit privacidad datos personales",
    "gdpr": "privacy gdpr personal data compliance legal privacidad datos personales",
    "policy": "rule policy allowed forbidden must regla politica permitido prohibido",
}


def concept_cues(predicate):
    """Default cues for a predicate, from CONCEPTS; empty when no word of it is known."""
    words = [w for w in re.split(r"[_\W]+", fold(predicate)) if w]
    found = []
    for word in words:
        if word in CONCEPTS:
            found.extend(CONCEPTS[word].split())
    return list(dict.fromkeys(found))


def predicate_name(value):
    return _CANONICAL.get(fold(value), fold(value))


def query_terms(query):
    tokens = [t for t in re.findall(r"[^\W_]+", fold(query)) if t not in STOPWORDS]
    expanded = []
    for token in tokens:
        expanded.append(token)
        expanded.extend(TRANSLATIONS.get(token, ()))
        for name, aliases in PREDICATES.items():
            if token in aliases:
                expanded.extend(name.split("_"))
                expanded.extend(alias for alias in aliases if "_" not in alias)
    return list(dict.fromkeys(expanded))[:32]


# Entity keys too generic to identify anything: a question saying "project" or "user" names none of them.
GENERIC_ENTITIES = frozenset({"user", "project", "person", "object", "usuario", "proyecto"})


def entity_mentions(query, records):
    query = " " + re.sub(r"[^\w]+", " ", fold(query)).strip() + " "
    found = {}
    for row in records:
        for label in (row["entity_key"], row.get("label", ""), *row.get("aliases", [])):
            label = re.sub(r"[^\w]+", " ", fold(label)).strip()
            if label and label not in GENERIC_ENTITIES and " " + label + " " in query:
                found.setdefault(label, set()).add(row["entity_key"])
    return found


def mentioned_entities(query, records):
    return set().union(*entity_mentions(query, records).values())


def ambiguous_entity_reference(query, records):
    return any(len(entities) > 1 for entities in entity_mentions(query, records).values())


def related_entities(seeds, relations, depth=2):
    """Only explicitly stored containment ancestors, never sibling expansion."""
    found, frontier = set(seeds), set(seeds)
    for _ in range(depth):
        parents = {r["parent"] for r in relations if r["child"] in frontier} - found
        found.update(parents)
        frontier = parents
    return found


# Relative periods. A fact whose own words name a period that ends ("publish v0.3 this week", "mañana trabajo desde
# casa") stops being current when the period does: read a month later, "this week" names the wrong week. Explicit
# dates are values and never end a fact. A start ("desde hoy", "from next week on"), a habit ("cada semana", "los
# viernes") and an idiom ("hoy en día") are not bounds.
_PERIODS = (  # earlier patterns win over the words inside them: "pasado mañana" is not also "mañana"
    (r"pasado manana|day after tomorrow", ("day", 2)),
    (r"esta (?:manana|tarde|noche)|this (?:morning|afternoon|evening)|tonight|hoy|today", ("day", 0)),
    (r"manana|tomorrow", ("day", 1)),
    (r"(?:la )?semana que viene|(?:la )?proxima semana|(?:la )?semana proxima|next week", ("week", 1)),
    (r"esta semana|this week|este fin de semana|this weekend", ("week", 0)),
    (r"(?:el )?mes que viene|(?:el )?proximo mes|(?:el )?mes proximo|next month", ("month", 1)),
    (r"este mes|this month", ("month", 0)),
    (r"(?:el )?ano que viene|(?:el )?proximo ano|next year", ("year", 1)),
    (r"este ano|this year", ("year", 0)),
)
_WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6,
             "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
# The word before a weekday: "this" includes today; "next" is read as the later of its two meanings, so a fact
# never ends early; a bare "el viernes" / "on Friday" is the next one to come.
_WEEKDAY_WORDS = ((r"el proximo|proximo|next", "next"), (r"este|this|hasta el|antes del|until|by|before", "this"),
                  (r"el|on", "coming"))
_NOT_A_PERIOD_BEFORE = re.compile(r" (?:desde|a partir de|from|starting|as of|since|beginning|empezando|comenzando|"
                                  r"cada|every|each|todos los|todas las|hoy por) $")
_NOT_A_PERIOD_AFTER = re.compile(r" (?:en dia|dia|por hoy|en adelante|onwards|forward|pasad[oa]|anterior)(?= )")
_MORNING = re.compile(r" (?:la|una|cada) $")  # "por la mañana", "una mañana": the morning, not tomorrow


def periods(value):
    """The relative periods `value` names: ("day" | "week" | "month" | "year", how many ahead), or ("weekday",
    0-6, "this" | "next" | "coming")."""
    words = " " + " ".join(re.findall(r"[^\W_]+", fold(value))) + " "
    found = set()

    def scan(pattern, period_of, morning=False):
        nonlocal words
        for match in re.finditer(rf"(?<= )(?:{pattern})(?= )", words):
            before, after = words[:match.start()], words[match.end():]
            if not (_NOT_A_PERIOD_BEFORE.search(before) or _NOT_A_PERIOD_AFTER.match(after)
                    or (morning and _MORNING.search(before))):
                found.add(period_of(match))
        # Consumed either way: the words inside a longer phrase are not read again on their own.
        words = re.sub(rf"(?<= )(?:{pattern})(?= )", lambda m: "#" * len(m.group(0)), words)

    for pattern, period in _PERIODS:
        scan(pattern, lambda m, period=period: period, morning=period == ("day", 1))
    names = "|".join(_WEEKDAYS)
    for before, mode in _WEEKDAY_WORDS:
        scan(rf"(?:{before}) (?:{names})", lambda m, mode=mode: ("weekday", _WEEKDAYS[m.group(0).split()[-1]], mode))
    return found


def period_end(value, said, anchor, tz=None):
    """When a fact stops being current because its own words name a relative period: the end of a period that both
    the fact (`value`) and the user's message (`said`) name, in either language, counted from `anchor` (when it was
    said) in local time (`tz`, default this machine's). The latest end when they share several; None for none."""
    shared = periods(value) & periods(said)
    if not shared:
        return None
    local = datetime.fromisoformat(timestamp(anchor)).astimezone(tz)
    today, ends = local.date(), []
    for unit, amount, *mode in shared:
        if unit == "day":
            last = today + timedelta(days=amount)
        elif unit == "week":
            last = today + timedelta(days=6 - today.weekday() + 7 * amount)
        elif unit == "month":
            year, month = today.year + (today.month + amount - 1) // 12, (today.month + amount - 1) % 12 + 1
            last = date(year, month, calendar.monthrange(year, month)[1])
        elif unit == "year":
            last = date(today.year + amount, 12, 31)
        else:
            ahead = (amount - today.weekday()) % 7
            if mode[0] == "next":
                ahead += 7
            elif mode[0] == "coming" and ahead == 0:
                ahead = 7
            last = today + timedelta(days=ahead)
        ends.append(datetime.combine(last + timedelta(days=1), time(0), tzinfo=local.tzinfo))
    return timestamp(max(ends))
