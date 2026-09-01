import json
import os
import urllib.request
import boto3

dynamodb = boto3.resource('dynamodb')
items = dynamodb.Table('items')

API_KEY = os.environ['ANTHROPIC_API_KEY']

EXTRACT_PROMPT = """The user is considering buying something. Extract what.

Return ONLY a JSON array of lowercase singular item names. No prose, no markdown fences.

Sentence: {text}"""


def call_claude(prompt, max_tokens=500):
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
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

    return "".join(b.get("text", "") for b in data.get("content", [])).strip()


def strip_fences(s):
    return s.removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def normalize(name):
    return " ".join(name.lower().split())


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
        names = json.loads(strip_fences(call_claude(EXTRACT_PROMPT.format(text=raw_text))))
    except Exception as e:
        return {'statusCode': 502, 'body': json.dumps({'error': 'parse failed: ' + str(e)})}

    results = []
    for name in names:
        key = normalize(name)
        owned = items.get_item(Key={'item_key': key}).get('Item')

        results.append({
            'name': name,
            'owned': bool(owned),
            'purchase_count': int(owned['purchase_count']) if owned else 0,
            'last_purchased': owned.get('last_purchased') if owned else None,
            'category': owned.get('category') if owned else None
        })

    return {'statusCode': 200, 'body': json.dumps({'results': results})}