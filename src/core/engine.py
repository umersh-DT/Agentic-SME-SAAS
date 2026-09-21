import os
import sys
import time

def main():
    tenant_dir = os.getenv("DATA_DIR", "/app/data/tenants")
    print(f"[Phase 1 Init] OpenHuman engine worker initialized. Watching: {tenant_dir}")
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()