import os
from alembic import context
from sqlalchemy import create_engine
from dotenv import load_dotenv
from company.database import Base

load_dotenv()
config = context.config
url = os.environ.get('DATABASE_URL', 'sqlite:///./data/company.db')
def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      render_as_batch=connection.dialect.name == 'sqlite')
    with context.begin_transaction():
        context.run_migrations()

connection = config.attributes.get('connection')
if connection is not None:
    migrate(connection)
else:
    with create_engine(url).begin() as connection:
        migrate(connection)
