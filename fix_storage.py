import os

storage_path = "/home/ubuntu/Job-Alert/storage.py"
content = open(storage_path).read()

old = '''def load_accounts():
    import json, os
    raw = os.environ.get("AMAZON_COOKIES", "")
    if not raw:
        return []
    try:
        cookies = json.loads(raw)
        return [{"id": 1, "cookies": json.dumps(cookies)}]
    except Exception:
        return []

def get_accounts():
    return load_accounts()'''

new = '''def load_accounts():
    import json, os
    accounts_file = os.path.join(DATA_DIR, "accounts.json")
    if os.path.exists(accounts_file):
        try:
            data = json.load(open(accounts_file))
            if data:
                return data
        except Exception:
            pass
    raw = os.environ.get("AMAZON_COOKIES", "")
    if not raw:
        return []
    try:
        cookies = json.loads(raw)
        return [{"id": 1, "cookies": json.dumps(cookies)}]
    except Exception:
        return []

def get_accounts():
    return load_accounts()'''

if old in content:
    content = content.replace(old, new)
    open(storage_path, "w").write(content)
    print("updated successfully")
else:
    print("pattern not found - checking current load_accounts:")
    for i, line in enumerate(content.split("\n")):
        if "load_accounts" in line:
            print(f"  line {i}: {line}")
