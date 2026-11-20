"""Run from project directory; passwords are entered without echo."""
import argparse
from getpass import getpass
from pathlib import Path
from services.access import AccessStore
p=argparse.ArgumentParser(description='Manage local development accounts')
p.add_argument('action',choices=['create','disable'])
p.add_argument('username')
p.add_argument('--role',choices=['ADMIN','DATA_ENTRY','ANALYST'],default='ADMIN')
a=p.parse_args()
store=AccessStore(Path(__file__).parent/'instance'/'portal.sqlite3')
try:
    if a.action=='create':
        password=getpass('Password (at least 12 characters): ')
        if password!=getpass('Repeat password: '): raise ValueError('Passwords do not match.')
        store.add_user(a.username,password,a.role)
    else: store.disable(a.username)
    print('Account operation completed.')
except Exception as exc:
    print('Account operation failed:',str(exc))
    raise SystemExit(1)
