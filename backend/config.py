import os

from dotenv import load_dotenv

load_dotenv(override=True)

JWT_SECRET = os.environ["JWT_SECRET"]
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 30

# ponytail: cookies Secure=True par défaut (obligatoire en prod/HTTPS) ;
# passer COOKIE_SECURE=false dans .env pour tester en local en http.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

# clé Fernet pour chiffrer les refresh tokens Google Calendar en base
ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"]

GOOGLE_OAUTH_CLIENT_ID = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
GOOGLE_OAUTH_CLIENT_SECRET = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
GOOGLE_OAUTH_REDIRECT_URI = os.environ["GOOGLE_OAUTH_REDIRECT_URI"]
