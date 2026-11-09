"""Pure helpers for presenting saved-session metadata."""


_GENERATED_TITLE_SUFFIX = ' · compact task'


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _public_row(row, *, compaction_count=None, archived_ids=None):
    result = {key: value for key, value in row.items()
              if not (isinstance(key, str) and key.startswith('_'))}
    count = _count(row.get('compaction_count')) if compaction_count is None else compaction_count
    result.pop('compaction_count', None)
    if count:
        result['compaction_count'] = count
    if archived_ids is not None:
        result['archived_ids'] = list(archived_ids)
    return result


def _generated_from(child_title, parent_title):
    """Return whether a branch title was generated from its archive title."""
    if not isinstance(child_title, str) or not isinstance(parent_title, str):
        return False
    child = ' '.join(child_title.split())
    parent = ' '.join(parent_title.split())
    if not child.endswith(_GENERATED_TITLE_SUFFIX):
        return False
    stem = child.removesuffix(_GENERATED_TITLE_SUFFIX)
    return stem == parent or stem == parent[:95].rstrip()


def _visible_title(rows, chain):
    title = rows[chain[0]].get('title')
    for parent_index, child_index in zip(chain, chain[1:]):
        child_title = rows[child_index].get('title')
        parent_title = rows[parent_index].get('title')
        if child_title != parent_title and not _generated_from(child_title, parent_title):
            title = child_title
    return title


def collapse_compaction_rows(rows, *, active_id=None):
    """Collapse legacy linear compaction chains without changing the inputs.

    ``rows`` must contain every session, newest first. Linked branches are
    represented by ``_archive_session_id`` and may only collapse with an
    existing archive in the same endpoint/model/wire/scope identity.
    """
    source = [dict(row) for row in rows]
    if not source:
        return []

    id_indices = {}
    for index, row in enumerate(source):
        identifier = row.get('id')
        if not isinstance(identifier, str) or not identifier:
            continue
        id_indices.setdefault(identifier, []).append(index)
    unique_ids = {identifier: indices[0] for identifier, indices in id_indices.items()
                  if len(indices) == 1}

    parent_by_child = {}
    children_by_parent = {}
    neighbours = {index: set() for index in range(len(source))}
    for child_index, row in enumerate(source):
        archive_id = row.get('_archive_session_id')
        if not isinstance(archive_id, str) or not archive_id:
            continue
        parent_index = unique_ids.get(archive_id)
        if parent_index is None:
            continue
        parent_by_child[child_index] = parent_index
        children_by_parent.setdefault(parent_index, []).append(child_index)
        neighbours[child_index].add(parent_index)
        neighbours[parent_index].add(child_index)

    seen = set()
    collapsed_at_tip = {}
    consumed = set()
    for start in range(len(source)):
        if start in seen or not neighbours[start]:
            continue
        component = set()
        pending = [start]
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            pending.extend(neighbours[index] - component)
        seen.update(component)
        if len(component) < 2:
            continue

        roots = [index for index in component if index not in parent_by_child]
        tips = [index for index in component
                if not children_by_parent.get(index)]
        if (len(roots) != 1 or len(tips) != 1
                or any(len(children_by_parent.get(index, ())) > 1 for index in component)):
            continue

        tip = tips[0]
        identity = source[tip].get('_scope_identity')
        if (identity is None or tip != min(component) or
                any(source[i].get('_scope_identity') != identity for i in component) or
                any(source[i]['id'] == active_id for i in component - {tip})):
            # A reopened ancestor or a divergent identity remains individually visible.
            continue
        reverse_chain = []
        local_seen = set()
        current = tip
        while True:
            if current in local_seen:
                break
            local_seen.add(current)
            reverse_chain.append(current)
            parent = parent_by_child.get(current)
            if parent is None:
                break
            current = parent
        chain = list(reversed(reverse_chain))
        if len(chain) != len(component) or set(chain) != component:
            continue

        archived_ids = [source[index]['id'] for index in chain[:-1]]
        count = len(archived_ids) + sum(_count(source[i].get('compaction_count')) for i in chain)
        result = _public_row(source[tip], compaction_count=count,
                             archived_ids=archived_ids)
        result['title'] = _visible_title(source, chain)
        collapsed_at_tip[tip] = result
        consumed.update(component)

    output = []
    for index, row in enumerate(source):
        if index in collapsed_at_tip:
            output.append(collapsed_at_tip[index])
        elif index not in consumed:
            output.append(_public_row(row))
    return output
