import json, glob, collections

def aggregate():
    user_totals = collections.defaultdict(lambda: {'amount': 0.0, 'items': 0})
    tag_counts = collections.defaultdict(int)
    for file_path in sorted(glob.glob('records_*.jsonl')):
        with open(file_path, 'r') as f:
            for line in f:
                data = json.loads(line)
                user = data.get('user', 'unknown')
                amount = float(data.get('amount', 0))
                items = int(data.get('items', 0))
                user_totals[user]['amount'] += amount
                user_totals[user]['items'] += items
                for tag in data.get('tags', []):
                    tag_counts[tag] += 1
    
    top_users = sorted(
        [(u, round(d['amount'], 2), d['items']) for u, d in user_totals.items()],
        key=lambda x: (-x[1], x[0])
    )[:5]
    top_tags = sorted(
        [(t, c) for t, c in tag_counts.items()],
        key=lambda x: (-x[1], x[0])
    )[:5]
    
    result = {
        'top_5_users_by_amount': {
            u: {'total_amount': float(a), 'total_items': int(i)} for u, a, i in top_users
        },
        'top_5_tags_by_count': {
            t: {'count': c} for t, c in top_tags
        }
    }
    with open('aggregates.json', 'w') as f:
        json.dump(result, f, indent=2)

if __name__ == '__main__':
    aggregate()
    