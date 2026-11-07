"""Build a correctly escaped database URL without emitting credentials."""
import os
import sys
from sqlalchemy.engine import URL

if 'DATABASE_URL' not in os.environ:
    os.environ['DATABASE_URL'] = URL.create(
        'postgresql+psycopg', username=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'], host=os.getenv('PGHOST','postgres'),
        database=os.environ['POSTGRES_DB'],
    ).render_as_string(hide_password=False)
os.execvp(sys.argv[1],sys.argv[1:])
