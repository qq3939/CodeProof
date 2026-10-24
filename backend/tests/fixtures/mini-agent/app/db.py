from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine("postgresql+psycopg://app:app@localhost/app")
SessionLocal = sessionmaker(bind=engine)
