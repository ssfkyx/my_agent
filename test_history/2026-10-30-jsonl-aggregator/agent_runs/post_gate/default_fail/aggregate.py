import json, glob, os

def aggregate():
    user_totals = {}
    tag_counts = {}
    for file_path in glob.glob('records_*.jsonl'):
        with open(file_path, 'r') as f:
            for line in f:
                record = json.loads(line)
                user = record.get('user', 'unknown')
                amount = float(record.get('amount', 0.0))
                items = record.get('items', [])
                tags = record.get('tags', [])
                user_totals[user] = user_totals.get(user, {'total_amount': 0.0, 'total_items': 0})
                user_totals[user]['total_amount'] += amount
                user_totals[user]['total_items'] += len(items)
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_users = sorted(
        [(u, d['total_amount'], d['total_items']) for u, d in user_totals.items()],
        key=lambda x: (-x[1], x[0])
    )[:5]
    top_tags = sorted(
        [(t, c) for t, c in tag_counts.items()],
        key=lambda x: (-x[1], x[0])
    )[:5]
    return {
        'top_5_users_by_amount': {u: {'total_amount': round(a, 2), 'total_items': i} for u, a, i in top_users},
        'top_5_tags_by_count': {t: {'count': c} for t, c in top_tags}
    }

if __name__ == '__main__':
    print(json.dumps(aggregate(), indent=2))