"""Unbound Flask extensions shared by the application and models."""

from authlib.integrations.flask_client import OAuth
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()
oauth = OAuth()
