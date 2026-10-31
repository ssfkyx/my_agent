import json, glob, os

def aggregate():
    user_totals = {}
    tag_counts = {}
    for file_path in sorted(glob.glob('records_*.jsonl')):
        with open(file_path, 'r') as f:
            for line in f:
                record = json.loads(line)
                user = record.get('user', 'unknown')
                amount = float(record.get('amount', 0))
                items = int(record.get('items', 0))
                tags = record.get('tags', [])
                user_totals[user] = user_totals.get(user, {'amount': 0, 'items': 0})
                user_totals[user]['amount'] += amount
                user_totals[user]['items'] += items
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    top_users = sorted(
        [(u, round(d['amount'], 2)) for u, d in user_totals.items()],
        key=lambda x: (-x[1], x[0])
    )[:5]
    top_tags = sorted(
        [(t, c) for t, c in tag_counts.items()],
        key=lambda x: (-x[1], x[0])
    )[:5]
    
    output = {
        'top_users': [{'user': u, 'amount': a} for u, a in top_users],
        'top_tags': [{'tag': t, 'count': c} for t, c in top_tags]
    }
    with open('aggregates.json', 'w') as f:
        json.dump(output, f, indent=2)

if __name__ == '__main__':
    aggregate()
    