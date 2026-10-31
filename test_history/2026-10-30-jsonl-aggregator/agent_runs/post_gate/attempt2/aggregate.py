import json, collections

def aggregate(records):
    user_totals = collections.defaultdict(lambda: {'amount': 0.0, 'items': 0})
    tag_counts = collections.Counter()
    for r in records:
        u = r.get('user', 'unknown')
        user_totals[u]['amount'] += float(r.get('amount', 0))
        user_totals[u]['items'] += len(r.get('items', []))
        for t in r.get('tags', []):
            tag_counts[t] += 1
    
    top_users = []
    for u, t in user_totals.items():
        t['total_amount'] = round(t['amount'], 2)
        t['user'] = u
        t['total_items'] = t['items']
    sorted_users = sorted(top_users, key=lambda x: (-x['total_amount'], x['user']))
    top_users = top_users[:5]
    
    top_tags = [{'tag': k, 'count': v} for k, v in tag_counts.items()]
    top_tags.sort(key=lambda x: -x['count'])
    top_tags = top_tags[:5]
    
    return {
        'top_users': top_users,
        'top_tags': top_tags
    }
