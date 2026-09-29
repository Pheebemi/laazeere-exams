"""
Django settings for laazeere_exams project.

Deployment target: Vercel (serverless WSGI) + Neon (Postgres) + WhiteNoise
(static). See README.md for the full deployment checklist. This mirrors the
jcda-election project's proven settings shape.
"""

import os
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env(key, default=""):
    return os.environ.get(key, default)


SECRET_KEY = env("DJANGO_SECRET_KEY", "django-insecure-change-me-in-production")

# Vercel injects its own VERCEL env var — use that as a safety net so DEBUG
# defaults to False in production even if DJANGO_DEBUG is forgotten in the
# dashboard, same pattern as jcda-election.
_default_debug = "False" if os.getenv("VERCEL") else "True"
DEBUG = env("DJANGO_DEBUG", _default_debug) == "True"

ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "roster",
    "exams",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "laazeere_exams.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "laazeere_exams.wsgi.application"


# Database
# Reads DATABASE_URL (or POSTGRES_URL) for Neon/hosted Postgres in
# production; falls back to local SQLite for local dev only — SQLite does
# not work on Vercel's read-only filesystem. Use Neon's POOLED connection
# string (the "-pooler" hostname) since each serverless invocation opens a
# fresh, non-pooled connection — there's no Django-side connection pooling
# here (no CONN_MAX_AGE), same as jcda-election.

_database_url = (env("DATABASE_URL") or env("POSTGRES_URL")).strip()

if _database_url:
    _parsed = urllib.parse.urlparse(_database_url)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _parsed.path.lstrip("/"),
            "USER": _parsed.username,
            "PASSWORD": _parsed.password,
            "HOST": _parsed.hostname,
            "PORT": _parsed.port or "5432",
            "OPTIONS": {"sslmode": "require"},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# Cache — DatabaseCache, not the in-memory default. Vercel's serverless
# functions are stateless per cold start, so an in-memory cache (used for
# django-ratelimit on the login views) wouldn't hold across invocations.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True


# Static files — collected at Vercel build time (see vercel.json) and served
# by WhiteNoise from the WSGI app itself, no separate CDN/static host needed.
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / env("STATIC_ROOT", "staticfiles")
_static_src = BASE_DIR / "static"
STATICFILES_DIRS = [_static_src] if _static_src.exists() else []

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Laazeere exam portal <-> raddai-backend integration ---
RADDAI_API_BASE_URL = env("RADDAI_API_BASE_URL", "https://laazeereacademy.com/api")
EXAM_PORTAL_API_KEY = env("EXAM_PORTAL_API_KEY", "")


# --- Security (production, behind HTTPS on Vercel) ---
SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT", "False") == "True"
SESSION_COOKIE_SECURE = env("SESSION_COOKIE_SECURE", "False") == "True"
CSRF_COOKIE_SECURE = env("CSRF_COOKIE_SECURE", "False") == "True"
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

LOGIN_URL = "exams:student_login"
