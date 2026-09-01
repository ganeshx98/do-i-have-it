import json
import os
import uuid
import urllib.request
import boto3
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
purchases = dynamodb.Table('purchaseTracker')
items = dynamodb.Table('items')

API_KEY = os.environ['ANTHROPIC_API_KEY']

PROMPT = """Extract every purchased item from the sentence below.

Return ONLY a JSON array. No prose, no markdown fences.

Each element must have:
- "name": the item, singular and lowercase, no quantity words
- "category": exactly one of "durable", "consumable", "subscription"
- "quantity": a number, or null if not stated
- "store": the store name, or null if not stated

durable = lasts, you keep owning it (velcro, a video game, a jacket)
consumable = gets used up (bananas, batteries, shampoo)
subscription = recurring charge (netflix, gym membership)

Sentence: {text}"""


def parse_items(text):
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": PROMPT.format(text=text)}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01"
        }
    )

    with urllib.request.urlopen(req, timeout=25) as res:
        data = json.loads(res.read())

    reply = "".join(b.get("text", "") for b in data.get("content", []))
    reply = reply.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(reply)


def normalize(name):
    return " ".join(name.lower().split())


def upsert_item(key, name, category, now):
    existing = items.get_item(Key={'item_key': key}).get('Item')

    if existing:
        items.update_item(
            Key={'item_key': key},
            UpdateExpression='SET purchase_count = purchase_count + :one, last_purchased = :now',
            ExpressionAttributeValues={':one': 1, ':now': now}
        )
        return False

    items.put_item(Item={
        'item_key': key,
        'display_name': name,
        'category': category,
        'purchase_count': 1,
        'first_purchased': now,
        'last_purchased': now
    })
    return True

 
def lambda_handler(event, context):
    body = event.get('body')
    if isinstance(body, str):
        body = json.loads(body)
    if body is None:
        body = event

    raw_text = body.get('raw_text')
    if not raw_text:
        return {'statusCode': 400, 'body': json.dumps({'error': 'raw_text is required'})}

    try:
        parsed = parse_items(raw_text)
    except Exception as e:
        return {'statusCode': 502, 'body': json.dumps({'error': 'parse failed: ' + str(e)})}

    now = datetime.now(timezone.utc).isoformat()
    saved = []

    for it in parsed:
        name = it.get('name')
        if not name:
            continue

        key = normalize(name)
        is_new = upsert_item(key, name, it.get('category'), now)

        record = {
            'purchase_id': str(uuid.uuid4()),
            'item_key': key,
            'item_name': name,
            'category': it.get('category'),
            'quantity': str(it.get('quantity')) if it.get('quantity') is not None else None,
            'store': it.get('store'),
            'raw_text': raw_text,
            'created_at': now
        }
        purchases.put_item(Item=record)

        record['first_time'] = is_new
        saved.append(record)

    return {'statusCode': 200, 'body': json.dumps({'saved': saved})}