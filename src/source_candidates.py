"""Build exact citable source rows and their owner-scoped dependencies."""

import local_rag_answer as rag
from public_rag_response import attachment_catalog, citation_label, render_sources


def _candidate(row, index, catalog, owners, ordinance_by_url):
    source = render_sources([row], catalog, owners)[0]
    name = row.get("ordin_name") or ordinance_by_url.get(source["source_url"])
    rag.require(bool(name), "missing_compact_source_ordinance")
    label = (rag._provision_locator(row) if row["kind"] == "body"
             else citation_label(row, catalog, owners, detail=True))
    return {"id": f"E{index}", "label": label, "ordinance": name,
            "locator": label.removeprefix(name).strip(), "quote": row["quote"],
            "unit_id": row["unit_id"], "owner_id": row["parent_chunk_id"],
            "public_label": source["citation"], "source_url": source["source_url"],
            "effective_date": source["effective_date"]}


def candidate_rows(citations, originals, expanded, units, primaries, conditions, project):
    support_rows = []
    eligible = [row for row in citations if not row.get("context_only")]
    catalog = attachment_catalog(project)
    owners = {row["unit_id"]: row for row in originals if row["kind"] != "required_context"}
    ordinance_by_url = {s["source_url"]: s["ordin_name"] for s in catalog.values()}
    ordinance_by_url.update({r["source_url"]: r["ordin_name"] for r in owners.values() if r.get("ordin_name")})
    rows = [_candidate(row, n, catalog, owners, ordinance_by_url) for n, row in enumerate(eligible, 1)]
    aliases = {row["unit_id"]: row["id"] for row in rows}
    def source_key(unit):
        return (unit["source_url"], unit["effective_date"], unit.get("source_unit_id"),
                rag.packed(unit["locator"]), unit["quote"])

    exact_aliases = {source_key(unit): aliases[unit["unit_id"]] for unit in eligible}

    # narrow_citations closes over all owners passed as originals. Restrict those
    # owners to this candidate and its declared dependencies before invoking it.
    scopes = {}
    for row in rows:
        owner = row["owner_id"]
        if owner not in scopes:
            scoped_originals = rag._citations([owner], units, primaries)
            scoped_owners = {original["parent_chunk_id"] for original in scoped_originals}
            scoped_expanded = [unit for unit in expanded if unit["parent_chunk_id"] in scoped_owners]
            scoped_conditions = [cond for cond in conditions if scoped_owners.intersection(cond["owners"])]
            scopes[owner] = scoped_originals, scoped_expanded, scoped_conditions
        scoped_originals, scoped_expanded, scoped_conditions = scopes[owner]
        closed = rag.narrow_citations([row["unit_id"]], scoped_expanded, scoped_originals, units, scoped_conditions)
        dependencies = []
        for unit in closed:
            if unit.get("context_only") or unit["unit_id"] == row["unit_id"]:
                continue
            # A shared paragraph can have an alias under each retrieved owner.
            # Match full provenance and text; retain any genuinely new required unit.
            key = source_key(unit)
            alias = aliases.get(unit["unit_id"]) or exact_aliases.get(key)
            if alias is None:
                extra = _candidate(unit, len(rows) + len(support_rows) + 1,
                                   catalog, owners, ordinance_by_url)
                extra["required_ids"] = []
                support_rows.append(extra)
                alias = exact_aliases[key] = extra["id"]
            if alias != row["id"] and alias not in dependencies:
                dependencies.append(alias)
        row["required_ids"] = dependencies
    return rows, support_rows
