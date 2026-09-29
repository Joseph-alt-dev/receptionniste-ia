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
