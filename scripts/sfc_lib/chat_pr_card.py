"""Project-card PR visibility with journaled writes and exact readback."""
from .common import Halt, write_json

FIELD_NAME = 'SFC PR'


def sync(gh, path, record, pull, verify_card):
    value = f"PR #{pull['url'].rsplit('/', 1)[1]} · {pull['url']}"
    card = verify_card()
    current = gh.chat_pr_card_snapshot(card)
    scope = {key: current[key] for key in ('project', 'item', 'view')}
    if scope['project'] != record['project']['id'] or scope['item'] != record['issue']['item_id']:
        raise Halt('PR card project or item identity changed')
    previous = record.get('pr_card')
    if previous and previous['scope'] != {**scope, 'field': current['field']}:
        raise Halt('PR card field identity changed; inspect before writing')
    pending = record.get('pr_card_pending')
    if pending:
        if pending['scope'] != scope:
            raise Halt('Pending PR card scope changed; inspect before another write')
        kind = pending['kind']
        if kind == 'field':
            confirmed = bool(current['field'])
        elif kind == 'view':
            confirmed = current['field'] == pending['field'] and current['visible'] == pending['visible']
        elif kind == 'value':
            confirmed = current['field'] == pending['field'] and current['value'] == pending['value']
        else:
            raise Halt('Unknown pending PR card operation; inspect the saved journal')
        if not confirmed:
            raise Halt('PR card write needs readback; the original write will not be repeated')
        record.pop('pr_card_pending')
        write_json(path, record)

    def apply(kind, details, operation):
        live_card = verify_card()
        if gh.chat_pr_card_snapshot(live_card) != current:
            raise Halt('PR card fields or saved view changed before writing')
        record['pr_card_pending'] = {'scope': scope, 'kind': kind, **details}
        write_json(path, record)
        operation()
        # Readback, including a lost response, is handled by this same entry
        # point on the next sync. Never resend a pending operation.
        return sync(gh, path, record, pull, verify_card)

    if not current['field']:
        return apply('field', {}, lambda: gh.create_chat_pr_field(scope['project']))
    field = current['field']
    if field not in current['visible']:
        visible = current['visible'] + [field]
        return apply('view', {'field': field, 'visible': visible},
                     lambda: gh.show_chat_pr_field(scope['view'], visible))
    if current['value'] != value:
        expected = previous.get('value', previous['url']) if previous else ''
        if current['value'] not in (expected, pull['url']):
            raise Halt('PR card link changed externally; preserve its value')
        return apply('value', {'field': field, 'value': value},
                     lambda: gh.write_chat_pr_field(scope['project'], scope['item'], field, value))
    record['pr_card'] = {'scope': {**scope, 'field': field}, 'url': pull['url'], 'value': value}
    write_json(path, record)
