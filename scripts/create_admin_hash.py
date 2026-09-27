from getpass import getpass

from werkzeug.security import generate_password_hash


password = getpass("New admin password: ")
confirmation = getpass("Confirm password: ")
if password != confirmation:
    raise SystemExit("Passwords do not match.")
if len(password) < 12:
    raise SystemExit("Use at least 12 characters.")
print(generate_password_hash(password))
