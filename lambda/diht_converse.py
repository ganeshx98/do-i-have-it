import json
import os
import uuid
import urllib.request
from decimal import Decimal
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource('dynamodb')
purchases = dynamodb.Table('purchaseTracker')
items = dynamodb.Table('items')

API_KEY = os.environ['ANTHROPIC_API_KEY']
MODEL = "claude-haiku-4-5-20251001"

SYSTEM = """You are the assistant inside "Do I Have It", an app that remembers what the user owns so they stop rebuying things.

Talk like a person. Short replies, no bullet lists, no headings. This is spoken conversation.

Use your tools:
- When the user mentions considering, wanting, or needing something, call lookup_item before responding.
- When the user says they bought something, call save_purchase.
- When they ask follow-up questions like "when did I buy it" or "how many times", call purchase_history.

Resolve pronouns from the conversation. If they said "screwdriver" two turns ago and now ask "when did I buy it", they mean the screwdriver.

If they already own something, say so plainly and mention when they last bought it. Don't lecture."""

TOOLS = [
    {
        "name": "lookup_item",
        "description": "Check whether the user already owns an item.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Item name, lowercase singular"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "purchase_history",
        "description": "Get every recorded purchase of one item, with dates and stores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Item name, lowercase singular"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "save_purchase",
        "description": "Record that the user bought one or more items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "category": {"type": "string", "enum": ["durable", "consumable", "subscription"]},
                            "quantity": {"type": ["number", "null"]},
                            "store": {"type": ["string", "null"]}
                        },
                        "required": ["name", "category"]
                    }
                }
            },
            "required": ["items"]
        }
    }
]


def normalize(name):
    return " ".join(name.lower().split())


def clean(obj):
    if isinstance(obj, Decimal):
        return int(obj)
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return obj


def do_lookup(name):
    row = items.get_item(Key={'item_key': normalize(name)}).get('Item')
    if not row:
        return {"owned": False, "name": name}
    return clean({
        "owned": True,
        "name": row.get('display_name', name),
        "category": row.get('category'),
        "purchase_count": row.get('purchase_count'),
        "first_purchased": row.get('first_purchased'),
        "last_purchased": row.get('last_purchased')
    })


def do_history(name):
    key = normalize(name)
    rows = purchases.scan(FilterExpression=Attr('item_key').eq(key)).get('Items', [])
    rows.sort(key=lambda r: r.get('created_at', ''))
    return clean({
        "name": name,
        "count": len(rows),
        "purchases": [
            {
                "date": r.get('created_at'),
                "store": r.get('store'),
                "quantity": r.get('quantity')
            } for r in rows
        ]
    })


def do_save(item_list):
    now = datetime.now(timezone.utc).isoformat()
    saved = []

    for it in item_list:
        name = it.get('name')
        if not name:
            continue
        key = normalize(name)

        existing = items.get_item(Key={'item_key': key}).get('Item')
        if existing:
            items.update_item(
                Key={'item_key': key},
                UpdateExpression='SET purchase_count = purchase_count + :one, last_purchased = :now',
                ExpressionAttributeValues={':one': 1, ':now': now}
            )
            first_time = False
        else:
            items.put_item(Item={
                'item_key': key,
                'display_name': name,
                'category': it.get('category'),
                'purchase_count': 1,
                'first_purchased': now,
                'last_purchased': now
            })
            first_time = True

        purchases.put_item(Item={
            'purchase_id': str(uuid.uuid4()),
            'item_key': key,
            'item_name': name,
            'category': it.get('category'),
            'quantity': str(it['quantity']) if it.get('quantity') is not None else None,
            'store': it.get('store'),
            'raw_text': None,
            'created_at': now
        })

        saved.append({"name": name, "first_time": first_time})

    return {"saved": saved}


def run_tool(name, args):
    if name == "lookup_item":
        return do_lookup(args['name'])
    if name == "purchase_history":
        return do_history(args['name'])
    if name == "save_purchase":
        return do_save(args['items'])
    return {"error": "unknown tool " + name}


def call_claude(messages):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1000,
        "system": SYSTEM,
        "tools": TOOLS,
        "messages": messages
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

    with urllib.request.urlopen(req, timeout=50) as res:
        return json.loads(res.read())


def lambda_handler(event, context):
    body = event.get('body')
    if isinstance(body, str):
        body = json.loads(body)
    if body is None:
        body = event

    messages = body.get('messages')
    if not messages:
        return {'statusCode': 400, 'body': json.dumps({'error': 'messages is required'})}

    try:
        for _ in range(6):
            reply = call_claude(messages)
            messages.append({"role": "assistant", "content": reply["content"]})

            if reply.get("stop_reason") != "tool_use":
                break

            results = []
            for block in reply["content"]:
                if block.get("type") == "tool_use":
                    out = run_tool(block["name"], block["input"])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": json.dumps(out)
                    })

            messages.append({"role": "user", "content": results})
    except Exception as e:
        return {'statusCode': 502, 'body': json.dumps({'error': str(e)})}

    text = "".join(
        b.get("text", "") for b in messages[-1]["content"] if b.get("type") == "text"
    )

    return {'statusCode': 200, 'body': json.dumps({'reply': text, 'messages': messages})}