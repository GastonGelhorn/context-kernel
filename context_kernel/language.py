"""Small bilingual vocabulary, not unrestricted language understanding."""

import re
import unicodedata


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
