#!/usr/bin/env python3
"""
Deploy Web Scrapper to Coolify Cloud Server via API.
Reads credentials from local .env file.
"""

import os
import sys
import json
import urllib.request
import urllib.error

def load_env():
    env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.exists(env_file):
        print("ERROR: .env file not found!")
        sys.exit(1)
    
    env_vars = {}
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
    return env_vars

def main():
    env = load_env()
    token = env.get("COOLIFY_API_TOKEN")
    base_url = env.get("COOLIFY_URL", "http://129.225.83.15:8000").rstrip("/")
    uuid = env.get("COOLIFY_APP_UUID", "qkngbcoplevuwsakxc0wmwhd")

    if not token:
        print("ERROR: COOLIFY_API_TOKEN missing from .env")
        sys.exit(1)

    deploy_url = f"{base_url}/api/v1/deploy?uuid={uuid}"
    print(f"Triggering Coolify deployment for App UUID: {uuid}")
    print(f"Server endpoint: {deploy_url}")

    req = urllib.request.Request(
        deploy_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print("\nSUCCESS: Deployment queued on Coolify!")
            print(json.dumps(data, indent=2))
            print(f"\nLive domain: http://leadscrapper.duckdns.org")
    except urllib.error.HTTPError as e:
        print(f"\nHTTP ERROR {e.code}: {e.reason}")
        print(e.read().decode("utf-8", errors="ignore"))
        sys.exit(1)
    except Exception as e:
        print(f"\nNetwork Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
