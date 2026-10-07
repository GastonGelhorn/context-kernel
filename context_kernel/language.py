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
