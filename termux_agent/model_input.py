"""Smaller model-facing receipts; persisted history keeps complete diagnostics."""
import json


def project_tool_messages(messages, cache=None):
    result = []
    retained = set()
    for message in messages:
        content = message.get('content')
        if message.get('role') == 'tool' and isinstance(content, str) and '"output_compression"' in content:
            identifier = id(message)
            retained.add(identifier)
            previous = cache.get(identifier) if cache is not None else None
            if previous is not None and previous[0] is message and previous[1] is content:
                result.append({**message, 'content': previous[2]})
                continue
            projected = content
            try:
                value = json.loads(content)
            except ValueError:
                value = None
            if isinstance(value, dict) and isinstance(value.get('output_compression'), dict):
                metadata = value['output_compression']
                small = {key: metadata[key] for key in (
                    'status', 'applied', 'raw_available', 'raw_capture_complete') if key in metadata}
                artifact = metadata.get('raw_artifact')
                if isinstance(artifact, dict):
                    small['raw_artifact'] = {key: artifact[key] for key in ('id', 'path', 'bytes') if key in artifact}
                value['output_compression'] = small
                projected = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            if cache is not None:
                # Holding the original object prevents id reuse. Replacing its
                # content invalidates this entry; metadata is copied afresh.
                cache[identifier] = (message, content, projected)
            message = {**message, 'content': projected}
        result.append(message)
    if cache is not None:
        # Compaction, /clear and history replacement must release old receipts.
        for identifier in cache.keys() - retained:
            del cache[identifier]
    return result
