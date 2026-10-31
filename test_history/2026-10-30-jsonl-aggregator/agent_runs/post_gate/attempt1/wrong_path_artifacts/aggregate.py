import json, collections, glob

def aggregate():
    user_amounts = collections.defaultdict(float)
    user_items = collections.defaultdict(int)
    tag_counts = collections.Counter()
    
    for line in open('records.jsonl'):
        data = json.loads(line)
        user = data.get('user', 'unknown')
        amount = float(data.get('amount', 0))
        items = data.get('items', [])
        tags = data.get('tags', [])
        user_amounts[user] += amount
        user_items[user] += len(items)
        for tag in tags: tag_counts[tag] += 1
    
    top_users = sorted(user_amounts.items(), key=lambda x: (-x[1], x[0]))[:5]
    top_tags = sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))[:5]
    
    output = {
        'top_users': [{'user': k, 'total_amount': round(v, 2), 'total_items': user_items[k]} for k, v in top_users]
    }
    for i, (tag, count) in enumerate(top_tags):
        output['top_tags'].append({'tag': tag, 'count': count})
    
    with open('aggregates.json', 'w') as f:
        json.dump(output, f, indent=2)

if __name__ == '__main__':
    aggregate()