"""Account command line. Passwords are typed without echo and never stored in plain text.

Only administrators have accounts (visitors see the weather pages without signing in).
Roles: ADMIN (add, edit and import readings) and SUPERADMIN (also delete readings and manage accounts).
Roles only change here, never from the web panel or the API.

Local demo accounts (SQLite):   python manage_users.py create super --role SUPERADMIN
                                python manage_users.py create admin
Accounts in RDS MySQL:          python manage_users.py create ana@correo.com --name "Ana" --mysql
Other actions: list, disable, enable, reset-password, set-role (for example: set-role ana@correo.com --role SUPERADMIN)
In MySQL, create and reset-password run the stored procedure sp_guardar_admin.
The system never allows disabling or demoting the last active SUPERADMIN.
"""
import argparse
from getpass import getpass
from pathlib import Path
from services.access import AccessStore

p = argparse.ArgumentParser(description='Manage administrator accounts')
p.add_argument('action', choices=['create', 'disable', 'enable', 'reset-password', 'list', 'set-role'])
p.add_argument('username', nargs='?', default='')
p.add_argument('--name', default='', help='display name')
p.add_argument('--role', choices=['ADMIN', 'SUPERADMIN'], default='ADMIN',
               help='create: role of the new account; set-role: the new role')
p.add_argument('--mysql', action='store_true', help='use the users table in RDS MySQL climate_transactional')
p.add_argument('--region', default='us-east-1')
a = p.parse_args()


def new_password():
    password = getpass('Password (12 to 128 characters, hidden): ')
    if password != getpass('Repeat password: '): raise ValueError('Passwords do not match.')
    return password


try:
    if a.mysql:
        from app import mysql_store
        store = mysql_store(a.region)
    else:
        store = AccessStore(Path(__file__).parent / 'instance' / 'portal.sqlite3')
    if a.action != 'list' and not a.username: raise ValueError('Write the username or email.')
    if a.action == 'create': store.add_user(a.username, new_password(), a.role, a.name or None)
    elif a.action == 'disable': store.disable(a.username)
    elif a.action == 'enable': store.enable(a.username)
    elif a.action == 'reset-password': store.set_password(a.username, new_password())
    elif a.action == 'set-role':
        if not store.set_role(a.username, a.role): print(f'{a.username} already has the role {a.role}.')
    else:
        for user in store.list_users():
            print(f"{user['username']:<40} {user.get('role', ''):<11} {'active' if user['active'] else 'disabled'}")
    print('Account operation completed.')
except Exception as exc:
    print('Account operation failed:', str(exc))
    raise SystemExit(1)
