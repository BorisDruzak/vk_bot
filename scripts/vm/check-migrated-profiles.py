"""Read-only identity acceptance of copied persistent profiles. No engagement."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright
import vk_accounts as accounts
import vk_control as control

result=[]
for number in accounts.account_numbers():
    expected=accounts.load_bindings(accounts.BINDINGS_PATH)['accounts'].get(str(number),{}).get('vk_id')
    try:
        with accounts.exclusive_profile(number),sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(str(accounts.profile_for_account(number)),
                headless=False,locale='ru-RU',viewport={'width':1320,'height':900})
            try:
                page=context.new_page()
                try:page.goto('https://vk.com/feed',wait_until='domcontentloaded',timeout=35000)
                except Exception:pass
                observed=accounts.inspect_context(context,attempts=15)
                result.append({'account':number,'profile_exists':True,
                    'identity_matches':observed is not None and observed==expected,
                    'identity_detected':observed is not None})
            finally:context.close()
    except Exception as exc:result.append({'account':number,'error':type(exc).__name__})
accounts.atomic_json(accounts.ROOT/'vk_migration_profile_check.json',result)
